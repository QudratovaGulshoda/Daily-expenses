"""Chek rasmidagi yozuvni o'qish (tekin, internetsiz) va JAMI summasini topish.

RapidOCR (ONNX modellari paket ichida keladi) ishlatiladi. O'zbek cheklari asosan lotin harflarida,
summalar raqam — shu uchun lotin/raqam modeli yetarli.
"""

import io
import logging
import re

from PIL import Image, ImageOps

log = logging.getLogger("hisob-bot.ocr")
logging.getLogger("RapidOCR").setLevel(logging.WARNING)

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR  # og'ir kutubxona — faqat kerak bo'lganda yuklanadi

        _engine = RapidOCR()
    return _engine


def read_lines(image_bytes: bytes) -> list[str]:
    """Rasmdagi matnni qatorlarga ajratib qaytaradi (yuqoridan pastga, chapdan o'ngga)."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    if max(img.size) > 2000:
        scale = 2000 / max(img.size)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)

    result = _get_engine()(img)
    if result.txts is None:
        return []

    # Har bir matn bo'lagining markazi va balandligi; bir xil balandlikdagilar bitta qator
    pieces = []
    for box, text in zip(result.boxes, result.txts):
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        pieces.append(((min(ys) + max(ys)) / 2, max(ys) - min(ys), min(xs), text))
    pieces.sort()

    lines: list[list[tuple]] = []
    for piece in pieces:
        if lines and abs(piece[0] - lines[-1][0][0]) < max(piece[1], lines[-1][0][1]) * 0.6:
            lines[-1].append(piece)
        else:
            lines.append([piece])
    return [" ".join(p[3] for p in sorted(line, key=lambda p: p[2])) for line in lines]


# "JAMI" OCR'da ko'pincha "JAMl", "JAM1" bo'lib o'qiladi
_TOTAL_RE = re.compile(
    r"(?<![a-zа-я])(j\s*a\s*m\s*[i1l|]|итого|итог|total|umumiy\s+summa|to['ʻ’`]?lov\s+summasi|к\s+оплате)",
    re.IGNORECASE,
)
# QQS (soliq), chegirma va foizli qatorlar jami summa emas
_NOT_TOTAL_RE = re.compile(r"%|qqs|ндс|nds|vat|soliq|chegirma|скидк|aksiz", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"\d{1,3}(?:[ '’`,.]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?")


def parse_amount(text: str) -> int | None:
    """"78'485.00" -> 78485, "1 250 000,50" -> 1250000."""
    text = re.sub(r"[.,]\d{1,2}$", "", text.strip())
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def find_total(lines: list[str]) -> int | None:
    candidates = []
    for line in lines:
        # Qiyshiq rasmda ikki ustun bitta qatorga qo'shilishi mumkin:
        # "QQS JAMI 12.0%=8'409.09 JAMI =78'485.00" — har bir "JAMI" dan boshlangan bo'lakni alohida ko'ramiz
        matches = list(_TOTAL_RE.finditer(line))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
            # Kalit so'z oldidagi so'zlar ("QQS JAMI" dagi "QQS") — oldingi summadan keyingi qismgina
            prev_end = matches[i - 1].end() if i > 0 else 0
            before = re.search(r"[^\d%]*$", line[prev_end:m.start()]).group()
            segment = line[m.end():end]
            if _NOT_TOTAL_RE.search(before + m.group() + segment):
                continue
            found = _AMOUNT_RE.search(segment)  # kalit so'zdan keyingi birinchi summa
            amount = parse_amount(found.group()) if found else None
            if amount and 100 <= amount < 10_000_000_000:
                candidates.append(amount)
    # Bir nechta "JAMI" bo'lsa (masalan oraliq jami), eng kattasi — umumiy summa
    return max(candidates) if candidates else None


_STORE_RE = re.compile(r"\b(mchj|m\.ch\.j|ooo|ооо|xk|yatt|ятт|ип|mas['ʻ’`]?uliyati)\b", re.IGNORECASE)


def find_store(lines: list[str]) -> str | None:
    for line in lines[:8]:  # do'kon nomi chekning boshida bo'ladi
        if _STORE_RE.search(line):
            name = re.sub(r"\s+", " ", line).strip(" «»\"'.,")
            return name[:60] or None
    return None


def read_receipt_total(image_bytes: bytes) -> tuple[int | None, str | None]:
    """Qaytaradi: (JAMI summasi yoki None, do'kon nomi yoki None)."""
    try:
        lines = read_lines(image_bytes)
    except Exception:
        log.exception("OCR ishlamadi")
        return None, None
    log.info("OCR: %d qator, JAMI qatorlari: %s", len(lines), [l for l in lines if _TOTAL_RE.search(l)])
    return find_total(lines), find_store(lines)
