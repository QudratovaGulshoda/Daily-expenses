"""Xarajatlar hisob-kitobi uchun Telegram bot."""

import asyncio
import csv
import html
import io
import logging
import os
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
# Chek summasini soliq.uz dan olish. O'chiq bo'lsa (standart) bot hech qayerga murojaat qilmaydi:
# QR-koddan faqat sanani oladi, summani foydalanuvchi o'zi yozadi.
SOLIQ_LOOKUP = os.getenv("SOLIQ_LOOKUP", "false").strip().lower() in ("1", "true", "yes", "ha")
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
Chekning rasmini yuboring — bot QR-koddan chek sanasini oladi, siz summani yozasiz.
QR-kod aniq ko'rinsin (yaqinroqdan, tekis qilib rasmga oling).

<b>Buyruqlar</b>
/bugun — bugungi xarajatlar
/hafta — shu hafta
/oy — shu oy
/oxirgi — oxirgi 10 ta (o'chirish mumkin)
/kartalar — kartalar ro'yxati
/karta — karta qo'shish
/karta_ochir 1234 — kartani o'chirish
/export — hamma xarajatlar CSV (Excel) faylda

📬 Har yakshanba kechqurun haftalik hisobot o'zi keladi."""


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
    await update.message.reply_text(
        HELP_TEXT
    )
    if not db.list_cards(update.effective_user.id):
        await update.message.reply_text("Boshlash uchun /karta ni bosing va kartalaringizni yozing 👇")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


# --- Kartalar ---

async def card_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.partition(" ")[2].strip()
    if text:
        await save_cards(update, context, text)
        return
    context.user_data["state"] = "add_card"
    await update.message.reply_text(
        "Kartalaringizni yozing — har birini alohida qatorda, nomi va raqami bilan:\n\n"
        "<code>Humo Kapitalbank 9860 1234 5678 9012\n"
        "Uzcard Asaka 8600 1111 2222 3333</code>\n\n"
        "To'liq raqam o'rniga faqat oxirgi 4 raqamni yozsangiz ham bo'ladi: <code>Humo 9012</code>"
    )


async def save_cards(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    user_id = update.effective_user.id
    context.user_data.pop("state", None)
    saved, bad = [], []
    for line in filter(None, (l.strip() for l in text.splitlines())):
        parsed = parse_card_line(line)
        if not parsed:
            bad.append(line)
            continue
        name, last4, _ = parsed
        is_new = db.add_card(user_id, name, last4)
        saved.append(f"{'➕' if is_new else '✏️'} {html.escape(name)} •••• {last4}")

    if contains_full_card_number(text):
        try:
            await update.message.delete()
        except TelegramError:
            log.warning("To'liq karta raqamli xabarni o'chirib bo'lmadi")

    lines = []
    if saved:
        lines.append("✅ Saqlandi:\n" + "\n".join(saved))
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
    lines = ["💳 <b>Kartalaringiz</b> (shu oydagi xarajat):", ""]
    for c in cards:
        lines.append(f"• {html.escape(c['name'])} •••• {c['last4']} — {format_sum(spent.get(c['id'], 0))}")
    if spent.get(None):
        lines.append(f"• 💵 Naqd — {format_sum(spent[None])}")
    await update.message.reply_text("\n".join(lines))


async def card_delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit() or len(context.args[0]) != 4:
        await update.message.reply_text("Oxirgi 4 raqamni yozing: <code>/karta_ochir 1234</code>")
        return
    if db.delete_card(update.effective_user.id, context.args[0]):
        await update.message.reply_text(
            f"🗑 •••• {context.args[0]} karta o'chirildi. Unga yozilgan xarajatlar saqlanib qoladi."
        )
    else:
        await update.message.reply_text("Bunday karta topilmadi.")


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


def pending_text(p: dict) -> str:
    text = (
        f"🧾 <b>{html.escape(p['description'])}</b> — {format_sum(p['amount'])}\n"
        f"📅 {p['spent_at'][8:10]}.{p['spent_at'][5:7]}.{p['spent_at'][:4]}"
    )
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
    db.add_expense(user_id, p["amount"], p["description"], card_id, p["spent_at"], p["source"])
    if context.user_data.get("last_pending") == pid:
        context.user_data.pop("last_pending", None)

    card_text = "💵 Naqd"
    if card_id is not None:
        card = next((c for c in db.list_cards(user_id) if c["id"] == card_id), None)
        if card is not None:
            card_text = f"💳 {html.escape(card['name'])} •{card['last4']}"

    day = p["spent_at"][:10]
    day_total = sum(r["amount"] for r in db.expenses_between(user_id, day, day + "~"))
    text = f"✅ Saqlandi\n{pending_text(p)}\n{card_text}\n\nShu kun jami: {format_sum(day_total)}"
    try:
        await context.bot.edit_message_text(text, chat_id=chat_id, message_id=p["message_id"])
    except TelegramError:
        await context.bot.send_message(chat_id, text)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if context.user_data.get("state") == "add_card":
        await save_cards(update, context, text)
        return

    # Kutilayotgan xarajatga karta tanlash: "1234" yoki "naqd"
    last_pid = context.user_data.get("last_pending")
    if last_pid and last_pid in context.user_data.get("pending", {}):
        if text.isdigit() and len(text) == 4:
            card = db.find_card(user_id, text)
            if card is None:
                await update.message.reply_text(
                    f"•••• {text} karta topilmadi. Tugmalardan birini tanlang yoki /karta orqali qo'shing."
                )
                return
            await finalize(context, user_id, update.effective_chat.id, last_pid, card["id"])
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
    await ask_card(update, context, {
        "amount": parsed.amount,
        "description": parsed.description,
        "spent_at": receipt_spent_at or spent.strftime("%Y-%m-%d %H:%M"),
        "source": "receipt" if receipt_spent_at else "text",
    })


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
        card = db.find_card(user_id, card_last4)
        if card:
            suggested = card["id"]
        else:
            extra.append(f"⚠️ Chekdagi karta •••• {card_last4} bazada yo'q.")

    return {
        "amount": amount,
        "description": description or "Chek bo'yicha xarid",
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

    status = await message.reply_text("⏳ Chek o'qilmoqda...")
    data = bytes(await tg_file.download_as_bytearray())

    # 1) Tekin yo'l: fiskal chekdagi QR-kod (soliq.uz ga faqat SOLIQ_LOOKUP yoqilgan bo'lsa murojaat qilinadi)
    if media_type != "application/pdf":
        qr_url = await asyncio.to_thread(soliq.extract_qr_url, data)
        if qr_url:
            reason = "off"
            if SOLIQ_LOOKUP:
                await status.edit_text("⏳ QR-kod topildi, soliq.uz dan ma'lumot olinmoqda...")
                r, reason = await soliq.fetch_receipt(qr_url)
                if r:
                    await ask_card(update, context, receipt_pending(
                        user_id, r.total, r.store, r.spent_at, r.items
                    ), reply_to=status)
                    return
            if not CLAUDE_ENABLED:
                spent = soliq.date_from_qr(qr_url) or now()
                context.user_data["receipt_spent_at"] = spent.strftime("%Y-%m-%d %H:%M")
                why = {
                    "off": "",
                    "pending": ", lekin chek hali soliq bazasiga tushmagan (bu 48 soatgacha vaqt oladi)",
                }.get(reason, ", lekin soliq.uz dan ma'lumot olib bo'lmadi")
                await status.edit_text(
                    f"📷 Chek sanasi: <b>{spent:%d.%m.%Y}</b>{why}.\n"
                    "Summani yozing — shu sana bilan saqlanadi, masalan: <code>Korzinka 345000</code>"
                )
                return

    # 2) Pullik yo'l: Claude (faqat ANTHROPIC_API_KEY bo'lsa)
    if not CLAUDE_ENABLED:
        await status.edit_text(
            "🤔 Chekda QR-kod topilmadi.\n"
            "QR-kodni yaqinroqdan, tekis va yorug' joyda rasmga oling yoki xarajatni matn bilan yozing."
        )
        return
    try:
        r = await read_receipt(data, media_type)
    except ReceiptError as e:
        await status.edit_text(f"⚠️ {e}")
        return
    except Exception:
        log.exception("Chekni o'qishda xato")
        await status.edit_text("⚠️ Chekni o'qishda xato yuz berdi. Xarajatni matn bilan yozing.")
        return
    if not r.is_receipt or r.total <= 0:
        await status.edit_text(
            "🤔 Bu rasmda chek yoki to'lov summasini topa olmadim. Xarajatni matn bilan yozing."
        )
        return
    try:
        spent = datetime.combine(datetime.strptime(r.date, "%Y-%m-%d").date(), now().time())
    except ValueError:
        spent = None
    await ask_card(update, context, receipt_pending(
        user_id, r.total, r.summary or r.store, spent, r.items, r.card_last4
    ), reply_to=status)


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
            if card_id not in {c["id"] for c in db.list_cards(user_id)}:
                await query.answer("Bu karta topilmadi.")
                return
        await query.answer()
        await finalize(context, user_id, query.message.chat_id, pid, card_id)

    elif action == "cancel":
        context.user_data.get("pending", {}).pop(rest, None)
        await query.answer("Bekor qilindi")
        await query.edit_message_text("❌ Bekor qilindi")

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
    await update.message.reply_text(reports.month_report(update.effective_user.id, now()))


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
    writer.writerow(["Sana", "Izoh", "Summa", "Karta", "Manba"])
    for r in rows:
        writer.writerow([r["spent_at"], r["description"], r["amount"], reports.card_label(r),
                         "chek" if r["source"] == "receipt" else "matn"])
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


async def weekly_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_weekly_reports(context.bot)


BOT_COMMANDS = [
    BotCommand("bugun", "Bugungi xarajatlar"),
    BotCommand("hafta", "Shu haftalik hisobot"),
    BotCommand("oy", "Shu oylik hisobot"),
    BotCommand("oxirgi", "Oxirgi 10 ta xarajat"),
    BotCommand("kartalar", "Kartalar ro'yxati"),
    BotCommand("karta", "Karta qo'shish"),
    BotCommand("export", "CSV (Excel) faylga yuklab olish"),
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
    app.add_handler(CommandHandler("bekor", cancel_cmd))
    app.add_handler(CommandHandler("bugun", today_cmd))
    app.add_handler(CommandHandler("hafta", week_cmd))
    app.add_handler(CommandHandler("oy", month_cmd))
    app.add_handler(CommandHandler("oxirgi", last_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
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
        # PTB'da kunlar: 0 = yakshanba, 1 = dushanba, ... 6 = shanba
        app.job_queue.run_daily(weekly_job, time=WEEKLY_REPORT_TIME, days=(0,), name="weekly_report")
    log.info("Bot ishga tushdi (polling)")
    # Vercel'da webhook o'rnatilgan bo'lsa, polling uni o'chirib yuboradi
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
