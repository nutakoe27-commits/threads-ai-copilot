"""Этап 3а: классификация компании по её сайту.

Реестр говорит, что компания есть и какого она размера. Чем она на самом
деле зарабатывает — говорит сайт. ОКВЭД 62.01 одинаково носят веб-студия,
вендор коробочного софта и дистрибьютор, поэтому финальный отбор ICP
делается здесь (DECISIONS.md, Р-019).

Проход идёт только по компаниям, прошедшим фильтр ICP: тратить обход сайта
и вызов модели на заведомо чужих незачем.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import db, facts as factlib, llm, log
from .sources.website import WebsiteFetcher

logger = log.get("dossier")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "config" / "prompts"

# Строгая схема ответа: модель обязана вернуть ровно эту форму,
# разбирать свободный текст и угадывать формат не приходится.
CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["outsourcing", "staffing", "product",
                     "integrator", "not_it", "unknown"],
        },
        "confidence": {"type": "number"},
        "summary": {"type": "string"},
        "specialization": {"type": "array", "items": {"type": "string"}},
        # Конкретика, из которой потом делается наблюдение в письме.
        # `quote` — дословный кусок текста сайта: по нему факт проверяется
        # кодом, без обращения к модели (DECISIONS.md, Р-025).
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["fact", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["type", "confidence", "summary", "specialization", "facts"],
    "additionalProperties": False,
}

TYPE_LABELS = {
    "outsourcing": "разработка на заказ",
    "staffing": "аутстафф команд",
    "product": "продуктовая компания",
    "integrator": "системный интегратор",
    "not_it": "не про IT",
    "unknown": "не удалось определить",
}


def verify_facts(raw: list[Any], site_text: str) -> tuple[list[str], int]:
    """Оставляет только те факты, чья цитата действительно есть на сайте.

    Возвращает (проверенные факты, сколько отброшено). Отброшенные — это
    либо выдумка, либо пересказ настолько вольный, что опираться на него
    в письме опасно: письмо утверждает, что факт найден на сайте.
    """
    verified: list[str] = []
    dropped = 0
    for item in raw or []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        fact = (item.get("fact") or "").strip()
        quote = (item.get("quote") or "").strip()
        if not fact or not quote:
            dropped += 1
            continue
        if factlib.quote_found(quote, site_text):
            verified.append(fact)
        else:
            dropped += 1
    return verified, dropped


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Не найден промпт {path}")
    return path.read_text(encoding="utf-8")


def build_user_content(company: Any, site_text: str) -> str:
    """Собирает то, что видит модель: реестровые факты плюс текст сайта."""
    facts = [f"Название: {company['name']}"]
    if company["okved_name"]:
        facts.append(f"ОКВЭД: {company['okved']} — {company['okved_name']}")
    if company["staff"]:
        facts.append(f"Численность: {company['staff']:.0f}")
    if company["revenue"]:
        facts.append(f"Выручка: {company['revenue'] / 1e6:.0f} млн ₽")
    if company["region"]:
        facts.append(f"Регион: {company['region']}")

    return (
        "Данные из реестра:\n" + "\n".join(facts)
        + "\n\nТекст с сайта компании:\n" + site_text
    )


def run(config: dict[str, Any], dry_run: bool = False, limit: int | None = None) -> dict[str, int]:
    """Обходит сайты компаний, прошедших ICP, и классифицирует их."""
    site_cfg = config.get("website", {})
    per_run = limit or int(site_cfg.get("per_run", 25))

    conn = db.connect()
    rows = conn.execute(
        """
        SELECT * FROM companies
        WHERE icp_status = 'passed' AND site_checked_at IS NULL
        ORDER BY checked_at
        LIMIT ?
        """,
        (per_run,),
    ).fetchall()

    if not rows:
        logger.info("Нет компаний, ожидающих проверки сайта. "
                    "Сначала соберите список: run.py --stage targets")
        conn.close()
        return {"checked": 0}

    logger.info("Компаний к проверке сайта: %d", len(rows))

    try:
        system_prompt = load_prompt("classify.md")
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        conn.close()
        return {"checked": 0}

    target_types = site_cfg.get("target_types") or ["outsourcing", "staffing"]

    fetcher = WebsiteFetcher(site_cfg)
    counters = {"checked": 0, "no_site": 0, "unreachable": 0, "classified": 0,
                "facts": 0, "facts_dropped": 0}
    by_type: dict[str, int] = {}

    for row in rows:
        name = row["name"]
        raw_site = (row["site"] or "").strip()

        if not raw_site:
            counters["no_site"] += 1
            logger.info("— %s: сайта нет в реестре", name)
            if not dry_run:
                _save(conn, row["inn"], {}, error="сайта нет в реестре")
            continue

        result = fetcher.fetch(raw_site)
        if not result["ok"]:
            counters["unreachable"] += 1
            logger.warning("— %s: %s (%s)", name, result["reason"], raw_site)
            if not dry_run:
                _save(conn, row["inn"], {"site_url": raw_site}, error=result["reason"])
            continue

        counters["checked"] += 1
        logger.debug("%s: прочитано страниц %d, символов %d",
                     name, len(result["pages"]), len(result["text"]))

        try:
            verdict = llm.classify(
                system_prompt, build_user_content(row, result["text"]), CLASSIFY_SCHEMA
            )
        except llm.LLMUnavailable as exc:
            # Нет ключа — это не авария прогона, но дальше идти бессмысленно.
            logger.error("%s", exc)
            break
        except llm.LLMError as exc:
            logger.warning("— %s: классификация не удалась (%s)", name, exc)
            if not dry_run:
                _save(conn, row["inn"], {"site_url": result["base_url"]},
                      error=f"классификация не удалась: {exc}")
            continue

        counters["classified"] += 1
        site_type = verdict.get("type", "unknown")
        by_type[site_type] = by_type.get(site_type, 0) + 1

        site_facts, dropped = verify_facts(verdict.get("facts"), result["text"])
        counters["facts"] += len(site_facts)
        counters["facts_dropped"] += dropped

        logger.info("%s %s — %s (уверенность %.0f%%): %s",
                    "✓" if site_type in target_types else "·",
                    name, TYPE_LABELS.get(site_type, site_type),
                    float(verdict.get("confidence", 0)) * 100,
                    verdict.get("summary", ""))
        for fact in site_facts:
            logger.info("      · %s", fact)
        if dropped:
            logger.warning("  ↳ %s: фактов не подтвердилось цитатой: %d", name, dropped)

        if not dry_run:
            _save(conn, row["inn"], {
                "site_url": result["base_url"],
                "site_type": site_type,
                "site_confidence": float(verdict.get("confidence", 0)),
                "site_summary": verdict.get("summary", ""),
                "site_specialization": json.dumps(
                    verdict.get("specialization", []), ensure_ascii=False),
                "site_facts": json.dumps(site_facts, ensure_ascii=False),
            })

    if not dry_run:
        conn.commit()
    conn.close()

    logger.info("Сайты: проверено %d, классифицировано %d "
                "(без сайта %d, недоступны %d)",
                counters["checked"], counters["classified"],
                counters["no_site"], counters["unreachable"])
    if counters["classified"]:
        logger.info("Фактов собрано %d (в среднем %.1f на компанию), "
                    "не подтвердилось цитатой %d",
                    counters["facts"], counters["facts"] / counters["classified"],
                    counters["facts_dropped"])
    if by_type:
        logger.info("--- Кто это оказался:")
        for site_type, count in sorted(by_type.items(), key=lambda x: -x[1]):
            logger.info("      %3d  %s", count, TYPE_LABELS.get(site_type, site_type))
        target = sum(by_type.get(t, 0) for t in target_types)
        labels = ", ".join(TYPE_LABELS.get(t, t) for t in target_types)
        logger.info("Подходят для письма (%s): %d из %d",
                    labels, target, counters["classified"])
    return counters


def _save(conn: Any, inn: str, fields: dict[str, Any], error: str = "") -> None:
    """Записывает результат проверки сайта. Отметка времени ставится всегда,
    чтобы недоступный сайт не обходился заново на каждом прогоне."""
    conn.execute(
        """
        UPDATE companies SET
            site_url = COALESCE(NULLIF(:site_url, ''), site_url),
            site_type = :site_type,
            site_confidence = :site_confidence,
            site_summary = :site_summary,
            site_specialization = :site_specialization,
            site_facts = :site_facts,
            site_error = :site_error,
            site_checked_at = :ts
        WHERE inn = :inn
        """,
        {
            "inn": inn,
            "site_url": fields.get("site_url", ""),
            "site_type": fields.get("site_type"),
            "site_confidence": fields.get("site_confidence"),
            "site_summary": fields.get("site_summary"),
            "site_specialization": fields.get("site_specialization"),
            "site_facts": fields.get("site_facts"),
            "site_error": error,
            "ts": db.now(),
        },
    )
