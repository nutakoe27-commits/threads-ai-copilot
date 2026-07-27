"""Учёт воронки по дням и самодиагностика.

ЗАЧЕМ ЭТО НУЖНО. Источник данных редко ломается заметно. Он деградирует:
продолжает отдавать данные, просто данные становятся хуже. Мы это уже
проходили — trudvsem честно отдавал 14 131 вакансию, среди которых не было
ни одной компании нашего профиля. Формально работал.

Без истории воронки такое видно только глазами и только если посмотреть.
С историей видно само: конверсия упала вдвое — приходит замечание.

КАК УСТРОЕНО. Таблица `funnel` — это ключ-значение по дням: день, этап,
название метрики, число. Новая метрика не требует миграции схемы.

Числа складываются в течение дня. Два прогона `enrich` за сутки дадут одну
строку с суммой, а не две записи.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import db, log

logger = log.get("metrics")

# Пары «сколько было → сколько осталось». По ним считается конверсия этапа.
# Порядок важен: он же порядок вывода в отчёте.
FUNNEL_STEPS: list[tuple[str, str, str, str]] = [
    ("targets", "checked", "passed", "проверено в реестре → прошло ICP"),
    ("dossier", "classified", "with_facts", "классифицировано сайтов → с фактами"),
    ("compose", "attempted", "written", "взято в письмо → письмо годное"),
    ("morning", "listed", "sent", "показано в списке → отправлено"),
    ("outreach", "sent", "replied", "отправлено → ответили"),
]

# Во сколько раз конверсия должна упасть, чтобы система пожаловалась.
DROP_FACTOR = 2.0

# Сколько дней берём для сравнения: последние N против предыдущих N.
WINDOW_DAYS = 7


def record(conn: sqlite3.Connection, stage: str, counters: dict[str, Any]) -> None:
    """Записывает числа этапа за сегодня. Складывает с уже записанными."""
    today = date.today().isoformat()
    for metric, value in counters.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        conn.execute(
            """
            INSERT INTO funnel (day, stage, metric, value) VALUES (?, ?, ?, ?)
            ON CONFLICT(day, stage, metric) DO UPDATE SET value = value + excluded.value
            """,
            (today, stage, metric, float(value)),
        )


def totals(conn: sqlite3.Connection, stage: str, metric: str,
           since: str, until: str | None = None) -> float:
    """Сумма метрики за период. `since` и `until` — даты в формате ГГГГ-ММ-ДД."""
    query = "SELECT COALESCE(SUM(value), 0) AS total FROM funnel " \
            "WHERE stage = ? AND metric = ? AND day >= ?"
    params: list[Any] = [stage, metric, since]
    if until:
        query += " AND day < ?"
        params.append(until)
    row = conn.execute(query, params).fetchone()
    return float(row["total"])


def _days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def check_drops(conn: sqlite3.Connection) -> list[str]:
    """Ищет этапы, где конверсия упала заметно. Возвращает список замечаний.

    Сравнивает последние WINDOW_DAYS дней с предыдущими WINDOW_DAYS.
    Молчит, если данных мало: на трёх компаниях конверсия скачет сама
    по себе, и жаловаться на это — значит приучить человека не читать
    предупреждения.
    """
    recent_from, older_from = _days_ago(WINDOW_DAYS), _days_ago(WINDOW_DAYS * 2)
    problems: list[str] = []

    for stage, top, bottom, human in FUNNEL_STEPS:
        recent_top = totals(conn, stage, top, recent_from)
        older_top = totals(conn, stage, top, older_from, recent_from)

        # Нужен объём в обоих окнах, иначе сравнение бессмысленно.
        if recent_top < 20 or older_top < 20:
            continue

        recent_rate = totals(conn, stage, bottom, recent_from) / recent_top
        older_rate = totals(conn, stage, bottom, older_from, recent_from) / older_top

        if older_rate <= 0:
            continue
        if recent_rate * DROP_FACTOR <= older_rate:
            problems.append(
                f"{stage}: конверсия «{human}» упала с {older_rate:.0%} "
                f"до {recent_rate:.0%} за неделю"
            )

    return problems


def report(conn: sqlite3.Connection, days: int = 14) -> str:
    """Человекочитаемая сводка за период. Возвращает готовый текст."""
    since = _days_ago(days)
    lines = [f"ВОРОНКА ЗА {days} ДНЕЙ (с {since})", "=" * 62, ""]

    empty = True
    for stage, top, bottom, human in FUNNEL_STEPS:
        top_value = totals(conn, stage, top, since)
        bottom_value = totals(conn, stage, bottom, since)
        if top_value == 0 and bottom_value == 0:
            continue
        empty = False
        rate = (bottom_value / top_value * 100) if top_value else 0.0
        lines.append(f"{human:<44} {top_value:>6.0f} → {bottom_value:>5.0f}  {rate:>5.1f}%")

    if empty:
        lines.append("Данных пока нет. Числа появятся после первого прогона этапов.")
        return "\n".join(lines)

    lines.append("")
    lines.append("-" * 62)

    # Сквозная конверсия: от проверенной в реестре компании до ответа.
    checked = totals(conn, "targets", "checked", since)
    replied = totals(conn, "outreach", "replied", since)
    if checked:
        lines.append(f"Сквозная: из {checked:.0f} проверенных компаний "
                     f"{replied:.0f} ответов ({replied / checked * 100:.2f}%)")

    spent = totals(conn, "checko", "requests", since)
    if spent:
        lines.append(f"Запросов к Checko потрачено: {spent:.0f} "
                     f"(в среднем {spent / days:.0f} в день из 100)")

    problems = check_drops(conn)
    if problems:
        lines.append("")
        lines.append("ВНИМАНИЕ:")
        lines.extend(f"  · {problem}" for problem in problems)
        lines.append("")
        lines.append("Так выглядит деградация источника. Проверьте RUNBOOK.md.")

    return "\n".join(lines)


def daily_table(conn: sqlite3.Connection, days: int = 14) -> str:
    """Таблица по дням: видно, в какой именно день что-то изменилось."""
    since = _days_ago(days)
    rows = conn.execute(
        """
        SELECT day,
               SUM(CASE WHEN stage='targets' AND metric='checked'   THEN value END) AS checked,
               SUM(CASE WHEN stage='targets' AND metric='passed'    THEN value END) AS passed,
               SUM(CASE WHEN stage='dossier' AND metric='with_facts' THEN value END) AS dossiers,
               SUM(CASE WHEN stage='compose' AND metric='written'   THEN value END) AS letters,
               SUM(CASE WHEN stage='outreach' AND metric='sent'     THEN value END) AS sent,
               SUM(CASE WHEN stage='outreach' AND metric='replied'  THEN value END) AS replied
        FROM funnel WHERE day >= ? GROUP BY day ORDER BY day
        """,
        (since,),
    ).fetchall()

    if not rows:
        return ""

    header = f"{'дата':<12}{'провер':>8}{'ICP':>6}{'досье':>7}{'письма':>8}{'ушло':>7}{'ответы':>8}"
    lines = ["", "ПО ДНЯМ", "-" * len(header), header]
    for row in rows:
        lines.append(
            f"{row['day']:<12}"
            f"{row['checked'] or 0:>8.0f}{row['passed'] or 0:>6.0f}"
            f"{row['dossiers'] or 0:>7.0f}{row['letters'] or 0:>8.0f}"
            f"{row['sent'] or 0:>7.0f}{row['replied'] or 0:>8.0f}"
        )
    return "\n".join(lines)


def run(days: int = 14) -> int:
    """Этап `stats`: печатает сводку и возвращает число замечаний."""
    conn = db.connect()
    print()
    print(report(conn, days))
    table = daily_table(conn, days)
    if table:
        print(table)
    print()

    problems = check_drops(conn)
    for problem in problems:
        # Уровень WARNING, а не ERROR: падение конверсии — это повод
        # посмотреть, а не признак поломки прогона.
        logger.warning("%s", problem)
    conn.close()
    return len(problems)


def stamp(conn: sqlite3.Connection) -> str:
    """Отметка времени в UTC. Дублирует db.now(), чтобы не тянуть импорт."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
