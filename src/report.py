"""Этап 1: передача человеку — утренний список.

Формат: один Markdown-файл на дату в morning/. Для каждой компании —
строка «почему она здесь», сигналы со ссылками и ссылка на проверку в hh.ru,
которую вы открываете сами (см. DECISIONS.md, Р-001: автоматически hh мы
не трогаем).
"""

from __future__ import annotations

import urllib.parse
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from . import db, log

logger = log.get("report")

MORNING_DIR = Path(__file__).resolve().parent.parent / "morning"


def hh_search_url(company_name: str) -> str:
    """Ссылка на поиск работодателя в hh.ru — для ручной проверки человеком."""
    query = urllib.parse.quote(company_name)
    return f"https://hh.ru/search/employer?text={query}"


def why_line(signals: list[Any]) -> str:
    """Одна строка «почему эта компания здесь» — то, что вы читаете первым."""
    strong = [s for s in signals if s["strength"] == "strong"]
    count = len(signals)

    if strong:
        keywords = strong[0]["matched"]
        base = f'в вакансии прямым текстом: «{keywords}»'
    else:
        jobs = ", ".join(sorted({s["job_name"] for s in signals if s["job_name"]}))
        base = f"ищут людей в продажи: {jobs}"

    if count > 1:
        base += f" — и это {count} вакансии сразу, похоже на текучку или перестройку отдела"

    return base


def build(config: dict[str, Any], dry_run: bool = False) -> Path | None:
    """Собирает утренний список. Возвращает путь к файлу или None, если пусто."""
    report_cfg = config.get("report", {})
    filters = config.get("filters", {})

    conn = db.connect()
    cooldown = db.companies_on_cooldown(conn, int(filters.get("company_cooldown_days", 30)))
    rows = db.unreported_signals(conn)

    # Группируем сигналы по компаниям: письмо пишется компании, а не вакансии.
    by_company: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        if row["inn"] in cooldown:
            continue
        by_company[row["inn"]].append(row)

    if not by_company:
        logger.warning(
            "Новых компаний нет. Если так повторяется несколько дней подряд — "
            "либо слишком узкие ключевые слова, либо источник не покрывает ваш ICP"
        )
        conn.close()
        return None

    # Сначала компании с сильными сигналами, внутри — с бо́льшим числом вакансий.
    def sort_key(item: tuple[str, list[Any]]) -> tuple[int, int]:
        _, signals = item
        has_strong = any(s["strength"] == "strong" for s in signals)
        return (0 if has_strong else 1, -len(signals))

    ordered = sorted(by_company.items(), key=sort_key) if report_cfg.get("strong_first", True) \
        else list(by_company.items())
    ordered = ordered[: int(report_cfg.get("max_companies", 25))]

    today = date.today().isoformat()
    lines: list[str] = [
        f"# Утренний список — {today}",
        "",
        f"Компаний: **{len(ordered)}**. Источник сигналов: «Работа России» (открытые данные).",
        "",
        "> Проверьте компанию глазами перед тем, как писать. Ссылка на hh.ru — "
        "для ручной проверки: открываете сами, система туда не ходит.",
        "",
        "---",
        "",
    ]

    reported_signal_ids: list[int] = []
    reported_inns: list[str] = []

    for index, (inn, signals) in enumerate(ordered, start=1):
        first = signals[0]
        name = first["company_name"]
        strong = any(s["strength"] == "strong" for s in signals)
        marker = "🔥" if strong else "•"

        lines.append(f"## {index}. {marker} {name}")
        lines.append("")
        lines.append(f"**Почему здесь:** {why_line(signals)}")
        lines.append("")

        details = [f"ИНН `{inn}`", f"регион: {first['region'] or '—'}"]
        if first["company_site"]:
            details.append(f"сайт: {first['company_site']}")
        if first["company_email"]:
            details.append(f"почта: {first['company_email']}")
        lines.append(" · ".join(details))
        lines.append("")

        lines.append("**Сигналы:**")
        for signal in signals:
            date_part = f" от {signal['created_date']}" if signal["created_date"] else ""
            title = signal["job_name"] or "вакансия"
            link = f"[{title}]({signal['url']})" if signal["url"] else title
            lines.append(f"- {link}{date_part} — совпало: _{signal['matched']}_")
            if signal["excerpt"]:
                lines.append(f"  > {signal['excerpt']}")
            reported_signal_ids.append(signal["id"])
        lines.append("")

        lines.append(f"**Проверить руками:** [вакансии компании на hh.ru]({hh_search_url(name)})")
        lines.append("")
        lines.append("---")
        lines.append("")

        reported_inns.append(inn)

    # Блок проблем: всё, что пошло не так за прогон, видно здесь же.
    problems = log.problems()
    if problems:
        lines.append("## ⚠️ Проблемы прогона")
        lines.append("")
        for problem in problems:
            lines.append(f"- {problem}")
        lines.append("")

    content = "\n".join(lines)

    if dry_run:
        logger.info("[dry-run] Отчёт не сохранён. Компаний было бы: %d", len(ordered))
        conn.close()
        return None

    MORNING_DIR.mkdir(parents=True, exist_ok=True)
    path = MORNING_DIR / f"{today}.md"
    path.write_text(content, encoding="utf-8")

    db.mark_reported(conn, reported_signal_ids, reported_inns)
    conn.commit()
    conn.close()

    logger.info("Утренний список готов: %s (%d компаний)", path, len(ordered))
    return path
