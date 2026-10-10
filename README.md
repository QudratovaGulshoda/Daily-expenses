# Xarajatlar hisob-kitobi — Telegram bot

Kundalik xarajatlarni yozib boradi (har bir foydalanuvchining ma'lumoti alohida), qaysi kartadan to'langanini so'raydi, chekni o'qiydi
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
   `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`, `WEBHOOK_SECRET`, `CRON_SECRET`
   (ixtiyoriy: `ANTHROPIC_API_KEY`, `ALLOWED_USER_IDS` — botni faqat ma'lum odamlarga cheklash uchun). `WEBHOOK_SECRET` va `CRON_SECRET` — istalgan uzun tasodifiy qator.
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
| Ism-familiya | Birinchi `/start` da so'raladi, `/ism` bilan o'zgartiriladi |
| Kartalarni qo'shish | `/karta`, keyin har qatorga bitta: `Humo Kapitalbank 9860 1234 5678 9012` (oxirgi 4 raqami bir xil kartalar nomi bilan ajratiladi) |
| Xarajat yozish | `Polene sumka 500000`, `Polene sumka 500 ming`, `Taksi 25k`, `kecha telefon 1 mln 200 ming` |
| Kartani tanlash | Tugmani bosing yoki oxirgi 4 raqamni yozing (`9012`); shunday kartalar bir nechta bo'lsa bot qaysi biri ekanini so'raydi. Naqd bo'lsa `naqd` |
| Chek | Chek rasmini yuboring — soliq.uz dan summa, do'kon va mahsulotlar olinadi (bo'lmasa JAMI rasmdan o'qiladi); rasmga izoh yozsangiz, u xarajat nomi bo'ladi |
| Hisobotlar | `/bugun`, `/hafta`, `/oy` |
| Tahrirlash | Saqlangandan keyin "↩️ Bekor qilish" tugmasi; `/oxirgi` — oxirgi 10 ta, 🗑 bilan o'chirish |
| Qidiruv | `/qidir sumka`, `/qidir Korzinka`, `/qidir qora` — topilganlar va jami summa |
| Oylik limit | `/limit 3 mln` — 80% va 100% ga yetganda ogohlantiradi; `/limit 0` — o'chirish |
| Excel | `/export` — CSV fayl |

## Xarajat matni qanday tahlil qilinadi

`qora polen sumka korobkasi bn 500 ming` →

| Ustun | Qiymat |
|---|---|
| description (tuzatilgan izoh) | qora Polene sumka korobkasi bilan |
| item_name (nomi) | Polene sumka |
| item_type (nimaligi) | sumka |
| brand | Polene |
| color | qora |
| has_box (korobka) | true |
| category | Aksessuarlar |
| raw_text (asl matn) | qora polen sumka korobkasi bn 500 ming |

Standart holatda bu `enrich.py` dagi lug'atlar bo'yicha bajariladi (tekin, internetsiz): ma'lum
buyumlar, brendlar va ranglarga o'xshash xato yozilgan so'zlar tuzatiladi. Lug'atda yo'q brend yoki
buyum bo'lsa, ustun bo'sh qoladi — yangi so'zlarni `ITEMS`, `BRANDS`, `COLORS` ga qo'shish mumkin.
`ANTHROPIC_API_KEY` berilsa, tahlil Claude orqali qilinadi (aniqroq, pullik).

## Avtomatik hisobotlar

Har kuni 21:00–22:00 (Toshkent) da Vercel Cron `/api/cron/daily` ni chaqiradi:
yakshanba — haftalik hisobot, oyning 1-kuni — o'tgan oy hisoboti (kategoriyalar bilan).

## Chek qanday o'qiladi

1. **QR-kod** (`soliq.py`) o'qiladi.
2. **soliq.uz** — summa, do'kon, mahsulotlar va to'lov turi olinadi. soliq.uz **faqat O'zbekistondan
   ochiladi** (Vercel'ning hamma regionlari bloklangan), shuning uchun buni O'zbekistondagi kompyuterda
   ishlaydigan `soliq_worker.py` bajaradi: bot vazifani Supabase'dagi `soliq_jobs` ga yozadi, yordamchi
   uni darhol oladi (LISTEN/NOTIFY), sahifani yashirin Chromium'da ochib, natijani qaytaradi (~3–5 soniya).
3. Yordamchi ishlamayotgan bo'lsa (kompyuter o'chiq/uxlab yotibdi) yoki chek hali soliq bazasiga
   tushmagan bo'lsa — **OCR** (`ocr.py`): JAMI summasi chek rasmidan o'qiladi (tekin, internetsiz).
4. Rasmga izoh yozilsa (`Uyga bozorlik`), xarajat shu nom bilan saqlanadi; izohda summa bo'lsa, o'sha olinadi.
5. Hech narsa topilmasa, bot sanani eslab qolib, summani yozishni so'raydi.

`ANTHROPIC_API_KEY=...` berilsa, OCR ham o'qiy olmagan cheklar Claude orqali o'qiladi (pullik).

### Yordamchini kompyuterda ishga tushirish (macOS)

```bash
.venv/bin/pip install playwright && .venv/bin/python -m playwright install chromium
# kompyuter yoqilganda o'zi ishga tushishi uchun:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/uz.hisobkitob.soliq-worker.plist
# to'xtatish:
launchctl bootout gui/$(id -u)/uz.hisobkitob.soliq-worker
```

Log: `soliq_worker.log`. Plist namunasi `deploy/uz.hisobkitob.soliq-worker.plist` da.
Kompyuter uxlab qolsa yordamchi ham to'xtaydi — bot bu vaqtda OCR bilan ishlaydi.

Serverda OCR ishlayotganini tekshirish: `curl -H "Authorization: Bearer $CRON_SECRET" https://<loyiha>.vercel.app/api/selftest`

## Supabase'da ko'rish

- `users` — Telegram ID, ism-familiya, Telegram username
- `cards` — kartalar (nomi, oxirgi 4 raqami, egasining ism-familiyasi)
- `xarajatlar` (view) — hamma xarajatlar ism-familiya, karta, nomi, turi, brendi, rangi, korobkasi, kategoriyasi, do'koni va chekdagi mahsulotlari bilan

## Xavfsizlik

Bazaga kartaning **faqat nomi va oxirgi 4 raqami** yoziladi. To'liq raqam yozilgan
xabarni bot chatdan o'chiradi. To'liq karta raqamini saqlash xavfli va hisob-kitob uchun keraksiz.

## Fayllar

- `app.py` — Vercel kirish nuqtasi: webhook va kunlik cron
- `bot.py` — Telegram handlerlar (kompyuterda `python bot.py` bilan polling rejimida ham ishlaydi)
- `setup_webhook.py` — Telegramga Vercel manzilini berish (bir marta)
- `vercel.json` — region (Supabase bilan bir joyda: Seul, `icn1`) va cron
- `parser.py` — matndan summa / izoh / karta raqamini ajratish
- `enrich.py` — imlo tuzatish; nomi, turi, brendi, rangi, korobkasi, kategoriyasini ajratish
- `soliq.py` — QR-kodni o'qish; ixtiyoriy ravishda soliq.uz dan chek ma'lumotini olish
- `ocr.py` — chekdagi yozuvni o'qib, JAMI summasini topish (tekin, internetsiz)
- `soliq_worker.py` — O'zbekistondagi kompyuterda ishlaydigan soliq.uz yordamchisi
- `receipt.py` — QR'siz cheklarni Claude orqali o'qish (ixtiyoriy, pullik)
- `reports.py` — kunlik / haftalik / oylik hisobotlar
- `db.py` — Postgres (Supabase) baza
