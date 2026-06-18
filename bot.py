#!/usr/bin/env python3
"""
UDATA Lab Telegram Bot - MVP v1.0
Workflow: photo → OCR (Claude) → ClickUp task → intake WhatsApp + Sheets link
"""

import os, base64, json, logging, requests
from urllib.parse import quote
from dotenv import load_dotenv
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

load_dotenv()  # loads .env file automatically

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY")
CLICKUP_TOKEN   = os.getenv("CLICKUP_TOKEN")
CLICKUP_LIST_ID = "901817725456"  # Diagnostics list
SHEETS_URL      = (
    "https://script.google.com/macros/s/"
    "AKfycbxP-mBb6C_TRlQbQENlbPfqn4v_jo8LwRe9RuuES-RsRsdti53Efpu2ruUpDgwQuhVx/exec"
)
FREE_DIAG_CODES = {"1209", "1414", "NTL", "FM", "9090"}
FREE_DIAG_NAMES = {"מדי מחשבים", "hanan", "oren technologies"}

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── ClickUp API ──────────────────────────────────────────────────────────────
def clickup_create_task(name: str, description: str) -> str:
    resp = requests.post(
        f"https://api.clickup.com/api/v2/list/{CLICKUP_LIST_ID}/task",
        headers={"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"},
        json={"name": name, "description": description},
        timeout=15
    )
    resp.raise_for_status()
    return resp.json()["id"]

# ── OCR via Claude Vision ─────────────────────────────────────────────────────
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

RULES:
- 4-digit number (1209, 9090, 1414) = DEALER CODE → is_dealer=true
- Name like "Hanan", "מדי מחשבים" = DEALER → is_dealer=true
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

def build_wa_intake(data: dict) -> str:
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

def build_sheets_url(data: dict) -> str:
    model = data['model'].replace('/', '%2F').replace(' ', '+')
    phone = str(data['phone']).replace(' ', '+')
    return (
        f"{SHEETS_URL}"
        f"?case_number={data['case_number']}"
        f"&phone={phone}"
        f"&model={model}"
        f"&serial={data['serial']}"
        f"&capacity={data['capacity']}"
    )

def build_wa_link(data: dict, wa_text: str) -> str:
    encoded = quote(wa_text)
    if not data.get("is_dealer") and data.get("phone", "—") != "—":
        phone = str(data['phone']).replace('-', '').replace(' ', '')
        if phone.startswith('0'):
            phone = '972' + phone[1:]
        return f"https://wa.me/{phone}?text={encoded}"
    return f"https://wa.me/?text={encoded}"

# ── Handlers ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 שלום! אני בוט UDATA Lab.\n\n"
        "📸 שלח לי צילום של מדבקת הדיסק ואני אדאג לכל השאר:\n\n"
        "✅ יצירת משימה ב-ClickUp\n"
        "💬 הודעת קבלה ל-WhatsApp\n"
        "📊 קישור לטבלה"
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

        wa_text    = build_wa_intake(data)
        wa_link    = build_wa_link(data, wa_text)
        sheets_url = build_sheets_url(data)
        cu_link    = f"https://app.clickup.com/t/{task_id}"
        free_note  = "\n🆓 דילר — דיאגנוסטיקה חינם" if is_free_diag(data) else ""

        reply = (
            f"✅ *תיק {data['case_number']} נוצר*\n\n"
            f"📱 {data['phone']}{'  (דילר)' if data['is_dealer'] else ''}{free_note}\n"
            f"💾 {data['brand']} {data['media_type']} {data['capacity']}\n"
            f"🔧 {data['model']}\n"
            f"🔑 {data['serial']}\n\n"
            f"*הודעת קבלה (העתק ל-WhatsApp):*\n"
            f"```\n{wa_text}\n```"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📲 WhatsApp", url=wa_link),
                InlineKeyboardButton("📊 Sheets", url=sheets_url),
            ],
            [InlineKeyboardButton("🔗 ClickUp", url=cu_link)]
        ])

        await msg.delete()
        await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=keyboard)

    except json.JSONDecodeError:
        await msg.edit_text("❌ שגיאה בפענוח המדבקה. נסה שוב.")
    except requests.HTTPError as e:
        await msg.edit_text(f"❌ שגיאת ClickUp: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await msg.edit_text(f"❌ שגיאה: {str(e)[:200]}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 שלח לי צילום של מדבקת הדיסק")

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("🤖 UDATA Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
