"""Этап 2: построение целевого списка компаний по критериям ICP.

Два прохода, оба возобновляемые:

  1. discover — поиск кандидатов по ОКВЭД и регионам. Пишет только ИНН и
     название. Останавливается, как только накоплен буфер работы на
     несколько дней вперёд, и запоминает, на какой странице остановился.
  2. enrich   — проверка кандидатов. Двухступенчатая, чтобы экономить запросы:
       ступень 1 (1 запрос): карточка. ОКВЭД, штат, возраст, активность.
                             Не прошло — на этом всё, финансы не запрашиваем.
       ступень 2 (1 запрос): финансы. Только для прошедших ступень 1.

ПРО ОБЪЁМ. Кандидатов десятки тысяч: только по ОКВЭД 62.01 в Москве Checko
показывает около 14 000 компаний. Перебрать всех при 100 запросах в сутки
невозможно, и не нужно: при потребности в 20 письмах в день достаточно
проверять ~50 компаний в сутки. Список не обязан быть полным — он обязан
давать стабильный приток. Поэтому discover набирает буфер и останавливается,
а enrich каждый день откусывает от него столько, сколько позволяет бюджет.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from . import db, log
from .providers.checko import BudgetExhausted, CheckoClient, CheckoError, RequestBudget

logger = log.get("targets")

PROVIDER = "checko"


def _years_since(date_str: str) -> float | None:
    """Возраст компании в годах по дате регистрации (формат ГГГГ-ММ-ДД)."""
    if not date_str or len(date_str) < 10:
        return None
    try:
        year, month, day = (int(part) for part in date_str[:10].split("-"))
        return (date.today() - date(year, month, day)).days / 365.25
    except ValueError:
        return None


def judge_profile(company: dict[str, Any], criteria: dict[str, Any]) -> tuple[bool, str]:
    """Ступень 1: всё, что видно из карточки. Финансы здесь не участвуют.

    Возвращает (проходит ли дальше, причина). Причина сохраняется в базу
    человеческим языком, чтобы потом можно было понять, не слишком ли жёсткий
    критерий, не перезапрашивая API.
    """
    if not company.get("active", True):
        return False, "компания не действует"

    exclude = [str(code) for code in criteria.get("exclude_okved", [])]
    all_okved = [company.get("okved") or ""] + list(company.get("extra_okved") or [])
    for code in all_okved:
        for bad in exclude:
            if code.startswith(bad):
                return False, f"ОКВЭД {code} в списке исключений (кадровые агентства)"

    staff = company.get("staff")
    staff_min, staff_max = criteria.get("staff_min"), criteria.get("staff_max")
    if staff is None:
        return False, "нет данных о численности персонала"
    if staff_min is not None and staff < staff_min:
        return False, f"штат {staff:.0f} меньше порога {staff_min}"
    if staff_max is not None and staff > staff_max:
        return False, f"штат {staff:.0f} больше порога {staff_max}"

    age = _years_since(company.get("registration_date") or "")
    min_age = criteria.get("min_age_years")
    if min_age is not None and age is not None and age < min_age:
        return False, f"возраст {age:.1f} года меньше порога {min_age}"

    return True, f"профиль подходит: ОКВЭД {company.get('okved')}, штат {staff:.0f}"


def judge_finances(finances: dict[str, Any], criteria: dict[str, Any],
                   staff: float | None = None) -> tuple[bool, str]:
    """Ступень 2: выручка. Запрашивается только для прошедших ступень 1."""
    revenue = finances.get("revenue")
    revenue_min, revenue_max = criteria.get("revenue_min"), criteria.get("revenue_max")

    if revenue is None:
        return False, "нет данных о выручке"
    if revenue_min is not None and revenue < revenue_min:
        return False, f"выручка {revenue / 1e6:.1f} млн меньше порога"
    if revenue_max is not None and revenue > revenue_max:
        return False, f"выручка {revenue / 1e6:.1f} млн больше порога"

    # Выручка на сотрудника отделяет разработку на заказ от перепродажи
    # железа и лицензий, которые прячутся под тем же ОКВЭД 62.01.
    per_employee: float | None = None
    if staff:
        per_employee = revenue / staff
        low = criteria.get("revenue_per_employee_min")
        high = criteria.get("revenue_per_employee_max")
        if low is not None and per_employee < low:
            return False, (f"выручка на сотрудника {per_employee / 1e6:.1f} млн — "
                           f"слишком мало для разработки")
        if high is not None and per_employee > high:
            return False, (f"выручка на сотрудника {per_employee / 1e6:.1f} млн — "
                           f"похоже на перепродажу, а не разработку")

    parts = [f"выручка {revenue / 1e6:.1f} млн"]
    if per_employee:
        parts.append(f"{per_employee / 1e6:.1f} млн на сотрудника")

    change = finances.get("revenue_change_pct")
    if change is None:
        parts.append("динамика неизвестна")
    else:
        parts.append(f"динамика {change:+.1f}%")
    return True, ", ".join(parts)


def describe_signal(change_pct: float | None, scoring: dict[str, Any]) -> tuple[str, str]:
    """Превращает динамику выручки в силу сигнала и строку «почему она здесь».

    Пока событийный источник не найден, приоритет в утреннем списке задаётся
    именно этим — см. DECISIONS.md, Р-017.
    """
    if change_pct is None:
        return "normal", "динамика выручки неизвестна"

    strong_decline = float(scoring.get("strong_decline_pct", -10.0))
    flat_from = float(scoring.get("flat_from_pct", -10.0))
    flat_to = float(scoring.get("flat_to_pct", 5.0))

    if change_pct <= strong_decline:
        return "strong", f"выручка упала на {abs(change_pct):.0f}% год к году"
    if flat_from < change_pct <= flat_to:
        # Округление до целых даёт уродливое «-0%», поэтому near-zero
        # описываем словами, а не числом.
        if abs(change_pct) < 1:
            return "strong", "выручка не изменилась год к году"
        return "strong", f"выручка почти не изменилась ({change_pct:+.0f}% год к году)"
    return "normal", f"выручка выросла на {change_pct:.0f}% год к году"


def discover(config: dict[str, Any], dry_run: bool = False) -> dict[str, int]:
    """Проход 1: набрать буфер кандидатов, запомнив, где остановились."""
    provider_cfg = config.get("provider", {})
    target_cfg = config.get("target_list", {})
    criteria = config.get("criteria", {})
    buffer_target = int(provider_cfg.get("candidate_buffer", 500))
    max_search_requests = int(provider_cfg.get("discover_per_run", 10))
    start_fraction = float(target_cfg.get("start_page_fraction", 0.0))
    min_age = criteria.get("min_age_years")

    conn = db.connect()
    spent_today = db.requests_spent_today(conn, PROVIDER)
    budget = RequestBudget(int(provider_cfg.get("daily_request_budget", 100)), spent_today)

    buffered = db.count_unchecked(conn)
    logger.info("Непроверенных кандидатов в буфере: %d (цель %d). "
                "Бюджет запросов сегодня: осталось %d", buffered, buffer_target, budget.left)

    counters = {"found": 0, "new": 0, "requests": 0,
                "skipped_inactive": 0, "skipped_young": 0}
    if buffered >= buffer_target:
        logger.info("Буфер полон, поиск не нужен — сразу к проверке")
        conn.close()
        return counters

    try:
        client = CheckoClient(provider_cfg, budget)
    except CheckoError as exc:
        logger.error("%s", exc)
        conn.close()
        return counters

    try:
        for okved in target_cfg.get("okved", []):
            for region in target_cfg.get("regions", []):
                if buffered >= buffer_target or counters["requests"] >= max_search_requests:
                    break

                cursor = db.get_cursor(conn, okved, region["code"])
                if cursor and cursor["exhausted"]:
                    continue
                page = int(cursor["next_page"]) if cursor else 1
                known_total = int(cursor["total_pages"]) if cursor and cursor["total_pages"] \
                    else None

                # Выдача отсортирована по ОГРН, а в ОГРН зашит год регистрации.
                # Поэтому первые страницы — это компании 90-х и начала 2000-х,
                # среди которых много спящих пустышек. Начинаем не с начала,
                # а со страницы, где регистрации уже ближе к нашему времени.
                if known_total and page == 1 and start_fraction > 0:
                    page = max(1, int(known_total * start_fraction))
                    logger.info("ОКВЭД %s / %s: стартуем со страницы %d из %d "
                                "(ранние страницы — компании 90-х)",
                                okved, region["name"], page, known_total)

                try:
                    payload = client.search_by_okved(
                        okved, region["code"], target_cfg.get("opf"),
                        bool(target_cfg.get("only_active", True)), page,
                    )
                except CheckoError as exc:
                    logger.error("ОКВЭД %s / %s стр. %d пропущена: %s",
                                 okved, region["name"], page, exc)
                    continue

                counters["requests"] += 1
                data = payload.get("data") or {}
                total_pages = int(data.get("СтрВсего") or 0) or None
                records = CheckoClient.parse_search_results(payload)

                logger.info("ОКВЭД %s / %s: страница %d из %s — %d компаний",
                            okved, region["name"], page, total_pages or "?", len(records))

                for record in records:
                    counters["found"] += 1

                    # Бесплатные отсевы: статус и возраст видны прямо в выдаче
                    # поиска, тратить на них запрос карточки не нужно.
                    if record["status"] and not record["status"].lower().startswith("действ"):
                        counters["skipped_inactive"] += 1
                        continue
                    age = _years_since(record["reg_date"])
                    if min_age is not None and age is not None and age < min_age:
                        counters["skipped_young"] += 1
                        continue

                    if dry_run:
                        continue
                    if db.add_candidate(conn, record["inn"], record["name"], region["name"]):
                        counters["new"] += 1
                        buffered += 1

                exhausted = not records or (total_pages is not None and page >= total_pages)

                # На самом первом запросе мы ещё не знали, сколько всего
                # страниц, поэтому взяли первую. Теперь знаем — и следующий
                # прогон должен начать не со второй страницы, а сразу
                # с нужной доли списка.
                next_page = page + 1
                if page == 1 and total_pages and start_fraction > 0:
                    next_page = max(2, int(total_pages * start_fraction))

                if not dry_run:
                    db.save_cursor(conn, okved, region["code"], next_page, total_pages, exhausted)
                    conn.commit()
    except BudgetExhausted as exc:
        logger.warning("%s", exc)

    if not dry_run:
        db.record_requests(conn, PROVIDER, budget.spent - spent_today)
        conn.commit()
    conn.close()

    logger.info(
        "Поиск: страниц запрошено %d, найдено %d, новых кандидатов %d "
        "(бесплатно отсеяно: недействующих %d, слишком молодых %d)",
        counters["requests"], counters["found"], counters["new"],
        counters["skipped_inactive"], counters["skipped_young"],
    )
    return counters


def enrich(config: dict[str, Any], dry_run: bool = False) -> dict[str, int]:
    """Проход 2: двухступенчатая проверка кандидатов."""
    provider_cfg = config.get("provider", {})
    criteria = config.get("criteria", {})
    scoring = config.get("signal_scoring", {})

    conn = db.connect()
    spent_today = db.requests_spent_today(conn, PROVIDER)
    budget = RequestBudget(int(provider_cfg.get("daily_request_budget", 100)), spent_today)

    # Каждая компания стоит 1–2 запроса, поэтому берём с запасом по бюджету.
    limit = min(int(provider_cfg.get("enrich_per_run", 40)), max(0, budget.left // 2))
    if limit <= 0:
        logger.warning("Бюджета на проверку не осталось (%d запросов). Продолжим завтра",
                       budget.left)
        conn.close()
        return {"checked": 0, "passed": 0, "rejected": 0}

    candidates = db.unchecked_candidates(conn, limit)
    if not candidates:
        logger.info("Все известные кандидаты проверены. Запустите discover за новыми")
        conn.close()
        return {"checked": 0, "passed": 0, "rejected": 0}

    logger.info("Компаний к проверке: %d (бюджет позволяет ~%d)",
                len(candidates), budget.left // 2)

    counters = {"checked": 0, "passed": 0, "rejected": 0, "saved_requests": 0}
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
                company = CheckoClient.parse_company(client.company(inn))
            except BudgetExhausted:
                raise
            except CheckoError as exc:
                logger.error("ИНН %s пропущен: %s", inn, exc)
                continue

            counters["checked"] += 1
            fields: dict[str, Any] = dict(company)

            ok, reason = judge_profile(company, criteria)
            if not ok:
                # Финансы не запрашиваем — экономим запрос.
                counters["rejected"] += 1
                counters["saved_requests"] += 1
                logger.debug("✗ %s — %s", company.get("name") or row["name"], reason)
                if not dry_run:
                    db.save_icp_verdict(conn, inn, fields, "rejected", reason)
                continue

            try:
                finances = CheckoClient.parse_finances(client.finances(inn))
            except BudgetExhausted:
                raise
            except CheckoError as exc:
                logger.warning("ИНН %s: финансы не получены (%s), решаем без них", inn, exc)
                finances = {}

            fields.update(finances)
            ok, fin_reason = judge_finances(finances, criteria, company.get("staff"))
            status = "passed" if ok else "rejected"
            counters[status] += 1

            if ok:
                strength, why = describe_signal(finances.get("revenue_change_pct"), scoring)
                reason = f"{fin_reason}. {why}"
                logger.info("✓ %s — %s [%s]", company.get("name") or row["name"], reason, strength)
            else:
                reason = fin_reason
                logger.debug("✗ %s — %s", company.get("name") or row["name"], reason)

            if not dry_run:
                db.save_icp_verdict(conn, inn, fields, status, reason)
    except BudgetExhausted as exc:
        logger.warning("%s", exc)

    if not dry_run:
        db.record_requests(conn, PROVIDER, budget.spent - spent_today)
        conn.commit()

    summary = db.icp_summary(conn)
    remaining = db.count_unchecked(conn)
    conn.close()

    logger.info("Проверено %d: прошло ICP %d, отсеяно %d (сэкономлено запросов на финансы: %d)",
                counters["checked"], counters["passed"], counters["rejected"],
                counters["saved_requests"])
    logger.info("Целевой список: %s. Ждут проверки: %d",
                ", ".join(f"{k}: {v}" for k, v in sorted(summary.items())), remaining)
    return counters


def rejudge(config: dict[str, Any], dry_run: bool = False) -> dict[str, int]:
    """Пересматривает вердикты по уже скачанным данным. Ноль запросов к API.

    Нужно после каждой правки критериев в icp.yaml: штат, выручка и ОКВЭД
    уже лежат в базе, платить за них второй раз незачем. Позволяет крутить
    пороги сколько угодно, не расходуя суточный лимит.
    """
    criteria = config.get("criteria", {})
    conn = db.connect()

    rows = conn.execute(
        "SELECT * FROM companies WHERE checked_at IS NOT NULL"
    ).fetchall()
    if not rows:
        logger.info("Проверенных компаний пока нет — сначала запустите enrich")
        conn.close()
        return {"rejudged": 0, "changed": 0}

    counters = {"rejudged": 0, "changed": 0, "passed": 0, "rejected": 0}
    for row in rows:
        company = {
            "okved": row["okved"] or "",
            # Дополнительные ОКВЭД в базе не храним — их проверка была на
            # этапе enrich и повторно не выполняется.
            "extra_okved": [],
            "staff": row["staff"],
            "registration_date": row["registration_date"] or "",
            "active": True,
        }
        ok, reason = judge_profile(company, criteria)
        if ok:
            finances = {
                "revenue": row["revenue"],
                "revenue_change_pct": row["revenue_change_pct"],
            }
            ok, reason = judge_finances(finances, criteria, row["staff"])

        status = "passed" if ok else "rejected"
        counters["rejudged"] += 1
        counters[status] += 1
        if status != row["icp_status"]:
            counters["changed"] += 1
            logger.info("%s: %s → %s (%s)", row["name"], row["icp_status"], status, reason)

        if not dry_run:
            conn.execute(
                "UPDATE companies SET icp_status = ?, icp_reason = ? WHERE inn = ?",
                (status, reason, row["inn"]),
            )

    if not dry_run:
        conn.commit()
    conn.close()

    logger.info("Пересмотрено записей: %d. Прошло %d, отсеяно %d, изменилось решений %d. "
                "Запросов к API потрачено: 0",
                counters["rejudged"], counters["passed"],
                counters["rejected"], counters["changed"])
    return counters


def run(config: dict[str, Any], dry_run: bool = False) -> dict[str, int]:
    """Полный проход этапа: сначала добрать кандидатов, потом проверить."""
    discover(config, dry_run=dry_run)
    return enrich(config, dry_run=dry_run)
