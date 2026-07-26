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
    def _first(value: Any) -> str:
        """Контакты приходят списками — берём первый непустой элемент."""
        if isinstance(value, list):
            for item in value:
                if item:
                    return str(item).strip()
            return ""
        return str(value or "").strip()

    @staticmethod
    def parse_company(payload: dict[str, Any]) -> dict[str, Any]:
        """Достаёт из карточки поля, нужные для проверки ICP.

        Имена полей сверены с документацией Checko и боевым ответом
        (зонд от 2026-07-26), поэтому здесь уже адресное обращение, а не
        рекурсивный поиск наугад.

        ВАЖНО: блоки «Руковод» и «Учред» содержат ФИО и ИНН физлиц. Мы их
        сознательно не читаем и не храним — см. DECISIONS.md, Р-000.
        Выручки в этом ответе нет, она в отдельном методе finances().
        """
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

        okved_node = data.get("ОКВЭД") or {}
        if isinstance(okved_node, dict):
            okved_code = str(okved_node.get("Код") or "")
            okved_name = str(okved_node.get("Наим") or "")
        else:
            okved_code, okved_name = str(okved_node or ""), ""

        # Дополнительные ОКВЭД — нужны, чтобы отсечь кадровые агентства,
        # у которых подбор персонала записан вторым видом деятельности.
        extra_okved = [
            str(item.get("Код"))
            for item in (data.get("ОКВЭДДоп") or [])
            if isinstance(item, dict) and item.get("Код")
        ]

        contacts = data.get("Контакты") or {}
        region_node = data.get("Регион") or {}
        status_node = data.get("Статус") or {}
        msp_node = data.get("РМСП") or {}
        taxes = data.get("Налоги") or {}

        status_name = (
            status_node.get("Наим") if isinstance(status_node, dict) else status_node
        )

        return {
            "inn": str(data.get("ИНН") or ""),
            "name": str(data.get("НаимСокр") or data.get("НаимПолн") or ""),
            "okved": okved_code,
            "okved_name": okved_name,
            "extra_okved": extra_okved,
            "region": str(region_node.get("Наим") if isinstance(region_node, dict)
                          else region_node or ""),
            "site": CheckoClient._first(contacts.get("ВебСайт")),
            "email": CheckoClient._first(contacts.get("Емэйл")),
            "phone": CheckoClient._first(contacts.get("Тел")),
            "staff": to_number(data.get("СЧР")),
            "registration_date": str(data.get("ДатаРег") or ""),
            "active": str(status_name or "").strip().lower().startswith("действ"),
            # Категория реестра МСП: микро / малое / среднее предприятие.
            # Бесплатный ориентир по размеру ещё до запроса финансов.
            "msp_category": str(msp_node.get("Кат") or "") if isinstance(msp_node, dict) else "",
            # Сумма уплаченных налогов — грубый, но дешёвый признак живости
            # компании: нулевые налоги при заявленном штате выглядят странно.
            "taxes_paid": to_number(taxes.get("СумУпл")) if isinstance(taxes, dict) else None,
        }

    # --------------------------------------------------------------- финансы

    def finances(self, inn: str) -> dict[str, Any]:
        """Финансовая отчётность. Отдельный эндпоинт, отдельный запрос."""
        return self._request("finances", {"inn": inn})

    @staticmethod
    def parse_finances(payload: dict[str, Any]) -> dict[str, Any]:
        """Достаёт выручку за два последних года и считает динамику.

        Структура ответа finances не сверялась с документацией (в присланной
        PDF её нет), поэтому здесь разбор остаётся рекурсивным и терпимым
        к разной вложенности: ищем словарь, где ключи — годы.
        """
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

        # Ищем узел вида {"2024": {...}, "2023": {...}} на любой глубине.
        def find_years(node: Any) -> dict[str, Any] | None:
            if isinstance(node, dict):
                years = [k for k in node if isinstance(k, str) and k.isdigit() and len(k) == 4]
                if len(years) >= 1:
                    return node
                for value in node.values():
                    found = find_years(value)
                    if found:
                        return found
            return None

        by_year = find_years(data) or {}
        years = sorted(
            (y for y in by_year if isinstance(y, str) and y.isdigit() and len(y) == 4),
            reverse=True,
        )

        def revenue_of(year: str) -> float | None:
            node = by_year.get(year)
            if node is None:
                return None
            if isinstance(node, (int, float, str)):
                return to_number(node)
            # Код 2110 — «Выручка» в форме бухгалтерской отчётности.
            return to_number(deep_pick(node, "2110", "Выручка", "revenue"))

        current = revenue_of(years[0]) if years else None
        previous = revenue_of(years[1]) if len(years) > 1 else None

        change_pct: float | None = None
        if current is not None and previous:
            change_pct = (current - previous) / abs(previous) * 100

        return {
            "revenue": current,
            "revenue_prev": previous,
            "revenue_change_pct": change_pct,
            "revenue_year": years[0] if years else "",
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
