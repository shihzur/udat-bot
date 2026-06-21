#!/usr/bin/env python3
"""
UDATA Lab Telegram Bot - v1.6
Added: phone-number lookup. Send a client's phone number -> bot finds their
case(s) by matching it against the task name/description, useful when the
client comes to pick up the disk but doesn't remember the case number.
"""

import os, base64, json, logging, re, requests
from urllib.parse import quote
from dotenv import load_dotenv
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes
)

load_dotenv()

logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY")
CLICKUP_TOKEN   = os.getenv("CLICKUP_TOKEN")
CLICKUP_LIST_ID = "901817725456"  # default list for new intake tasks (Диагностика)
SHEETS_URL      = (
    "https://script.google.com/macros/s/"
    "AKfycbxP-mBb6C_TRlQbQENlbPfqn4v_jo8LwRe9RuuES-RsRsdti53Efpu2ruUpDgwQuhVx/exec"
)
FREE_DIAG_CODES = {"1209", "1414", "NTL", "FM", "9090"}
FREE_DIAG_NAMES = {"מדי מחשבים", "hanan", "oren technologies", "etl"}

# ── Status / list map (UDATA pipeline) ───────────────────────────────────────
# Each status: full label (used in confirmations) + short button label (inline keyboard)
STATUS_LISTS = {
    "901817725445": {
        "label": "💳 Ожидание оплаты диагностики",
        "button": "💳 Оплата",
    },
    "901817725456": {
        "label": "🔍 Диагностика",
        "button": "🔍 Диагностика",
    },
    "901817725460": {
        "label": "📋 Отчёт отправлен — ожидаем подписи",
        "button": "📋 Ожидаем подписи",
    },
    "901817725462": {
        "label": "⚙️ В работе",
        "button": "⚙️ В работе",
    },
    "901817725465": {
        "label": "✅ Готово к выдаче",
        "button": "✅ Готово к выдаче",
    },
    "901817725467": {
        "label": "❌ Отказ / невозможно восстановить",
        "button": "❌ Отказ",
    },
    "901817725472": {
        "label": "📦 Архив / выдано",
        "button": "📦 Архив / выдано",
    },
}
# Order in which status buttons are shown in the keyboard (2 per row)
STATUS_ORDER = [
    "901817725445", "901817725456", "901817725460",
    "901817725462", "901817725465", "901817725467", "901817725472",
]

CASE_NUMBER_RE = re.compile(r"^\s*(\d{3,5})\s*$")  # message is JUST a number -> case lookup
PHONE_RE = re.compile(
    r"^\s*0?5\d[\s-]?\d{3}[\s-]?\d{4}\s*$"      # mobile: 05X-XXXXXXX
    r"|^\s*0\d{1,2}[\s-]?\d{3}[\s-]?\d{4}\s*$"  # landline: 0X(X)-XXXXXXX
    r"|^\s*972\d{8,9}\s*$"                       # international: 972XXXXXXXXX
)
DESC_FIELD_RE = {
    "phone":    re.compile(r"טלפון:\s*(.+?)(?:\n|$)"),
    "media":    re.compile(r"מדיה:\s*(.+?)(?:\n|$)"),
    "model":    re.compile(r"MDL:\s*(.+?)(?:\n|$)"),
    "serial":   re.compile(r"S/N:\s*(.+?)(?:\n|$)"),
    "price":    re.compile(r"מחיר:\s*(.+?)(?:\n|$)"),
    "recovered": re.compile(r"שוחזר:\s*(.+?)(?:\n|$)"),
}

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── ClickUp ──────────────────────────────────────────────────────────────────
def clickup_create_task(name: str, description: str) -> str:
    resp = requests.post(
        f"https://api.clickup.com/api/v2/list/{CLICKUP_LIST_ID}/task",
        headers={"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"},
        json={"name": name, "description": description},
        timeout=15
    )
    resp.raise_for_status()
    return resp.json()["id"]

def clickup_get_all_tasks() -> list:
    """Fetch all team tasks once (used by both case-number and phone search)."""
    resp = requests.get(
        "https://api.clickup.com/api/v2/team",
        headers={"Authorization": CLICKUP_TOKEN},
        timeout=15
    )
    resp.raise_for_status()
    teams = resp.json().get("teams", [])
    if not teams:
        return []
    team_id = teams[0]["id"]

    resp = requests.get(
        f"https://api.clickup.com/api/v2/team/{team_id}/task",
        headers={"Authorization": CLICKUP_TOKEN},
        timeout=20
    )
    resp.raise_for_status()
    return resp.json().get("tasks", [])

def normalize_phone(phone: str) -> str:
    """Strip spaces/dashes, the leading 0, and the 972 country code,
    for loose phone comparison (050-1234567 == 972501234567 == 501234567)."""
    p = re.sub(r"[\s\-]", "", phone or "")
    if p.startswith("972"):
        p = p[3:]
    elif p.startswith("0"):
        p = p[1:]
    return p

def clickup_find_task_by_case(case_number: str):
    """Search team tasks for one whose name starts with the case number.
    Returns {'id', 'name', 'list_id', 'list_name', 'description'} or None."""
    tasks = clickup_get_all_tasks()
    for t in tasks:
        name = t.get("name", "")
        first_token = name.split("|")[0].strip()
        if first_token == case_number or name.startswith(case_number + " "):
            return {
                "id": t["id"],
                "name": name,
                "list_id": t.get("list", {}).get("id"),
                "list_name": t.get("list", {}).get("name"),
                "description": t.get("text_content") or t.get("description") or "",
            }
    return None

def clickup_find_tasks_by_phone(phone_query: str) -> list:
    """Search all team tasks for ones whose phone field matches phone_query.
    Returns a list of {'id', 'name', 'list_id', 'list_name', 'description'}
    (a client may have more than one case)."""
    target = normalize_phone(phone_query)
    if not target:
        return []

    tasks = clickup_get_all_tasks()
    matches = []
    for t in tasks:
        name = t.get("name", "")
        description = t.get("text_content") or t.get("description") or ""

        # Phone usually also appears in the task name: "6280 | HDD | 050-..."
        name_phone = ""
        parts = [p.strip() for p in name.split("|")]
        if len(parts) >= 3:
            name_phone = parts[2]

        desc_m = DESC_FIELD_RE["phone"].search(description)
        desc_phone = desc_m.group(1).strip() if desc_m else ""

        if target in (normalize_phone(name_phone), normalize_phone(desc_phone)):
            matches.append({
                "id": t["id"],
                "name": name,
                "list_id": t.get("list", {}).get("id"),
                "list_name": t.get("list", {}).get("name"),
                "description": description,
            })
    return matches

def clickup_move_task(task_id: str, list_id: str) -> bool:
    resp = requests.post(
        f"https://api.clickup.com/api/v2/list/{list_id}/task/{task_id}",
        headers={"Authorization": CLICKUP_TOKEN},
        timeout=15
    )
    return resp.status_code in (200, 204)

def clickup_get_task_description(task_id: str) -> str:
    """Fetch full description for a task (list endpoint truncates/omits it)."""
    resp = requests.get(
        f"https://api.clickup.com/api/v2/task/{task_id}",
        headers={"Authorization": CLICKUP_TOKEN},
        timeout=15
    )
    resp.raise_for_status()
    j = resp.json()
    return j.get("text_content") or j.get("description") or ""

def clickup_update_task_description(task_id: str, new_description: str) -> bool:
    resp = requests.put(
        f"https://api.clickup.com/api/v2/task/{task_id}",
        headers={"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"},
        json={"description": new_description},
        timeout=15
    )
    return resp.status_code == 200

def set_price_recovered_in_description(description: str, price: str = None, recovered: str = None) -> str:
    """Add or replace 'מחיר:' and 'שוחזר:' lines in the task description text.
    Only touches the field(s) explicitly passed; the other one (if already
    present) is preserved as-is."""
    lines = (description or "").split("\n")

    existing_price = None
    existing_recovered = None
    kept_lines = []
    for l in lines:
        if l.startswith("מחיר:"):
            existing_price = l[len("מחיר:"):].strip()
            continue
        if l.startswith("שוחזר:"):
            existing_recovered = l[len("שוחזר:"):].strip()
            continue
        kept_lines.append(l)

    while kept_lines and kept_lines[-1].strip() == "":
        kept_lines.pop()

    final_price = price if price is not None else existing_price
    final_recovered = recovered if recovered is not None else existing_recovered

    if final_price is not None:
        kept_lines.append(f"מחיר: {final_price}")
    if final_recovered is not None:
        kept_lines.append(f"שוחזר: {final_recovered}")

    return "\n".join(kept_lines)

def parse_task_card(name: str, description: str) -> dict:
    """Extract phone/media/model/serial/price/recovered from the task name +
    ClickUp description (description follows the fixed format written by
    clickup_create_task, with price/recovered appended later by the bot)."""
    card = {"phone": "—", "media": "—", "model": "—", "serial": "—",
            "price": None, "recovered": None}

    # Fallback from the task name itself: "6280 | HDD | 050-..."
    parts = [p.strip() for p in name.split("|")]
    if len(parts) >= 3:
        card["media"] = parts[1]
        card["phone"] = parts[2]

    for field, pattern in DESC_FIELD_RE.items():
        m = pattern.search(description or "")
        if m:
            card[field] = m.group(1).strip()

    return card

# ── Google Sheets ─────────────────────────────────────────────────────────────
def sheets_add_row(data: dict) -> bool:
    try:
        model = data['model'].replace('/', '%2F').replace(' ', '+')
        phone = str(data['phone']).replace(' ', '+')
        url = (
            f"{SHEETS_URL}"
            f"?case_number={data['case_number']}"
            f"&phone={phone}"
            f"&model={model}"
            f"&serial={data['serial']}"
            f"&capacity={data['capacity']}"
        )
        requests.get(url, timeout=20, allow_redirects=True)
        return True
    except Exception as e:
        logger.error(f"Sheets error: {e}")
        return False

# ── OCR ──────────────────────────────────────────────────────────────────────
def extract_label_data(image_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode()
    prompt = """You are reading a disk/device label at UDATA Data Recovery Lab (Israel).

Extract EXACTLY these fields as pure JSON (use "—" if not visible):
{
  "case_number": "4-digit number from UDATA sticker",
  "date": "date from UDATA sticker DD/MM/YYYY",
  "phone": "phone number OR dealer code OR dealer name",
  "is_dealer": true or false,
  "media_type": "HDD / SSD / NVMe / SD Card / USB Flash / DVD / Laptop / other",
  "brand": "manufacturer brand",
  "model": "MDL or MODEL from manufacturer label",
  "serial": "S/N from manufacturer label",
  "capacity": "e.g. 1TB, 500GB"
}

CRITICAL RULES:
- The UDATA sticker has TWO sections:
  LEFT side: U-Data contact info (www.udata.co.il, 072-249-4570) — IGNORE COMPLETELY
  RIGHT side: case number, "בדיקה", and CLIENT phone/dealer — extract ONLY from RIGHT side
- NEVER use 072-249-4570 as phone — that is UDATA's own number
- 4-digit number (1209, 9090, 1414) = DEALER CODE → is_dealer=true
- Name like "Hanan", "מדי מחשבים", "ETL", "לפי מחשבים" = DEALER → is_dealer=true
- Code like "ETL-205310" = dealer → is_dealer=true, phone="ETL-205310"
- Phone 05X-XXXXXXX = regular client → is_dealer=false
- Return ONLY valid JSON, no markdown"""

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": prompt}
            ]
        }]
    )
    text = response.content[0].text.strip()
    if "```" in text:
        for part in text.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith("{"):
                text = part
                break
    return json.loads(text.strip())

# ── Helpers ──────────────────────────────────────────────────────────────────
def is_free_diag(data: dict) -> bool:
    if not data.get("is_dealer"):
        return False
    phone = str(data.get("phone", "")).strip()
    return phone in FREE_DIAG_CODES or phone.lower() in FREE_DIAG_NAMES

def build_wa_walkin(data: dict) -> str:
    """WhatsApp for walk-in clients."""
    media = f"{data['brand']} {data['media_type']} {data['capacity']}".strip()
    return (
        f"שלום 😊\n\n"
        f"קיבלנו את הדיסק שלך למעבדה לצורך בדיקה.\n\n"
        f"פרטי הקבלה:\n"
        f"🔢 מספר עבודה: *{data['case_number']}*\n"
        f"💾 מדיה: *{media}*\n"
        f"🔧 דגם: {data['model']}\n"
        f"🔑 מס' סידורי: {data['serial']}\n\n"
        f"נעדכן אותך בתוצאות הבדיקה בהקדם 🙏\n\n"
        f"_UDATA – Data Recovery Lab_\n"
        f"📞 072-249-4570"
    )

def build_wa_mail(data: dict) -> str:
    """WhatsApp when disk was brought by someone else on behalf of the owner."""
    media = f"{data['brand']} {data['media_type']} {data['capacity']}".strip()
    return (
        f"שלום 😊\n\n"
        f"קיבלנו את הכונן שלך למעבדה.\n\n"
        f"פרטי הקבלה:\n"
        f"🔢 מספר עבודה: *{data['case_number']}*\n"
        f"💾 מדיה: *{media}*\n\n"
        f"בקרוב נשלח אליך *קישור לתשלום* עבור עלות הבדיקה.\n"
        f"מיד עם קבלת התשלום נתחיל בבדיקה ונעדכן אותך בממצאים 🔍\n\n"
        f"❓ על שם מי להוציא קבלה?\n\n"
        f"אני כאן לכל שאלה 😊\n\n"
        f"_UDATA – Data Recovery Lab_\n"
        f"📞 072-249-4570"
    )

def build_wa_link(phone: str, wa_text: str, is_dealer: bool) -> str:
    encoded = quote(wa_text)
    if not is_dealer and phone != "—":
        p = phone.replace('-', '').replace(' ', '')
        if p.startswith('0'):
            p = '972' + p[1:]
        return f"https://wa.me/{p}?text={encoded}"
    return f"https://wa.me/?text={encoded}"

def build_status_keyboard(case_number: str, current_list_id: str, has_price: bool) -> InlineKeyboardMarkup:
    """Build inline keyboard: price/recovered button on top, then status buttons
    (2 per row), excluding the current status."""
    rows = []

    price_btn_label = "✏️ עדכן מחיר / נתונים" if has_price else "💰 הוסף מחיר / נתונים שוחזרו"
    rows.append([InlineKeyboardButton(price_btn_label, callback_data=f"price|{case_number}")])

    row = []
    for list_id in STATUS_ORDER:
        if list_id == current_list_id:
            continue
        info = STATUS_LISTS[list_id]
        row.append(InlineKeyboardButton(
            info["button"], callback_data=f"mv|{case_number}|{list_id}"
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)

# ── Handlers ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 שלום! אני בוט UDATA Lab.\n\n"
        "📸 שלח לי צילום של מדבקת הדיסק — אני אוצר משימה ב-ClickUp,\n"
        "אוסיף לטבלה ואכין הודעת קבלה ל-WhatsApp.\n\n"
        "🔢 שלח רק את מספר התיק (למשל: `6280`) — ואני אציג לך:\n"
        "   • מדיה, דגם, טלפון לקוח\n"
        "   • מחיר שהוסכם + כמה מידע שוחזר\n"
        "   • סטטוס נוכחי\n"
        "   • כפתורים לשינוי הסטטוס / עדכון מחיר\n\n"
        "📱 שלח מספר טלפון של לקוח — אם לא זוכר/ת את מספר התיק\n\n"
        "📌 /statuses — רשימת כל הסטטוסים הזמינים",
        parse_mode="Markdown"
    )

async def cmd_status_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["📌 *סטטוסים זמינים:*\n"]
    for list_id in STATUS_ORDER:
        lines.append(STATUS_LISTS[list_id]["label"])
    lines.append("\n🔢 שלח מספר תיק (למשל `6280`) כדי לראות ולשנות סטטוס.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("📷 מעבד תמונה...")
    try:
        photo_file = await context.bot.get_file(update.message.photo[-1].file_id)
        image_bytes = await photo_file.download_as_bytearray()

        await msg.edit_text("🔍 קורא מדבקה...")
        data = extract_label_data(bytes(image_bytes))

        if data.get("case_number") == "—":
            await msg.edit_text("❌ לא הצלחתי לקרוא את המדבקה. נסה לצלם שוב עם יותר אור.")
            return

        await msg.edit_text("📋 יוצר משימה ב-ClickUp...")
        task_name = f"{data['case_number']} | {data['media_type']} | {data['phone']}"
        task_desc = (
            f"ДАННЫЕ ЗАКАЗА\n"
            f"Номер работы: {data['case_number']}\n"
            f"Дата поступления: {data['date']}\n"
            f"טלפון: {data['phone']}{'  (דילר)' if data['is_dealer'] else ''}\n"
            f"מדיה: {data['brand']} {data['media_type']} {data['capacity']}\n"
            f"MDL: {data['model']}\n"
            f"S/N: {data['serial']}"
        )
        task_id = clickup_create_task(task_name, task_desc)

        # Auto-add to Sheets
        sheets_add_row(data)

        # Save data for callback
        context.user_data['pending'] = {
            'data': data,
            'task_id': task_id
        }

        free_note = "\n🆓 דילר — דיאגנוסטיקה חינם" if is_free_diag(data) else ""
        media = f"{data['brand']} {data['media_type']} {data['capacity']}"

        summary = (
            f"✅ *תיק {data['case_number']} נוצר*\n\n"
            f"📱 {data['phone']}{'  (דילר)' if data['is_dealer'] else ''}{free_note}\n"
            f"💾 {media}\n"
            f"🔧 {data['model']}\n"
            f"🔑 {data['serial']}\n"
            f"📊 Sheets: ✅\n\n"
            f"*איך הגיע הדיסק?*"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📍 הגיע למעבדה", callback_data="walkin"),
            InlineKeyboardButton("📬 הגיע דרך שליח", callback_data="mail"),
        ]])

        await msg.delete()
        await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=keyboard)

    except json.JSONDecodeError:
        await msg.edit_text("❌ שגיאה בפענוח המדבקה. נסה שוב.")
    except requests.HTTPError as e:
        await msg.edit_text(f"❌ שגיאת ClickUp: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await msg.edit_text(f"❌ שגיאה: {str(e)[:200]}")

async def handle_delivery_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pending = context.user_data.get('pending')
    if not pending:
        await query.edit_message_text("❌ פג תוקף — שלח את התמונה שוב.")
        return

    data    = pending['data']
    task_id = pending['task_id']
    choice  = query.data  # "walkin" or "mail"

    if choice == "walkin":
        wa_text  = build_wa_walkin(data)
        delivery = "📍 הגיע למעבדה"
    else:
        wa_text  = build_wa_mail(data)
        delivery = "📬 דואר / שליח"

    wa_link = build_wa_link(data['phone'], wa_text, data.get('is_dealer', False))
    cu_link = f"https://app.clickup.com/t/{task_id}"

    reply = (
        f"✅ *תיק {data['case_number']}* — {delivery}\n\n"
        f"*הודעת קבלה (העתק ל-WhatsApp):*\n"
        f"```\n{wa_text}\n```"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📲 WhatsApp", url=wa_link),
            InlineKeyboardButton("🔗 ClickUp", url=cu_link),
        ]
    ])

    await query.edit_message_text(reply, parse_mode="Markdown", reply_markup=keyboard)
    context.user_data.pop('pending', None)

# Invisible Unicode direction marks that iOS/Android keyboards sometimes inject
# around text in RTL (Hebrew/Arabic) chat contexts. Must be stripped before any
# regex matching, otherwise "3103" arrives as "\u200e3103" and silently fails
# to match CASE_NUMBER_RE / PHONE_RE.
_INVISIBLE_MARKS_RE = re.compile(
    "[\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff]"
)

def clean_text(text: str) -> str:
    return _INVISIBLE_MARKS_RE.sub("", text or "").strip()

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = clean_text(update.message.text)

    # Step 2 of price flow: waiting for "recovered amount" after price was given
    awaiting_recovered = context.user_data.get("awaiting_recovered_for")
    if awaiting_recovered:
        await save_recovered_and_finish(update, context, awaiting_recovered, text)
        return

    # Step 1 of price flow: waiting for price
    awaiting_price = context.user_data.get("awaiting_price_for")
    if awaiting_price:
        await save_price_and_ask_recovered(update, context, awaiting_price, text)
        return

    m = CASE_NUMBER_RE.match(text)
    if m:
        await show_case_card(update, context, m.group(1))
        return

    if PHONE_RE.match(text):
        await show_cases_by_phone(update, context, text)
        return

    await update.message.reply_text(
        "📸 שלח לי צילום של מדבקת הדיסק\n"
        "🔢 או שלח רק את מספר התיק (למשל `6280`) לבדיקת סטטוס\n"
        "📱 או שלח מספר טלפון של הלקוח לחיפוש התיק שלו",
        parse_mode="Markdown"
    )

async def show_cases_by_phone(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
    msg = await update.message.reply_text(f"🔍 מחפש תיקים עבור {phone}...")
    try:
        matches = clickup_find_tasks_by_phone(phone)

        if not matches:
            await msg.edit_text(f"❌ לא נמצאו תיקים עבור הטלפון {phone}.")
            return

        if len(matches) == 1:
            # Exactly one case -> show its full card right away
            task = matches[0]
            case_number = task["name"].split("|")[0].strip()
            await msg.delete()
            await show_case_card(update, context, case_number)
            return

        # Multiple cases for this client -> let them pick which one
        lines = [f"📱 *נמצאו {len(matches)} תיקים עבור {phone}:*\n"]
        keyboard_rows = []
        for task in matches:
            case_number = task["name"].split("|")[0].strip()
            status_label = STATUS_LISTS.get(task["list_id"], {}).get("label", task.get("list_name", "—"))
            lines.append(f"🔹 {task['name']}\n   {status_label}")
            keyboard_rows.append([InlineKeyboardButton(
                f"📁 תיק {case_number}", callback_data=f"open|{case_number}"
            )])

        await msg.edit_text(
            "\n\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard_rows)
        )

    except requests.HTTPError as e:
        await msg.edit_text(f"❌ שגיאת ClickUp: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Phone search error: {e}", exc_info=True)
        await msg.edit_text(f"❌ שגיאה: {str(e)[:200]}")

async def handle_open_case_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, case_number = query.data.split("|")
    except ValueError:
        await query.edit_message_text("❌ שגיאה בנתוני הכפתור.")
        return
    await show_case_card(update, context, case_number)

async def save_price_and_ask_recovered(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                         case_number: str, price_text: str):
    context.user_data.pop("awaiting_price_for", None)

    task_info = context.user_data.get(f"task_{case_number}")
    if not task_info:
        task = clickup_find_task_by_case(case_number)
        if not task:
            await update.message.reply_text(f"❌ תיק {case_number} לא נמצא ב-ClickUp.")
            return
        task_info = {"task_id": task["id"], "description": task.get("description", "")}

    new_desc = set_price_recovered_in_description(task_info["description"], price=price_text)
    ok = clickup_update_task_description(task_info["task_id"], new_desc)

    if not ok:
        await update.message.reply_text(f"❌ שגיאה בשמירת המחיר עבור תיק {case_number}.")
        return

    context.user_data[f"task_{case_number}"] = {
        "task_id": task_info["task_id"], "description": new_desc
    }
    context.user_data["awaiting_recovered_for"] = case_number

    await update.message.reply_text(
        f"✅ מחיר נשמר: *{price_text}*\n\n"
        f"📦 *כמה מידע שוחזר?*\n"
        f"(לדוגמה: `1.8TB` או `500GB` או `כ-95%`)",
        parse_mode="Markdown"
    )

async def save_recovered_and_finish(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                      case_number: str, recovered_text: str):
    context.user_data.pop("awaiting_recovered_for", None)

    task_info = context.user_data.get(f"task_{case_number}")
    if not task_info:
        task = clickup_find_task_by_case(case_number)
        if not task:
            await update.message.reply_text(f"❌ תיק {case_number} לא נמצא ב-ClickUp.")
            return
        task_info = {"task_id": task["id"], "description": task.get("description", "")}

    new_desc = set_price_recovered_in_description(task_info["description"], recovered=recovered_text)
    ok = clickup_update_task_description(task_info["task_id"], new_desc)

    context.user_data.pop(f"task_{case_number}", None)

    if not ok:
        await update.message.reply_text(f"❌ שגיאה בשמירת הנתונים עבור תיק {case_number}.")
        return

    await update.message.reply_text(
        f"✅ *תיק {case_number} עודכן*\n\n"
        f"📦 שוחזר: {recovered_text}\n\n"
        f"שלח/י שוב את מספר התיק (`{case_number}`) לראות את הכרטיס המעודכן.",
        parse_mode="Markdown"
    )

async def show_case_card(update: Update, context: ContextTypes.DEFAULT_TYPE, case_number: str):
    msg = await update.message.reply_text(f"🔍 מחפש תיק {case_number}...")
    try:
        task = clickup_find_task_by_case(case_number)
        if not task:
            await msg.edit_text(f"❌ לא נמצא תיק שמתחיל ב-\"{case_number}\" ב-ClickUp.")
            return

        description = task.get("description") or clickup_get_task_description(task["id"])
        card = parse_task_card(task["name"], description)

        current_list_id = task["list_id"]
        current_label = STATUS_LISTS.get(current_list_id, {}).get("label", task.get("list_name", "—"))

        # Keep latest description handy for the price/recovered flow
        context.user_data[f"task_{case_number}"] = {
            "task_id": task["id"],
            "description": description,
        }

        price_line = f"💰 מחיר: *{card['price']}*\n" if card.get("price") else ""
        recovered_line = f"📦 שוחזר: *{card['recovered']}*\n" if card.get("recovered") else ""

        text = (
            f"📁 *תיק {case_number}*\n\n"
            f"💾 {card['media']}\n"
            f"🔧 {card['model']}\n"
            f"📱 {card['phone']}\n"
            f"{price_line}"
            f"{recovered_line}"
            f"\n📍 סטטוס נוכחי: {current_label}\n\n"
            f"בחר/י פעולה:"
        )
        keyboard = build_status_keyboard(case_number, current_list_id, has_price=bool(card.get("price")))
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

    except requests.HTTPError as e:
        await msg.edit_text(f"❌ שגיאת ClickUp: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Case card error: {e}", exc_info=True)
        await msg.edit_text(f"❌ שגיאה: {str(e)[:200]}")

async def handle_price_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, case_number = query.data.split("|")
    except ValueError:
        await query.edit_message_text("❌ שגיאה בנתוני הכפתור.")
        return

    context.user_data["awaiting_price_for"] = case_number
    await query.message.reply_text(
        f"💰 תיק {case_number} — *מה המחיר שהוסכם עם הלקוח?*\n"
        f"(לדוגמה: `1800 + מע\"מ`)",
        parse_mode="Markdown"
    )

async def handle_status_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, case_number, list_id = query.data.split("|")
    except ValueError:
        await query.edit_message_text("❌ שגיאה בנתוני הכפתור.")
        return

    try:
        task = clickup_find_task_by_case(case_number)
        if not task:
            await query.edit_message_text(f"❌ תיק {case_number} לא נמצא (אולי שונה בינתיים).")
            return

        if task["list_id"] == list_id:
            label = STATUS_LISTS[list_id]["label"]
            await query.edit_message_text(f"ℹ️ תיק {case_number} כבר נמצא ב-{label}.")
            return

        ok = clickup_move_task(task["id"], list_id)
        if ok:
            label = STATUS_LISTS[list_id]["label"]
            cu_link = f"https://app.clickup.com/t/{task['id']}"
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔗 פתח ב-ClickUp", url=cu_link)
            ]])
            await query.edit_message_text(
                f"✅ *תיק {case_number}* הועבר ל:\n{label}",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        else:
            await query.edit_message_text(f"❌ שגיאה בהעברת תיק {case_number} ב-ClickUp.")

    except requests.HTTPError as e:
        await query.edit_message_text(f"❌ שגיאת ClickUp: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Status pick error: {e}", exc_info=True)
        await query.edit_message_text(f"❌ שגיאה: {str(e)[:200]}")

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("statuses", cmd_status_help))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_delivery_choice, pattern="^(walkin|mail)$"))
    app.add_handler(CallbackQueryHandler(handle_price_button, pattern="^price\\|"))
    app.add_handler(CallbackQueryHandler(handle_status_pick, pattern="^mv\\|"))
    app.add_handler(CallbackQueryHandler(handle_open_case_button, pattern="^open\\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("🤖 UDATA Bot v1.6 started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
