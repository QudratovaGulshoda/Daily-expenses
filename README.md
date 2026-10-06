# Xarajatlar hisob-kitobi — Telegram bot

Kundalik xarajatlarni yozib boradi, qaysi kartadan to'langanini so'raydi, chekni o'qiydi
va har yakshanba haftalik hisobot yuboradi.

## Qanday ishlaydi

- **Vercel** — bot kodi. Telegram har bir xabarni `https://<loyiha>.vercel.app/api/webhook` ga yuboradi.
- **Supabase** — Postgres baza: kartalar, xarajatlar, kutilayotgan xarajatlar.
- **Vercel Cron** — har kuni 21:00 (Toshkent) da `/api/cron/daily` ni chaqiradi: yakshanba kuni
  haftalik hisobot yuboradi, qolgan kunlari bazaga bitta so'rov yuborib Supabase'ni "uyg'oq" ushlab turadi.

## O'rnatish (Vercel + Supabase)

1. **Supabase:** yangi loyiha → **Connect** → **Transaction pooler** manzilini
   nusxalang (`...pooler.supabase.com:6543/postgres`), `[YOUR-PASSWORD]` o'rniga loyiha parolini yozing.
   Jadvallarni bot o'zi yaratadi.
2. **Vercel:** shu repo'ni import qiling va Settings → Environment Variables ga kiriting:
   `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`, `WEBHOOK_SECRET`, `CRON_SECRET`, `ALLOWED_USER_IDS`
   (ixtiyoriy: `ANTHROPIC_API_KEY`). `WEBHOOK_SECRET` va `CRON_SECRET` — istalgan uzun tasodifiy qator.
3. **Telegramni Vercel'ga ulash** (kompyuterda, bir marta; `.env` da yuqoridagi qiymatlar bo'lsin):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python setup_webhook.py https://<loyiha>.vercel.app
```

Kompyuterda sinash uchun `.venv/bin/python bot.py` ham ishlaydi (polling), lekin u Vercel'dagi
webhook'ni o'chirib qo'yadi — keyin `setup_webhook.py` ni qayta ishga tushiring.

## Foydalanish

| Nima qilasiz | Misol |
|---|---|
| Kartalarni qo'shish | `/karta`, keyin har qatorga bitta: `Humo Kapitalbank 9860 1234 5678 9012` |
| Xarajat yozish | `Polene sumka 500000`, `Polene sumka 500 ming`, `Taksi 25k`, `kecha telefon 1 mln 200 ming` |
| Kartani tanlash | Tugmani bosing yoki oxirgi 4 raqamni yozing (`9012`), naqd bo'lsa `naqd` |
| Chek | Chek rasmini yuboring — QR-koddan sana olinadi, summani o'zingiz yozasiz |
| Hisobotlar | `/bugun`, `/hafta`, `/oy` |
| Tahrirlash | `/oxirgi` — oxirgi 10 ta, 🗑 bilan o'chirish |
| Excel | `/export` — CSV fayl |

## Chek qanday o'qiladi

Standart holatda bot **hech qayerga murojaat qilmaydi**: chekdagi QR-koddan faqat sanani
o'qiydi (bot o'zi, tashqi xizmatlarsiz), summani esa siz yozasiz.

Ixtiyoriy sozlamalar (`.env` da):
- `SOLIQ_LOOKUP=true` — summa, do'kon va mahsulotlar ofd.soliq.uz ning ochiq chek tekshirish
  sahifasidan avtomatik olinadi (yashirin Chromium orqali, **faqat kompyuterda**, Vercel'da ishlamaydi:
  `pip install playwright && python -m playwright install chromium`). Yangi chek soliq bazasiga
  48 soatgacha kechikib tushishi mumkin.
- `ANTHROPIC_API_KEY=...` — QR-kodsiz cheklar (Payme/Click skrinshotlari, PDF) Claude orqali o'qiladi (pullik).

Fiskal chekda karta raqami bo'lmaydi, shuning uchun bot qaysi kartadan to'langanini baribir so'raydi.

## Xavfsizlik

Bazaga kartaning **faqat nomi va oxirgi 4 raqami** yoziladi. To'liq raqam yozilgan
xabarni bot chatdan o'chiradi. To'liq karta raqamini saqlash xavfli va hisob-kitob uchun keraksiz.

## Fayllar

- `app.py` — Vercel kirish nuqtasi: webhook va kunlik cron
- `bot.py` — Telegram handlerlar (kompyuterda `python bot.py` bilan polling rejimida ham ishlaydi)
- `setup_webhook.py` — Telegramga Vercel manzilini berish (bir marta)
- `vercel.json` — region (Supabase bilan bir joyda: Seul, `icn1`) va cron
- `parser.py` — matndan summa / izoh / karta raqamini ajratish
- `soliq.py` — QR-kodni o'qish; ixtiyoriy ravishda soliq.uz dan chek ma'lumotini olish
- `receipt.py` — QR'siz cheklarni Claude orqali o'qish (ixtiyoriy, pullik)
- `reports.py` — kunlik / haftalik / oylik hisobotlar
- `db.py` — Postgres (Supabase) baza
