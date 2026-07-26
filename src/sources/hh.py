"""Клиент поиска вакансий hh.ru. НЕ ПОДКЛЮЧЁН К КОНВЕЙЕРУ.

    ┌──────────────────────────────────────────────────────────────────┐
    │ СТАТУС НА 2026-07-26: НЕРАБОЧИЙ ПУТЬ.                            │
    │ Проверка с боевого VPS вернула HTTP 403 на публичный поиск       │
    │ вакансий. Выясняется, закрыт эндпоинт для всех или заблокирован  │
    │ IP дата-центра. Модуль дописан до этой проверки и оставлен как   │
    │ есть — в run.py не подключён, в конфиге не выбирается.           │
    │                                                                  │
    │ Не подключать, пока не будет ясности. Если окажется, что дело    │
    │ в блокировке IP — обход блокировки не рассматривается: это       │
    │ другой класс риска, чем тот, что был принят владельцем.          │
    └──────────────────────────────────────────────────────────────────┘

Решение об использовании hh как источника сигнала было принято владельцем
осознанно, с принятием риска по условиям использования. Дальнейшая судьба
модуля — в DECISIONS.md, запись Р-015.

Ключевая идея: не выкачивать базу, а пользоваться полнотекстовым поиском
самого hh. Мы задаём точные фразы («холодные звонки»), hh возвращает уже
отобранное. Это десятки запросов вместо тысяч.

Ограничения API, заложенные в код:
  * обязателен заголовок User-Agent, без него запросы отклоняются;
  * per_page максимум 100;
  * суммарно не более 2000 вакансий на один поисковый запрос — обходим
    сужением окна дат и разбивкой по регионам;
  * ИНН работодателя hh НЕ отдаёт, только название и id. ИНН выясняется
    отдельно через Checko (src/providers/checko.py).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import requests

from .. import log

logger = log.get("hh")

# hh требует осмысленный User-Agent с контактом.
DEFAULT_USER_AGENT = "leadgen/0.1 (b2b research)"

MAX_RESULTS_PER_QUERY = 2000  # жёсткое ограничение hh


def _pick(data: dict[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        if name in data and data[name] not in (None, ""):
            return data[name]
    return default


class HhError(RuntimeError):
    pass


class HhClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.base_url = config.get("base_url", "https://api.hh.ru").rstrip("/")
        self.page_size = min(int(config.get("page_size", 100)), 100)
        self.timeout = int(config.get("timeout_seconds", 30))
        self.retries = int(config.get("retries", 3))
        self.lookback_days = int(config.get("lookback_days", 3))
        self.pause = float(config.get("pause_seconds", 0.5))

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.get("user_agent", DEFAULT_USER_AGENT),
            "Accept": "application/json",
        })

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)

                if response.status_code == 200:
                    return response.json()

                if response.status_code in (401, 403):
                    # Отдельное сообщение: это не сетевая проблема, а закрытый
                    # доступ. Повторять бессмысленно.
                    raise HhError(
                        f"HTTP {response.status_code}: hh отклонил запрос. "
                        f"Похоже, публичный поиск закрыт или требует авторизации. "
                        f"Ответ: {response.text[:200]}"
                    )
                if response.status_code == 429:
                    # Слишком часто — ждём дольше обычного.
                    wait = 5 * attempt
                    logger.warning("hh: слишком много запросов, пауза %dс", wait)
                    time.sleep(wait)
                    last_error = HhError("HTTP 429")
                    continue
                if 400 <= response.status_code < 500:
                    raise HhError(f"HTTP {response.status_code}: {response.text[:200]}")

                last_error = HhError(f"HTTP {response.status_code}")
            except (requests.RequestException, ValueError) as exc:
                last_error = exc

            if attempt < self.retries:
                delay = 2 ** (attempt - 1)
                logger.warning("hh: попытка %d/%d не удалась (%s), повтор через %dс",
                               attempt, self.retries, last_error, delay)
                time.sleep(delay)

        raise HhError(f"Не удалось получить {url}: {last_error}")

    def search(self, phrase: str, area_id: str, area_name: str,
               search_field: str | None = None) -> Iterator[dict[str, Any]]:
        """Ищет вакансии по точной фразе в одном регионе.

        `search_field="name"` ограничивает поиск названием должности — так
        ищем обычные сигналы («менеджер по продажам»), чтобы не собирать
        каждую вакансию, где слово «продажи» встретилось в тексте.
        """
        date_from = (datetime.now(timezone.utc) - timedelta(days=self.lookback_days)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )

        params: dict[str, Any] = {
            # Кавычки просят hh искать точную фразу, а не отдельные слова.
            "text": f'"{phrase}"',
            "area": area_id,
            "per_page": self.page_size,
            "date_from": date_from,
            "order_by": "publication_time",
        }
        if search_field:
            params["search_field"] = search_field

        page = 0
        found: int | None = None

        while True:
            params["page"] = page
            payload = self._get("/vacancies", params)

            if found is None:
                found = int(payload.get("found") or 0)
                if found:
                    logger.info("hh «%s» / %s: найдено %d", phrase, area_name, found)
                if found > MAX_RESULTS_PER_QUERY:
                    logger.warning(
                        "hh «%s» / %s: найдено %d, но отдаётся максимум %d. "
                        "Уменьшите lookback_days, иначе часть вакансий не увидим",
                        phrase, area_name, found, MAX_RESULTS_PER_QUERY,
                    )

            items = payload.get("items") or []
            if not items:
                break

            for item in items:
                yield item

            page += 1
            pages_total = int(payload.get("pages") or 0)
            if page >= pages_total:
                break
            if page * self.page_size >= MAX_RESULTS_PER_QUERY:
                break
            time.sleep(self.pause)

    @staticmethod
    def parse(raw: dict[str, Any]) -> dict[str, Any]:
        """Приводит вакансию hh к тому же плоскому виду, что и trudvsem."""
        employer = raw.get("employer") or {}
        area = raw.get("area") or {}
        snippet = raw.get("snippet") or {}

        # В сниппете hh подсвечивает совпадения тегами — убираем их здесь,
        # чтобы дальше по конвейеру шёл чистый текст.
        text_parts = [
            str(_pick(snippet, "requirement", default="")),
            str(_pick(snippet, "responsibility", default="")),
        ]
        duty = " ".join(part for part in text_parts if part)

        return {
            "vacancy_id": f"hh:{_pick(raw, 'id')}",
            "job_name": str(_pick(raw, "name", "job-name")),
            "duty": duty,
            "url": str(_pick(raw, "alternate_url", "url")),
            "created_date": str(_pick(raw, "published_at", "created_at"))[:10],
            # ИНН у hh нет — узнаём позже через Checko.
            "inn": "",
            "employer_id": str(_pick(employer, "id")),
            "company_name": str(_pick(employer, "name")),
            "company_site": str(_pick(employer, "alternate_url", default="")),
            "company_email": "",
            "company_phone": "",
            # hh не помечает вакансии кадровых агентств надёжным признаком.
            # Отсекаем их позже по ОКВЭД 78.10 через Checko — это точнее,
            # чем гадать по полям вакансии.
            "hr_agency": False,
            "region": str(_pick(area, "name", default="")),
            "specialisation": "",
        }
