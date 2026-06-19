#!/usr/bin/env python3
"""
UDATA Lab Telegram Bot - v1.2
Added: delivery method selection (walk-in vs mail/courier)
"""

import os, base64, json, logging, requests
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
CLICKUP_LIST_ID = "901817725456"
SHEETS_URL      = (
    "https://script.google.com/macros/s/"
    "AKfycbxP-mBb6C_TRlQbQENlbPfqn4v_jo8LwRe9RuuES-RsRsdti53Efpu2ruUpDgwQuhVx/exec"
)
FREE_DIAG_CODES = {"1209", "1414", "NTL", "FM", "9090"}
FREE_DIAG_NAMES = {"מדי מחשבים", "hanan", "oren technologies", "etl"}

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

# ── Handlers ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 שלום! אני בוט UDATA Lab.\n\n"
        "📸 שלח לי צילום של מדבקת הדיסק — אני אוצר משימה ב-ClickUp,\n"
        "אוסיף לטבלה ואכין הודעת קבלה ל-WhatsApp."
    )

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

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 שלח לי צילום של מדבקת הדיסק")

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_delivery_choice, pattern="^(walkin|mail)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("🤖 UDATA Bot v1.2 started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
