"""Xarajatlar hisob-kitobi uchun Telegram bot."""

import asyncio
import csv
import html
import io
import logging
import os
import re
import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    TypeHandler,
    filters,
)

import db
import enrich
import ocr
import reports
from parser import contains_full_card_number, format_sum, parse_card_line, parse_expense
import soliq
from receipt import ReceiptError, read_receipt

load_dotenv()

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("hisob-bot")

TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Tashkent"))
ALLOWED_USER_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}
# Claude faqat QR-kodsiz cheklar uchun ishlatiladi va faqat kalit berilgan bo'lsa (pullik)
CLAUDE_ENABLED = bool(os.getenv("ANTHROPIC_API_KEY"))
# Chek ma'lumotini soliq.uz dan olish. soliq.uz faqat O'zbekistondan ochiladi, shuning uchun uni
# kompyuterdagi soliq_worker.py bajaradi; u ishlamayotgan bo'lsa summa chek rasmidan (OCR) o'qiladi.
SOLIQ_LOOKUP = os.getenv("SOLIQ_LOOKUP", "true").strip().lower() in ("1", "true", "yes", "ha")
SOLIQ_WAIT_SECONDS = float(os.getenv("SOLIQ_WAIT_SECONDS", "20"))
# Haftalik hisobot vaqti: yakshanba, 21:00 (o'zgartirish mumkin)
WEEKLY_REPORT_TIME = time(
    int(os.getenv("WEEKLY_REPORT_HOUR", "21")), int(os.getenv("WEEKLY_REPORT_MINUTE", "0")), tzinfo=TZ
)

HELP_TEXT = """👋 <b>Xarajatlar hisob-kitobi boti</b>

<b>1. Avval kartalaringizni qo'shing</b>
/karta buyrug'ini bosing va har bir kartani alohida qatorda yozing:
<code>Humo Kapitalbank 9860 1234 5678 9012
Uzcard Asaka 8600 1111 2222 3333
Visa 4111 1111 1111 4444</code>
🔒 Bazaga faqat karta nomi va <b>oxirgi 4 raqami</b> saqlanadi, to'liq raqamli xabaringiz o'chirib tashlanadi.

<b>2. Xarajatni yozing</b>
<code>Polene sumka 500000</code>
<code>Polene sumka 500 ming</code>
<code>Taksi 25k</code>
<code>kecha telefon 1 mln 200 ming</code>
Bot qaysi kartadan to'langanini so'raydi — tugmani bosing yoki oxirgi 4 raqamni yozing.

<b>3. Chek tashlang</b>
Chekning rasmini yuboring — bot soliq.uz dan summa, do'kon va mahsulotlarni oladi
(bo'lmasa JAMI summasini rasmdan o'qiydi).
Rasmga izoh yozsangiz (masalan <code>Qo'zi go'shti</code>), xarajat shu nom bilan saqlanadi.
"JAMI" qatori va QR-kod aniq ko'rinsin (tekis, yorug' joyda rasmga oling).

<b>Buyruqlar</b>
/bugun — bugungi xarajatlar
/hafta — shu hafta
/oy — shu oy
/oxirgi — oxirgi 10 ta (o'chirish mumkin)
/kartalar — kartalar ro'yxati
/karta — karta qo'shish
/karta_ochir 1234 — kartani o'chirish
/ism — ism-familiyani o'zgartirish
/qidir sumka — xarajatlar ichidan qidirish
/limit 3 mln — oylik limit (80% va 100% da ogohlantiradi)
/export — hamma xarajatlar CSV (Excel) faylda

✍️ Imlo xatolari o'zi tuzatiladi; nomi, brendi, rangi, korobkasi va kategoriyasi alohida saqlanadi.
📬 Har yakshanba kechqurun haftalik, har oyning 1-kuni oylik hisobot o'zi keladi."""


def now() -> datetime:
    return datetime.now(TZ)


# --- Ruxsat ---

async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not ALLOWED_USER_IDS or (user and user.id in ALLOWED_USER_IDS):
        # Serverless'da xotira so'rovlar orasida saqlanmaydi — holatni har safar bazadan olamiz
        if user and context.user_data is not None:
            context.user_data.clear()
            context.user_data.update(db.load_state(user.id))
            db.touch_user(user.id, user.full_name, user.username)
        return
    if update.effective_message:
        await update.effective_message.reply_text(
            f"⛔ Bu bot shaxsiy. Sizning ID: {user.id if user else '?'}"
        )
    raise ApplicationHandlerStop


async def save_user_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Har bir xabardan keyin (oxirgi guruhda) holatni bazaga yozadi."""
    if update.effective_user and context.user_data is not None:
        db.save_state(update.effective_user.id, dict(context.user_data))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Xatolik", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id, "⚠️ Xatolik yuz berdi, birozdan keyin qayta urinib ko'ring."
            )
        except TelegramError:
            pass


# --- Umumiy buyruqlar ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)
    user_id = update.effective_user.id
    if not db.get_full_name(user_id):
        await ask_name(update, context)
    elif not db.list_cards(user_id):
        await update.message.reply_text("Boshlash uchun /karta ni bosing va kartalaringizni yozing 👇")


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE, then: str | None = None) -> None:
    context.user_data["state"] = "ask_name"
    if then:
        context.user_data["after_name"] = then
    await update.message.reply_text(
        "👤 Ism-familiyangizni yozing (kartalaringiz shu nomga yoziladi):\n"
        "<code>Gulshoda Qudratova</code>"
    )


async def name_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    current = db.get_full_name(update.effective_user.id)
    if current:
        await update.message.reply_text(f"Hozirgi ism-familiyangiz: <b>{html.escape(current)}</b>")
    await ask_name(update, context)


_NAME_WORD_RE = re.compile(r"^[^\W\d_]+(?:[ʻʼ'’`-][^\W\d_]+)*\.?$")


def normalize_full_name(text: str) -> str | None:
    """'gulshoda qudratova' -> 'Gulshoda Qudratova'. Kamida 2 so'z, raqamsiz bo'lishi kerak."""
    words = text.split()
    if not 2 <= len(words) <= 4 or len(text) > 60:
        return None
    if not all(_NAME_WORD_RE.match(w) for w in words):
        return None
    return " ".join(w[0].upper() + w[1:] for w in words)


async def save_name(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    full_name = normalize_full_name(text)
    if not full_name:
        await update.message.reply_text(
            "Ism va familiyani harflar bilan, bo'sh joy bilan ajratib yozing, masalan: "
            "<code>Gulshoda Qudratova</code>"
        )
        return
    user_id = update.effective_user.id
    db.set_full_name(user_id, full_name)
    context.user_data.pop("state", None)
    after = context.user_data.pop("after_name", None)
    await update.message.reply_text(f"✅ Saqlandi: <b>{html.escape(full_name)}</b>")
    if after == "add_card" or not db.list_cards(user_id):
        await prompt_cards(update, context)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


# --- Kartalar ---

async def card_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not db.get_full_name(update.effective_user.id):
        await ask_name(update, context, then="add_card")
        return
    text = update.message.text.partition(" ")[2].strip()
    if text:
        await save_cards(update, context, text)
        return
    await prompt_cards(update, context)


async def prompt_cards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["state"] = "add_card"
    await update.message.reply_text(
        "Kartalaringizni yozing — har birini alohida qatorda, nomi va raqami bilan:\n\n"
        "<code>Humo Kapitalbank 9860 1234 5678 9012\n"
        "Uzcard Asaka 8600 1111 2222 3333</code>\n\n"
        "To'liq raqam o'rniga faqat oxirgi 4 raqamni yozsangiz ham bo'ladi: <code>Humo 9012</code>\n"
        "Oxirgi 4 raqami bir xil kartalarni nomi bilan ajrating: <code>Humo 1234</code>, <code>Uzcard 1234</code>"
    )


async def save_cards(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    user_id = update.effective_user.id
    context.user_data.pop("state", None)
    holder = db.get_full_name(user_id)
    saved, bad = [], []
    for line in filter(None, (l.strip() for l in text.splitlines())):
        parsed = parse_card_line(line)
        if not parsed:
            bad.append(line)
            continue
        name, last4, _ = parsed
        is_new = db.add_card(user_id, name, last4, holder)
        saved.append(f"{'➕' if is_new else '♻️ (oldin bor edi)'} {html.escape(name)} •••• {last4}")

    if contains_full_card_number(text):
        try:
            await update.message.delete()
        except TelegramError:
            log.warning("To'liq karta raqamli xabarni o'chirib bo'lmadi")

    lines = []
    if saved:
        lines.append(f"✅ Saqlandi ({html.escape(holder or '')}):\n" + "\n".join(saved))
    if bad:
        lines.append("⚠️ Bu qatorlarda karta raqami topilmadi:\n" + "\n".join(html.escape(b) for b in bad))
        context.user_data["state"] = "add_card"
        lines.append("Qaytadan yozing yoki /bekor ni bosing.")
    elif saved:
        lines.append("\nEndi xarajatlaringizni yozishingiz mumkin, masalan: <code>Polene sumka 500000</code>")
    await context.bot.send_message(update.effective_chat.id, "\n".join(lines))


async def cards_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    cards = db.list_cards(user_id)
    if not cards:
        await update.message.reply_text("Hali karta qo'shilmagan. /karta ni bosing.")
        return
    first = now().date().replace(day=1)
    month_rows = db.expenses_between(user_id, first.isoformat(), "9999-12-31")
    spent: dict[int | None, int] = {}
    for r in month_rows:
        spent[r["card_id"]] = spent.get(r["card_id"], 0) + r["amount"]
    holder = db.get_full_name(user_id)
    lines = [f"💳 <b>Kartalaringiz</b>{' — ' + html.escape(holder) if holder else ''} (shu oydagi xarajat):", ""]
    for c in cards:
        lines.append(f"• {html.escape(c['name'])} •••• {c['last4']} — {format_sum(spent.get(c['id'], 0))}")
    if spent.get(None):
        lines.append(f"• 💵 Naqd — {format_sum(spent[None])}")
    await update.message.reply_text("\n".join(lines))


async def card_delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit() or len(context.args[0]) != 4:
        await update.message.reply_text("Oxirgi 4 raqamni yozing: <code>/karta_ochir 1234</code>")
        return
    cards = db.find_cards(update.effective_user.id, context.args[0])
    if not cards:
        await update.message.reply_text("Bunday karta topilmadi.")
    elif len(cards) == 1:
        db.delete_card(update.effective_user.id, cards[0]["id"])
        await update.message.reply_text(
            f"🗑 {html.escape(cards[0]['name'])} •••• {cards[0]['last4']} o'chirildi. "
            "Unga yozilgan xarajatlar saqlanib qoladi."
        )
    else:
        await update.message.reply_text(
            f"•••• {context.args[0]} bilan {len(cards)} ta karta bor. Qaysi birini o'chiray?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🗑 {c['name']} •{c['last4']}", callback_data=f"delcard:{c['id']}")]
                for c in cards
            ]),
        )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("state", None)
    context.user_data.pop("pending", None)
    context.user_data.pop("receipt_spent_at", None)
    await update.message.reply_text("Bekor qilindi.")


# --- Xarajat kiritish ---

def card_keyboard(user_id: int, pid: str, suggested_card_id: int | None = None) -> InlineKeyboardMarkup:
    cards = sorted(db.list_cards(user_id), key=lambda c: c["id"] != suggested_card_id)
    buttons = [
        InlineKeyboardButton(
            f"{'✅ ' if c['id'] == suggested_card_id else ''}{c['name']} •{c['last4']}",
            callback_data=f"card:{pid}:{c['id']}",
        )
        for c in cards
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([
        InlineKeyboardButton("💵 Naqd", callback_data=f"card:{pid}:cash"),
        InlineKeyboardButton("❌ Bekor", callback_data=f"cancel:{pid}"),
    ])
    return InlineKeyboardMarkup(rows)


def info_text(info: dict | None) -> str:
    """'🏷 Polene · 🎨 qora · 📦 korobka bilan · 🗂 Aksessuarlar'"""
    if not info:
        return ""
    parts = []
    if info.get("brand"):
        parts.append(f"🏷 {html.escape(info['brand'])}")
    if info.get("color"):
        parts.append(f"🎨 {html.escape(info['color'])}")
    if info.get("has_box") is not None:
        parts.append("📦 korobka bilan" if info["has_box"] else "📦 korobkasiz")
    if info.get("category"):
        parts.append(f"🗂 {html.escape(info['category'])}")
    return " · ".join(parts)


def pending_text(p: dict) -> str:
    text = (
        f"🧾 <b>{html.escape(p['description'])}</b> — {format_sum(p['amount'])}\n"
        f"📅 {p['spent_at'][8:10]}.{p['spent_at'][5:7]}.{p['spent_at'][:4]}"
    )
    details = info_text(p.get("info"))
    if details:
        text += "\n" + details
    if p.get("extra"):
        text += "\n" + p["extra"]
    return text


async def ask_card(update: Update, context: ContextTypes.DEFAULT_TYPE, p: dict, reply_to=None) -> None:
    user_id = update.effective_user.id
    pid = uuid.uuid4().hex[:8]
    pending = context.user_data.setdefault("pending", {})
    pending[pid] = p
    context.user_data["last_pending"] = pid

    question = "\n\n💳 Qaysi kartadan? Tugmani bosing yoki oxirgi 4 raqamni yozing."
    if p.get("suggested_card_id"):
        question = "\n\n💳 Chekdagi karta ✅ bilan belgilandi — tasdiqlang yoki boshqasini tanlang."
    if not db.list_cards(user_id):
        question += "\n(Kartalar hali qo'shilmagan — /karta)"
    markup = card_keyboard(user_id, pid, p.get("suggested_card_id"))

    if reply_to is not None:
        msg = await reply_to.edit_text(pending_text(p) + question, reply_markup=markup)
    else:
        msg = await update.message.reply_text(pending_text(p) + question, reply_markup=markup)
    p["message_id"] = msg.message_id


async def finalize(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int, pid: str,
                   card_id: int | None) -> None:
    p = context.user_data.get("pending", {}).pop(pid, None)
    if p is None:
        return
    expense_id = db.add_expense(
        user_id, p["amount"], p["description"], card_id, p["spent_at"], p["source"],
        info=p.get("info"), raw_text=p.get("raw_text"),
        store=p.get("store"), receipt_items=p.get("receipt_items"),
    )
    if context.user_data.get("last_pending") == pid:
        context.user_data.pop("last_pending", None)

    card_text = "💵 Naqd"
    if card_id is not None:
        card = db.get_card(user_id, card_id)
        if card is not None:
            card_text = f"💳 {html.escape(card['name'])} •{card['last4']}"

    day = p["spent_at"][:10]
    day_total = sum(r["amount"] for r in db.expenses_between(user_id, day, day + "~"))
    text = f"✅ Saqlandi\n{pending_text(p)}\n{card_text}\n\nShu kun jami: {format_sum(day_total)}"
    undo = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Bekor qilish", callback_data=f"undo:{expense_id}")]])
    try:
        await context.bot.edit_message_text(text, chat_id=chat_id, message_id=p["message_id"], reply_markup=undo)
    except TelegramError:
        await context.bot.send_message(chat_id, text, reply_markup=undo)
    await check_limit(context.bot, user_id, chat_id, p["spent_at"])


async def check_limit(bot, user_id: int, chat_id: int, spent_at: str) -> None:
    """Oylik limitning 80% va 100% iga yetganda bir martadan ogohlantiradi."""
    limit = db.get_monthly_limit(user_id)
    month = spent_at[:7]
    if not limit or month != now().strftime("%Y-%m"):
        return
    total = db.month_total(user_id, month)
    for pct in (100, 80):
        if total * 100 >= limit * pct:
            if db.mark_report_sent(user_id, f"limit{pct}:{month}"):
                if pct == 100:
                    text = (f"🚨 Oylik limit tugadi! Shu oy {format_sum(total)} sarfladingiz "
                            f"(limit {format_sum(limit)}, {format_sum(total - limit)} ortiqcha).")
                else:
                    text = (f"⚠️ Oylik limitning {total * 100 // limit}% i ishlatildi: "
                            f"{format_sum(total)} / {format_sum(limit)}. Qoldi: {format_sum(limit - total)}.")
                await bot.send_message(chat_id, text)
            break


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if context.user_data.get("state") == "ask_name":
        await save_name(update, context, text)
        return
    if context.user_data.get("state") == "add_card":
        await save_cards(update, context, text)
        return

    # Kutilayotgan xarajatga karta tanlash: "1234" yoki "naqd"
    last_pid = context.user_data.get("last_pending")
    if last_pid and last_pid in context.user_data.get("pending", {}):
        if text.isdigit() and len(text) == 4:
            cards = db.find_cards(user_id, text)
            if not cards:
                await update.message.reply_text(
                    f"•••• {text} karta topilmadi. Tugmalardan birini tanlang yoki /karta orqali qo'shing."
                )
            elif len(cards) == 1:
                await finalize(context, user_id, update.effective_chat.id, last_pid, cards[0]["id"])
            else:
                await update.message.reply_text(
                    f"•••• {text} bilan {len(cards)} ta karta bor. Qaysi biri?",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(f"{c['name']} •{c['last4']}", callback_data=f"card:{last_pid}:{c['id']}")
                        for c in cards
                    ]]),
                )
            return
        if text.lower() in ("naqd", "naqd pul", "нақд", "наличные"):
            await finalize(context, user_id, update.effective_chat.id, last_pid, None)
            return

    if contains_full_card_number(text):
        await update.message.reply_text("Karta qo'shmoqchi bo'lsangiz, avval /karta ni bosing.")
        return

    parsed = parse_expense(text)
    if parsed is None:
        await update.message.reply_text(
            "Summani topa olmadim 🤔 Masalan shunday yozing: <code>Polene sumka 500000</code> "
            "yoki <code>Taksi 25 ming</code>"
        )
        return
    if parsed.amount > 100_000_000_000:
        await update.message.reply_text("Summa juda katta, tekshirib qayta yozing.")
        return

    receipt_spent_at = context.user_data.pop("receipt_spent_at", None)
    spent = now() - timedelta(days=parsed.days_ago)
    info = await enrich.enrich(parsed.description)
    await ask_card(update, context, {
        "amount": parsed.amount,
        "description": info.description,
        "raw_text": text,
        "info": info.to_dict(),
        "spent_at": receipt_spent_at or spent.strftime("%Y-%m-%d %H:%M"),
        "source": "receipt" if receipt_spent_at else "text",
    })


async def lookup_soliq(qr_url: str) -> tuple[soliq.SoliqReceipt | None, str]:
    """Chekni soliq.uz dan olish: vazifani navbatga qo'yib, yordamchining javobini kutadi.

    Sabab: "ok", "off" (o'chirilgan), "offline" (yordamchi ishlamayapti), "pending" (chek hali
    soliq bazasiga tushmagan), "timeout" yoki "error".
    """
    if not SOLIQ_LOOKUP:
        return None, "off"
    if not db.soliq_worker_alive():
        return None, "offline"
    job_id = db.enqueue_soliq_job(qr_url)
    deadline = asyncio.get_running_loop().time() + SOLIQ_WAIT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.7)
        job = db.get_soliq_job(job_id)
        if job and job["status"] == "ok":
            return soliq.SoliqReceipt.from_dict(job["result"]), "ok"
        if job and job["status"] in ("pending", "error"):
            return None, job["status"]
    return None, "timeout"


def receipt_pending(user_id: int, amount: int, description: str, spent: datetime | None,
                    items: list[dict], card_last4: str = "") -> dict:
    """Chekdan olingan ma'lumotdan kutilayotgan xarajat yasaydi."""
    current = now()
    if spent is None or spent.date() > current.date():
        spent = current
    extra = []
    if items:
        shown = items[:8]
        extra.append("\n".join(f"  · {html.escape(i['name'])} — {format_sum(i['amount'])}" for i in shown))
        if len(items) > len(shown):
            extra.append(f"  · ... yana {len(items) - len(shown)} ta")

    suggested = None
    if len(card_last4) == 4:
        cards = db.find_cards(user_id, card_last4)
        if len(cards) == 1:
            suggested = cards[0]["id"]
        elif cards:
            extra.append(f"💳 Chekdagi •••• {card_last4} bilan {len(cards)} ta kartangiz bor — tanlang.")
        else:
            extra.append(f"⚠️ Chekdagi karta •••• {card_last4} bazada yo'q.")

    description = description or "Chek bo'yicha xarid"
    info = enrich.enrich_local(description).to_dict()
    if info["category"] == "Boshqa" and items:
        info["category"] = enrich.items_category(items)
    return {
        "amount": amount,
        "description": description,
        "info": info,
        "spent_at": spent.strftime("%Y-%m-%d %H:%M"),
        "source": "receipt",
        "suggested_card_id": suggested,
        "extra": "\n".join(extra),
    }


async def on_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user_id = update.effective_user.id
    if message.photo:
        tg_file = await message.photo[-1].get_file()
        media_type = "image/jpeg"
    else:
        doc = message.document
        media_type = doc.mime_type or ""
        allowed = ["image/jpeg", "image/png", "image/webp"] + (["application/pdf"] if CLAUDE_ENABLED else [])
        if media_type not in allowed:
            await message.reply_text("Bu fayl turini o'qiy olmayman. Chekni rasm (JPG/PNG) qilib yuboring.")
            return
        tg_file = await doc.get_file()

    # Rasm izohi: "Qo'zi go'shti" — xarajat nomi, "Qo'zi go'shti 78 ming" — nomi va summasi
    caption = (message.caption or "").strip()
    caption_parsed = parse_expense(caption) if caption else None

    status = await message.reply_text("⏳ Chek o'qilmoqda...")
    data = bytes(await tg_file.download_as_bytearray())
    is_image = media_type != "application/pdf"

    qr_url = await asyncio.to_thread(soliq.extract_qr_url, data) if is_image else None
    receipt_date = soliq.date_from_qr(qr_url) if qr_url else None

    # 1) soliq.uz (kompyuterdagi yordamchi orqali)
    reason = "off"
    if qr_url:
        await status.edit_text("⏳ QR-kod topildi, soliq.uz dan ma'lumot olinmoqda...")
        r, reason = await lookup_soliq(qr_url)
        if r:
            amount = caption_parsed.amount if caption_parsed else r.total
            description = (caption_parsed.description if caption_parsed else caption) or r.store
            p = receipt_pending(user_id, amount, description, r.spent_at, r.items)
            p["raw_text"] = caption or None
            p["store"] = r.store or None
            p["receipt_items"] = r.items
            paid = "💳 Chekda: karta orqali to'langan" if r.card and not r.cash else (
                "💵 Chekda: naqd to'langan" if r.cash and not r.card else "")
            p["extra"] = "\n".join(filter(None, [
                f"🏪 {html.escape(r.store)}" if r.store else "", p.get("extra"), paid, "✅ soliq.uz dan olindi",
            ]))
            await ask_card(update, context, p, reply_to=status)
            return

    # 2) Tekin: chekdagi yozuvni o'qib, JAMI summasini topish (internetsiz)
    total, store = None, None
    if is_image and not caption_parsed:
        await status.edit_text("⏳ Chekdagi summa o'qilmoqda...")
        total, store = await asyncio.to_thread(ocr.read_receipt_total, data)
    amount = caption_parsed.amount if caption_parsed else total
    if amount:
        description = (caption_parsed.description if caption_parsed else caption) or store or "Chek bo'yicha xarid"
        p = receipt_pending(user_id, amount, description, receipt_date, [])
        p["raw_text"] = caption or None
        if not caption_parsed:
            note = "🔍 Summa chek rasmidan o'qildi. Noto'g'ri bo'lsa ❌ Bekor qiling va summani yozing."
            if qr_url and reason in ("offline", "timeout"):
                note += "\nℹ️ soliq.uz yordamchisi hozir ishlamayapti (kompyuter o'chiq), shuning uchun mahsulotlar ro'yxati yo'q."
            elif reason == "pending":
                note += "\nℹ️ Chek hali soliq bazasiga tushmagan (48 soatgacha vaqt oladi)."
            if not caption:
                note += "\n💡 Keyingi safar rasmga izoh yozing (masalan «Qo'zi go'shti») — nomi shunday saqlanadi."
            p["extra"] = "\n".join(filter(None, [p.get("extra"), note]))
        await ask_card(update, context, p, reply_to=status)
        return

    # 3) Pullik: Claude (faqat ANTHROPIC_API_KEY bo'lsa)
    if CLAUDE_ENABLED:
        try:
            r = await read_receipt(data, media_type)
        except ReceiptError as e:
            r = None
            log.warning("Claude chekni o'qiy olmadi: %s", e)
        except Exception:
            r = None
            log.exception("Chekni o'qishda xato")
        if r and r.is_receipt and r.total > 0:
            try:
                spent = datetime.combine(datetime.strptime(r.date, "%Y-%m-%d").date(), now().time())
            except ValueError:
                spent = receipt_date
            await ask_card(update, context, receipt_pending(
                user_id, r.total, caption or r.summary or r.store, spent, r.items, r.card_last4
            ), reply_to=status)
            return

    # 4) Summani topib bo'lmadi — foydalanuvchidan so'raymiz
    if qr_url:
        spent = receipt_date or now()
        context.user_data["receipt_spent_at"] = spent.strftime("%Y-%m-%d %H:%M")
        why = {
            "pending": " Chek hali soliq bazasiga tushmagan (bu 48 soatgacha vaqt oladi).",
            "error": " soliq.uz dan ma'lumot olib bo'lmadi.",
            "offline": " soliq.uz yordamchisi hozir ishlamayapti (kompyuter o'chiq).",
            "timeout": " soliq.uz javob bermadi.",
        }.get(reason, "")
        await status.edit_text(
            f"📷 Chek sanasi: <b>{spent:%d.%m.%Y}</b>, lekin summani o'qiy olmadim.{why}\n"
            "Summani yozing — shu sana bilan saqlanadi, masalan: <code>Korzinka 345000</code>"
        )
        return
    await status.edit_text(
        "🤔 Chekdan summani o'qiy olmadim.\n"
        "Chekni tekis, yorug' joyda, \"JAMI\" qatori aniq ko'rinadigan qilib rasmga oling — "
        "yoki xarajatni matn bilan yozing."
    )


# --- Tugmalar ---

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    action, _, rest = query.data.partition(":")

    if action == "card":
        pid, _, card = rest.partition(":")
        if pid not in context.user_data.get("pending", {}):
            await query.answer("Bu xarajat allaqachon saqlangan yoki eskirgan.")
            await query.edit_message_reply_markup(None)
            return
        card_id = None
        if card != "cash":
            # Tugmadagi karta shu foydalanuvchiniki ekanini tekshiramiz (boshqaning kartasiga yozib bo'lmasin)
            card_id = int(card) if card.isdigit() else -1
            if db.get_card(user_id, card_id) is None:
                await query.answer("Bu karta topilmadi.")
                return
        await query.answer()
        await finalize(context, user_id, query.message.chat_id, pid, card_id)

    elif action == "delcard":
        card = db.get_card(user_id, int(rest)) if rest.isdigit() else None
        if card and db.delete_card(user_id, card["id"]):
            await query.answer("O'chirildi")
            await query.edit_message_text(
                f"🗑 {html.escape(card['name'])} •••• {card['last4']} o'chirildi. "
                "Unga yozilgan xarajatlar saqlanib qoladi."
            )
        else:
            await query.answer("Karta topilmadi")

    elif action == "cancel":
        context.user_data.get("pending", {}).pop(rest, None)
        await query.answer("Bekor qilindi")
        await query.edit_message_text("❌ Bekor qilindi")

    elif action == "undo":
        if rest.isdigit() and db.delete_expense(user_id, int(rest)):
            await query.answer("Bekor qilindi")
            original = query.message.text_html if query.message and query.message.text else ""
            await query.edit_message_text("↩️ <b>Bekor qilindi, o'chirildi</b>\n\n<s>" + original.removeprefix("✅ Saqlandi\n") + "</s>")
        else:
            await query.answer("Allaqachon o'chirilgan")
            await query.edit_message_reply_markup(None)

    elif action == "del":
        if db.delete_expense(user_id, int(rest)):
            await query.answer("O'chirildi")
            await send_last(update, context, edit=True)
        else:
            await query.answer("Topilmadi")


# --- Hisobotlar ---

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(reports.today_report(update.effective_user.id, now()))


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(reports.week_report(update.effective_user.id, now()))


async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = reports.month_report(user_id, now())
    limit = db.get_monthly_limit(user_id)
    if limit:
        spent = db.month_total(user_id, now().strftime("%Y-%m"))
        left = limit - spent
        text += (f"\n\n🎯 Limit: {format_sum(limit)} — "
                 + (f"qoldi {format_sum(left)}" if left >= 0 else f"{format_sum(-left)} oshib ketdi"))
    await update.message.reply_text(text)


async def limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    arg = " ".join(context.args).strip()
    month = now().strftime("%Y-%m")
    if not arg:
        limit = db.get_monthly_limit(user_id)
        if not limit:
            await update.message.reply_text(
                "Oylik limit qo'yilmagan. Masalan: <code>/limit 3 mln</code> yoki <code>/limit 2500000</code>"
            )
            return
        spent = db.month_total(user_id, month)
        await update.message.reply_text(
            f"🎯 Oylik limit: {format_sum(limit)}\nShu oy: {format_sum(spent)} ({spent * 100 // limit}%)\n"
            "O'chirish uchun: <code>/limit 0</code>"
        )
        return
    if arg in ("0", "yo'q", "off"):
        db.set_monthly_limit(user_id, None)
        await update.message.reply_text("🎯 Oylik limit o'chirildi.")
        return
    parsed = parse_expense(arg)
    if not parsed or parsed.amount < 1000:
        await update.message.reply_text("Summani tushunmadim. Masalan: <code>/limit 3 mln</code>")
        return
    db.set_monthly_limit(user_id, parsed.amount)
    db.reset_limit_warnings(user_id, month)
    spent = db.month_total(user_id, month)
    await update.message.reply_text(
        f"🎯 Oylik limit: {format_sum(parsed.amount)}\nShu oy hozircha: {format_sum(spent)} "
        f"({spent * 100 // parsed.amount}%). 80% va 100% ga yetganda ogohlantiraman."
    )


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if len(query) < 2:
        await update.message.reply_text("Nimani qidiray? Masalan: <code>/qidir sumka</code>, <code>/qidir Korzinka</code>")
        return
    rows, count, total = db.search_expenses(update.effective_user.id, query)
    if not count:
        await update.message.reply_text(f"🔎 «{html.escape(query)}» bo'yicha xarajat topilmadi.")
        return
    lines = [f"🔎 <b>«{html.escape(query)}»</b>: {count} ta xarajat, jami <b>{format_sum(total)}</b>", ""]
    for r in rows:
        details = info_text(r)
        lines.append(
            f"• {r['spent_at'][8:10]}.{r['spent_at'][5:7]}.{r['spent_at'][2:4]} — {html.escape(r['description'])} — "
            f"{format_sum(r['amount'])}" + (f"\n   {details}" if details else "")
        )
    if count > len(rows):
        lines.append(f"\n... va yana {count - len(rows)} ta (to'liq ro'yxat: /export)")
    await update.message.reply_text("\n".join(lines))


async def send_last(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> None:
    rows = db.last_expenses(update.effective_user.id, 10)
    if not rows:
        text, markup = "Hali xarajat yozilmagan.", None
    else:
        lines = ["🕘 <b>Oxirgi xarajatlar</b> (o'chirish uchun tugmani bosing):", ""]
        buttons = []
        for i, r in enumerate(rows, 1):
            lines.append(
                f"{i}. {r['spent_at'][8:10]}.{r['spent_at'][5:7]} — {html.escape(r['description'])} — "
                f"{format_sum(r['amount'])} ({html.escape(reports.card_label(r))})"
            )
            buttons.append(InlineKeyboardButton(f"🗑 {i}", callback_data=f"del:{r['id']}"))
        text = "\n".join(lines)
        markup = InlineKeyboardMarkup([buttons[i : i + 5] for i in range(0, len(buttons), 5)])
    if edit:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_last(update, context)


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.all_expenses(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Hali xarajat yozilmagan.")
        return
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Sana", "Izoh", "Nomi", "Turi", "Brend", "Rang", "Korobka", "Kategoriya",
                     "Summa", "Karta", "Manba", "Asl matn"])
    for r in rows:
        box = {True: "bor", False: "yo'q"}.get(r.get("has_box"), "")
        writer.writerow([r["spent_at"], r["description"], r.get("item_name") or "", r.get("item_type") or "",
                         r.get("brand") or "", r.get("color") or "", box, r.get("category") or "",
                         r["amount"], reports.card_label(r),
                         "chek" if r["source"] == "receipt" else "matn", r.get("raw_text") or ""])
    # utf-8-sig — Excel o'zbek/kirill harflarini to'g'ri ochishi uchun
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    await update.message.reply_document(data, filename=f"xarajatlar_{now():%Y-%m-%d}.csv")


# --- Haftalik monitoring ---

async def send_weekly_reports(bot) -> int:
    """Hamma foydalanuvchilarga shu haftaning hisobotini yuboradi (har haftaga bir marta)."""
    current = now()
    monday = (current - timedelta(days=current.weekday())).strftime("%Y-%m-%d")
    user_ids = db.all_user_ids()
    if ALLOWED_USER_IDS:
        user_ids = [u for u in user_ids if u in ALLOWED_USER_IDS]
    sent = 0
    for user_id in user_ids:
        if not db.mark_report_sent(user_id, f"week:{monday}"):
            continue  # cron ikki marta chaqirilsa ham hisobot bir marta boradi
        try:
            await bot.send_message(user_id, reports.week_report(user_id, current))
            sent += 1
        except Forbidden:
            log.info("Foydalanuvchi %s botni bloklagan", user_id)
        except TelegramError:
            log.exception("Haftalik hisobotni %s ga yuborib bo'lmadi", user_id)
    return sent


async def send_monthly_reports(bot) -> int:
    """Har oyning 1-kuni o'tgan oy hisobotini yuboradi (har oyga bir marta)."""
    current = now()
    last_month = (current.date().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    user_ids = db.all_user_ids()
    if ALLOWED_USER_IDS:
        user_ids = [u for u in user_ids if u in ALLOWED_USER_IDS]
    sent = 0
    for user_id in user_ids:
        if not db.mark_report_sent(user_id, f"month:{last_month}"):
            continue
        try:
            await bot.send_message(user_id, reports.previous_month_report(user_id, current))
            sent += 1
        except Forbidden:
            log.info("Foydalanuvchi %s botni bloklagan", user_id)
        except TelegramError:
            log.exception("Oylik hisobotni %s ga yuborib bo'lmadi", user_id)
    return sent


async def run_daily_tasks(bot) -> dict:
    """Har kuni kechqurun chaqiriladi (Vercel Cron yoki lokal job)."""
    current = now()
    result = {"weekly_reports_sent": 0, "monthly_reports_sent": 0}
    if current.weekday() == 6:  # yakshanba
        result["weekly_reports_sent"] = await send_weekly_reports(bot)
    if current.day == 1:
        result["monthly_reports_sent"] = await send_monthly_reports(bot)
    return result


async def daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_daily_tasks(context.bot)


BOT_COMMANDS = [
    BotCommand("bugun", "Bugungi xarajatlar"),
    BotCommand("hafta", "Shu haftalik hisobot"),
    BotCommand("oy", "Shu oylik hisobot"),
    BotCommand("oxirgi", "Oxirgi 10 ta xarajat"),
    BotCommand("kartalar", "Kartalar ro'yxati"),
    BotCommand("karta", "Karta qo'shish"),
    BotCommand("qidir", "Xarajatlar ichidan qidirish"),
    BotCommand("limit", "Oylik limit"),
    BotCommand("export", "CSV (Excel) faylga yuklab olish"),
    BotCommand("ism", "Ism-familiyani o'zgartirish"),
    BotCommand("yordam", "Qo'llanma"),
]


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(BOT_COMMANDS)


async def post_shutdown(app: Application) -> None:
    await soliq.shutdown()


def build_application(**builder_options) -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN o'rnatilmagan")
    builder = Application.builder().token(token).defaults(Defaults(parse_mode=ParseMode.HTML, tzinfo=TZ))
    for name, value in builder_options.items():
        builder = getattr(builder, name)(value)
    app = builder.build()

    app.add_handler(TypeHandler(Update, access_guard), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["yordam", "help"], help_cmd))
    app.add_handler(CommandHandler("karta", card_cmd))
    app.add_handler(CommandHandler("kartalar", cards_list))
    app.add_handler(CommandHandler("karta_ochir", card_delete_cmd))
    app.add_handler(CommandHandler("ism", name_cmd))
    app.add_handler(CommandHandler("bekor", cancel_cmd))
    app.add_handler(CommandHandler("bugun", today_cmd))
    app.add_handler(CommandHandler("hafta", week_cmd))
    app.add_handler(CommandHandler("oy", month_cmd))
    app.add_handler(CommandHandler("oxirgi", last_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("qidir", search_cmd))
    app.add_handler(CommandHandler("limit", limit_cmd))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE | filters.Document.PDF, on_receipt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(TypeHandler(Update, save_user_state), group=1)
    app.add_error_handler(on_error)
    return app


def main() -> None:
    """Kompyuterda sinash uchun (polling). Serverda app.py (webhook) ishlatiladi."""
    db.init()
    app = build_application(post_init=post_init, post_shutdown=post_shutdown)
    if app.job_queue:
        app.job_queue.run_daily(daily_job, time=WEEKLY_REPORT_TIME, name="daily_tasks")
    log.info("Bot ishga tushdi (polling)")
    # Vercel'da webhook o'rnatilgan bo'lsa, polling uni o'chirib yuboradi
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
