"""Статусы касаний: что произошло после того, как письмо ушло.

Владелец отправляет письма руками из своего ящика и отмечает результат тоже
руками (DECISIONS.md, Р-040). Значит, отметка должна занимать секунды, иначе
её перестанут делать через две недели — так происходит с любой CRM.

Поэтому команда одна и принимает сразу несколько ИНН:

    python3 run.py --stage mark sent 7703283933 5018046069
    python3 run.py --stage mark replied 7722260272
    python3 run.py --stage mark refused 4025070110

ОСОБЫЙ СЛУЧАЙ — `refused`. Человек попросил ему не писать. Такой ИНН уходит
в стоп-лист навсегда, и это не вежливость, а требование: повторное письмо
после явного отказа — это уже не деловая переписка. Стоп-лист лежит файлом
data/stoplist.txt и переживает пересборку базы.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import db, log, metrics

logger = log.get("outreach")

# Статусы и что каждый означает для человека.
STATUSES: dict[str, str] = {
    "sent": "письмо отправлено",
    "replied": "ответили",
    "meeting": "договорились о разговоре",
    "refused": "попросили не писать — уходит в стоп-лист навсегда",
    "bounced": "адрес не существует",
}

# Статусы, после которых компания больше не показывается и не дожимается.
TERMINAL = {"refused", "bounced", "meeting"}


def add_to_stoplist(inn: str, name: str, reason: str) -> None:
    """Дописывает ИНН в вечный стоп-лист. Файл, а не таблица: он должен
    пережить пересборку базы и быть читаемым глазами."""
    path = db.DB_PATH.parent / "stoplist.txt"
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.split("#", 1)[0].strip()
            if value:
                existing.add(value)

    if inn in existing:
        return

    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{inn}  # {name} — {reason}, {db.now()[:10]}\n")
    logger.info("В стоп-лист добавлен ИНН %s (%s)", inn, name)


def mark(status: str, inns: list[str], dry_run: bool = False) -> dict[str, int]:
    """Проставляет статус нескольким компаниям сразу."""
    if status not in STATUSES:
        logger.error("Неизвестный статус «%s». Доступны: %s",
                     status, ", ".join(STATUSES))
        return {"marked": 0, "unknown": 0}

    conn = db.connect()
    counters = {"marked": 0, "unknown": 0}
    ts = db.now()

    for inn in inns:
        row = conn.execute(
            "SELECT inn, name, touch_count FROM companies WHERE inn = ?", (inn,)
        ).fetchone()
        if row is None:
            counters["unknown"] += 1
            logger.warning("ИНН %s в базе не найден — пропускаю", inn)
            continue

        counters["marked"] += 1
        logger.info("%s — %s", row["name"], STATUSES[status])

        if dry_run:
            continue

        conn.execute(
            "UPDATE companies SET outreach_status = ?, outreach_at = ? WHERE inn = ?",
            (status, ts, inn),
        )

        if status == "sent":
            # Отправка запускает часы дожима и закрывает черновик.
            conn.execute(
                "UPDATE companies SET touch_count = COALESCE(touch_count, 0) + 1, "
                "last_touch_at = ? WHERE inn = ?", (ts, inn))
            conn.execute(
                "UPDATE touches SET status = 'sent', sent_at = ? "
                "WHERE inn = ? AND status = 'draft'", (ts, inn))

        if status == "refused":
            add_to_stoplist(inn, row["name"], "попросили не писать")

    if not dry_run:
        metrics.record(conn, "outreach", {status: counters["marked"]})
        conn.commit()
    conn.close()

    if counters["marked"]:
        logger.info("Отмечено компаний: %d (%s)", counters["marked"], STATUSES[status])
    return counters


def pending_followups(conn: sqlite3.Connection, delay_days: int,
                      max_touches: int) -> list[sqlite3.Row]:
    """Кому пора написать второй или третий раз.

    Условия все сразу: письмо было отправлено, ответа нет, прошло достаточно
    дней, касаний ещё не максимум, компания не в терминальном статусе.
    """
    placeholders = ",".join("?" for _ in TERMINAL)
    return conn.execute(
        f"""
        SELECT * FROM companies
        WHERE outreach_status = 'sent'
          AND outreach_status NOT IN ({placeholders})
          AND COALESCE(touch_count, 0) < ?
          AND last_touch_at IS NOT NULL
          AND julianday('now') - julianday(last_touch_at) >= ?
        ORDER BY last_touch_at
        """,
        (*TERMINAL, max_touches, delay_days),
    ).fetchall()


def previous_touches(conn: sqlite3.Connection, inn: str) -> list[sqlite3.Row]:
    """Что этой компании уже писали. Нужно, чтобы дожим не повторял письмо."""
    return conn.execute(
        "SELECT step, subject, body, sent_at FROM touches "
        "WHERE inn = ? AND status = 'sent' ORDER BY step", (inn,)
    ).fetchall()


def save_touch(conn: sqlite3.Connection, inn: str, step: int,
               subject: str, body: str, why: str, facts: str) -> None:
    """Сохраняет черновик касания. Отправку отмечает человек командой mark."""
    conn.execute(
        """
        INSERT INTO touches (inn, step, subject, body, why, facts, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)
        """,
        (inn, step, subject, body, why, facts, db.now()),
    )


def summary(conn: sqlite3.Connection) -> dict[str, int]:
    """Сколько компаний в каком статусе. Для утреннего списка и отчёта."""
    rows = conn.execute(
        "SELECT COALESCE(outreach_status, 'не отправлено') AS status, "
        "COUNT(*) AS count FROM companies WHERE letter_body IS NOT NULL "
        "GROUP BY COALESCE(outreach_status, 'не отправлено')"
    ).fetchall()
    return {row["status"]: int(row["count"]) for row in rows}
