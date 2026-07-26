"""Этап 1: передача человеку — утренний список.

Формат: один Markdown-файл на дату в morning/. Для каждой компании —
строка «почему она здесь», сигналы со ссылками и ссылка на проверку в hh.ru,
которую вы открываете сами (см. DECISIONS.md, Р-001: автоматически hh мы
не трогаем).
"""

from __future__ import annotations

import json
import urllib.parse
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from . import contacts as contactlib, db, log

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


# ---------------------------------------------------------------------------
# Утренний список по целевым компаниям (Этап 3).
# Отличается от списка выше тем, что строится не от вакансии-сигнала,
# а от компании с готовым письмом.
# ---------------------------------------------------------------------------

TYPE_LABELS = {
    "outsourcing": "разработка на заказ",
    "staffing": "аутстафф команд",
    "product": "продуктовая компания",
    "integrator": "системный интегратор",
    "not_it": "не про IT",
    "unknown": "тип не определён",
}


def build_morning(config: dict[str, Any], dry_run: bool = False) -> Path | None:
    """Утренний список: компания, строка «почему», готовое письмо."""
    site_cfg = config.get("website", {})
    target_types = site_cfg.get("target_types") or ["outsourcing", "staffing"]
    limit = int(config.get("report", {}).get("max_companies", 20))

    conn = db.connect()
    placeholders = ",".join("?" for _ in target_types)

    # Отложенные письма: фактов оказалось меньше минимума. В список они не
    # идут, но и молча пропасть не должны — показываем именами в хвосте.
    thin_rows = conn.execute(
        f"""
        SELECT inn, name, site_url, letter_why FROM companies
        WHERE letter_status = 'thin'
          AND site_type IN ({placeholders})
          AND last_reported IS NULL
        ORDER BY name
        """,
        tuple(target_types),
    ).fetchall()

    rows = conn.execute(
        f"""
        SELECT * FROM companies
        WHERE letter_body IS NOT NULL
          AND COALESCE(letter_status, 'ok') = 'ok'
          AND site_type IN ({placeholders})
          AND last_reported IS NULL
        ORDER BY
            -- Продуктовые компании первыми: у них ICP острее, и система
            -- показывает себя на них лучше всего (DECISIONS.md, Р-020).
            CASE WHEN site_type = 'product' THEN 0 ELSE 1 END,
            CASE WHEN revenue_change_pct IS NULL THEN 1 ELSE 0 END,
            revenue_change_pct
        LIMIT ?
        """,
        (*target_types, limit),
    ).fetchall()

    if not rows:
        if thin_rows:
            logger.warning(
                "Годных писем нет, отложено из-за нехватки фактов: %d. "
                "Смотреть: sqlite3 data/leads.db < tools/thin.sql", len(thin_rows)
            )
        else:
            logger.warning("Нет компаний с готовыми письмами. "
                           "Порядок: --stage targets, --stage dossier, --stage compose")
        conn.close()
        return None

    today = date.today().isoformat()
    lines: list[str] = [
        f"# Утренний список — {today}",
        "",
        f"Компаний: **{len(rows)}**. Каждое письмо прочитайте и поправьте перед отправкой.",
        "",
        "> Имя и должность получателя система не хранит и не подставляет — "
        "добавьте руками в почтовом клиенте.",
        "",
        "---",
        "",
    ]

    for index, row in enumerate(rows, start=1):
        try:
            facts = json.loads(row["letter_facts"] or "[]")
        except (json.JSONDecodeError, TypeError):
            facts = []

        lines.append(f"## {index}. {row['name']}")
        lines.append("")
        lines.append(f"**Почему здесь:** {row['letter_why'] or '—'}")
        lines.append("")

        details = [TYPE_LABELS.get(row["site_type"], row["site_type"] or "—")]
        if row["staff"]:
            details.append(f"{row['staff']:.0f} чел.")
        if row["revenue"]:
            money = f"{row['revenue'] / 1e6:.0f} млн ₽"
            if row["revenue_change_pct"] is not None:
                money += f" ({row['revenue_change_pct']:+.0f}%)"
            details.append(money)
        if row["region"]:
            details.append(row["region"])
        lines.append(" · ".join(details))
        lines.append("")

        if row["site_summary"]:
            lines.append(f"_{row['site_summary']}_")
            lines.append("")

        where = []
        if row["site_url"]:
            where.append(f"[сайт]({row['site_url']})")
        if row["contact_email"]:
            note = contactlib.label(row["contact_email"])
            address = f"`{row['contact_email']}`"
            # Куда именно попадёт письмо, видно до отправки, а не после:
            # tender@ читает тендерный отдел, письмо про клиентов ему не нужно.
            where.append(f"{address} — ⚠️ {note}" if note else address)
        else:
            where.append("почты нет — ищите общий адрес на сайте")
        where.append(f"ИНН `{row['inn']}`")
        lines.append(" · ".join(where))
        lines.append("")

        lines.append(f"**Тема:** {row['letter_subject'] or '—'}")
        lines.append("")
        lines.append("```")
        lines.append(row["letter_body"] or "")
        lines.append("```")
        lines.append("")

        if facts:
            # Не самоотчёт модели, а результат сверки: каждый факт найден
            # и на сайте компании, и в тексте письма (DECISIONS.md, Р-025).
            lines.append(f"<sub>Проверено — письмо опирается на: {'; '.join(facts)}</sub>")
            lines.append("")

        lines.append("---")
        lines.append("")

    if thin_rows:
        lines.append("## Отложено: не на чем построить письмо")
        lines.append("")
        lines.append(
            "По этим компаниям письмо получилось бы общим — фактов с сайта "
            "набралось меньше минимума. Письмо, которое говорит «вас нашла "
            "система», и при этом подходит кому угодно, вредит больше, "
            "чем неотправленное."
        )
        lines.append("")
        for row in thin_rows:
            site = f" — {row['site_url']}" if row["site_url"] else ""
            why = f" · {row['letter_why']}" if row["letter_why"] else ""
            lines.append(f"- {row['name']} (ИНН `{row['inn']}`){site}{why}")
        lines.append("")
        lines.append(
            "Если хотите написать им руками — посмотрите, что нашлось: "
            "`sqlite3 data/leads.db < tools/thin.sql`"
        )
        lines.append("")

    problems = log.problems()
    if problems:
        lines.append("## ⚠️ Проблемы прогона")
        lines.append("")
        lines.extend(f"- {problem}" for problem in problems)
        lines.append("")

    if dry_run:
        logger.info("[dry-run] Отчёт не сохранён. Компаний было бы: %d", len(rows))
        conn.close()
        return None

    MORNING_DIR.mkdir(parents=True, exist_ok=True)
    path = MORNING_DIR / f"{today}.md"
    path.write_text("\n".join(lines), encoding="utf-8")

    # Отложенные тоже помечаем показанными: они попали в хвост отчёта, и
    # каждое утро повторять их незачем. Найти их потом — tools/thin.sql.
    db.mark_reported(conn, [], [row["inn"] for row in rows] + [r["inn"] for r in thin_rows])
    conn.commit()
    conn.close()

    logger.info("Утренний список готов: %s (%d писем, отложено %d)",
                path, len(rows), len(thin_rows))
    return path
