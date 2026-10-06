"""Matndan summa, izoh va karta raqamini ajratib olish."""

import re
from dataclasses import dataclass

MULTIPLIERS = {
    "mlrd": 1_000_000_000,
    "milliard": 1_000_000_000,
    "млрд": 1_000_000_000,
    "mln": 1_000_000,
    "million": 1_000_000,
    "millon": 1_000_000,
    "млн": 1_000_000,
    "ming": 1_000,
    "минг": 1_000,
    "тыс": 1_000,
    "k": 1_000,
    "к": 1_000,
}
_MULT_RE = "|".join(sorted(map(re.escape, MULTIPLIERS), key=len, reverse=True))
_AMOUNT_RE = re.compile(
    rf"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*({_MULT_RE})?(?!\w)", re.IGNORECASE
)
# "500 000", "500.000", "1,250,000" -> raqamlar orasidagi minglik ajratgichni olib tashlash
_THOUSANDS_SEP_RE = re.compile(r"(?<=\d)[ .,](?=\d{3}(?!\d))")
_CURRENCY_RE = re.compile(r"\b(so[ʻ'’`]?m|sum|сум|сўм|uzs)\b", re.IGNORECASE)
_YESTERDAY_RE = re.compile(r"\b(kecha|вчера|кеча)\b", re.IGNORECASE)
_TODAY_RE = re.compile(r"\b(bugun|сегодня|бугун)\b", re.IGNORECASE)


@dataclass
class ParsedExpense:
    amount: int
    description: str
    days_ago: int = 0


def parse_expense(text: str) -> ParsedExpense | None:
    """'Polene sumka 500000', 'Taksi 25 ming', '1 mln 200 ming telefon' kabi matnni o'qiydi."""
    normalized = _THOUSANDS_SEP_RE.sub("", text)
    matches = list(_AMOUNT_RE.finditer(normalized))
    if not matches:
        return None

    # Har bir moslikni (summa, boshlanish, tugash, ko'paytiruvchi bormi) ko'rinishiga keltiramiz,
    # ketma-ket kelgan "1 mln 200 ming" kabilarni bitta summaga birlashtiramiz.
    candidates: list[tuple[int, int, int, bool]] = []
    for m in matches:
        number = float(m.group(1).replace(",", "."))
        mult = m.group(2)
        value = int(round(number * MULTIPLIERS[mult.lower()])) if mult else int(number)
        prev = candidates[-1] if candidates else None
        if (
            prev
            and prev[3]
            and mult
            and normalized[prev[2] : m.start()].strip().lower() in ("", "u", "va")
        ):
            candidates[-1] = (prev[0] + value, prev[1], m.end(), True)
        else:
            candidates.append((value, m.start(), m.end(), bool(mult)))

    with_mult = [c for c in candidates if c[3]]
    amount, start, end, _ = max(with_mult or candidates, key=lambda c: c[0])
    if amount <= 0:
        return None

    days_ago = 1 if _YESTERDAY_RE.search(normalized) else 0
    description = normalized[:start] + " " + normalized[end:]
    description = _CURRENCY_RE.sub(" ", description)
    description = _YESTERDAY_RE.sub(" ", description)
    description = _TODAY_RE.sub(" ", description)
    description = re.sub(r"\s+", " ", description).strip(" -–—:;,.")
    return ParsedExpense(amount=amount, description=description or "Xarajat", days_ago=days_ago)


_CARD_NUMBER_RE = re.compile(r"\d[\d\s*-]{2,}\d")


def parse_card_line(line: str) -> tuple[str, str, bool] | None:
    """'Humo Kapitalbank 9860 1234 5678 9012' -> ('Humo Kapitalbank', '9012', to'liq_raqam_bormi)."""
    found = None
    for m in _CARD_NUMBER_RE.finditer(line):
        digits = re.sub(r"\D", "", m.group())
        if len(digits) >= 4:
            found = (m, digits)
    if not found:
        return None
    m, digits = found
    name = re.sub(r"\s+", " ", line[: m.start()] + " " + line[m.end() :]).strip(" -–—:;,.")
    return name or "Karta", digits[-4:], len(digits) >= 12


def contains_full_card_number(text: str) -> bool:
    return any(len(re.sub(r"\D", "", m.group())) >= 12 for m in _CARD_NUMBER_RE.finditer(text))


def format_sum(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " so'm"
