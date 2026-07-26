#!/usr/bin/env python3
"""Зонд Checko: тратит 3 запроса и показывает реальную структуру ответа.

Зачем отдельный скрипт. Имена полей в ответе Checko я не проверял — доступа
к API из среды разработки не было. Разбор в src/providers/checko.py написан
защитно (ищет поля рекурсивно), но лучше один раз посмотреть на настоящий
ответ, чем полагаться на догадки. История с параметром `offset` у trudvsem,
где номер страницы приняли за смещение, стоила нам целого прогона.

Запуск:
    python probe_checko.py            # поиск + карточка + финансы (3 запроса)
    python probe_checko.py 7736207543 # карточка конкретного ИНН

После запуска сверьте вывод с тем, что ищет `parse_company` —
и, если надо, допишите реальные имена полей в списки в checko.py.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests
import yaml

BASE_URL = "https://api.checko.ru/v2"


def load_env() -> None:
    """Читает .env без внешних зависимостей."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def show(title: str, payload: object) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    # Ограничиваем вывод: полная карточка бывает очень длинной.
    print(text[:6000])
    if len(text) > 6000:
        print(f"\n… обрезано, всего {len(text)} символов")


def main() -> int:
    load_env()
    key = os.environ.get("CHECKO_API_KEY", "").strip()
    if not key:
        print("Не задан CHECKO_API_KEY. Положите ключ в .env:\n"
              "  CHECKO_API_KEY=ваш_ключ", file=sys.stderr)
        return 1

    config = yaml.safe_load(Path("config/icp.yaml").read_text(encoding="utf-8"))
    target = config["target_list"]
    okved = target["okved"][0]
    region = target["regions"][0]

    session = requests.Session()
    inn_from_search: str | None = None

    # --- Запрос 1: поиск -----------------------------------------------------
    print(f"Запрос 1/2: поиск компаний. ОКВЭД {okved}, регион "
          f"{region['name']} (код {region['code']}), ОПФ {target.get('opf')}")
    try:
        response = session.get(
            f"{BASE_URL}/search",
            params={
                "key": key, "by": "okved", "obj": "org", "query": okved,
                "region": region["code"], "opf": target.get("opf"),
                "active": "true", "page": 1,
            },
            timeout=30,
        )
        print(f"HTTP {response.status_code}")
        payload = response.json()
        show("ОТВЕТ ПОИСКА", payload)

        # Пробуем найти первый ИНН — им же проверим карточку.
        def first_inn(node: object) -> str | None:
            if isinstance(node, dict):
                for name in ("ИНН", "inn"):
                    if node.get(name):
                        return str(node[name])
                for value in node.values():
                    found = first_inn(value)
                    if found:
                        return found
            elif isinstance(node, list):
                for item in node:
                    found = first_inn(item)
                    if found:
                        return found
            return None

        inn_from_search = first_inn(payload)
        print(f"\nПервый найденный ИНН: {inn_from_search or 'не найден'}")
    except Exception as exc:  # noqa: BLE001 — зонд, важно показать любую ошибку
        print(f"Поиск не удался: {exc}", file=sys.stderr)

    # --- Запрос 2: карточка --------------------------------------------------
    inn = sys.argv[1] if len(sys.argv) > 1 else inn_from_search
    if not inn:
        print("\nИНН для карточки не определён — второй запрос пропущен")
        return 0

    print(f"\nЗапрос 2/2: карточка компании по ИНН {inn}")
    try:
        response = session.get(
            f"{BASE_URL}/company", params={"key": key, "inn": inn}, timeout=30
        )
        print(f"HTTP {response.status_code}")
        payload = response.json()
        show("ОТВЕТ КАРТОЧКИ", payload)

        # Показываем, что из этого извлёк наш разбор.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from src.providers.checko import CheckoClient

        parsed = CheckoClient.parse_company(payload)
        show("ЧТО ИЗ ЭТОГО ИЗВЛЁК НАШ РАЗБОР", parsed)

        missing = [name for name, value in parsed.items()
                   if value in (None, "", []) and name not in ("extra_okved", "msp_category")]
        if missing:
            print(f"\n⚠️  Не заполнены поля: {', '.join(missing)}")
            print("   Найдите их реальные имена в ответе выше и допишите в")
            print("   src/providers/checko.py → parse_company()")
        else:
            print("\n✓ Все поля карточки извлечены")
    except Exception as exc:  # noqa: BLE001
        print(f"Карточка не получена: {exc}", file=sys.stderr)

    # --- Запрос 3: финансы ---------------------------------------------------
    # Выручки в карточке нет — она в отдельном эндпоинте. Его структуру
    # в присланной документации не описали, поэтому смотрим вживую.
    print(f"\nЗапрос 3/3: финансовая отчётность по ИНН {inn}")
    try:
        response = session.get(
            f"{BASE_URL}/finances", params={"key": key, "inn": inn}, timeout=30
        )
        print(f"HTTP {response.status_code}")
        payload = response.json()
        show("ОТВЕТ ФИНАНСОВ", payload)

        from src.providers.checko import CheckoClient as _Client

        parsed_fin = _Client.parse_finances(payload)
        show("ЧТО ИЗ ЭТОГО ИЗВЛЁК НАШ РАЗБОР", parsed_fin)

        if parsed_fin.get("revenue") is None:
            print("\n⚠️  Выручка не извлечена. Пришлите вывод выше — поправлю разбор")
            print("   (ищем код строки 2110 «Выручка» в отчётности по годам)")
        else:
            print("\n✓ Выручка извлечена, динамика посчитана")
    except Exception as exc:  # noqa: BLE001
        print(f"Финансы не получены: {exc}", file=sys.stderr)
        print("Возможно, метод называется иначе или не входит в ваш тариф.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
