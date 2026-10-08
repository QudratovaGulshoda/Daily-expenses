"""Postgres (Supabase) baza: kartalar, xarajatlar va bot holati."""

import json
import logging
import os
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("hisob-bot.db")

# spent_at matn ko'rinishida ('YYYY-MM-DD HH:MM') saqlanadi va COLLATE "C" bilan solishtiriladi,
# aks holda til qoidalariga ko'ra saralash sana oralig'ini buzadi.
SCHEMA = """
-- Foydalanuvchilar: Telegram ID va ism-familiya
CREATE TABLE IF NOT EXISTS users (
    user_id     BIGINT PRIMARY KEY,
    full_name   TEXT,               -- foydalanuvchi o'zi yozgan ism-familiya
    tg_name     TEXT,               -- Telegram profilidagi ism
    tg_username TEXT,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    name        TEXT   NOT NULL,
    last4       TEXT   NOT NULL,
    holder_name TEXT,               -- karta egasining ism-familiyasi
    created_at  TEXT   NOT NULL
);
-- Oldingi versiyada (user_id, last4) takrorlanmas edi; turli kartalarning oxirgi 4 raqami bir xil bo'lishi mumkin
ALTER TABLE cards DROP CONSTRAINT IF EXISTS cards_user_id_last4_key;
ALTER TABLE cards ADD COLUMN IF NOT EXISTS holder_name TEXT;
-- O'chirilgan karta yashiriladi, lekin eski xarajatlarda nomi ko'rinib turadi
ALTER TABLE cards ADD COLUMN IF NOT EXISTS deleted_at TEXT;
CREATE INDEX IF NOT EXISTS idx_cards_user_last4 ON cards (user_id, last4);
CREATE TABLE IF NOT EXISTS expenses (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    amount      BIGINT NOT NULL,
    description TEXT   NOT NULL,
    card_id     BIGINT REFERENCES cards(id) ON DELETE SET NULL,  -- NULL = naqd pul
    spent_at    TEXT COLLATE "C" NOT NULL,                       -- 'YYYY-MM-DD HH:MM'
    source      TEXT   NOT NULL DEFAULT 'text',                  -- 'text' yoki 'receipt'
    created_at  TEXT   NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses (user_id, spent_at);
-- Xarajat matnidan ajratilgan ma'lumotlar (enrich.py)
ALTER TABLE expenses ADD COLUMN IF NOT EXISTS raw_text  TEXT;     -- foydalanuvchi yozgan asl matn
ALTER TABLE expenses ADD COLUMN IF NOT EXISTS item_name TEXT;     -- nomi, masalan "Polene sumka"
ALTER TABLE expenses ADD COLUMN IF NOT EXISTS item_type TEXT;     -- nimaligi, masalan "sumka"
ALTER TABLE expenses ADD COLUMN IF NOT EXISTS brand     TEXT;
ALTER TABLE expenses ADD COLUMN IF NOT EXISTS color     TEXT;
ALTER TABLE expenses ADD COLUMN IF NOT EXISTS has_box   BOOLEAN;  -- korobka bilan / korobkasiz / NULL
ALTER TABLE expenses ADD COLUMN IF NOT EXISTS category  TEXT;
ALTER TABLE users    ADD COLUMN IF NOT EXISTS monthly_limit BIGINT;
-- Tugma bosilishini kutayotgan xarajatlar va suhbat holati (serverless'da xotira saqlanmaydi)
CREATE TABLE IF NOT EXISTS user_state (
    user_id BIGINT PRIMARY KEY,
    data    JSONB  NOT NULL DEFAULT '{}'
);
-- Haftalik hisobot ikki marta yuborilmasligi uchun
CREATE TABLE IF NOT EXISTS sent_reports (
    user_id BIGINT NOT NULL,
    period  TEXT   NOT NULL,
    PRIMARY KEY (user_id, period)
);
-- Supabase jadvallarni ochiq REST API orqali ham ko'rsatadi. RLS yoqilib, qoida qo'shilmasa,
-- API orqali kirish yopiladi; bot esa jadval egasi sifatida to'g'ridan-to'g'ri ulanadi.
ALTER TABLE users        ENABLE ROW LEVEL SECURITY;
ALTER TABLE cards        ENABLE ROW LEVEL SECURITY;
ALTER TABLE expenses     ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_state   ENABLE ROW LEVEL SECURITY;
ALTER TABLE sent_reports ENABLE ROW LEVEL SECURITY;
-- Supabase Table Editor'da qulay ko'rish uchun: xarajatlar ism-familiya va karta bilan
CREATE OR REPLACE VIEW xarajatlar WITH (security_invoker = true) AS
SELECT e.id, e.user_id, u.full_name, e.spent_at, e.description, e.amount,
       c.name AS karta, c.last4, c.holder_name AS karta_egasi, e.source,
       e.item_name AS nomi, e.item_type AS turi, e.brand AS brend, e.color AS rang,
       CASE e.has_box WHEN true THEN 'bor' WHEN false THEN 'yo''q' END AS korobka,
       e.category AS kategoriya, e.raw_text AS asl_matn
FROM expenses e
LEFT JOIN users u ON u.user_id = e.user_id
LEFT JOIN cards c ON c.id = e.card_id;
"""

_conn: psycopg.Connection | None = None
_initialized = False


def _connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL o'rnatilmagan")
    # prepare_threshold=None — Supabase pooler (pgbouncer, 6543-port) bilan ishlashi uchun
    return psycopg.connect(
        url, autocommit=True, row_factory=dict_row, prepare_threshold=None, connect_timeout=10
    )


def _execute(sql: str, params: tuple = ()) -> psycopg.Cursor:
    """Bitta ulanishni qayta ishlatadi; ulanish uzilgan bo'lsa bir marta qayta ulanadi."""
    global _conn, _initialized
    for attempt in (1, 2):
        try:
            if _conn is None or _conn.closed:
                _conn = _connect()
            if not _initialized:
                _conn.execute(SCHEMA)
                _initialized = True
            return _conn.execute(sql, params)
        except psycopg.OperationalError:
            if attempt == 2:
                raise
            log.warning("Baza ulanishi uzildi, qayta ulanilmoqda")
            _conn = None


def init() -> None:
    _execute("SELECT 1")


def ping() -> None:
    """Supabase tekin loyihasi uxlab qolmasligi uchun kunlik so'rov."""
    _execute("SELECT 1")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- Foydalanuvchilar ---

def touch_user(user_id: int, tg_name: str, tg_username: str | None) -> None:
    """Har bir xabarda Telegram profilini yangilaydi (birinchi marta bo'lsa yaratadi)."""
    now = _now()
    _execute(
        "INSERT INTO users (user_id, tg_name, tg_username, created_at, last_seen)"
        " VALUES (%s, %s, %s, %s, %s)"
        " ON CONFLICT (user_id) DO UPDATE SET tg_name = EXCLUDED.tg_name,"
        " tg_username = EXCLUDED.tg_username, last_seen = EXCLUDED.last_seen",
        (user_id, tg_name, tg_username, now, now),
    )


def get_full_name(user_id: int) -> str | None:
    row = _execute("SELECT full_name FROM users WHERE user_id = %s", (user_id,)).fetchone()
    return row["full_name"] if row else None


def set_full_name(user_id: int, full_name: str) -> None:
    """Ism-familiyani saqlaydi va egasi yozilmagan kartalarga ham qo'yadi."""
    now = _now()
    _execute(
        "INSERT INTO users (user_id, full_name, created_at, last_seen) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (user_id) DO UPDATE SET full_name = EXCLUDED.full_name",
        (user_id, full_name, now, now),
    )
    _execute(
        "UPDATE cards SET holder_name = %s WHERE user_id = %s AND holder_name IS NULL",
        (full_name, user_id),
    )


def reset_limit_warnings(user_id: int, month: str) -> None:
    """Limit o'zgartirilganda shu oy uchun ogohlantirishlar qaytadan yuborilishi uchun."""
    _execute(
        "DELETE FROM sent_reports WHERE user_id = %s AND period IN (%s, %s)",
        (user_id, f"limit80:{month}", f"limit100:{month}"),
    )


def get_monthly_limit(user_id: int) -> int | None:
    row = _execute("SELECT monthly_limit FROM users WHERE user_id = %s", (user_id,)).fetchone()
    return row["monthly_limit"] if row else None


def set_monthly_limit(user_id: int, amount: int | None) -> None:
    now = _now()
    _execute(
        "INSERT INTO users (user_id, monthly_limit, created_at, last_seen) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (user_id) DO UPDATE SET monthly_limit = EXCLUDED.monthly_limit",
        (user_id, amount, now, now),
    )


# --- Kartalar ---

def add_card(user_id: int, name: str, last4: str, holder_name: str | None) -> bool:
    """Karta qo'shadi. Xuddi shu nom va oxirgi 4 raqamli karta bo'lsa, yangisini qo'shmaydi (False).

    Oxirgi 4 raqami bir xil, lekin nomi boshqa kartalar alohida saqlanadi.
    """
    existing = _execute(
        "SELECT id, deleted_at FROM cards WHERE user_id = %s AND last4 = %s AND lower(name) = lower(%s)",
        (user_id, last4, name),
    ).fetchone()
    if existing:
        _execute(
            "UPDATE cards SET holder_name = COALESCE(%s, holder_name), deleted_at = NULL WHERE id = %s",
            (holder_name, existing["id"]),
        )
        return existing["deleted_at"] is not None  # o'chirilgan karta qaytarilsa — yangi deb hisoblanadi
    _execute(
        "INSERT INTO cards (user_id, name, last4, holder_name, created_at) VALUES (%s, %s, %s, %s, %s)",
        (user_id, name, last4, holder_name, _now()),
    )
    return True


def list_cards(user_id: int) -> list[dict]:
    return _execute(
        "SELECT * FROM cards WHERE user_id = %s AND deleted_at IS NULL ORDER BY id", (user_id,)
    ).fetchall()


def find_cards(user_id: int, last4: str) -> list[dict]:
    """Oxirgi 4 raqami mos keladigan hamma kartalar (bir nechta bo'lishi mumkin)."""
    return _execute(
        "SELECT * FROM cards WHERE user_id = %s AND last4 = %s AND deleted_at IS NULL ORDER BY id",
        (user_id, last4),
    ).fetchall()


def get_card(user_id: int, card_id: int) -> dict | None:
    return _execute(
        "SELECT * FROM cards WHERE user_id = %s AND id = %s AND deleted_at IS NULL", (user_id, card_id)
    ).fetchone()


def delete_card(user_id: int, card_id: int) -> bool:
    cur = _execute(
        "UPDATE cards SET deleted_at = %s WHERE user_id = %s AND id = %s AND deleted_at IS NULL",
        (_now(), user_id, card_id),
    )
    return cur.rowcount > 0


# --- Xarajatlar ---

def add_expense(
    user_id: int, amount: int, description: str, card_id: int | None, spent_at: str, source: str,
    info: dict | None = None, raw_text: str | None = None,
) -> int:
    """info — enrich.ItemInfo.to_dict(): item_name, item_type, brand, color, has_box, category."""
    info = info or {}
    row = _execute(
        "INSERT INTO expenses (user_id, amount, description, card_id, spent_at, source, created_at,"
        " raw_text, item_name, item_type, brand, color, has_box, category)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (user_id, amount, description, card_id, spent_at, source, _now(), raw_text,
         info.get("item_name"), info.get("item_type"), info.get("brand"), info.get("color"),
         info.get("has_box"), info.get("category")),
    ).fetchone()
    return row["id"]


def search_expenses(user_id: int, query: str, limit: int = 15) -> tuple[list[dict], int, int]:
    """Izoh, nomi, turi, brendi, rangi yoki kategoriyasida so'z bor xarajatlar.

    Qaytaradi: (oxirgi `limit` ta xarajat, jami soni, jami summasi).
    """
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    where = (
        " WHERE e.user_id = %s AND (e.description ILIKE %s OR e.item_name ILIKE %s OR e.item_type ILIKE %s"
        " OR e.brand ILIKE %s OR e.color ILIKE %s OR e.category ILIKE %s OR e.raw_text ILIKE %s)"
    )
    params = (user_id,) + (pattern,) * 7
    totals = _execute(
        "SELECT count(*) AS n, coalesce(sum(e.amount), 0) AS total FROM expenses e" + where, params
    ).fetchone()
    rows = _execute(
        _EXPENSE_SELECT + where + " ORDER BY e.spent_at DESC, e.id DESC LIMIT %s", params + (limit,)
    ).fetchall()
    return rows, totals["n"], int(totals["total"])


def month_total(user_id: int, month: str) -> int:
    """month — 'YYYY-MM'."""
    row = _execute(
        "SELECT coalesce(sum(amount), 0) AS total FROM expenses"
        " WHERE user_id = %s AND spent_at >= %s AND spent_at < %s",
        (user_id, month, month + "~"),
    ).fetchone()
    return int(row["total"])


def delete_expense(user_id: int, expense_id: int) -> bool:
    cur = _execute("DELETE FROM expenses WHERE id = %s AND user_id = %s", (expense_id, user_id))
    return cur.rowcount > 0


_EXPENSE_SELECT = (
    "SELECT e.*, c.name AS card_name, c.last4 AS card_last4, c.deleted_at AS card_deleted_at"
    " FROM expenses e LEFT JOIN cards c ON c.id = e.card_id"
)


def expenses_between(user_id: int, start: str, end: str) -> list[dict]:
    """[start, end) oralig'idagi xarajatlar, karta ma'lumoti bilan."""
    return _execute(
        _EXPENSE_SELECT + " WHERE e.user_id = %s AND e.spent_at >= %s AND e.spent_at < %s"
        " ORDER BY e.spent_at",
        (user_id, start, end),
    ).fetchall()


def last_expenses(user_id: int, limit: int = 10) -> list[dict]:
    return _execute(
        _EXPENSE_SELECT + " WHERE e.user_id = %s ORDER BY e.spent_at DESC, e.id DESC LIMIT %s",
        (user_id, limit),
    ).fetchall()


def all_expenses(user_id: int) -> list[dict]:
    return _execute(
        _EXPENSE_SELECT + " WHERE e.user_id = %s ORDER BY e.spent_at", (user_id,)
    ).fetchall()


def all_user_ids() -> list[int]:
    rows = _execute("SELECT user_id FROM cards UNION SELECT user_id FROM expenses").fetchall()
    return [r["user_id"] for r in rows]


# --- Bot holati ---

def load_state(user_id: int) -> dict:
    row = _execute("SELECT data FROM user_state WHERE user_id = %s", (user_id,)).fetchone()
    return row["data"] if row else {}


def save_state(user_id: int, data: dict) -> None:
    _execute(
        "INSERT INTO user_state (user_id, data) VALUES (%s, %s)"
        " ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data",
        (user_id, json.dumps(data)),
    )


def mark_report_sent(user_id: int, period: str) -> bool:
    """Hisobot shu davr uchun hali yuborilmagan bo'lsa belgilaydi va True qaytaradi."""
    row = _execute(
        "INSERT INTO sent_reports (user_id, period) VALUES (%s, %s)"
        " ON CONFLICT DO NOTHING RETURNING user_id",
        (user_id, period),
    ).fetchone()
    return row is not None
