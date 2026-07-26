"""Клиент Checko: поиск компаний по критериям и карточка юрлица по ИНН.

ПРО ИМЕНА ПОЛЕЙ. Ответ Checko приходит на русских ключах («ИНН», «НаимСокр»,
«ОКВЭД», «СЧР»), и точную структуру я не проверял — доступа к API из среды
разработки нет. Поэтому разбор здесь рекурсивный: `deep_pick` обходит
вложенные словари и ищет поле по нескольким возможным именам, на любой
глубине. Это переживает и переименования, и изменения вложенности.

Чтобы увидеть реальную структуру, потратив 2–3 запроса, запустите:
    python probe_checko.py

Ключ читается только из переменной окружения CHECKO_API_KEY.
"""

from __future__ import annotations

import os
import time
from typing import Any, Iterator

import requests

from .. import log

logger = log.get("checko")


class CheckoError(RuntimeError):
    pass


class BudgetExhausted(RuntimeError):
    """Суточный лимит запросов исчерпан. Не ошибка — повод остановиться."""


def deep_pick(data: Any, *names: str, default: Any = None) -> Any:
    """Ищет значение по любому из имён на любой глубине вложенности.

    Порядок важен: сначала проверяем все имена на текущем уровне, и только
    потом спускаемся глубже. Иначе вложенное совпадение может перебить
    более точное на верхнем уровне.
    """
    if isinstance(data, dict):
        for name in names:
            if name in data and data[name] not in (None, "", [], {}):
                return data[name]
        for value in data.values():
            found = deep_pick(value, *names, default=None)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = deep_pick(item, *names, default=None)
            if found is not None:
                return found
    return default


def to_number(value: Any) -> float | None:
    """Приводит значение к числу, переживая строки вида '1 234 567,89'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


class CheckoClient:
    def __init__(self, config: dict[str, Any], budget: "RequestBudget") -> None:
        self.base_url = config.get("base_url", "https://api.checko.ru/v2").rstrip("/")
        self.timeout = int(config.get("timeout_seconds", 30))
        self.retries = int(config.get("retries", 3))
        self.pause = float(config.get("pause_seconds", 0.5))
        self.budget = budget

        self.key = os.environ.get("CHECKO_API_KEY", "").strip()
        if not self.key:
            raise CheckoError(
                "Не задан CHECKO_API_KEY. Зарегистрируйтесь на checko.ru, "
                "возьмите ключ в личном кабинете и положите в .env "
                "(шаблон — в .env.example)"
            )

        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def _request(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """Один запрос к API с учётом суточного бюджета и повторами."""
        if not self.budget.try_spend():
            raise BudgetExhausted(
                f"Исчерпан суточный бюджет запросов Checko "
                f"({self.budget.limit}). Продолжим завтра — прогресс сохранён"
            )

        url = f"{self.base_url}/{endpoint}"
        request_params = {"key": self.key, **params}
        last_error: Exception | None = None

        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(url, params=request_params, timeout=self.timeout)

                if response.status_code == 200:
                    payload = response.json()
                    # Checko сообщает об ошибках в теле при HTTP 200.
                    meta = payload.get("meta") or {}
                    status = str(meta.get("status") or "").lower()
                    if status and status not in ("ok", "200", "success"):
                        message = meta.get("message") or payload.get("message") or status
                        raise CheckoError(f"Checko вернул статус «{status}»: {message}")
                    time.sleep(self.pause)
                    return payload

                if response.status_code in (401, 403):
                    raise CheckoError(
                        f"HTTP {response.status_code}: ключ отклонён или нет прав. "
                        f"Проверьте CHECKO_API_KEY. Ответ: {response.text[:200]}"
                    )
                if response.status_code == 429:
                    raise CheckoError(
                        "HTTP 429: превышен лимит запросов на стороне Checko. "
                        "Уменьшите enrich_per_run в config/icp.yaml"
                    )
                if 400 <= response.status_code < 500:
                    raise CheckoError(f"HTTP {response.status_code}: {response.text[:200]}")

                last_error = CheckoError(f"HTTP {response.status_code}")
            except (requests.RequestException, ValueError) as exc:
                last_error = exc

            if attempt < self.retries:
                delay = 2 ** (attempt - 1)
                logger.warning("Checko: попытка %d/%d не удалась (%s), повтор через %dс",
                               attempt, self.retries, last_error, delay)
                time.sleep(delay)

        raise CheckoError(f"Не удалось получить {url}: {last_error}")

    # ------------------------------------------------------------------ поиск

    def search_by_okved(self, okved: str, region: str, opf: str | None,
                        only_active: bool, page: int = 1) -> dict[str, Any]:
        """Одна страница поиска компаний по ОКВЭД в регионе."""
        params: dict[str, Any] = {
            "by": "okved",
            "obj": "org",
            "query": okved,
            "region": region,
            "page": page,
        }
        if opf:
            params["opf"] = opf
        if only_active:
            params["active"] = "true"
        return self._request("search", params)

    @staticmethod
    def parse_search_results(payload: dict[str, Any]) -> list[dict[str, str]]:
        """Достаёт из ответа поиска пары ИНН + название.

        Структура ответа неизвестна заранее, поэтому идём от списка: находим
        первый список словарей, в которых есть что-то похожее на ИНН.
        """
        def find_records(node: Any) -> list[dict[str, Any]]:
            if isinstance(node, list):
                records = [item for item in node
                           if isinstance(item, dict) and deep_pick(item, "ИНН", "inn")]
                if records:
                    return records
                for item in node:
                    found = find_records(item)
                    if found:
                        return found
            elif isinstance(node, dict):
                for value in node.values():
                    found = find_records(value)
                    if found:
                        return found
            return []

        results = []
        for record in find_records(payload):
            inn = str(deep_pick(record, "ИНН", "inn") or "").strip()
            if not inn:
                continue
            name = str(
                deep_pick(record, "НаимСокрЮЛ", "НаимСокр", "НаимПолн", "Наим", "name")
                or ""
            ).strip()
            results.append({"inn": inn, "name": name})
        return results

    def iter_candidates(self, okved: str, region_code: str, region_name: str,
                        opf: str | None, only_active: bool,
                        max_pages: int = 20) -> Iterator[dict[str, str]]:
        """Перебирает страницы поиска, пока они не кончатся."""
        for page in range(1, max_pages + 1):
            payload = self.search_by_okved(okved, region_code, opf, only_active, page)
            records = self.parse_search_results(payload)
            if not records:
                break
            logger.info("Checko поиск: ОКВЭД %s / %s, страница %d — %d компаний",
                        okved, region_name, page, len(records))
            for record in records:
                yield record
            # Неполная страница означает, что дальше ничего нет.
            if len(records) < 10:
                break

    # --------------------------------------------------------------- карточка

    def company(self, inn: str) -> dict[str, Any]:
        return self._request("company", {"inn": inn})

    @staticmethod
    def parse_company(payload: dict[str, Any]) -> dict[str, Any]:
        """Достаёт из карточки поля, нужные для проверки ICP."""
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

        okved_node = deep_pick(data, "ОКВЭД", "ОсновнойВидДеятельности", "okved")
        if isinstance(okved_node, dict):
            okved_code = str(deep_pick(okved_node, "Код", "code") or "")
            okved_name = str(deep_pick(okved_node, "Наим", "Название", "name") or "")
        else:
            okved_code = str(okved_node or "")
            okved_name = ""

        # Дополнительные ОКВЭД — нужны, чтобы отсечь кадровые агентства,
        # у которых подбор персонала записан вторым видом деятельности.
        extra_node = deep_pick(data, "ОКВЭДДоп", "ДопВидДеятельности", "okved_extra", default=[])
        extra_okved: list[str] = []
        if isinstance(extra_node, list):
            for item in extra_node:
                code = deep_pick(item, "Код", "code") if isinstance(item, dict) else item
                if code:
                    extra_okved.append(str(code))

        return {
            "inn": str(deep_pick(data, "ИНН", "inn") or ""),
            "name": str(deep_pick(data, "НаимСокрЮЛ", "НаимСокр", "НаимПолн", "Наим") or ""),
            "okved": okved_code,
            "okved_name": okved_name,
            "extra_okved": extra_okved,
            "region": str(deep_pick(data, "Регион", "region") or ""),
            "site": str(deep_pick(data, "Сайт", "ВебСайт", "site") or ""),
            "email": str(deep_pick(data, "Емэйл", "Email", "email") or ""),
            "phone": str(deep_pick(data, "Телефон", "phone") or ""),
            "staff": to_number(deep_pick(data, "СЧР", "СреднесписочнаяЧисленность",
                                         "Численность", "staff")),
            "revenue": to_number(deep_pick(data, "Выручка", "revenue")),
            "registration_date": str(deep_pick(data, "ДатаРег", "ДатаРегистрации") or ""),
            "active": deep_pick(data, "Активность", "Статус", "active"),
        }


class RequestBudget:
    """Считает израсходованные за сутки запросы и не даёт выйти за лимит."""

    def __init__(self, limit: int, spent_today: int = 0) -> None:
        self.limit = limit
        self.spent = spent_today

    def try_spend(self) -> bool:
        if self.spent >= self.limit:
            return False
        self.spent += 1
        return True

    @property
    def left(self) -> int:
        return max(0, self.limit - self.spent)
