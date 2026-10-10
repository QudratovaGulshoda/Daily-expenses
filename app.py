"""Vercel uchun kirish nuqtasi: Telegram webhook va kunlik cron."""

import asyncio
import hmac
import logging
import os

from fastapi import FastAPI, Request, Response
from telegram import Update

import bot
import db

log = logging.getLogger("hisob-bot.app")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_application = None
_application_loop = None


async def get_application():
    """Bot obyektini bir marta yaratib, keyingi so'rovlarda qayta ishlatadi."""
    global _application, _application_loop
    loop = asyncio.get_running_loop()
    if _application is None or _application_loop is not loop:
        application = bot.build_application()
        await application.initialize()
        _application, _application_loop = application, loop
    return _application


def _secret_ok(given: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


@app.get("/")
async def health():
    return {"ok": True}


@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    # Telegram setWebhook(secret_token=...) dagi qiymatni har bir so'rovda shu sarlavhada yuboradi
    if not _secret_ok(request.headers.get("x-telegram-bot-api-secret-token", ""),
                      os.getenv("WEBHOOK_SECRET", "")):
        return Response(status_code=401)
    data = await request.json()
    try:
        application = await get_application()
        await application.process_update(Update.de_json(data, application.bot))
    except Exception:
        # Telegram xato javobda xabarni qayta-qayta yuboraveradi, shuning uchun baribir 200 qaytaramiz
        log.exception("Xabarni qayta ishlashda xato")
    return {"ok": True}


@app.get("/api/cron/daily")
async def daily_cron(request: Request):
    # Vercel CRON_SECRET ni "Authorization: Bearer ..." sarlavhasida yuboradi
    if not _secret_ok(request.headers.get("authorization", ""),
                      "Bearer " + os.getenv("CRON_SECRET", "") if os.getenv("CRON_SECRET") else ""):
        return Response(status_code=401)
    db.ping()  # Supabase tekin loyihasi 7 kun so'rovsiz qolsa uxlab qoladi
    application = await get_application()
    result = await bot.run_daily_tasks(application.bot)  # yakshanba — haftalik, 1-kun — oylik hisobot
    return {"ok": True, **result}


@app.get("/api/selftest")
async def selftest(request: Request):
    """Serverda OCR va baza ishlayotganini tekshirish (CRON_SECRET bilan)."""
    if not _secret_ok(request.headers.get("authorization", ""),
                      "Bearer " + os.getenv("CRON_SECRET", "") if os.getenv("CRON_SECRET") else ""):
        return Response(status_code=401)
    import io
    import time

    from PIL import Image, ImageDraw, ImageFont

    import ocr

    img = Image.new("RGB", (700, 160), "white")
    draw = ImageDraw.Draw(img)
    draw.text((30, 50), "JAMI =78'485.00", fill="black", font=ImageFont.load_default(size=48))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    started = time.time()
    total, _ = await asyncio.to_thread(ocr.read_receipt_total, buf.getvalue())
    db.ping()
    return {"ok": total == 78485, "ocr_total": total, "ocr_seconds": round(time.time() - started, 2)}
