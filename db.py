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
CREATE TABLE IF NOT EXISTS cards (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    name       TEXT   NOT NULL,
    last4      TEXT   NOT NULL,
    created_at TEXT   NOT NULL,
    UNIQUE (user_id, last4)
);
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


# --- Kartalar ---

def add_card(user_id: int, name: str, last4: str) -> bool:
    """Yangi karta qo'shadi. Shu oxirgi 4 raqamli karta bo'lsa nomini yangilaydi va False qaytaradi."""
    row = _execute(
        "INSERT INTO cards (user_id, name, last4, created_at) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (user_id, last4) DO UPDATE SET name = EXCLUDED.name"
        " RETURNING (xmax = 0) AS inserted",
        (user_id, name, last4, _now()),
    ).fetchone()
    return row["inserted"]


def list_cards(user_id: int) -> list[dict]:
    return _execute("SELECT * FROM cards WHERE user_id = %s ORDER BY id", (user_id,)).fetchall()


def find_card(user_id: int, last4: str) -> dict | None:
    return _execute(
        "SELECT * FROM cards WHERE user_id = %s AND last4 = %s", (user_id, last4)
    ).fetchone()


def delete_card(user_id: int, last4: str) -> bool:
    cur = _execute("DELETE FROM cards WHERE user_id = %s AND last4 = %s", (user_id, last4))
    return cur.rowcount > 0


# --- Xarajatlar ---

def add_expense(
    user_id: int, amount: int, description: str, card_id: int | None, spent_at: str, source: str
) -> int:
    row = _execute(
        "INSERT INTO expenses (user_id, amount, description, card_id, spent_at, source, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (user_id, amount, description, card_id, spent_at, source, _now()),
    ).fetchone()
    return row["id"]


def delete_expense(user_id: int, expense_id: int) -> bool:
    cur = _execute("DELETE FROM expenses WHERE id = %s AND user_id = %s", (expense_id, user_id))
    return cur.rowcount > 0


_EXPENSE_SELECT = (
    "SELECT e.*, c.name AS card_name, c.last4 AS card_last4"
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
