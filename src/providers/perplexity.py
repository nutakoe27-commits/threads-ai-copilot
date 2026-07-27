"""Perplexity Sonar — поиск страниц про компанию, которых нет на главной.

РОЛЬ В СИСТЕМЕ. Sonar здесь не источник фактов, а указатель, где искать
(DECISIONS.md, Р-028). Причина простая: он отдаёт пересказ со ссылками,
а не текст страницы. Проверить факт цитатой, как мы делаем со своим
обходчиком, по пересказу невозможно — и мы вернулись бы ровно к той
проблеме, из-за которой письма получались пустыми: письмо утверждает
«факт нашла система», а факт непроверяем.

Поэтому порядок такой: Sonar называет адреса → наш обходчик идёт по ним
сам → факт достаётся из скачанного текста и сверяется цитатой.

Для чего он НЕ годится:
  * поиск компаний по критериям — не умеет перечислять, нет пагинации
    и фильтров, на просьбу «дай список» начинает выдумывать;
  * сигналы — про компанию на 30 человек новостей не пишет никто.

Цены (проверено 2026-07-27, docs.perplexity.ai/docs/getting-started/pricing):
$1 за миллион входных и $1 за миллион выходных токенов у модели `sonar`,
плюс сбор за запрос от $5 до $14 за тысячу запросов в зависимости от
глубины поиска.

ВНИМАНИЕ: имена полей в ответе не сверены с боевым API — здесь они
разобраны защитно, по нескольким возможным вариантам. Первый прогон
делается через probe_perplexity.py, который печатает сырой ответ; после
него имена надо зафиксировать, как мы это сделали с Checko.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests

from .. import log

logger = log.get("perplexity")

API_URL = "https://api.perplexity.ai/chat/completions"

# `sonar` — самая дешёвая модель с веб-поиском. Для нашей задачи (найти
# адреса страниц) более дорогие ничего не добавляют: рассуждать не нужно.
DEFAULT_MODEL = "sonar"


class PerplexityError(RuntimeError):
    pass


class PerplexityUnavailable(PerplexityError):
    """Нет ключа. Это не авария, а причина пропустить этап."""


class PerplexityClient:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.api_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
        self.model = config.get("model", DEFAULT_MODEL)
        self.timeout = int(config.get("timeout_seconds", 45))
        # Глубина поиска прямо влияет на цену запроса ($5 против $14
        # за тысячу), а нам нужны ссылки, а не развёрнутый ответ.
        self.search_context = config.get("search_context_size", "low")
        self.requests_made = 0

    def ask(self, system: str, question: str,
            domain_filter: list[str] | None = None) -> dict[str, Any]:
        """Один запрос к Sonar. Возвращает текст ответа и найденные ссылки."""
        if not self.api_key:
            raise PerplexityUnavailable(
                "Не задан PERPLEXITY_API_KEY. Ключ берётся из переменной "
                "окружения или файла .env, в коде его быть не должно"
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
            "web_search_options": {"search_context_size": self.search_context},
        }
        if domain_filter:
            payload["search_domain_filter"] = domain_filter

        try:
            response = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise PerplexityError(f"сеть недоступна: {exc}") from exc

        self.requests_made += 1

        if response.status_code == 401:
            raise PerplexityUnavailable("ключ Perplexity не принят (401)")
        if response.status_code == 429:
            raise PerplexityError("превышен лимит запросов Perplexity (429)")
        if response.status_code >= 400:
            raise PerplexityError(
                f"HTTP {response.status_code}: {response.text[:300]}")

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise PerplexityError(f"ответ не разобрался как JSON: {exc}") from exc

        return {"raw": data, "text": extract_text(data), "urls": extract_urls(data)}


def extract_text(data: dict[str, Any]) -> str:
    """Текст ответа. Формат совместим с OpenAI, но проверяем защитно."""
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        logger.warning("Не нашёл текст ответа в структуре: %s",
                       ", ".join(sorted(data)) if isinstance(data, dict) else type(data))
        return ""


def extract_urls(data: dict[str, Any]) -> list[str]:
    """Ссылки на источники.

    В разных версиях API это либо `citations` со строками, либо
    `search_results` со словарями. Разбираем оба варианта, чтобы смена
    формата не роняла прогон молча.
    """
    urls: list[str] = []

    for value in data.get("citations") or []:
        if isinstance(value, str):
            urls.append(value)
        elif isinstance(value, dict) and value.get("url"):
            urls.append(str(value["url"]))

    for item in data.get("search_results") or []:
        if isinstance(item, dict) and item.get("url"):
            urls.append(str(item["url"]))

    # Порядок важен: Sonar ставит релевантные источники первыми.
    seen: set[str] = set()
    ordered = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered
