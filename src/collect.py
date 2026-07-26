"""Этап 1: наблюдатель за сигналами.

Тянет свежие вакансии из «Работы России», отбирает те, что похожи на боль
в лидгене, и складывает в SQLite. Всё, что решает «подходит / не подходит»,
задано в config/signals.yaml — код менять для этого не нужно.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from typing import Any

from . import db, log
from .sources.trudvsem import TrudvsemClient, TrudvsemError

logger = log.get("collect")


def normalize(text: str) -> str:
    """Приводит текст к виду, пригодному для поиска подстрок.

    Убираем HTML-теги (в поле duty у trudvsem часто лежит размеченный текст),
    схлопываем пробелы, приводим к нижнему регистру и заменяем «ё» на «е» —
    иначе «холодные звонки» не найдётся в тексте с «холодныё» опечаткой
    и в разном регистре.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"<[^>]+>", " ", text)          # снимаем HTML-разметку
    text = text.replace("ё", "е").replace("Ё", "Е")
    text = re.sub(r"\s+", " ", text)
    return text.lower().strip()


# Скомпилированные шаблоны ключевых слов — чтобы не пересобирать их на каждой вакансии.
_pattern_cache: dict[str, re.Pattern[str]] = {}


def contains_keyword(text: str, keyword: str) -> bool:
    """Ищет ключевое слово с привязкой к началу слова.

    Простой поиск подстроки здесь не годится: стоп-слово «водитель» находится
    внутри «рукоВОДИТЕЛЬ отдела продаж», и вакансия РОПа молча отсеивалась.

    Поэтому требуем, чтобы совпадение начиналось с границы слова
    (lookbehind `(?<!\\w)`), но НЕ требуем границы в конце — иначе перестанут
    работать основы вроде «лидогенерац», которые должны ловить и
    «лидогенерацию», и «лидогенератора».
    """
    pattern = _pattern_cache.get(keyword)
    if pattern is None:
        pattern = re.compile(r"(?<!\w)" + re.escape(normalize(keyword)))
        _pattern_cache[keyword] = pattern
    return pattern.search(text) is not None


def match_keywords(job_name: str, duty: str, keywords: dict[str, list[str]]) -> tuple[str, list[str]] | None:
    """Определяет силу сигнала и список совпавших слов.

    Возвращает None, если вакансия нам не подходит.

    Логика:
      1. Стоп-слова в названии должности — сразу мимо (розница, B2C, HR).
      2. Сильное слово где угодно (название или текст) — strong.
      3. Обычное слово в названии должности — normal.
         В тексте вакансии обычные слова НЕ ищем: почти в каждой вакансии
         встретится слово «продаж», и список утонет в мусоре.
    """
    job = normalize(job_name)
    body = normalize(duty)
    full = f"{job} {body}"

    for stop in keywords.get("exclude_job", []):
        if contains_keyword(job, stop):
            return None

    strong_hits = [kw for kw in keywords.get("strong", []) if contains_keyword(full, kw)]
    if strong_hits:
        return "strong", strong_hits

    normal_hits = [kw for kw in keywords.get("normal", []) if contains_keyword(job, kw)]
    if normal_hits:
        return "normal", normal_hits

    return None


def excerpt_around(text: str, keyword: str, width: int = 200) -> str:
    """Вырезает фрагмент текста вокруг найденного слова — для утреннего списка.

    Нужно, чтобы вы одним взглядом видели, на каком основании компания
    попала в список, не открывая вакансию.
    """
    normalized = normalize(text)
    position = normalized.find(normalize(keyword))
    if position == -1:
        return normalized[:width].strip()
    start = max(0, position - width // 2)
    fragment = normalized[start:start + width].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + width < len(normalized) else ""
    return f"{prefix}{fragment}{suffix}"


def run(config: dict[str, Any], dry_run: bool = False, raw: bool = False) -> dict[str, int]:
    """Основной проход этапа 1. Возвращает счётчики для сводки."""
    source_cfg = config.get("source", {})
    keywords = config.get("keywords", {})
    filters = config.get("filters", {})

    client = TrudvsemClient(source_cfg)
    conn = db.connect()
    run_id = db.start_run(conn, "collect")
    stoplist = db.load_stoplist()

    counters = {"fetched": 0, "matched": 0, "new_signals": 0, "skipped_hr": 0,
                "skipped_no_inn": 0, "skipped_stoplist": 0, "skipped_spec": 0,
                "duplicates": 0}
    raw_dumped = False

    # Диагностика: какие отрасли и ключевые слова реально дают совпадения.
    # Нужно, чтобы решать по данным, а не на глаз: подходит ли нам источник
    # и какие слова тащат мусор.
    spec_stats: Counter[str] = Counter()
    keyword_stats: Counter[str] = Counter()

    spec_include = [s.lower() for s in filters.get("specialisation_include", []) or []]
    spec_exclude = [s.lower() for s in filters.get("specialisation_exclude", []) or []]

    for region in source_cfg.get("regions", []) or config.get("regions", []):
        code, name = region["code"], region["name"]
        logger.info("--- Регион: %s (%s)", name, code)

        try:
            for raw_vacancy in client.fetch_region(code, name):
                counters["fetched"] += 1

                # Режим отладки: показываем сырую запись целиком, чтобы можно
                # было сверить имена полей с реальным ответом API.
                if raw and not raw_dumped:
                    logger.info(
                        "СЫРОЙ ОТВЕТ (первая вакансия):\n%s",
                        json.dumps(raw_vacancy, ensure_ascii=False, indent=2)[:3000],
                    )
                    raw_dumped = True

                vacancy = TrudvsemClient.parse(raw_vacancy)

                if filters.get("skip_hr_agencies", True) and vacancy["hr_agency"]:
                    counters["skipped_hr"] += 1
                    continue

                if filters.get("require_inn", True) and not vacancy["inn"]:
                    counters["skipped_no_inn"] += 1
                    continue

                if vacancy["inn"].lower() in stoplist:
                    counters["skipped_stoplist"] += 1
                    continue

                verdict = match_keywords(vacancy["job_name"], vacancy["duty"], keywords)
                if verdict is None:
                    continue

                strength, hits = verdict

                # Отраслевой фильтр по классификатору trudvsem. Пустые списки
                # в конфиге = фильтр выключен (нужно, чтобы сначала посмотреть
                # статистику и узнать реальные названия отраслей).
                spec = (vacancy.get("specialisation") or "").lower()
                if spec_include and not any(s in spec for s in spec_include):
                    counters["skipped_spec"] += 1
                    continue
                if spec_exclude and any(s in spec for s in spec_exclude):
                    counters["skipped_spec"] += 1
                    continue

                counters["matched"] += 1
                spec_stats[vacancy.get("specialisation") or "(не указана)"] += 1
                keyword_stats.update(hits)

                if dry_run:
                    logger.info(
                        "[dry-run] %s | %s | %s | %s",
                        strength, vacancy["company_name"], vacancy["job_name"], ", ".join(hits),
                    )
                    continue

                db.upsert_company(conn, {
                    "inn": vacancy["inn"],
                    "name": vacancy["company_name"],
                    "region": vacancy["region"] or name,
                    "site": vacancy["company_site"],
                    "email": vacancy["company_email"],
                    "phone": vacancy["company_phone"],
                })

                is_new = db.insert_signal(conn, {
                    "vacancy_id": vacancy["vacancy_id"],
                    "inn": vacancy["inn"],
                    "company_name": vacancy["company_name"],
                    "job_name": vacancy["job_name"],
                    "region": vacancy["region"] or name,
                    "url": vacancy["url"],
                    "created_date": vacancy["created_date"],
                    "strength": strength,
                    "matched": "; ".join(hits),
                    "excerpt": excerpt_around(vacancy["duty"], hits[0]),
                    "specialisation": vacancy.get("specialisation", ""),
                })

                if is_new:
                    counters["new_signals"] += 1
                    logger.info(
                        "+ %s | %s — %s (%s)",
                        strength.upper(), vacancy["company_name"],
                        vacancy["job_name"], ", ".join(hits),
                    )
                else:
                    counters["duplicates"] += 1

        except TrudvsemError as exc:
            # Падение одного региона не должно ронять весь прогон.
            logger.error("Регион %s пропущен: %s", name, exc)
            continue

        if not dry_run:
            conn.commit()

    db.finish_run(conn, run_id, problems=len(log.problems()), **{
        k: v for k, v in counters.items() if k in ("fetched", "matched", "new_signals")
    })
    conn.commit()
    conn.close()

    logger.info(
        "Итог сбора: получено %d, подошло %d, новых %d, дублей %d "
        "(отсеяно: кадровых агентств %d, без ИНН %d, по стоп-листу %d, по отрасли %d)",
        counters["fetched"], counters["matched"], counters["new_signals"],
        counters["duplicates"], counters["skipped_hr"],
        counters["skipped_no_inn"], counters["skipped_stoplist"], counters["skipped_spec"],
    )

    _log_diagnostics(spec_stats, keyword_stats)
    return counters


def _log_diagnostics(spec_stats: Counter[str], keyword_stats: Counter[str]) -> None:
    """Показывает, откуда берутся совпадения.

    Две таблицы отвечают на два разных вопроса:
      * отрасли — есть ли в источнике вообще наш ICP;
      * ключевые слова — какие из них тащат мусор и подлежат правке.
    """
    if not spec_stats:
        return

    logger.info("--- Отрасли среди совпавших вакансий (классификатор trudvsem):")
    for name, count in spec_stats.most_common(15):
        logger.info("      %4d  %s", count, name)

    logger.info("--- Сработавшие ключевые слова:")
    for name, count in keyword_stats.most_common(15):
        logger.info("      %4d  %s", count, name)

    logger.info(
        "Подсказка: скопируйте нужные названия отраслей в "
        "filters.specialisation_include в config/signals.yaml — это отсечёт "
        "розницу и банки бесплатно, до платных запросов в Checko"
    )
