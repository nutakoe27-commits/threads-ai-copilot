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
    specialisation  TEXT,                   -- отрасль по классификатору trudvsem
    collected_at    TEXT NOT NULL,
    reported_at     TEXT,
    FOREIGN KEY (inn) REFERENCES companies(inn)
);

CREATE INDEX IF NOT EXISTS idx_signals_inn ON signals(inn);
CREATE INDEX IF NOT EXISTS idx_signals_reported ON signals(reported_at);

-- Счётчик запросов к платным API по дням. Нужен, чтобы сборка целевого
-- списка была возобновляемой: упёрлись в суточный лимит — завтра
-- продолжим с того же места.
CREATE TABLE IF NOT EXISTS api_usage (
    day             TEXT NOT NULL,
    provider        TEXT NOT NULL,
    requests        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider)
);

-- Курсор поиска: докуда дошли по каждой паре ОКВЭД+регион.
-- Кандидатов десятки тысяч, за раз их не перебрать, поэтому поиск
-- продолжается с того места, где остановился вчера.
CREATE TABLE IF NOT EXISTS search_cursor (
    okved           TEXT NOT NULL,
    region          TEXT NOT NULL,
    next_page       INTEGER NOT NULL DEFAULT 1,
    total_pages     INTEGER,
    exhausted       INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT,
    PRIMARY KEY (okved, region)
);

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


# Колонки, добавленные после первой версии схемы. SQLite не умеет
# "ADD COLUMN IF NOT EXISTS", поэтому проверяем наличие руками.
MIGRATIONS = [
    ("signals", "specialisation", "TEXT"),
    # Поля ICP, добавленные на Этапе 2 (проверка компании через Checko).
    ("companies", "okved", "TEXT"),
    ("companies", "okved_name", "TEXT"),
    ("companies", "staff", "REAL"),
    ("companies", "revenue", "REAL"),
    ("companies", "revenue_prev", "REAL"),
    ("companies", "revenue_change_pct", "REAL"),
    ("companies", "registration_date", "TEXT"),
    # candidate → passed | rejected. Пока NULL — компания не проверена.
    ("companies", "icp_status", "TEXT"),
    ("companies", "icp_reason", "TEXT"),
    ("companies", "checked_at", "TEXT"),
    # Этап 3: классификация по сайту. Реестр не отличает разработку на заказ
    # от продуктовой компании, а сайт отличает — см. DECISIONS.md, Р-019.
    ("companies", "site_url", "TEXT"),
    # outsourcing | staffing | product | integrator | not_it | unknown
    ("companies", "site_type", "TEXT"),
    ("companies", "site_confidence", "REAL"),
    ("companies", "site_summary", "TEXT"),
    ("companies", "site_specialization", "TEXT"),
    ("companies", "site_checked_at", "TEXT"),
    ("companies", "site_error", "TEXT"),
    # Этап 3б: сгенерированное письмо. Не отправляется — попадает
    # в утренний список, где вы его читаете и правите.
    ("companies", "letter_subject", "TEXT"),
    ("companies", "letter_body", "TEXT"),
    ("companies", "letter_why", "TEXT"),
    ("companies", "letter_facts", "TEXT"),
    ("companies", "letter_written_at", "TEXT"),
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Открывает базу, при необходимости создаёт схему и до-накатывает колонки."""
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)

    for table, column, column_type in MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            logger.info("Схема обновлена: %s.%s", table, column)
    conn.commit()
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
                                 specialisation, collected_at)
            VALUES (:vacancy_id, :inn, :company_name, :job_name, :region,
                    :url, :created_date, :strength, :matched, :excerpt,
                    :specialisation, :ts)
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


def requests_spent_today(conn: sqlite3.Connection, provider: str) -> int:
    """Сколько запросов к провайдеру уже потрачено сегодня."""
    day = datetime.now(timezone.utc).date().isoformat()
    row = conn.execute(
        "SELECT requests FROM api_usage WHERE day = ? AND provider = ?", (day, provider)
    ).fetchone()
    return int(row["requests"]) if row else 0


def record_requests(conn: sqlite3.Connection, provider: str, count: int) -> None:
    """Записывает израсходованные запросы. Вызывать в конце прогона."""
    if count <= 0:
        return
    day = datetime.now(timezone.utc).date().isoformat()
    conn.execute(
        """
        INSERT INTO api_usage (day, provider, requests) VALUES (?, ?, ?)
        ON CONFLICT(day, provider) DO UPDATE SET requests = requests + excluded.requests
        """,
        (day, provider, count),
    )


def add_candidate(conn: sqlite3.Connection, inn: str, name: str, region: str) -> bool:
    """Добавляет компанию-кандидата. False — если она уже известна."""
    ts = now()
    cur = conn.execute(
        """
        INSERT INTO companies (inn, name, region, first_seen, last_seen, status)
        VALUES (?, ?, ?, ?, ?, 'candidate')
        ON CONFLICT(inn) DO NOTHING
        """,
        (inn, name, region, ts, ts),
    )
    return cur.rowcount > 0


def get_cursor(conn: sqlite3.Connection, okved: str, region: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM search_cursor WHERE okved = ? AND region = ?", (okved, region)
    ).fetchone()


def save_cursor(conn: sqlite3.Connection, okved: str, region: str,
                next_page: int, total_pages: int | None, exhausted: bool) -> None:
    conn.execute(
        """
        INSERT INTO search_cursor (okved, region, next_page, total_pages, exhausted, updated_at)
        VALUES (:okved, :region, :next_page, :total_pages, :exhausted, :ts)
        ON CONFLICT(okved, region) DO UPDATE SET
            next_page = :next_page, total_pages = :total_pages,
            exhausted = :exhausted, updated_at = :ts
        """,
        {"okved": okved, "region": region, "next_page": next_page,
         "total_pages": total_pages, "exhausted": 1 if exhausted else 0, "ts": now()},
    )


def count_unchecked(conn: sqlite3.Connection) -> int:
    """Сколько кандидатов ждут проверки. Это буфер работы на будущие дни."""
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM companies WHERE checked_at IS NULL"
    ).fetchone()
    return int(row["count"])


def unchecked_candidates(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Кандидаты, по которым ещё не запрашивали карточку в Checko."""
    return conn.execute(
        """
        SELECT inn, name FROM companies
        WHERE checked_at IS NULL
        ORDER BY first_seen
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def save_icp_verdict(conn: sqlite3.Connection, inn: str, fields: dict[str, Any],
                     status: str, reason: str) -> None:
    """Сохраняет данные карточки и вердикт по ICP."""
    conn.execute(
        """
        UPDATE companies SET
            name = COALESCE(NULLIF(:name, ''), name),
            site = COALESCE(NULLIF(:site, ''), site),
            region = COALESCE(NULLIF(:region, ''), region),
            contact_email = COALESCE(NULLIF(:email, ''), contact_email),
            contact_phone = COALESCE(NULLIF(:phone, ''), contact_phone),
            okved = :okved, okved_name = :okved_name,
            staff = :staff, revenue = :revenue,
            revenue_prev = :revenue_prev, revenue_change_pct = :revenue_change_pct,
            registration_date = :registration_date,
            icp_status = :status, icp_reason = :reason, checked_at = :ts
        WHERE inn = :inn
        """,
        {
            "inn": inn,
            "name": fields.get("name") or "",
            "site": fields.get("site") or "",
            "region": fields.get("region") or "",
            "email": fields.get("email") or "",
            "phone": fields.get("phone") or "",
            "okved": fields.get("okved") or "",
            "okved_name": fields.get("okved_name") or "",
            "staff": fields.get("staff"),
            "revenue": fields.get("revenue"),
            "revenue_prev": fields.get("revenue_prev"),
            "revenue_change_pct": fields.get("revenue_change_pct"),
            "registration_date": fields.get("registration_date") or "",
            "status": status,
            "reason": reason,
            "ts": now(),
        },
    )


def icp_summary(conn: sqlite3.Connection) -> dict[str, int]:
    """Сводка по целевому списку: сколько кандидатов, прошло, отсеяно."""
    rows = conn.execute(
        """
        SELECT COALESCE(icp_status, 'не проверено') AS status, COUNT(*) AS count
        FROM companies GROUP BY COALESCE(icp_status, 'не проверено')
        """
    ).fetchall()
    return {row["status"]: int(row["count"]) for row in rows}


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
