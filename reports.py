"""Kunlik / haftalik / oylik hisobot matnlari."""

import html
from collections import defaultdict
from datetime import date, datetime, timedelta

import db
from parser import format_sum

WEEKDAYS = ["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"]


def card_label(row) -> str:
    if row["card_id"] is None:
        return "Naqd"
    label = f"{row['card_name']} •{row['card_last4']}"
    return label + " (o'chirilgan)" if row.get("card_deleted_at") else label


def _range_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def build_report(
    user_id: int, start: date, end: date, title: str, today: date, prev_start: date, prev_label: str
) -> str:
    """[start, end] (ikkalasi ham kiradi) oralig'i uchun hisobot.

    Davr hali tugamagan bo'lsa, faqat bugungacha bo'lgan kunlar hisobga olinadi va
    oldingi davrning xuddi shuncha kuni bilan solishtiriladi (prev_start dan boshlab).
    """
    end = min(end, today)
    rows = db.expenses_between(user_id, _range_str(start), _range_str(end + timedelta(days=1)))
    period = f"{start:%d.%m} – {end:%d.%m.%Y}" if start != end else f"{start:%d.%m.%Y}"
    lines = [f"📊 <b>{title}</b> ({period})", ""]

    if not rows:
        lines.append("Bu davrda xarajat yozilmagan.")
        return "\n".join(lines)

    total = sum(r["amount"] for r in rows)
    lines.append(f"💰 Jami: <b>{format_sum(total)}</b> ({len(rows)} ta xarajat)")

    days = (end - start).days + 1
    prev_end = prev_start + timedelta(days=days - 1)
    prev_rows = db.expenses_between(
        user_id, _range_str(prev_start), _range_str(prev_end + timedelta(days=1))
    )
    prev_total = sum(r["amount"] for r in prev_rows)
    if prev_total:
        diff = (total - prev_total) / prev_total * 100
        arrow = "🔺" if diff > 0 else "🔻"
        lines.append(f"{arrow} {prev_label}ga nisbatan: {diff:+.0f}% ({format_sum(prev_total)})")

    by_card: dict[str, int] = defaultdict(int)
    for r in rows:
        by_card[card_label(r)] += r["amount"]
    lines += ["", "💳 <b>Kartalar bo'yicha:</b>"]
    for label, amount in sorted(by_card.items(), key=lambda x: -x[1]):
        lines.append(f"  • {html.escape(label)}: {format_sum(amount)} ({amount / total * 100:.0f}%)")

    if days > 1:
        by_day: dict[str, int] = defaultdict(int)
        for r in rows:
            by_day[r["spent_at"][:10]] += r["amount"]
        lines += ["", "📅 <b>Kunlar bo'yicha:</b>"]
        d = start
        while d <= end:
            amount = by_day.get(_range_str(d), 0)
            if amount or days <= 7:
                lines.append(f"  {WEEKDAYS[d.weekday()]} {d:%d.%m}: {format_sum(amount)}")
            d += timedelta(days=1)
        lines.append(f"  O'rtacha kuniga: {format_sum(total // days)}")

    top = sorted(rows, key=lambda r: -r["amount"])[:5]
    lines += ["", "🔝 <b>Eng katta xarajatlar:</b>"]
    for r in top:
        lines.append(
            f"  • {html.escape(r['description'])} — {format_sum(r['amount'])} "
            f"({r['spent_at'][8:10]}.{r['spent_at'][5:7]}, {html.escape(card_label(r))})"
        )
    return "\n".join(lines)


def today_report(user_id: int, now: datetime) -> str:
    today = now.date()
    return build_report(
        user_id, today, today, "Bugungi xarajatlar", today, today - timedelta(days=1), "Kecha"
    )


def week_report(user_id: int, now: datetime) -> str:
    today = now.date()
    monday = today - timedelta(days=today.weekday())
    return build_report(
        user_id, monday, monday + timedelta(days=6), "Haftalik hisobot", today,
        monday - timedelta(days=7), "O'tgan hafta",
    )


def month_report(user_id: int, now: datetime) -> str:
    today = now.date()
    first = today.replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    prev_first = (first - timedelta(days=1)).replace(day=1)
    return build_report(
        user_id, first, next_month - timedelta(days=1), "Oylik hisobot", today,
        prev_first, "O'tgan oy",
    )
