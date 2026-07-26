"""Клиент открытого API «Работа России» (trudvsem.ru).

Это государственные открытые данные — использование не ограничено целью,
в отличие от hh.ru. Подробности выбора — в DECISIONS.md, запись Р-001.

ВНИМАНИЕ ПРО ИМЕНА ПОЛЕЙ. Структура ответа взята из документации и публичных
примеров, но живьём на момент написания не проверялась. Поэтому парсер
защитный: каждое поле ищется под несколькими возможными именами
(`job-name` / `job_name` / `jobName`). Если API отдаёт что-то третье —
запустите `python run.py --stage collect --raw`, посмотрите сырой JSON первой
вакансии и допишите нужное имя в списки ниже. Кода это правка на одну строку.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Iterator

import requests

from .. import log

logger = log.get("trudvsem")

USER_AGENT = "leadgen-research/0.1 (b2b lead generation, contact via site)"


def _pick(data: dict[str, Any], *names: str, default: Any = "") -> Any:
    """Достаёт значение по первому подошедшему имени поля.

    Нужно потому, что в разных версиях API поля называются то через дефис,
    то через подчёркивание.
    """
    for name in names:
        if name in data and data[name] not in (None, ""):
            return data[name]
    return default


class TrudvsemError(RuntimeError):
    pass


class TrudvsemClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.base_url = config.get("base_url", "http://opendata.trudvsem.ru/api/v1").rstrip("/")
        self.page_size = int(config.get("page_size", 100))
        self.max_pages = int(config.get("max_pages_per_region", 50))
        self.timeout = int(config.get("timeout_seconds", 30))
        self.retries = int(config.get("retries", 3))
        self.lookback_days = int(config.get("lookback_days", 3))

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    # ---------------------------------------------------------------- запросы

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET с повторами и экспоненциальной задержкой (1с, 2с, 4с)."""
        last_error: Exception | None = None

        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code == 200:
                    return response.json()
                # 4xx повторять бессмысленно — это мы неправильно спросили.
                if 400 <= response.status_code < 500:
                    raise TrudvsemError(
                        f"HTTP {response.status_code} на {url} — проверьте код региона "
                        f"и параметры запроса. Ответ: {response.text[:200]}"
                    )
                last_error = TrudvsemError(f"HTTP {response.status_code}")
            except (requests.RequestException, ValueError) as exc:
                last_error = exc

            if attempt < self.retries:
                delay = 2 ** (attempt - 1)
                logger.warning(
                    "Попытка %d/%d не удалась (%s), повтор через %dс",
                    attempt, self.retries, last_error, delay,
                )
                time.sleep(delay)

        raise TrudvsemError(f"Не удалось получить {url}: {last_error}")

    def fetch_region(self, region_code: str, region_name: str) -> Iterator[dict[str, Any]]:
        """Отдаёт сырые записи вакансий по региону, разбирая пагинацию.

        Ограничиваем выборку по дате изменения (lookback_days), иначе по
        Москве прилетят десятки тысяч записей на каждом прогоне.
        """
        url = f"{self.base_url}/vacancies/region/{region_code}"
        modified_from = (datetime.now() - timedelta(days=self.lookback_days)).strftime(
            "%Y-%m-%dT00:00:00Z"
        )

        offset = 0
        total: int | None = None
        pages = 0

        while pages < self.max_pages:
            payload = self._get(
                url,
                {"offset": offset, "limit": self.page_size, "modifiedFrom": modified_from},
            )

            meta = payload.get("meta") or {}
            if total is None:
                total = int(meta.get("total") or 0)
                logger.info("Регион %s: всего доступно %d вакансий", region_name, total)
                if total == 0:
                    logger.warning(
                        "Регион %s (код %s) вернул 0 вакансий — вероятно, неверный "
                        "код региона в signals.yaml",
                        region_name, region_code,
                    )

            results = payload.get("results") or {}
            items = results.get("vacancies") or []
            if not items:
                break

            for item in items:
                # Каждый элемент обёрнут: {"vacancy": {...}}
                yield item.get("vacancy", item)

            pages += 1
            offset += len(items)
            if total is not None and offset >= total:
                break
            time.sleep(0.3)  # вежливая пауза между страницами

        if pages >= self.max_pages:
            logger.warning(
                "Регион %s: упёрлись в потолок max_pages_per_region=%d. "
                "Часть вакансий не просмотрена — уменьшите lookback_days",
                region_name, self.max_pages,
            )

    # ---------------------------------------------------------------- разбор

    @staticmethod
    def parse(raw: dict[str, Any]) -> dict[str, Any]:
        """Приводит сырую запись к плоскому виду, который понимает остальной код."""
        company = raw.get("company") or {}
        region = raw.get("region") or {}

        inn = str(_pick(company, "inn", "INN")).strip()
        # У API встречается и строковое "true", и настоящий bool.
        hr_agency = _pick(company, "hr-agency", "hr_agency", default=False)
        if isinstance(hr_agency, str):
            hr_agency = hr_agency.strip().lower() in ("true", "1", "да")

        return {
            "vacancy_id": str(_pick(raw, "id", "vacancy_id", "vacancyId")),
            "job_name": str(_pick(raw, "job-name", "job_name", "jobName")),
            "duty": str(_pick(raw, "duty", "requirement_text", "description")),
            "url": str(_pick(raw, "vac_url", "vacancy_url", "url")),
            "created_date": str(_pick(raw, "creation-date", "creation_date", "createdDate")),
            "inn": inn,
            "company_name": str(_pick(company, "name", "companyname", "company_name")),
            "company_site": str(_pick(company, "site", "url", "www")),
            "company_email": str(_pick(company, "email")),
            "company_phone": str(_pick(company, "phone")),
            "hr_agency": bool(hr_agency),
            "region": str(_pick(region, "name", "region_name", default="")),
        }
