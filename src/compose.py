"""Этап 3б: генерация письма по досье.

Промпт собирается из двух частей:
  * config/prompts/letter.md — оффер, первый шаг, запреты, тон. Общее для всех.
  * config/prompts/angles/<тип>.md — угол под тип компании.

Разделение не косметическое. Запреты и тон одинаковы всегда, а разговор с
продуктовой компанией и с аутсорсером идёт о разном: у первой ICP острый и
ей интересно ловить покупателя по событию, у второй боль в предсказуемости
потока. Один текст на обоих превращается в рассылку — см. DECISIONS.md, Р-021.

Письмо не отправляется. Оно попадает в утренний список, где вы его читаете,
правите и отправляете руками.

Письмо, не набравшее минимума фактов из досье (compose.min_facts в icp.yaml),
в утренний список не идёт вовсе — см. DECISIONS.md, Р-024.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import db, llm, log

logger = log.get("compose")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "config" / "prompts"

LETTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "why_line": {"type": "string"},
        "facts_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["subject", "body", "why_line", "facts_used"],
    "additionalProperties": False,
}


def load_prompt(relative: str) -> str:
    path = PROMPTS_DIR / relative
    if not path.exists():
        raise FileNotFoundError(f"Не найден промпт {path}")
    return path.read_text(encoding="utf-8")


def build_system_prompt(site_type: str) -> str:
    """Склеивает общий каркас с углом под тип компании."""
    base = load_prompt("letter.md")
    angle_file = f"angles/{site_type}.md"
    try:
        angle = load_prompt(angle_file)
    except FileNotFoundError:
        angle = load_prompt("angles/default.md")

    return (
        base
        + "\n\n---\n\n" + angle
        + "\n\n---\n\n## Формат ответа\n\n"
        "Верни JSON: `subject` — тема письма; `body` — текст письма; "
        "`why_line` — одна строка для утреннего списка, объясняющая, почему "
        "эта компания здесь; `facts_used` — список фактов из досье, на "
        "которые ты реально сослался в письме.\n\n"
        "`facts_used` — не украшение, а самопроверка. Перечисляй только то, "
        "что действительно попало в текст письма, дословно или близко. "
        "Общие слова вроде «IT-компания» фактом не считаются. Письмо, "
        "набравшее меньше двух фактов, человеку не показывается: лучше "
        "не написать письма вовсе, чем написать такое, которое выдаёт себя "
        "за найденное системой и при этом подходит кому угодно."
    )


def build_dossier(row: Any) -> str:
    """Собирает досье компании — единственный источник фактов для письма."""
    facts: list[str] = [f"Название: {row['name']}"]

    if row["site_summary"]:
        facts.append(f"Чем занимается (с их сайта): {row['site_summary']}")
    if row["site_specialization"]:
        try:
            spec = json.loads(row["site_specialization"])
            if spec:
                facts.append(f"Специализация: {', '.join(spec)}")
        except (json.JSONDecodeError, TypeError):
            pass
    if row["site_url"]:
        facts.append(f"Сайт: {row['site_url']}")
    if row["region"]:
        facts.append(f"Регион: {row['region']}")
    if row["staff"]:
        facts.append(f"Численность: {row['staff']:.0f} человек")

    if row["revenue"]:
        line = f"Выручка: {row['revenue'] / 1e6:.0f} млн ₽"
        if row["revenue_change_pct"] is not None:
            line += f" ({row['revenue_change_pct']:+.0f}% год к году)"
        facts.append(line)
        if row["staff"]:
            facts.append(
                f"Выручка на сотрудника: {row['revenue'] / row['staff'] / 1e6:.1f} млн ₽"
            )

    if row["okved_name"]:
        facts.append(f"Основной вид деятельности по реестру: {row['okved_name']}")

    facts.append(
        "\nВНИМАНИЕ: это все факты, которые есть. Ничего сверх этого списка "
        "в письме быть не должно."
    )
    return "\n".join(facts)


def run(config: dict[str, Any], dry_run: bool = False, limit: int | None = None) -> dict[str, int]:
    """Генерирует письма для компаний, прошедших классификацию по сайту."""
    site_cfg = config.get("website", {})
    target_types = site_cfg.get("target_types") or ["outsourcing", "staffing"]
    compose_cfg = config.get("compose", {})
    per_run = limit or int(compose_cfg.get("per_run", 20))
    min_facts = int(compose_cfg.get("min_facts", 2))

    placeholders = ",".join("?" for _ in target_types)
    conn = db.connect()
    rows = conn.execute(
        f"""
        SELECT * FROM companies
        WHERE icp_status = 'passed'
          AND site_type IN ({placeholders})
          AND letter_body IS NULL
        ORDER BY
            CASE WHEN revenue_change_pct IS NULL THEN 1 ELSE 0 END,
            revenue_change_pct
        LIMIT ?
        """,
        (*target_types, per_run),
    ).fetchall()

    if not rows:
        logger.info("Нет компаний, готовых к письму. "
                    "Сначала: run.py --stage targets, затем --stage dossier")
        conn.close()
        return {"written": 0}

    logger.info("Компаний к написанию письма: %d", len(rows))

    counters = {"written": 0, "failed": 0, "thin": 0}
    for row in rows:
        name = row["name"]
        try:
            system_prompt = build_system_prompt(row["site_type"] or "default")
        except FileNotFoundError as exc:
            logger.error("%s", exc)
            break

        try:
            result = llm.classify(system_prompt, build_dossier(row), LETTER_SCHEMA,
                                  max_tokens=2048)
        except llm.LLMUnavailable as exc:
            logger.error("%s", exc)
            break
        except llm.LLMError as exc:
            counters["failed"] += 1
            logger.warning("— %s: письмо не написалось (%s)", name, exc)
            continue

        facts_used = result.get("facts_used") or []
        body = result.get("body", "")

        # Самопроверка. Письмо говорит адресату, что его компанию и факт о ней
        # нашла система. Если факт при этом один и общий, письмо опровергает
        # само себя, поэтому такое письмо человеку не показывается вовсе.
        thin = len(facts_used) < min_facts
        status = "thin" if thin else "ok"

        if thin:
            counters["thin"] += 1
            # Уровень INFO, а не WARNING: это штатный исход, а не поломка.
            # В блок «Проблемы прогона» ему попадать незачем — отложенные
            # письма и так перечислены в хвосте отчёта отдельным разделом.
            logger.info(
                "— %s: фактов %d из %d — письмо отложено, в утренний список не пойдёт",
                name, len(facts_used), min_facts,
            )
        else:
            counters["written"] += 1
            logger.info("✓ %s — %s", name, result.get("why_line", ""))

        if not dry_run:
            conn.execute(
                """
                UPDATE companies SET
                    letter_subject = ?, letter_body = ?, letter_why = ?,
                    letter_facts = ?, letter_status = ?, letter_written_at = ?
                WHERE inn = ?
                """,
                (result.get("subject", ""), body, result.get("why_line", ""),
                 json.dumps(facts_used, ensure_ascii=False), status,
                 db.now(), row["inn"]),
            )

    if not dry_run:
        conn.commit()
    conn.close()

    logger.info("Письма: годных %d, отложено (мало фактов) %d, не удалось %d",
                counters["written"], counters["thin"], counters["failed"])
    if counters["thin"]:
        logger.info("Отложенные письма: sqlite3 data/leads.db < tools/thin.sql")
    return counters
