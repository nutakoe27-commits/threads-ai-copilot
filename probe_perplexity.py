#!/usr/bin/env python3
"""Разведка Perplexity Sonar на компаниях, по которым письмо не собралось.

Отвечает на два вопроса сразу:

  1. Находит ли Sonar про наши компании то, чего нет на их главной
     странице, — кейсы, клиентов, выступления, публикации.
  2. Можно ли эти находки проверить нашим способом: сходить по названной
     ссылке своим обходчиком и найти там факт дословно.

Второй вопрос важнее первого. Пересказ от Sonar в письмо не попадёт
никогда: письмо утверждает, что факт найден системой, и факт должен быть
проверяемым (DECISIONS.md, Р-025 и Р-028).

Тратит по одному запросу Perplexity на компанию плюс обход названных
страниц. К Checko не обращается вовсе.

    python3 probe_perplexity.py                 # по отсеянным компаниям
    python3 probe_perplexity.py 7703283933 ...  # по конкретным ИНН
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from run import load_env
from src import db, facts as factlib, log
from src.providers.perplexity import (PerplexityClient, PerplexityError,
                                      PerplexityUnavailable)
from src.sources.website import WebsiteFetcher

ROOT = Path(__file__).resolve().parent

SYSTEM = (
    "Ты ищешь публичную информацию о российской компании. Отвечай только "
    "тем, что подтверждено найденными страницами. Если ничего не нашёл — "
    "так и скажи. Не пересказывай общие сведения об отрасли."
)

QUESTION = (
    "Компания «{name}», ИНН {inn}, сайт {site}. "
    "Найди конкретику, которой обычно нет на главной странице: описания "
    "проектов и кейсов, названных заказчиков, отрасли клиентов, публикации "
    "сотрудников, доклады на конференциях, участие в отраслевых рейтингах "
    "и реестрах. Для каждой находки укажи, на какой странице она найдена. "
    "Ничего не додумывай."
)


def main() -> int:
    load_env()
    logger = log.setup()

    inns = [arg.strip() for arg in sys.argv[1:] if arg.strip().isdigit()]

    conn = db.connect()
    if inns:
        placeholders = ",".join("?" for _ in inns)
        rows = conn.execute(
            f"SELECT inn, name, site_url, site, site_facts FROM companies "
            f"WHERE inn IN ({placeholders})", inns).fetchall()
    else:
        rows = conn.execute(
            "SELECT inn, name, site_url, site, site_facts FROM companies "
            "WHERE letter_status = 'thin' ORDER BY name").fetchall()
    conn.close()

    if not rows:
        logger.error("Компании не найдены. Укажите ИНН аргументами или "
                     "сначала прогоните --stage compose")
        return 1

    logger.info("Компаний к разведке: %d. Запросов к Perplexity будет столько же",
                len(rows))

    site_cfg = yaml.safe_load(
        (ROOT / "config" / "icp.yaml").read_text(encoding="utf-8")).get("website", {})
    fetcher = WebsiteFetcher(site_cfg)

    try:
        client = PerplexityClient(site_cfg.get("perplexity", {}))
    except PerplexityUnavailable as exc:
        logger.error("%s", exc)
        return 1

    for row in rows:
        name = row["name"]
        site = row["site_url"] or row["site"] or "—"
        print("\n" + "=" * 72)
        print(f"{name}  (ИНН {row['inn']}, {site})")
        print("=" * 72)

        known = json.loads(row["site_facts"] or "[]")
        print(f"\nЧто уже знаем с их сайта ({len(known)}):")
        for fact in known:
            print(f"  · {fact}")
        if not known:
            print("  (ничего)")

        try:
            answer = client.ask(SYSTEM, QUESTION.format(
                name=name, inn=row["inn"], site=site))
        except PerplexityUnavailable as exc:
            logger.error("%s", exc)
            return 1
        except PerplexityError as exc:
            logger.warning("%s: запрос не удался (%s)", name, exc)
            continue

        print("\n--- Что ответил Sonar ---")
        print(answer["text"][:2500] or "(пусто)")

        urls = answer["urls"]
        print(f"\n--- Названные источники ({len(urls)}) ---")
        for url in urls:
            print(f"  {url}")

        if not urls:
            print("\nСсылок нет — проверять нечего. Для нас такой ответ бесполезен.")
            continue

        # Главная проверка: доходим до страницы сами и смотрим, есть ли там
        # то, что Sonar пересказал. Если нет — факт непроверяем, и в письмо
        # он не попадёт при любых обстоятельствах.
        print("\n--- Проверка: идём по ссылкам своим обходчиком ---")
        checked = 0
        for url in urls[:3]:
            result = fetcher.fetch(url)
            if not result["ok"]:
                print(f"  ✗ {url}\n      не читается: {result['reason']}")
                continue
            checked += 1
            text = result["text"]
            overlap = factlib.coverage(
                factlib.words(answer["text"])[:40], factlib.words(text))
            print(f"  ✓ {url}\n      прочитано {len(text)} символов, "
                  f"пересечение с ответом Sonar: {overlap:.0%}")

        if not checked:
            print("\n  Ни одна страница не открылась нашим обходчиком. "
                  "Значит, Sonar расширяет охват, но не даёт проверяемых фактов.")

    print("\n" + "=" * 72)
    print(f"Запросов к Perplexity потрачено: {client.requests_made}")
    print("При тарифе $5–14 за тысячу запросов это меньше доллара.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
