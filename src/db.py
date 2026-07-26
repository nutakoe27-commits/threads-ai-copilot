"""Хранилище: SQLite, один файл data/leads.db.

Что важно помнить про схему:

* Персональные данные здесь не хранятся. Только реквизиты юрлица и текст
  сигнала. Имя и должность конкретного человека вы подставляете руками при
  отправке — см. DECISIONS.md, запись Р-000.
* Дедупликация двухуровневая: по vacancy_id (одна вакансия не попадёт
  дважды никогда) и по компании (компания не всплывает каждое утро —
  см. company_cooldown_days в signals.yaml).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import log

logger = log.get("db")

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "leads.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    inn             TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    region          TEXT,
    site            TEXT,
    -- Обезличенный корпоративный адрес (info@/sales@), НЕ личный email.
    contact_email   TEXT,
    contact_phone   TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    last_reported   TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vacancy_id      TEXT NOT NULL UNIQUE,
    inn             TEXT NOT NULL,
    company_name    TEXT NOT NULL,
    job_name        TEXT,
    region          TEXT,
    url             TEXT,
    created_date    TEXT,
    strength        TEXT NOT NULL,          -- strong | normal
    matched         TEXT,                   -- совпавшие ключевые слова через ;
    excerpt         TEXT,                   -- фрагмент текста вакансии
    collected_at    TEXT NOT NULL,
    reported_at     TEXT,
    FOREIGN KEY (inn) REFERENCES companies(inn)
);

CREATE INDEX IF NOT EXISTS idx_signals_inn ON signals(inn);
CREATE INDEX IF NOT EXISTS idx_signals_reported ON signals(reported_at);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stage           TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    fetched         INTEGER DEFAULT 0,      -- сколько вакансий получено от API
    matched         INTEGER DEFAULT 0,      -- сколько прошло по ключевым словам
    new_signals     INTEGER DEFAULT 0,      -- сколько записано (без дублей)
    problems        INTEGER DEFAULT 0
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Открывает базу, при необходимости создаёт схему."""
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def load_stoplist() -> set[str]:
    """ИНН и домены, которым больше не пишем. По одному на строку, # — комментарий.

    Стоп-лист вечный: отказался — больше никогда. См. DECISIONS.md, Р-007.
    """
    path = DB_PATH.parent / "stoplist.txt"
    if not path.exists():
        return set()

    entries: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if value:
            entries.add(value.lower())
    logger.info("Стоп-лист: %d записей", len(entries))
    return entries


def upsert_company(conn: sqlite3.Connection, company: dict[str, Any]) -> None:
    """Добавляет компанию или обновляет last_seen у уже известной."""
    conn.execute(
        """
        INSERT INTO companies (inn, name, region, site, contact_email,
                               contact_phone, first_seen, last_seen)
        VALUES (:inn, :name, :region, :site, :email, :phone, :ts, :ts)
        ON CONFLICT(inn) DO UPDATE SET
            last_seen = :ts,
            -- не затираем уже известные значения пустыми
            name    = COALESCE(NULLIF(excluded.name, ''), companies.name),
            site    = COALESCE(NULLIF(excluded.site, ''), companies.site),
            region  = COALESCE(NULLIF(excluded.region, ''), companies.region)
        """,
        {
            "inn": company["inn"],
            "name": company.get("name") or "",
            "region": company.get("region") or "",
            "site": company.get("site") or "",
            "email": company.get("email") or "",
            "phone": company.get("phone") or "",
            "ts": now(),
        },
    )


def insert_signal(conn: sqlite3.Connection, signal: dict[str, Any]) -> bool:
    """Записывает сигнал. Возвращает False, если такая вакансия уже была."""
    try:
        conn.execute(
            """
            INSERT INTO signals (vacancy_id, inn, company_name, job_name, region,
                                 url, created_date, strength, matched, excerpt,
                                 collected_at)
            VALUES (:vacancy_id, :inn, :company_name, :job_name, :region,
                    :url, :created_date, :strength, :matched, :excerpt, :ts)
            """,
            {**signal, "ts": now()},
        )
        return True
    except sqlite3.IntegrityError:
        # UNIQUE по vacancy_id — вакансия уже собрана в прошлый прогон.
        return False


def companies_on_cooldown(conn: sqlite3.Connection, days: int) -> set[str]:
    """ИНН компаний, которые попадали в отчёт за последние `days` дней."""
    if days <= 0:
        return set()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT inn FROM companies WHERE last_reported IS NOT NULL AND last_reported >= ?",
        (since,),
    ).fetchall()
    return {row["inn"] for row in rows}


def unreported_signals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Сигналы, ещё не попавшие ни в один утренний список."""
    return conn.execute(
        """
        SELECT s.*, c.site AS company_site, c.contact_email AS company_email
        FROM signals s
        LEFT JOIN companies c ON c.inn = s.inn
        WHERE s.reported_at IS NULL
        ORDER BY s.strength DESC, s.collected_at DESC
        """
    ).fetchall()


def mark_reported(conn: sqlite3.Connection, signal_ids: Iterable[int], inns: Iterable[str]) -> None:
    """Помечает сигналы и компании как показанные в утреннем списке."""
    ts = now()
    ids = [(ts, sid) for sid in signal_ids]
    if ids:
        conn.executemany("UPDATE signals SET reported_at = ? WHERE id = ?", ids)
    inn_rows = [(ts, inn) for inn in set(inns)]
    if inn_rows:
        conn.executemany("UPDATE companies SET last_reported = ? WHERE inn = ?", inn_rows)


def start_run(conn: sqlite3.Connection, stage: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (stage, started_at) VALUES (?, ?)", (stage, now())
    )
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, **counters: int) -> None:
    conn.execute(
        """
        UPDATE runs
        SET finished_at = ?, fetched = ?, matched = ?, new_signals = ?, problems = ?
        WHERE id = ?
        """,
        (
            now(),
            counters.get("fetched", 0),
            counters.get("matched", 0),
            counters.get("new_signals", 0),
            counters.get("problems", 0),
            run_id,
        ),
    )
