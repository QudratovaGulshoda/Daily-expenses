"""Chek rasmini (yoki PDF) Claude yordamida o'qish."""

import base64
import json
from dataclasses import dataclass

import anthropic

MODEL = "claude-opus-5-5"

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


PROMPT = """Bu O'zbekistondagi xarid cheki (to'lov kvitansiyasi, POS-terminal cheki, Payme/Click skrinshoti va h.k.) bo'lishi mumkin.
Undan quyidagilarni ajratib ol:
- is_receipt: rasm haqiqatan chek/to'lov tasdig'imi
- store: do'kon yoki xizmat nomi (bo'lmasa bo'sh qator)
- total: jami to'langan summa, butun so'mda (JAMI / ИТОГО / Umumiy summa / Итого к оплате qatori). Tiyinlarni tashlab yubor.
- date: xarid sanasi YYYY-MM-DD formatida (topilmasa bo'sh qator)
- card_last4: to'lov qilingan kartaning oxirgi 4 raqami, masalan "8600 **** **** 1234" yoki "HUMO *1234" dan "1234" (topilmasa bo'sh qator)
- items: asosiy mahsulotlar ro'yxati (nomi va summasi), ko'pi bilan 15 ta
- summary: xarajatni 2-5 so'zda tasvirlovchi qisqa izoh o'zbek tilida, masalan "Korzinka - oziq-ovqat"
Faqat chekda aniq ko'rinib turgan ma'lumotni yoz, taxmin qilma."""

SCHEMA = {
    "type": "object",
    "properties": {
        "is_receipt": {"type": "boolean"},
        "store": {"type": "string"},
        "total": {"type": "integer"},
        "date": {"type": "string"},
        "card_last4": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "amount": {"type": "integer"},
                },
                "required": ["name", "amount"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["is_receipt", "store", "total", "date", "card_last4", "items", "summary"],
    "additionalProperties": False,
}


@dataclass
class ReceiptData:
    is_receipt: bool
    store: str
    total: int
    date: str
    card_last4: str
    items: list[dict]
    summary: str


class ReceiptError(Exception):
    pass


async def read_receipt(data: bytes, media_type: str) -> ReceiptData:
    encoded = base64.standard_b64encode(data).decode("utf-8")
    if media_type == "application/pdf":
        file_block = {
            "type": "document",
            "source": {"type": "base64", "media_type": media_type, "data": encoded},
        }
    else:
        file_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": encoded},
        }

    try:
        response = await _get_client().beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": SCHEMA},
            },
            messages=[{"role": "user", "content": [file_block, {"type": "text", "text": PROMPT}]}],
        )
    except anthropic.AuthenticationError as e:
        raise ReceiptError("ANTHROPIC_API_KEY noto'g'ri yoki o'rnatilmagan.") from e
    except anthropic.RateLimitError as e:
        raise ReceiptError("Claude API limiti tugadi, birozdan keyin qayta urinib ko'ring.") from e
    except anthropic.APIStatusError as e:
        raise ReceiptError(f"Claude API xatosi ({e.status_code}).") from e
    except anthropic.APIConnectionError as e:
        raise ReceiptError("Claude API ga ulanib bo'lmadi.") from e

    if response.stop_reason == "refusal":
        raise ReceiptError("Chekni o'qib bo'lmadi.")
    if response.stop_reason == "max_tokens":
        raise ReceiptError("Chek juda uzun, o'qib bo'lmadi.")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise ReceiptError("Chekdan javob olinmadi.")
    parsed = json.loads(text)
    return ReceiptData(**parsed)
