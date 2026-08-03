"""Схождение сигналов: один балл вместо сортировки по одному признаку.

ПОЧЕМУ БАЛЛ, А НЕ СОРТИРОВКА. Раньше утренний список сортировался
последовательно: сначала те, кто нанимает, потом продуктовые, потом
по динамике выручки. Пока признак был один, это работало.

Обзоры intent-data сходятся в одном: **один сигнал почти никогда не значит
ничего, значение имеет схождение нескольких на одной компании.** Компания
с тремя слабыми признаками интереснее компании с одним сильным.

Поэтому теперь считается сумма. Каждый признак даёт свой вес, веса лежат
в конфиге и правятся без кода (DECISIONS.md, Р-046).

ЧТО ЭТО НЕ ЗНАЧИТ. Балл не решает, писать компании или нет. Он решает
только порядок в списке. Отсев делают критерии ICP и порог фактов —
там решения бинарные и объяснимые, а балл по природе размытый.
"""

from __future__ import annotations

import json
from typing import Any

from . import log

logger = log.get("scoring")

# Веса по умолчанию. Порядок величин важнее точных чисел: событие весит
# больше состояния, состояние больше догадки.
DEFAULT_WEIGHTS: dict[str, float] = {
    # Событие: происходит сегодня, завтра может не повториться.
    "hiring_strong": 5.0,      # ищут людей прямо на холодные звонки
    "hiring_normal": 3.0,      # ищут в продажи вообще
    "registry_software": 4.0,  # попали в реестр отечественного ПО

    # Состояние: верно и вчера, и через полгода.
    "pays_for_traffic": 3.0,   # стоит коллтрекинг или Roistat
    "captures_leads": 1.5,     # виджеты захвата
    "has_analytics": 1.0,      # хотя бы считают трафик
    "product_company": 2.0,    # у них ICP уже, система нагляднее
    "revenue_dropped": 2.0,    # выручка просела за год
    "revenue_flat": 1.0,       # стагнация
    "accredited": 0.5,         # аккредитация — скорее подтверждение, что IT

    # Штрафы.
    "no_marketing_traces": -1.0,   # ни счётчиков, ни виджетов
    "wrong_desk_email": -1.0,      # адрес тендерного отдела или кадров
}

# Что показать человеку рядом с баллом.
LABELS: dict[str, str] = {
    "hiring_strong": "ищут людей на холодный поиск клиентов",
    "hiring_normal": "нанимают в отдел продаж",
    "registry_software": "продукт в реестре отечественного ПО",
    "pays_for_traffic": "платит за трафик и меряет его",
    "captures_leads": "ловит входящие виджетами",
    "has_analytics": "считает трафик",
    "product_company": "продуктовая компания, круг покупателей узкий",
    "revenue_dropped": "выручка просела за год",
    "revenue_flat": "выручка не растёт",
    "accredited": "аккредитованная IT-компания",
    "no_marketing_traces": "следов маркетинга на сайте нет",
    "wrong_desk_email": "известен только адрес не того отдела",
}


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (json.JSONDecodeError, TypeError):
        return default


def signals_of(row: Any) -> list[str]:
    """Какие признаки есть у компании. Возвращает список ключей весов."""
    from . import contacts

    found: list[str] = []

    hiring = _loads(row["site_hiring"] if "site_hiring" in row.keys() else None, [])
    if hiring:
        strengths = {item.get("strength") for item in hiring if isinstance(item, dict)}
        found.append("hiring_strong" if "strong" in strengths else "hiring_normal")

    flags = _loads(row["registry_flags"] if "registry_flags" in row.keys() else None, [])
    if "software" in flags:
        found.append("registry_software")
    if "accredited" in flags:
        found.append("accredited")

    stack = _loads(row["tech_stack"] if "tech_stack" in row.keys() else None, {})
    if stack.get("paid"):
        found.append("pays_for_traffic")
    if stack.get("capture"):
        found.append("captures_leads")
    if stack.get("analytics"):
        found.append("has_analytics")
    if stack and not any(stack.get(group) for group in ("paid", "capture", "analytics")):
        found.append("no_marketing_traces")

    if row["site_type"] == "product":
        found.append("product_company")

    change = row["revenue_change_pct"]
    if change is not None:
        if change <= -10:
            found.append("revenue_dropped")
        elif -10 < change <= 5:
            found.append("revenue_flat")

    email = row["contact_email"] if "contact_email" in row.keys() else ""
    if email:
        kind, _ = contacts.classify(email)
        if kind in ("wrong_desk", "free"):
            found.append("wrong_desk_email")

    return found


def score(row: Any, weights: dict[str, float] | None = None) -> tuple[float, list[str]]:
    """Считает балл и возвращает его вместе с человеческим объяснением."""
    table = {**DEFAULT_WEIGHTS, **(weights or {})}
    found = signals_of(row)
    total = sum(table.get(key, 0.0) for key in found)

    # Объяснение сортируем по весу: сверху то, что дало больше всего.
    reasons = sorted(found, key=lambda key: -abs(table.get(key, 0.0)))
    return round(total, 1), [LABELS.get(key, key) for key in reasons]


def recompute(config: dict[str, Any], dry_run: bool = False) -> dict[str, int]:
    """Пересчитывает баллы всем компаниям с готовым письмом.

    Запросов наружу не делает вообще: считает по тому, что уже в базе.
    Поэтому запускать можно сколько угодно раз, в том числе после правки
    весов в конфиге.
    """
    from . import db

    weights = config.get("scoring", {}).get("weights", {})
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM companies WHERE icp_status = 'passed'").fetchall()

    counters = {"scored": 0}
    distribution: dict[str, int] = {}

    for row in rows:
        value, reasons = score(row, weights)
        counters["scored"] += 1
        bucket = f"{int(value // 2) * 2}–{int(value // 2) * 2 + 2}"
        distribution[bucket] = distribution.get(bucket, 0) + 1

        if not dry_run:
            conn.execute(
                "UPDATE companies SET signal_score = ?, signal_reasons = ? WHERE inn = ?",
                (value, json.dumps(reasons, ensure_ascii=False), row["inn"]))

    if not dry_run:
        conn.commit()
    conn.close()

    logger.info("Баллы пересчитаны: %d компаний", counters["scored"])
    if distribution:
        logger.info("--- Распределение баллов:")
        for bucket, count in sorted(distribution.items(),
                                    key=lambda item: -float(item[0].split("–")[0])):
            logger.info("      %-8s %3d", bucket, count)
    return counters
