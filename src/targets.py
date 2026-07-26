"""Этап 2: построение целевого списка компаний по критериям ICP.

Работает в два прохода, оба возобновляемые:

  1. discover — поиск кандидатов по ОКВЭД и регионам. Пишет в базу только
     ИНН и название, без обогащения. Дёшево по запросам.
  2. enrich   — по каждому неизвестному кандидату запрашивает карточку и
     выносит вердикт по ICP. Дорого: 1 запрос на компанию.

Суточный лимит Checko (100 запросов на бесплатном тарифе) — не помеха:
упёрлись — остановились, прогресс в базе, завтра продолжаем с того же
места. Ничего не теряется и не запрашивается повторно.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from . import db, log
from .providers.checko import BudgetExhausted, CheckoClient, CheckoError, RequestBudget

logger = log.get("targets")

PROVIDER = "checko"


def _years_since(date_str: str) -> float | None:
    """Возраст компании в годах по дате регистрации."""
    if not date_str:
        return None
    for fmt_len, sep in ((10, "-"), (10, ".")):
        parts = date_str[:fmt_len].split(sep)
        if len(parts) == 3:
            try:
                # Формат может быть и ГГГГ-ММ-ДД, и ДД.ММ.ГГГГ.
                if len(parts[0]) == 4:
                    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                else:
                    day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
                delta = date.today() - date(year, month, day)
                return delta.days / 365.25
            except ValueError:
                continue
    return None


def judge(company: dict[str, Any], criteria: dict[str, Any]) -> tuple[str, str]:
    """Выносит вердикт по ICP. Возвращает (статус, причина человеческим языком).

    Причина сохраняется в базу, чтобы потом можно было понять, почему
    компания не попала в список, и при необходимости смягчить критерий —
    не перезапрашивая API.
    """
    exclude = [str(code) for code in criteria.get("exclude_okved", [])]
    all_okved = [company.get("okved") or ""] + list(company.get("extra_okved") or [])
    for code in all_okved:
        for bad in exclude:
            if code.startswith(bad):
                return "rejected", f"ОКВЭД {code} в списке исключений (кадровые агентства)"

    staff = company.get("staff")
    staff_min = criteria.get("staff_min")
    staff_max = criteria.get("staff_max")
    if staff is None:
        return "rejected", "нет данных о численности персонала"
    if staff_min is not None and staff < staff_min:
        return "rejected", f"штат {staff:.0f} меньше порога {staff_min}"
    if staff_max is not None and staff > staff_max:
        return "rejected", f"штат {staff:.0f} больше порога {staff_max}"

    revenue = company.get("revenue")
    revenue_min = criteria.get("revenue_min")
    revenue_max = criteria.get("revenue_max")
    if revenue is None:
        return "rejected", "нет данных о выручке"
    if revenue_min is not None and revenue < revenue_min:
        return "rejected", f"выручка {revenue / 1e6:.1f} млн меньше порога"
    if revenue_max is not None and revenue > revenue_max:
        return "rejected", f"выручка {revenue / 1e6:.1f} млн больше порога"

    age = _years_since(company.get("registration_date") or "")
    min_age = criteria.get("min_age_years")
    if min_age is not None and age is not None and age < min_age:
        return "rejected", f"возраст {age:.1f} года меньше порога {min_age}"

    return "passed", (
        f"ОКВЭД {company.get('okved')}, штат {staff:.0f}, "
        f"выручка {revenue / 1e6:.1f} млн"
    )


def discover(config: dict[str, Any], dry_run: bool = False) -> dict[str, int]:
    """Проход 1: поиск кандидатов по ОКВЭД и регионам."""
    provider_cfg = config.get("provider", {})
    target_cfg = config.get("target_list", {})

    conn = db.connect()
    spent_today = db.requests_spent_today(conn, PROVIDER)
    budget = RequestBudget(int(provider_cfg.get("daily_request_budget", 100)), spent_today)
    logger.info("Бюджет запросов Checko на сегодня: осталось %d из %d",
                budget.left, budget.limit)

    counters = {"found": 0, "new": 0}
    try:
        client = CheckoClient(provider_cfg, budget)
    except CheckoError as exc:
        logger.error("%s", exc)
        conn.close()
        return counters

    try:
        for okved in target_cfg.get("okved", []):
            for region in target_cfg.get("regions", []):
                try:
                    for record in client.iter_candidates(
                        okved, region["code"], region["name"],
                        target_cfg.get("opf"), bool(target_cfg.get("only_active", True)),
                    ):
                        counters["found"] += 1
                        if dry_run:
                            continue
                        if db.add_candidate(conn, record["inn"], record["name"], region["name"]):
                            counters["new"] += 1
                except CheckoError as exc:
                    # Один неудачный срез не должен ронять весь проход.
                    logger.error("ОКВЭД %s / %s пропущен: %s", okved, region["name"], exc)
                    continue
                if not dry_run:
                    conn.commit()
    except BudgetExhausted as exc:
        logger.warning("%s", exc)

    if not dry_run:
        db.record_requests(conn, PROVIDER, budget.spent - spent_today)
        conn.commit()
    conn.close()

    logger.info("Поиск кандидатов: найдено %d, новых %d, запросов потрачено %d",
                counters["found"], counters["new"], budget.spent - spent_today)
    return counters


def enrich(config: dict[str, Any], dry_run: bool = False) -> dict[str, int]:
    """Проход 2: карточка по каждому кандидату и вердикт по ICP."""
    provider_cfg = config.get("provider", {})
    criteria = config.get("criteria", {})

    conn = db.connect()
    spent_today = db.requests_spent_today(conn, PROVIDER)
    budget = RequestBudget(int(provider_cfg.get("daily_request_budget", 100)), spent_today)

    per_run = int(provider_cfg.get("enrich_per_run", 80))
    limit = min(per_run, budget.left)
    if limit <= 0:
        logger.warning("Бюджет запросов Checko на сегодня исчерпан. Продолжим завтра")
        conn.close()
        return {"checked": 0, "passed": 0, "rejected": 0}

    candidates = db.unchecked_candidates(conn, limit)
    if not candidates:
        logger.info("Все известные кандидаты уже проверены")
        conn.close()
        return {"checked": 0, "passed": 0, "rejected": 0}

    logger.info("Проверяю %d компаний (бюджет позволяет %d)", len(candidates), budget.left)

    counters = {"checked": 0, "passed": 0, "rejected": 0}
    try:
        client = CheckoClient(provider_cfg, budget)
    except CheckoError as exc:
        logger.error("%s", exc)
        conn.close()
        return counters

    try:
        for row in candidates:
            inn = row["inn"]
            try:
                payload = client.company(inn)
            except BudgetExhausted:
                raise
            except CheckoError as exc:
                logger.error("ИНН %s пропущен: %s", inn, exc)
                continue

            company = CheckoClient.parse_company(payload)
            status, reason = judge(company, criteria)
            counters["checked"] += 1
            counters[status] += 1

            level = logger.info if status == "passed" else logger.debug
            level("%s %s — %s (%s)", "✓" if status == "passed" else "✗",
                  company.get("name") or row["name"], reason, inn)

            if not dry_run:
                db.save_icp_verdict(conn, inn, company, status, reason)
    except BudgetExhausted as exc:
        logger.warning("%s", exc)

    if not dry_run:
        db.record_requests(conn, PROVIDER, budget.spent - spent_today)
        conn.commit()

    summary = db.icp_summary(conn)
    conn.close()

    logger.info("Проверено %d: прошло ICP %d, отсеяно %d",
                counters["checked"], counters["passed"], counters["rejected"])
    logger.info("Целевой список сейчас: %s",
                ", ".join(f"{k}: {v}" for k, v in sorted(summary.items())))
    return counters


def run(config: dict[str, Any], dry_run: bool = False) -> dict[str, int]:
    """Полный проход этапа: сначала поиск, потом обогащение."""
    discover(config, dry_run=dry_run)
    return enrich(config, dry_run=dry_run)
