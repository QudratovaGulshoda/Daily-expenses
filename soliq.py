"""Fiskal chekdagi QR-kodni o'qish va chek ma'lumotini ofd.soliq.uz dan olish (tekin).

O'zbekiston cheklaridagi QR-kod shunday manzilni saqlaydi:
    https://ofd.soliq.uz/epul/?t=TERMINAL&r=CHEK_RAQAMI&c=YYYYMMDDHHMMSS&s=...

2026-yil iyunidan soliq.uz sahifasi React ilovaga aylangan: oddiy HTTP so'rov bo'sh sahifa
qaytaradi, ma'lumot esa sahifa ichidan new-ofd.soliq.uz/api/payment ga imzolangan so'rov bilan
olinadi. Shuning uchun sahifani yashirin Chromium (Playwright) da ochib, shu javobni ushlab olamiz.
"""

import asyncio
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import zxingcpp
from PIL import Image, ImageOps

log = logging.getLogger("hisob-bot.soliq")

_API_URL_FRAGMENT = "new-ofd.soliq.uz/api/payment"
_NAV_TIMEOUT_MS = 30_000
_API_WAIT_SECONDS = 15

_playwright = None
_browser = None
_lock = asyncio.Lock()


@dataclass
class SoliqReceipt:
    store: str
    total: int
    spent_at: datetime | None
    items: list[dict] = field(default_factory=list)


# --- QR-kod ---

def extract_qr_url(image_bytes: bytes) -> str | None:
    """Rasmdan soliq.uz QR-kodini topadi. Topilmasa None."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img).convert("L")
    except Exception:
        log.warning("Rasmni ochib bo'lmadi", exc_info=True)
        return None

    variants = [img, ImageOps.autocontrast(img)]
    w, h = img.size
    if max(w, h) > 2000:
        scale = 1600 / max(w, h)
        variants.append(img.resize((int(w * scale), int(h * scale)), Image.LANCZOS))
    elif max(w, h) < 900:
        variants.append(img.resize((w * 2, h * 2), Image.LANCZOS))

    for variant in variants:
        for barcode in zxingcpp.read_barcodes(variant, formats=zxingcpp.BarcodeFormat.QRCode):
            text = barcode.text.strip()
            if "soliq.uz" in text.lower():
                return text
    return None


def date_from_qr(qr_url: str) -> datetime | None:
    """QR manzilidagi c=YYYYMMDDHHMMSS parametridan sana."""
    c = (parse_qs(urlparse(qr_url).query).get("c") or [""])[0]
    for fmt, length in (("%Y%m%d%H%M%S", 14), ("%Y%m%d", 8)):
        try:
            return datetime.strptime(c[:length], fmt)
        except ValueError:
            continue
    return None


# --- soliq.uz ---

async def _get_browser():
    global _playwright, _browser
    if _browser is None or not _browser.is_connected():
        from playwright.async_api import async_playwright

        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
    return _browser


async def shutdown() -> None:
    global _playwright, _browser
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None


def _to_int(value) -> int:
    try:
        return int(round(float(str(value).replace(" ", "").replace(",", "."))))
    except (TypeError, ValueError):
        return 0


def _parse_payment(data: dict, qr_url: str) -> SoliqReceipt | None:
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(payload, dict):
        return None

    total = _to_int(payload.get("cardTotal")) + _to_int(payload.get("cashTotal"))
    store = str((payload.get("extraInfo") or {}).get("companyName") or "").strip()

    spent_at = None
    raw_date = str(payload.get("paymentDate") or "")  # "15.03.2026 13:04:55"
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            spent_at = datetime.strptime(raw_date, fmt)
            break
        except ValueError:
            continue
    spent_at = spent_at or date_from_qr(qr_url)

    items = []
    for it in payload.get("paymentDetails") or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or it.get("productName") or "").strip()
        qty = float(it.get("amount") or 0) or 1
        price = _to_int(it.get("price"))
        amount = int(qty * price) if price else _to_int(it.get("goodPrice"))
        if name:
            items.append({"name": name, "amount": amount})

    if total <= 0 and items:
        total = sum(i["amount"] for i in items)
    if total <= 0:
        return None
    return SoliqReceipt(store=store, total=total, spent_at=spent_at, items=items)


def _normalize_url(qr_url: str) -> str:
    """Eski /epul/ manzillarni soliq.uz ilovasi tushunadigan /check ga o'tkazadi."""
    parsed = urlparse(qr_url)
    if parsed.path.rstrip("/").endswith("/epul"):
        return f"https://ofd.soliq.uz/check?{parsed.query}"
    return qr_url


async def fetch_receipt(qr_url: str) -> tuple[SoliqReceipt | None, str]:
    """soliq.uz sahifasini ochib, chek ma'lumotini oladi.

    Qaytaradi: (chek yoki None, sabab). Sabab: "ok", "pending" (chek hali soliq bazasiga
    tushmagan — 48 soatgacha vaqt oladi) yoki "error".
    """
    qr_url = _normalize_url(qr_url)
    captured: dict = {}
    got_it = asyncio.Event()

    async def on_response(resp):
        if _API_URL_FRAGMENT not in resp.url:
            return
        try:
            body = await resp.json()
        except Exception:
            body = None
        captured["status"] = resp.status
        captured["value"] = body
        got_it.set()

    try:
        async with _lock:
            browser = await _get_browser()
            context = await browser.new_context(ignore_https_errors=True)
            try:
                page = await context.new_page()
                page.on("response", lambda r: asyncio.create_task(on_response(r)))
                # "networkidle" ishlatilmaydi: sahifadagi xarita doim so'rov yuborib turadi.
                await page.goto(qr_url, wait_until="load", timeout=_NAV_TIMEOUT_MS)
                try:
                    await asyncio.wait_for(got_it.wait(), timeout=_API_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    log.warning("soliq.uz API javobi kelmadi: %s", qr_url)
            finally:
                await context.close()
    except Exception:
        log.exception("soliq.uz sahifasini ochib bo'lmadi: %s", qr_url)

    body = captured.get("value")
    if captured.get("status") == 200 and isinstance(body, dict):
        receipt = _parse_payment(body, qr_url)
        if receipt:
            return receipt, "ok"
        log.warning("soliq.uz javobini tushunib bo'lmadi: %s", str(body)[:500])
    elif captured.get("status") == 404:
        return None, "pending"
    return None, "error"
