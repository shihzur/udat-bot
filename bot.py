#!/usr/bin/env python3
"""
UDATA Lab Telegram Bot - v1.8
Added in v1.8: REMINDERS
  * daily digest (10:00 Israel time, Sun-Fri) of ACTIVE cases by status
    (payment / diagnostics / waiting for signature / in work; finished cases are NOT included)
  * persistent nagging (12/14/16/18/20) about active cases that are overdue in their
    status, until the owner reacts (✅ Учтено / 😴 +1 день / moves the status)
  * case lookup now paginates all lists (old version saw only the first 100 team tasks)
  * /remind = digest now, /myid = show chat id (put it into Railway as OWNER_CHAT_ID)
  * state lives in ClickUp (task status + date_updated + comment markers), so it
    survives Railway restarts
Added in v1.7:
  * the sticker photo is saved as an attachment on the ClickUp task
  * "📝 הערה" button -> free-text note saved as a ClickUp comment
  * "📷 הוסף תמונה" button -> extra photos (e.g. remarks found during checking)
    attached to the task, no OCR
  * case lookup now shows a FULL card: sticker photo, extra photos, notes
    (old cases without photos/notes still work as before)
Fixed: show_case_card now works when opened from an inline button
(multi-case phone search), previously update.message was None there.
"""

import os, asyncio, base64, json, logging, re, time, requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta, time as dtime
from urllib.parse import quote
from dotenv import load_dotenv
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes
)

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

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

LABEL_MARK = "_label"          # sticker photo file name: <case>_label.jpg
IMG_EXTS   = (".jpg", ".jpeg", ".png", ".webp")
MAX_EXTRA_PHOTOS_SHOWN = 9     # + sticker photo = 10 = Telegram media group limit
MAX_NOTE_CHARS_SHOWN   = 500

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
        "label": "❌ Клиент отказался",
        "button": "❌ Клиент отказался",
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

_team_id_cache = None

def clickup_get_team_id() -> str:
    """Fetch and cache the workspace (team) ID — needed for v3 endpoints."""
    global _team_id_cache
    if _team_id_cache:
        return _team_id_cache
    resp = requests.get(
        "https://api.clickup.com/api/v2/team",
        headers={"Authorization": CLICKUP_TOKEN},
        timeout=15
    )
    resp.raise_for_status()
    teams = resp.json().get("teams", [])
    if not teams:
        raise RuntimeError("No ClickUp teams/workspaces found for this token")
    _team_id_cache = teams[0]["id"]
    return _team_id_cache

def clickup_get_all_tasks() -> list:
    """All tasks of the pipeline lists incl. archive, fully paginated (used by phone search).
    (The old team-wide endpoint returned only the first 100 tasks, so older cases were 'not found'.)"""
    return list(_iter_case_candidates())

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
    for t in _iter_case_candidates():
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
    """Move a task to a new home List, using the v3 endpoint.
    IMPORTANT: the old v2 endpoint (POST /list/{list_id}/task/{task_id}) adds
    the task as an *additional* List instead of moving it — ClickUp counts
    that as a "Tasks in Multiple Lists" (TIML) use, which has a lifetime cap
    on the Free plan and returns a 403 TIML_001 error once exhausted. The v3
    "home_list" endpoint performs a real move instead."""
    team_id = clickup_get_team_id()
    resp = requests.put(
        f"https://api.clickup.com/api/v3/workspaces/{team_id}/tasks/{task_id}/home_list/{list_id}",
        headers={"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"},
        json={},
        timeout=15
    )
    if resp.status_code not in (200, 204):
        logger.error(
            f"clickup_move_task failed: task={task_id} list={list_id} "
            f"status={resp.status_code} body={resp.text[:500]}"
        )
        return False
    return True

def clickup_get_task_full(task_id: str) -> dict:
    """Full task JSON (description + attachments)."""
    resp = requests.get(
        f"https://api.clickup.com/api/v2/task/{task_id}",
        headers={"Authorization": CLICKUP_TOKEN},
        timeout=20
    )
    resp.raise_for_status()
    return resp.json()

def clickup_get_task_description(task_id: str) -> str:
    """Fetch full description for a task (list endpoint truncates/omits it)."""
    j = clickup_get_task_full(task_id)
    return j.get("text_content") or j.get("description") or ""

def clickup_update_task_description(task_id: str, new_description: str) -> bool:
    resp = requests.put(
        f"https://api.clickup.com/api/v2/task/{task_id}",
        headers={"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"},
        json={"description": new_description},
        timeout=15
    )
    return resp.status_code == 200

def clickup_attach_file(task_id: str, file_bytes: bytes, filename: str,
                        content_type: str = "image/jpeg") -> dict:
    """Upload a file as an attachment on the task (multipart/form-data)."""
    resp = requests.post(
        f"https://api.clickup.com/api/v2/task/{task_id}/attachment",
        headers={"Authorization": CLICKUP_TOKEN},   # no Content-Type: requests sets the boundary
        files={"attachment": (filename, file_bytes, content_type)},
        timeout=60
    )
    resp.raise_for_status()
    return resp.json()

def clickup_add_comment(task_id: str, text: str) -> bool:
    resp = requests.post(
        f"https://api.clickup.com/api/v2/task/{task_id}/comment",
        headers={"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"},
        json={"comment_text": text, "notify_all": False},
        timeout=15
    )
    if resp.status_code != 200:
        logger.error(f"clickup_add_comment failed: {resp.status_code} {resp.text[:300]}")
        return False
    return True

def clickup_get_comments(task_id: str) -> list:
    resp = requests.get(
        f"https://api.clickup.com/api/v2/task/{task_id}/comment",
        headers={"Authorization": CLICKUP_TOKEN},
        timeout=15
    )
    resp.raise_for_status()
    return resp.json().get("comments", [])

def download_attachment(url: str):
    """Download an attachment's bytes. Returns bytes or None."""
    if not url:
        return None
    try:
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            r = requests.get(url, headers={"Authorization": CLICKUP_TOKEN}, timeout=30)
        if r.status_code == 200 and r.content:
            return r.content
        logger.error(f"download_attachment: status={r.status_code}")
    except Exception as e:
        logger.error(f"download_attachment error: {e}")
    return None

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

# ── Attachments / notes helpers ──────────────────────────────────────────────
def fmt_ts(ms) -> str:
    """ClickUp timestamp (ms since epoch, as str/int) -> 'DD/MM HH:MM' Israel time."""
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        if ZoneInfo:
            try:
                dt = dt.astimezone(ZoneInfo("Asia/Jerusalem"))
            except Exception:
                pass
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return ""

def att_title(a: dict) -> str:
    return (a.get("title") or a.get("name") or "").lower()

def is_image_att(a: dict) -> bool:
    ext = "." + (a.get("extension") or "").lower().lstrip(".")
    return ext in IMG_EXTS or att_title(a).endswith(IMG_EXTS)

def split_photos(full_task: dict):
    """Return (label_attachment_or_None, [extra_attachments oldest->newest])."""
    atts = [a for a in (full_task.get("attachments") or []) if is_image_att(a)]
    atts.sort(key=lambda a: int(a.get("date") or 0))
    label = next((a for a in atts if LABEL_MARK in att_title(a)), None)
    extras = [a for a in atts if a is not label]
    return label, extras

def get_task_id(context: ContextTypes.DEFAULT_TYPE, case_number: str):
    info = context.user_data.get(f"task_{case_number}")
    if info and info.get("task_id"):
        return info["task_id"]
    task = clickup_find_task_by_case(case_number)
    return task["id"] if task else None

async def send_case_photos(message, label, extras) -> bool:
    """Send sticker photo + extra photos. Returns False if something failed."""
    items = []
    if label:
        items.append((label, "📸 מדבקת הדיסק"))
    shown_extras = extras[-MAX_EXTRA_PHOTOS_SHOWN:]
    for a in shown_extras:
        items.append((a, f"📷 {fmt_ts(a.get('date'))}".strip()))

    loaded = []
    failed = False
    for a, cap in items:
        data = download_attachment(a.get("url"))
        if data:
            loaded.append((data, cap))
        else:
            failed = True

    try:
        if len(loaded) == 1:
            await message.reply_photo(photo=loaded[0][0], caption=loaded[0][1])
        elif len(loaded) > 1:
            await message.reply_media_group(
                media=[InputMediaPhoto(d, caption=c) for d, c in loaded]
            )
    except Exception as e:
        logger.error(f"send_case_photos error: {e}", exc_info=True)
        failed = True

    if len(extras) > len(shown_extras):
        await message.reply_text(
            f"ℹ️ יש {len(extras)} תמונות נוספות, מוצגות האחרונות {len(shown_extras)}."
        )
    return not failed

def md_escape(s: str) -> str:
    """Escape legacy-Markdown special chars so free-text notes can't break the card."""
    return re.sub(r"([_*`\[])", r"\\\1", s)

def build_notes_block(comments: list, budget: int = 2400, per_note: int = 300) -> str:
    """Notes (newest first) as a text block INSIDE the case card. '' if there are none."""
    notes = []
    for c in comments:
        txt = (c.get("comment_text") or "").strip()
        if not txt or SNZ_RE.search(txt):   # skip empty + reminder service markers
            continue
        notes.append((int(c.get("date") or 0), txt))
    if not notes:
        return ""
    notes.sort(key=lambda x: x[0], reverse=True)

    head = f"📝 *הערות ({len(notes)}):*"
    lines, used, shown = [head], len(head), 0
    for ts, txt in notes:
        if len(txt) > per_note:
            txt = txt[:per_note] + "…"
        line = f"• {fmt_ts(ts)} — {md_escape(txt)}"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    if shown < len(notes):
        lines.append(f"… ועוד {len(notes) - shown}")
    return "\n".join(lines) + "\n\n"

async def send_case_notes(message, comments: list):
    """Send notes (newest first) as plain text (no Markdown: free text may contain *_`)."""
    notes = []
    for c in comments:
        txt = (c.get("comment_text") or "").strip()
        if not txt or SNZ_RE.search(txt):   # skip empty + reminder service markers
            continue
        notes.append((int(c.get("date") or 0), txt))
    if not notes:
        return
    notes.sort(key=lambda x: x[0], reverse=True)

    lines = [f"📝 הערות ({len(notes)}):", ""]
    for ts, txt in notes:
        if len(txt) > MAX_NOTE_CHARS_SHOWN:
            txt = txt[:MAX_NOTE_CHARS_SHOWN] + "…"
        lines.append(f"• {fmt_ts(ts)} — {txt}")
        lines.append("")
    out = "\n".join(lines).strip()
    if len(out) > 3900:
        out = out[:3900] + "…"
    await message.reply_text(out)

# ── Reminders ────────────────────────────────────────────────────────────────
def _lab_tz():
    if ZoneInfo:
        try:
            return ZoneInfo("Asia/Jerusalem")
        except Exception:
            pass
    return timezone(timedelta(hours=2))   # fallback if tzdata is missing

LAB_TZ          = _lab_tz()
DIGEST_TIME     = dtime(10, 0, tzinfo=LAB_TZ)
NAG_TIMES       = [dtime(h, 0, tzinfo=LAB_TZ) for h in (12, 14, 16, 18, 20)]
REMIND_DAYS     = (0, 1, 2, 3, 4, 5)   # PTB: 0=Sunday ... 6=Saturday -> no reminders on Saturday
ARCHIVE_LIST_ID = "901817725472"
READY_LIST_ID   = "901817725465"
REFUSED_LIST_ID = "901817725467"
# reminders (digest + nags) cover ONLY active cases; finished ones (ready/refused/archive) are never sent
REMIND_LISTS    = {"901817725445", "901817725456", "901817725460", "901817725462"}
CASES_PER_MSG   = 8

# days in the same status after which a case is "overdue" (None = never nag)
OVERDUE_DAYS = {
    "901817725445": 3,    # ожидание оплаты диагностики
    "901817725456": 2,    # диагностика
    "901817725460": 3,    # отчёт отправлен, ждём подписи
    "901817725462": 14,   # в работе
    "901817725465": 0,    # готово к выдаче -> nag at once until "клиенту сообщил"
}
OVERDUE_TEXT = {
    "901817725445": "ждёт оплаты диагностики",
    "901817725456": "диагностика не завершена / отчёт не отправлен",
    "901817725460": "отчёт отправлен, подписи нет — написать клиенту",
    "901817725462": "долго в работе — проверить",
    "901817725465": "готов к выдаче — сообщил клиенту?",
}
SNOOZE_HOURS = {"ok": 72, "snz": 24, "ntf": 168, "h3": 3}
SNOOZE_LABEL = {
    "ok":  "✅ Учтено",
    "snz": "😴 Отложено на день",
    "ntf": "✅ Клиент уведомлён",
    "h3":  "⏰ Напомнить через 3 часа",
}
SNZ_RE = re.compile(r"\[snz:(\d+):(\d+)\]")   # [snz:<until_ms>:<list_id>]

def fmt_days(d: int) -> str:
    return "сегодня" if d <= 0 else f"{d} дн."

def get_owner_chat_id(app):
    env = os.getenv("OWNER_CHAT_ID")
    if env and env.strip().lstrip("-").isdigit():
        return int(env.strip())
    return app.bot_data.get("last_chat_id")

def _fetch_list_tasks(list_id: str, max_pages: int = 20, include_closed: bool = False) -> list:
    """All tasks of ONE list (paginated, 100 per page)."""
    tasks = []
    for page in range(max_pages):
        resp = requests.get(
            f"https://api.clickup.com/api/v2/list/{list_id}/task",
            headers={"Authorization": CLICKUP_TOKEN},
            params={"page": page, "include_closed": "true" if include_closed else "false"},
            timeout=30
        )
        resp.raise_for_status()
        batch = resp.json().get("tasks", [])
        for t in batch:
            t["list"] = {"id": list_id}
        tasks.extend(batch)
        if len(batch) < 100:
            break
    return tasks

def _iter_case_candidates():
    """Tasks of the working lists first (fast, parallel); the big archive only if nothing matched before."""
    working = [l for l in STATUS_ORDER if l != ARCHIVE_LIST_ID]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda l: _fetch_list_tasks(l, include_closed=True), working))
    for r in results:
        for t in r:
            yield t
    for t in _fetch_list_tasks(ARCHIVE_LIST_ID, max_pages=60, include_closed=True):
        yield t

def clickup_get_open_tasks() -> list:
    """Open tasks of the ACTIVE lists only (payment / diagnostics / waiting for signature / in work),
    fetched in parallel. Finished cases (ready, refused, archive) are not reminded about."""
    list_ids = [l for l in STATUS_ORDER if l in REMIND_LISTS]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_fetch_list_tasks, list_ids))
    return [t for r in results for t in r]

def get_snooze_until_ms(task_id: str, list_id: str) -> int:
    """Latest snooze marker written for the CURRENT status (older statuses ignored)."""
    best_date, best_until = -1, 0
    try:
        for c in clickup_get_comments(task_id):
            m = SNZ_RE.search(c.get("comment_text") or "")
            if not m or m.group(2) != str(list_id):
                continue
            d = int(c.get("date") or 0)
            if d > best_date:
                best_date, best_until = d, int(m.group(1))
    except Exception as e:
        logger.error(f"get_snooze_until_ms failed: {e}")
    return best_until

def collect_reminder_items() -> list:
    """Blocking: one dict per open (non-archived) case."""
    t0 = time.time()
    now_ms = int(t0 * 1000)
    items = []
    raw = clickup_get_open_tasks()
    t_fetch = time.time() - t0
    for t in raw:
        lid = (t.get("list") or {}).get("id")
        if lid not in REMIND_LISTS:
            continue
        name = t.get("name", "")
        case = name.split("|")[0].strip()
        try:
            upd = int(t.get("date_updated") or t.get("date_created") or now_ms)
        except Exception:
            upd = now_ms
        days = max(0, (now_ms - upd) // 86400000)
        thr = OVERDUE_DAYS.get(lid)
        overdue = thr is not None and days >= thr
        items.append({"id": t["id"], "case": case, "name": name[:70], "list_id": lid,
                      "days": days, "overdue": overdue, "snoozed": False})

    # snooze markers: one comments request per overdue case, in parallel
    need = [i for i in items if i["overdue"]]
    if need:
        with ThreadPoolExecutor(max_workers=8) as pool:
            untils = list(pool.map(lambda i: get_snooze_until_ms(i["id"], i["list_id"]), need))
        for i, u in zip(need, untils):
            i["snoozed"] = u > now_ms
    logger.info(
        f"collect_reminder_items: {len(items)} open, {len(need)} overdue, "
        f"fetch={t_fetch:.1f}s total={time.time() - t0:.1f}s"
    )
    return items

def build_digest_texts(items: list) -> list:
    today = datetime.now(LAB_TZ).strftime("%d/%m")
    if not items:
        return [f"☀️ Дайджест {today}: открытых кейсов нет."]
    attention = sum(1 for i in items if i["overdue"])
    blocks = [f"☀️ Дайджест {today}: открытых кейсов {len(items)}, требуют внимания {attention}"]
    for lid in STATUS_ORDER:
        if lid == ARCHIVE_LIST_ID:
            continue
        group = sorted([i for i in items if i["list_id"] == lid], key=lambda i: -i["days"])
        if not group:
            continue
        lines = [f"\n{STATUS_LISTS[lid]['label']} ({len(group)})"]
        for i in group:
            mark = "😴" if i["snoozed"] else ("⚠️" if i["overdue"] else "•")
            lines.append(f"{mark} {i['name']} — {fmt_days(i['days'])}")
        blocks.append("\n".join(lines))
    # split by LINES so a single long status group can never exceed Telegram's 4096 limit
    all_lines = []
    for b in blocks:
        all_lines.extend(b.split("\n"))
    texts, cur = [], ""
    for ln in all_lines:
        ln = ln[:300]
        if cur and len(cur) + len(ln) + 1 > 3800:
            texts.append(cur.strip())
            cur = ""
        cur += ln + "\n"
    if cur.strip():
        texts.append(cur.strip())
    return texts

async def send_attention(bot, chat_id, items: list, header: str):
    """Cases that need action, with buttons. Chunked to stay under Telegram limits."""
    items = sorted(items, key=lambda i: -i["days"])
    for n in range(0, len(items), CASES_PER_MSG):
        chunk = items[n:n + CASES_PER_MSG]
        lines = [header if n == 0 else "…продолжение", ""]
        rows = []
        for i in chunk:
            lines.append(
                f"⚠️ {i['name']}\n   {OVERDUE_TEXT[i['list_id']]} · {fmt_days(i['days'])}"
            )
            rows.append([
                InlineKeyboardButton(f"📁 {i['case']}", callback_data=f"open|{i['case']}"),
                InlineKeyboardButton("✅ Учтено", callback_data=f"rem|ok|{i['case']}"),
                InlineKeyboardButton("😴 +1 день", callback_data=f"rem|snz|{i['case']}"),
            ])
        await bot.send_message(chat_id, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

async def send_digest(bot, chat_id):
    items = await asyncio.to_thread(collect_reminder_items)
    for text in build_digest_texts(items):
        await bot.send_message(chat_id, text)
    need = [i for i in items if i["overdue"] and not i["snoozed"]]
    if need:
        await send_attention(bot, chat_id, need, "👇 Нужно действие:")

async def job_digest(context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_owner_chat_id(context.application)
    if not chat_id:
        logger.warning("Digest skipped: no OWNER_CHAT_ID and no chat seen yet (send /myid)")
        return
    try:
        await send_digest(context.bot, chat_id)
    except Exception as e:
        logger.error(f"job_digest failed: {e}", exc_info=True)
        try:
            await context.bot.send_message(chat_id, "⚠️ Не смог собрать дайджест из ClickUp.")
        except Exception:
            pass

async def job_nag(context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_owner_chat_id(context.application)
    if not chat_id:
        return
    try:
        items = await asyncio.to_thread(collect_reminder_items)
        need = [i for i in items if i["overdue"] and not i["snoozed"]]
        if need:
            await send_attention(context.bot, chat_id, need,
                                 f"🔔 Напоминание: ждут действия ({len(need)})")
    except Exception as e:
        logger.error(f"job_nag failed: {e}", exc_info=True)

async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["last_chat_id"] = update.effective_chat.id
    wait = await update.message.reply_text("⏳ Собираю данные из ClickUp...")
    try:
        await send_digest(context.bot, update.effective_chat.id)
    except Exception as e:
        logger.error(f"/remind failed: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:200]}")
    try:
        await wait.delete()
    except Exception:
        pass

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"ID этого чата: {update.effective_chat.id}\n"
        f"Добавь в Railway → Variables: OWNER_CHAT_ID = {update.effective_chat.id}"
    )

async def remember_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        context.application.bot_data["last_chat_id"] = update.effective_chat.id

async def handle_reminder_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, action, case_number = query.data.split("|")
        hours = SNOOZE_HOURS[action]
    except (ValueError, KeyError):
        return
    try:
        task = await asyncio.to_thread(clickup_find_task_by_case, case_number)
        if not task:
            await query.message.reply_text(f"❌ Кейс {case_number} не найден в ClickUp.")
            return
        until_ms = int((time.time() + hours * 3600) * 1000)
        comment = f"🔔 {SNOOZE_LABEL[action]} ({fmt_ts(until_ms)}) [snz:{until_ms}:{task['list_id']}]"
        ok = await asyncio.to_thread(clickup_add_comment, task["id"], comment)
        if not ok:
            await query.message.reply_text(f"❌ Не удалось сохранить отметку по кейсу {case_number}.")
            return

        # remove this case's button row from the reminder message
        markup = query.message.reply_markup
        rows = []
        if markup:
            for r in markup.inline_keyboard:
                if not any(b.callback_data and b.callback_data.endswith(f"|{case_number}") for b in r):
                    rows.append(r)
        if rows:
            await query.edit_message_reply_markup(InlineKeyboardMarkup(rows))
        else:
            await query.edit_message_text((query.message.text or "") + f"\n\n✔ {case_number}: {SNOOZE_LABEL[action]}")
    except Exception as e:
        logger.error(f"reminder button error: {e}", exc_info=True)
        await query.message.reply_text(f"❌ Ошибка: {str(e)[:200]}")

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
    """Build inline keyboard: price/recovered button on top, note/photo buttons,
    then status buttons (2 per row), excluding the current status."""
    rows = []

    price_btn_label = "✏️ עדכן מחיר / נתונים" if has_price else "💰 הוסף מחיר / נתונים שוחזרו"
    rows.append([InlineKeyboardButton(price_btn_label, callback_data=f"price|{case_number}")])

    rows.append([
        InlineKeyboardButton("📝 הערה", callback_data=f"note|{case_number}"),
        InlineKeyboardButton("📷 הוסף תמונה", callback_data=f"photo|{case_number}"),
    ])

    if current_list_id == READY_LIST_ID:
        rows.append([InlineKeyboardButton("📲 הודעת 'מוכן' ללקוח", callback_data=f"wa|ready|{case_number}")])
    elif current_list_id == REFUSED_LIST_ID:
        rows.append([
            InlineKeyboardButton("📲 הודעת סירוב ללקוח", callback_data=f"wa|refused|{case_number}"),
            InlineKeyboardButton("📲 לא ניתן לשחזר", callback_data=f"wa|unrec|{case_number}"),
        ])

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
    rows.append([InlineKeyboardButton("🚫 Нельзя восстановить", callback_data=f"unrec|{case_number}")])
    return InlineKeyboardMarkup(rows)

def card_contact(card: dict):
    """(phone_digits_or_'—', is_dealer) from a parsed card. A dealer code/name has no direct WhatsApp number."""
    raw = card.get("phone") or ""
    digits = re.sub(r"\D", "", raw)
    is_dealer = "דילר" in raw or not (9 <= len(digits) <= 12)
    return ("—" if is_dealer else digits), is_dealer

def build_wa_ready(case_number: str, card: dict) -> str:
    """WhatsApp text for 'ready for pickup'. Gender-neutral (plural), price shown as typed,
    VAT-inclusive label added when the price text has no VAT mention, offer to verify data before payment."""
    media = (card.get("media") or "").strip()
    head = f"תיק מספר {case_number}" + (f" ({media})" if media and media != "—" else "")
    lines = ["שלום 😊", "", f"{head} מוכן לאיסוף במעבדה ✅", ""]
    rec = card.get("recovered")
    price = card.get("price")
    if rec:
        lines.append(f"📦 מידע שוחזר: {rec}")
    if price:
        if "מע" not in price:
            price = f"{price} כולל מע״מ"
        lines.append(f"💰 עלות העבודה: {price}")
    if rec or price:
        lines.append("")
    lines += [
        "חשוב: להעברת המידע המשוחזר יש להביא דיסק יעד חדש ובנפח מתאים. "
        "ההעתקה אורכת זמן ולא תתבצע במקום באותה שעה.",
        "",
        "לפני ביצוע התשלום, אם תרצו — ניתן לתאם מראש בדיקה ואישור של המידע המשוחזר.",
        "",
        "📍 בית הלל 20, תל אביב. נשמח לתאם מועד מראש.",
        "",
        "אני כאן לכל שאלה 😊",
        "",
        "UDATA – Data Recovery Lab",
        "📞 072-249-4570",
    ]
    return "\n".join(lines).replace("`", "'")

def build_wa_refused(case_number: str, card: dict, unrecoverable: bool = False) -> str:
    """WhatsApp text when the client declined the recovery: no extra charge beyond the paid
    diagnostics (never 'free'/'no cost'), media can be collected within 60 days. Gender-neutral."""
    media = (card.get("media") or "").strip()
    head = f"תיק מספר {case_number}" + (f" ({media})" if media and media != "—" else "")
    if unrecoverable:
        intro = f"עדכון לגבי {head}: לצערנו, לא ניתן לשחזר את המידע מהמדיה."
    else:
        intro = f"עדכון לגבי {head}: הבנו שהחלטתם לא להמשיך בשחזור."
    lines = [
        "שלום 😊",
        "",
        intro,
        "",
        "לא יהיה חיוב נוסף מעבר לעלות הבדיקה.",
        "",
        "ניתן לאסוף את המדיה מהמעבדה (בית הלל 20, תל אביב) בתוך 60 יום, בתיאום מראש. "
        "לאחר מכן המדיה תיחשב כנטושה בהתאם לתנאי השירות.",
        "",
        "אני כאן לכל שאלה 😊",
        "",
        "UDATA – Data Recovery Lab",
        "📞 072-249-4570",
    ]
    return "\n".join(lines)

async def send_ready_message(message, task_id: str, case_number: str, name: str = "",
                             kind: str = "ready"):
    """Prepare a client WhatsApp message (kind: 'ready' = ready for pickup, 'refused' = client declined):
    copyable text + WhatsApp button."""
    try:
        full = await asyncio.to_thread(clickup_get_task_full, task_id)
        desc = full.get("text_content") or full.get("description") or ""
        card = parse_task_card(name or full.get("name", ""), desc)
        if kind == "refused":
            wa_text = build_wa_refused(case_number, card)
        elif kind == "unrec":
            wa_text = build_wa_refused(case_number, card, unrecoverable=True)
        else:
            wa_text = build_wa_ready(case_number, card)
        phone, is_dealer = card_contact(card)
        link = build_wa_link(phone, wa_text, is_dealer)

        notes = []
        if kind == "ready" and not card.get("price"):
            notes.append("⚠️ Цена не указана в карточке — в сообщении нет стоимости. "
                         "Добавь её кнопкой 💰 в карточке и открой сообщение снова.")
        if is_dealer:
            notes.append("ℹ️ Дилер / нет номера телефона — в WhatsApp выбери контакт вручную.")

        rows = []
        if len(link) <= 2000:
            rows.append([InlineKeyboardButton("📲 WhatsApp", url=link)])
        else:
            notes.append("ℹ️ Сообщение слишком длинное для кнопки — скопируй текст (нажми на него).")
        notified_btn = InlineKeyboardButton("✅ Клиенту сообщил", callback_data=f"rem|ntf|{case_number}")
        if kind in ("refused", "unrec"):
            rows.append([notified_btn])
        else:
            rows.append([
                notified_btn,
                InlineKeyboardButton("⏰ Через 3 часа", callback_data=f"rem|h3|{case_number}"),
            ])

        text = f"📲 Сообщение клиенту — кейс {case_number} (нажми на текст, чтобы скопировать)\n"
        if notes:
            text += "\n".join(notes) + "\n"
        text += f"\n```\n{wa_text}\n```"
        await message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        logger.error(f"send_ready_message error: {e}", exc_info=True)
        await message.reply_text(f"❌ Не удалось подготовить сообщение клиенту: {str(e)[:200]}")

async def handle_wa_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, kind, case_number = query.data.split("|")
    except ValueError:
        return
    task = await asyncio.to_thread(clickup_find_task_by_case, case_number)
    if not task:
        await query.message.reply_text(f"❌ Кейс {case_number} не найден в ClickUp.")
        return
    await send_ready_message(query.message, task["id"], case_number, task.get("name", ""),
                             kind=kind if kind in ("refused", "unrec") else "ready")

async def handle_unrec_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'Нельзя восстановить': move the case to the refusals list, note it in ClickUp,
    and prepare the client message (collect the media within 60 days)."""
    query = update.callback_query
    await query.answer()
    try:
        _, case_number = query.data.split("|")
    except ValueError:
        return
    try:
        task = await asyncio.to_thread(clickup_find_task_by_case, case_number)
        if not task:
            await query.message.reply_text(f"❌ Кейс {case_number} не найден в ClickUp.")
            return
        if task["list_id"] != REFUSED_LIST_ID:
            ok = await asyncio.to_thread(clickup_move_task, task["id"], REFUSED_LIST_ID)
            if not ok:
                await query.message.reply_text(f"❌ Ошибка переноса кейса {case_number} в ClickUp.")
                return
            await asyncio.to_thread(clickup_add_comment, task["id"], "🚫 Нельзя восстановить")
            cu_link = f"https://app.clickup.com/t/{task['id']}"
            await query.edit_message_text(
                f"✅ *תיק {case_number}* הועבר ל:\n❌ Нельзя восстановить",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔗 פתח ב-ClickUp", url=cu_link)]])
            )
        await send_ready_message(query.message, task["id"], case_number, task.get("name", ""), kind="unrec")
    except Exception as e:
        logger.error(f"unrec button error: {e}", exc_info=True)
        await query.message.reply_text(f"❌ Ошибка: {str(e)[:200]}")

def open_card_keyboard(case_number: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"📁 הצג כרטיס {case_number}", callback_data=f"open|{case_number}")
    ]])

CARD_RE = re.compile(r"^📁 תיק (\d{3,5})")

def case_from_reply(update: Update):
    """If the message is a Telegram Reply to one of the bot's case cards, return that case number."""
    r = update.message.reply_to_message if update.message else None
    if r and r.from_user and r.from_user.is_bot and r.text:
        m = CARD_RE.match(r.text.strip())
        if m:
            return m.group(1)
    return None

def clear_awaiting(context: ContextTypes.DEFAULT_TYPE):
    for k in ("awaiting_price_for", "awaiting_recovered_for",
              "awaiting_note_for", "awaiting_photo_for"):
        context.user_data.pop(k, None)

# ── Handlers ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 שלום! אני בוט UDATA Lab.\n\n"
        "📸 שלח לי צילום של מדבקת הדיסק — אני אוצר משימה ב-ClickUp,\n"
        "אשמור את התמונה בתיק, אוסיף לטבלה ואכין הודעת קבלה ל-WhatsApp.\n\n"
        "🔢 שלח רק את מספר התיק (למשל: `6280`) — ואני אציג לך:\n"
        "   • מדיה, דגם, טלפון לקוח\n"
        "   • מחיר שהוסכם + כמה מידע שוחזר\n"
        "   • סטטוס נוכחי\n"
        "   • תמונת המדבקה, תמונות נוספות והערות\n"
        "   • כפתורים: הערה / הוספת תמונה / שינוי סטטוס / עדכון מחיר\n\n"
        "📱 שלח מספר טלפון של לקוח — אם לא זוכר/ת את מספר התיק\n\n"
        "📌 /statuses — רשימת כל הסטטוסים הזמינים\n"
        "↩️ /cancel — ביטול פעולה (הערה / תמונה / מחיר)\n"
        "🔔 /remind — сводка напоминаний сейчас\n"
        "🆔 /myid — ID чата (для OWNER_CHAT_ID в Railway)",
        parse_mode="Markdown"
    )

async def cmd_status_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["📌 *סטטוסים זמינים:*\n"]
    for list_id in STATUS_ORDER:
        lines.append(STATUS_LISTS[list_id]["label"])
    lines.append("\n🔢 שלח מספר תיק (למשל `6280`) כדי לראות ולשנות סטטוס.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    had = any(context.user_data.get(k) for k in (
        "awaiting_price_for", "awaiting_recovered_for",
        "awaiting_note_for", "awaiting_photo_for"))
    clear_awaiting(context)
    await update.message.reply_text("↩️ בוטל." if had else "אין פעולה פעילה.")

async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    case_number = context.user_data.pop("awaiting_photo_for", None)
    if not case_number:
        await update.message.reply_text("אין מצב הוספת תמונות פעיל.")
        return
    await update.message.reply_text(
        f"✅ סיימת להוסיף תמונות לתיק {case_number}.",
        reply_markup=open_card_keyboard(case_number)
    )

async def save_extra_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, case_number: str,
                           reply_mode: bool = False):
    """Photo sent after pressing '📷 הוסף תמונה' -> attach to the case, no OCR."""
    msg = await update.message.reply_text("📎 שומר תמונה...")
    try:
        task_id = get_task_id(context, case_number)
        if not task_id:
            await msg.edit_text(f"❌ תיק {case_number} לא נמצא ב-ClickUp.")
            return

        photo_file = await context.bot.get_file(update.message.photo[-1].file_id)
        image_bytes = bytes(await photo_file.download_as_bytearray())
        filename = f"{case_number}_photo_{int(time.time())}.jpg"
        clickup_attach_file(task_id, image_bytes, filename)

        caption = (update.message.caption or "").strip()
        if caption:
            clickup_add_comment(task_id, f"📷 {caption}")

        tail = "" if reply_mode else "\nאפשר לשלוח עוד תמונות, או /done לסיום."
        await msg.edit_text(
            f"✅ התמונה נשמרה בתיק {case_number}.{tail}",
            reply_markup=open_card_keyboard(case_number)
        )
    except requests.HTTPError as e:
        logger.error(f"extra photo ClickUp error: {e.response.status_code} {e.response.text[:300]}")
        await msg.edit_text(f"❌ שגיאת ClickUp בשמירת התמונה: {e.response.status_code}")
    except Exception as e:
        logger.error(f"extra photo error: {e}", exc_info=True)
        await msg.edit_text(f"❌ שגיאה: {str(e)[:200]}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Photo sent as a Reply to a case card -> extra photo for that case
    reply_case = case_from_reply(update)
    if reply_case:
        await save_extra_photo(update, context, reply_case, reply_mode=True)
        return

    # If '📷 הוסף תמונה' was pressed -> this is an extra photo for that case, not a new intake
    awaiting_photo = context.user_data.get("awaiting_photo_for")
    if awaiting_photo:
        await save_extra_photo(update, context, awaiting_photo)
        return

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

        # Save the sticker photo on the task (failure must not break intake)
        photo_saved = False
        try:
            safe_case = re.sub(r"\W", "", str(data['case_number'])) or "case"
            clickup_attach_file(task_id, bytes(image_bytes), f"{safe_case}{LABEL_MARK}.jpg")
            photo_saved = True
        except Exception as e:
            logger.error(f"Attach label photo failed: {e}", exc_info=True)

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
            f"📊 Sheets: ✅\n"
            f"📎 תמונה ב-ClickUp: {'✅' if photo_saved else '⚠️ לא נשמרה'}\n\n"
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

    # Reply to a case card -> the text is a note for that case
    reply_case = case_from_reply(update)
    if reply_case and text:
        await save_note(update, context, reply_case, text)
        return

    # Free-text note for a case (after pressing '📝 הערה')
    awaiting_note = context.user_data.get("awaiting_note_for")
    if awaiting_note:
        await save_note(update, context, awaiting_note, text)
        return

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
        context.user_data.pop("awaiting_photo_for", None)
        await show_case_card(update, context, m.group(1))
        return

    if PHONE_RE.match(text):
        context.user_data.pop("awaiting_photo_for", None)
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

async def handle_note_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, case_number = query.data.split("|")
    except ValueError:
        await query.edit_message_text("❌ שגיאה בנתוני הכפתור.")
        return
    clear_awaiting(context)
    context.user_data["awaiting_note_for"] = case_number
    await query.message.reply_text(
        f"📝 תיק {case_number} — *כתוב את ההערה* (טקסט חופשי).\n"
        f"לביטול: /cancel",
        parse_mode="Markdown"
    )

async def handle_photo_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, case_number = query.data.split("|")
    except ValueError:
        await query.edit_message_text("❌ שגיאה בנתוני הכפתור.")
        return
    clear_awaiting(context)
    context.user_data["awaiting_photo_for"] = case_number
    await query.message.reply_text(
        f"📷 תיק {case_number} — *שלח תמונה* (אפשר כמה, אפשר להוסיף כיתוב).\n"
        f"התמונות יישמרו בתיק ללא זיהוי מדבקה.\n"
        f"לסיום: /done",
        parse_mode="Markdown"
    )

async def save_note(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    case_number: str, note_text: str):
    context.user_data.pop("awaiting_note_for", None)
    try:
        task_id = get_task_id(context, case_number)
        if not task_id:
            await update.message.reply_text(f"❌ תיק {case_number} לא נמצא ב-ClickUp.")
            return
        ok = clickup_add_comment(task_id, note_text)
        if ok:
            await update.message.reply_text(
                f"✅ ההערה נשמרה בתיק {case_number}.",
                reply_markup=open_card_keyboard(case_number)
            )
        else:
            await update.message.reply_text(f"❌ שגיאה בשמירת ההערה בתיק {case_number}.")
    except requests.HTTPError as e:
        await update.message.reply_text(f"❌ שגיאת ClickUp: {e.response.status_code}")
    except Exception as e:
        logger.error(f"save_note error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ שגיאה: {str(e)[:200]}")

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
        parse_mode="Markdown",
        reply_markup=open_card_keyboard(case_number)
    )

async def show_case_card(update: Update, context: ContextTypes.DEFAULT_TYPE, case_number: str):
    # effective_message works for both text messages and inline-button callbacks
    message = update.effective_message
    msg = await message.reply_text(f"🔍 מחפש תיק {case_number}...")
    try:
        task = clickup_find_task_by_case(case_number)
        if not task:
            await msg.edit_text(f"❌ לא נמצא תיק שמתחיל ב-\"{case_number}\" ב-ClickUp.")
            return

        # Full task: complete description + attachments
        full = clickup_get_task_full(task["id"])
        description = full.get("text_content") or full.get("description") or task.get("description") or ""
        card = parse_task_card(task["name"], description)

        current_list_id = task["list_id"]
        current_label = STATUS_LISTS.get(current_list_id, {}).get("label", task.get("list_name", "—"))

        # Keep latest description handy for the price/recovered flow
        context.user_data[f"task_{case_number}"] = {
            "task_id": task["id"],
            "description": description,
        }

        try:
            comments = clickup_get_comments(task["id"])
        except Exception as e:
            logger.error(f"get_comments failed: {e}")
            comments = []
        notes_block = build_notes_block(comments)

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
            f"{notes_block}"
            f"✍️ הערה / תמונה: השב (Reply) על הכרטיס הזה\n"
            f"בחר/י פעולה:"
        )
        keyboard = build_status_keyboard(case_number, current_list_id, has_price=bool(card.get("price")))

        # Order: photos -> card with buttons -> notes (notes never come before the card)
        label, extras = split_photos(full)
        photos_ok = True
        if label or extras:
            photos_ok = await send_case_photos(message, label, extras)

        if not photos_ok:
            await message.reply_text("⚠️ חלק מהתמונות לא נטענו.")

        try:
            await msg.delete()
        except Exception:
            pass
        try:
            await message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            # a note with odd formatting must never block the card: resend as plain text
            logger.error(f"card markdown failed, sending plain: {e}")
            await message.reply_text(text.replace("*", "").replace("\\", ""), reply_markup=keyboard)

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

    clear_awaiting(context)
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
            # moved to "Ready for pickup" -> prepare the WhatsApp message for the client right away
            if list_id == READY_LIST_ID:
                await send_ready_message(query.message, task["id"], case_number, task.get("name", ""))
            elif list_id == REFUSED_LIST_ID:
                await send_ready_message(query.message, task["id"], case_number, task.get("name", ""),
                                         kind="refused")
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
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(MessageHandler(filters.ALL, remember_chat, block=False), group=-1)
    app.add_handler(CallbackQueryHandler(handle_reminder_button, pattern="^rem\\|"))
    app.add_handler(CallbackQueryHandler(handle_wa_button, pattern="^wa\\|"))
    app.add_handler(CallbackQueryHandler(handle_unrec_button, pattern="^unrec\\|"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_delivery_choice, pattern="^(walkin|mail)$"))
    app.add_handler(CallbackQueryHandler(handle_price_button, pattern="^price\\|"))
    app.add_handler(CallbackQueryHandler(handle_note_button, pattern="^note\\|"))
    app.add_handler(CallbackQueryHandler(handle_photo_button, pattern="^photo\\|"))
    app.add_handler(CallbackQueryHandler(handle_status_pick, pattern="^mv\\|"))
    app.add_handler(CallbackQueryHandler(handle_open_case_button, pattern="^open\\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    jq = app.job_queue
    if jq is None:
        logger.warning("JobQueue unavailable: use 'python-telegram-bot[job-queue]' in requirements.txt")
    else:
        jq.run_daily(job_digest, DIGEST_TIME, days=REMIND_DAYS, name="digest")
        for n, t in enumerate(NAG_TIMES):
            jq.run_daily(job_nag, t, days=REMIND_DAYS, name=f"nag{n}")
    logger.info("🤖 UDATA Bot v1.8 started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
