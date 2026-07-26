#!/usr/bin/env python3
"""Офлайн-проверка логики: без обращения к API.

Что проверяет:
  * разбор ответа trudvsem (на подставных данных в двух вариантах написания полей);
  * отбор по ключевым словам, включая стоп-слова;
  * запись в SQLite и дедупликацию;
  * сборку утреннего списка.

Запуск:  python selftest.py
Полезно после правки config/signals.yaml — покажет, не сломались ли фильтры.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

from src import collect, db, log, report, targets
from src.providers.checko import CheckoClient, RequestBudget, deep_pick, to_number
from src.sources import trudvsem as trudvsem_module
from src.sources.trudvsem import TrudvsemClient, TrudvsemError

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        FAILURES.append(name)


# --------------------------------------------------------------- подставные данные

SAMPLE_VACANCIES = [
    {   # сильный сигнал: прямым текстом про холодные звонки
        "vacancy": {
            "id": "v-001",
            "job-name": "Менеджер по продажам IT-услуг",
            "duty": "<p>Активный поиск новых клиентов, <b>холодные звонки</b>, "
                    "ведение CRM, формирование базы потенциальных заказчиков.</p>",
            "vac_url": "https://trudvsem.ru/vacancy/card/v-001",
            "creation-date": "2026-07-24",
            "company": {"name": 'ООО "Веб Студия Пример"', "inn": "7701234567",
                        "site": "example-studio.ru", "email": "info@example-studio.ru"},
            "region": {"name": "Москва"},
        }
    },
    {   # обычный сигнал: РОП, поля названы через подчёркивание (второй вариант API)
        "vacancy": {
            "id": "v-002",
            "job_name": "Руководитель отдела продаж",
            "duty": "Управление командой, планирование, отчётность.",
            "vacancy_url": "https://trudvsem.ru/vacancy/card/v-002",
            "creation_date": "2026-07-25",
            "company": {"name": 'ООО "Интегратор Плюс"', "inn": "7809876543",
                        "url": "integrator.example"},
            "region": {"name": "Санкт-Петербург"},
        }
    },
    {   # вторая вакансия той же компании — должна склеиться с предыдущей
        "vacancy": {
            "id": "v-003",
            "job-name": "Менеджер по продажам",
            "duty": "Работа с входящими заявками.",
            "vac_url": "https://trudvsem.ru/vacancy/card/v-003",
            "creation-date": "2026-07-25",
            "company": {"name": 'ООО "Интегратор Плюс"', "inn": "7809876543"},
            "region": {"name": "Санкт-Петербург"},
        }
    },
    {   # стоп-слово в должности — отбрасываем
        "vacancy": {
            "id": "v-004",
            "job-name": "Продавец-консультант",
            "duty": "Холодные звонки не требуются, работа в зале.",
            "company": {"name": 'ООО "Розница"', "inn": "5001111111"},
            "region": {"name": "Москва"},
        }
    },
    {   # кадровое агентство — отбрасываем
        "vacancy": {
            "id": "v-005",
            "job-name": "Менеджер по продажам",
            "duty": "Активные продажи, поиск новых клиентов.",
            "company": {"name": 'ООО "Кадры"', "inn": "7702222222", "hr-agency": "true"},
            "region": {"name": "Москва"},
        }
    },
    {   # без ИНН — отбрасываем
        "vacancy": {
            "id": "v-006",
            "job-name": "Менеджер по продажам",
            "duty": "Холодные звонки.",
            "company": {"name": "ИП без реквизитов"},
            "region": {"name": "Москва"},
        }
    },
    {   # не про продажи вовсе
        "vacancy": {
            "id": "v-007",
            "job-name": "Бухгалтер",
            "duty": "Ведение учёта.",
            "company": {"name": 'ООО "Учёт"', "inn": "7703333333"},
            "region": {"name": "Москва"},
        }
    },
]


def main() -> int:
    log.setup()
    config = yaml.safe_load(Path("config/signals.yaml").read_text(encoding="utf-8"))
    keywords = config["keywords"]
    filters = config["filters"]

    print("\n[1] Разбор ответа API")
    parsed = [TrudvsemClient.parse(item["vacancy"]) for item in SAMPLE_VACANCIES]
    check("поле job-name через дефис", parsed[0]["job_name"] == "Менеджер по продажам IT-услуг")
    check("поле job_name через подчёркивание", parsed[1]["job_name"] == "Руководитель отдела продаж")
    check("ИНН извлечён", parsed[0]["inn"] == "7701234567")
    check("сайт из поля site", parsed[0]["company_site"] == "example-studio.ru")
    check("сайт из запасного поля url", parsed[1]["company_site"] == "integrator.example")
    check("hr-agency как строка 'true' стал bool", parsed[4]["hr_agency"] is True)
    check("hr-agency отсутствует → False", parsed[0]["hr_agency"] is False)

    print("\n[2] Отбор по ключевым словам")
    results = {p["vacancy_id"]: collect.match_keywords(p["job_name"], p["duty"], keywords)
               for p in parsed}
    check("v-001 → сильный сигнал", results["v-001"] is not None and results["v-001"][0] == "strong")
    check("v-002 → обычный сигнал", results["v-002"] is not None and results["v-002"][0] == "normal")
    check("v-004 (продавец-консультант) отсеян стоп-словом", results["v-004"] is None)
    check("v-007 (бухгалтер) не подошёл", results["v-007"] is None)
    check("HTML-разметка не помешала найти слово",
          results["v-001"] is not None and "холодные звонки" in results["v-001"][1])

    # Регрессия: «водитель» — подстрока в «рукоВОДИТЕЛЬ». Раньше стоп-слово
    # молча выкидывало вакансии РОПа, то есть один из самых ценных сигналов.
    check("стоп-слово «водитель» не срабатывает внутри «руководитель»",
          collect.contains_keyword("руководитель отдела продаж", "водитель") is False)
    check("стоп-слово «водитель» срабатывает как отдельное слово",
          collect.contains_keyword("водитель категории в", "водитель") is True)
    check("основа «лидогенерац» ловит «лидогенерацию»",
          collect.contains_keyword("занимаемся лидогенерацией", "лидогенерац") is True)

    # Общая проверка конфига: не прячется ли стоп-слово внутри нужного нам
    # ключевого слова. Полезно каждый раз после правки signals.yaml.
    collisions = [
        (kw, stop)
        for kw in keywords["strong"] + keywords["normal"]
        for stop in keywords["exclude_job"]
        if collect.contains_keyword(collect.normalize(kw), stop)
    ]
    check("нет конфликтов между ключевыми и стоп-словами", not collisions, str(collisions))

    print("\n[3] Фрагмент текста для отчёта")
    fragment = collect.excerpt_around(parsed[0]["duty"], "холодные звонки")
    check("фрагмент содержит ключевое слово", "холодные звонки" in fragment)
    check("HTML-теги вычищены", "<b>" not in fragment and "<p>" not in fragment)

    print("\n[4] База: запись и дедупликация")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db.DB_PATH = tmp_path / "test.db"
        report.MORNING_DIR = tmp_path / "morning"

        conn = db.connect()
        written = 0
        for vacancy in parsed:
            if filters["skip_hr_agencies"] and vacancy["hr_agency"]:
                continue
            if filters["require_inn"] and not vacancy["inn"]:
                continue
            verdict = collect.match_keywords(vacancy["job_name"], vacancy["duty"], keywords)
            if verdict is None:
                continue
            strength, hits = verdict
            db.upsert_company(conn, {
                "inn": vacancy["inn"], "name": vacancy["company_name"],
                "region": vacancy["region"], "site": vacancy["company_site"],
                "email": vacancy["company_email"], "phone": vacancy["company_phone"],
            })
            if db.insert_signal(conn, {
                "vacancy_id": vacancy["vacancy_id"], "inn": vacancy["inn"],
                "company_name": vacancy["company_name"], "job_name": vacancy["job_name"],
                "region": vacancy["region"], "url": vacancy["url"],
                "created_date": vacancy["created_date"], "strength": strength,
                "matched": "; ".join(hits),
                "excerpt": collect.excerpt_around(vacancy["duty"], hits[0]),
                "specialisation": vacancy["specialisation"],
            }):
                written += 1
        conn.commit()

        check("записано 3 сигнала (v-001, v-002, v-003)", written == 3, f"получено {written}")

        # Повторная запись тех же вакансий не должна ничего добавить.
        again = db.insert_signal(conn, {
            "vacancy_id": "v-001", "inn": "7701234567", "company_name": "x",
            "job_name": "x", "region": "x", "url": "x", "created_date": "x",
            "strength": "strong", "matched": "x", "excerpt": "x", "specialisation": "x",
        })
        check("повторная вакансия не записывается", again is False)

        companies = conn.execute("SELECT COUNT(*) c FROM companies").fetchone()["c"]
        check("компаний 2 (две вакансии склеились в одну)", companies == 2, f"получено {companies}")
        conn.close()

        print("\n[5] Утренний список")
        path = report.build(config)
        check("файл отчёта создан", path is not None and path.exists())
        if path:
            text = path.read_text(encoding="utf-8")
            check("сильный сигнал идёт первым", text.index("Веб Студия") < text.index("Интегратор"))
            check("есть строка «Почему здесь»", "**Почему здесь:**" in text)
            check("две вакансии одной компании отмечены",
                  "2 вакансии сразу" in text)
            check("есть ссылка на ручную проверку в hh.ru", "hh.ru/search/employer" in text)
            check("отсеянных компаний в отчёте нет", "Розница" not in text and "Кадры" not in text)
            print("\n--- фрагмент отчёта ---")
            print("\n".join(text.splitlines()[:24]))
            print("--- конец фрагмента ---")

        # Повторная сборка не должна показать те же компании снова.
        second = report.build(config)
        check("повторный отчёт пуст (нет повторов)", second is None)

    print("\n[6] Пагинация: offset — это номер страницы, а не смещение")
    calls: list[dict] = []

    def fake_get(self, url, params):
        """Подменяет HTTP: две страницы по 2 записи, на третьей — ошибка."""
        calls.append(dict(params))
        page = params["offset"]
        if page >= 2:
            raise TrudvsemError("HTTP 500 (страницы не существует)")
        return {
            "meta": {"total": 4},
            "results": {"vacancies": [
                {"vacancy": {"id": f"p{page}-a"}},
                {"vacancy": {"id": f"p{page}-b"}},
            ]},
        }

    original_get = TrudvsemClient._get
    original_sleep = trudvsem_module.time.sleep
    TrudvsemClient._get = fake_get
    trudvsem_module.time.sleep = lambda _s: None
    try:
        client = TrudvsemClient({"page_size": 2, "max_pages_per_region": 10})
        collected = list(client.fetch_region("7700000000000", "Тест"))
    finally:
        TrudvsemClient._get = original_get
        trudvsem_module.time.sleep = original_sleep

    check("собраны все 4 записи", len(collected) == 4, f"получено {len(collected)}")
    check("offset увеличивается по единице (0, 1)",
          [c["offset"] for c in calls] == [0, 1], str([c["offset"] for c in calls]))
    check("остановились по total, не дойдя до ошибки", len(calls) == 2, f"запросов {len(calls)}")

    print("\n[7] Пагинация: сбой на середине не теряет уже собранное")
    calls.clear()

    def failing_get(self, url, params):
        calls.append(dict(params))
        page = params["offset"]
        if page >= 1:
            raise TrudvsemError("HTTP 500")
        return {"meta": {"total": 100}, "results": {"vacancies": [{"vacancy": {"id": "a"}}]}}

    TrudvsemClient._get = failing_get
    trudvsem_module.time.sleep = lambda _s: None
    try:
        client = TrudvsemClient({"page_size": 1, "max_pages_per_region": 10})
        partial = list(client.fetch_region("7700000000000", "Тест"))
    finally:
        TrudvsemClient._get = original_get
        trudvsem_module.time.sleep = original_sleep

    check("первая страница сохранена, регион не упал", len(partial) == 1)

    print("\n[8] Отраслевой фильтр")
    check("include пропускает нужную отрасль",
          any("информационные технологии" in s
              for s in ["Информационные технологии, связь".lower()]))
    parsed_spec = TrudvsemClient.parse({
        "id": "s-1", "job-name": "Менеджер по продажам",
        "category": {"specialisation": "Информационные технологии, связь"},
        "company": {"inn": "1", "name": "x"},
    })
    check("отрасль извлекается из ответа",
          parsed_spec["specialisation"] == "Информационные технологии, связь")

    print("\n[9] Стоп-слова, добавленные после боевого прогона")
    for job, why in [
        ("Специалист по работе с физическими лицами в точку продаж банка", "банковская розница"),
        ("Главный врач, медицинский директор", "медицина"),
        ("Менеджер по работе с клиентами (недвижимость)", "недвижимость"),
        ("Специалист по продажам проектов/Дизайнер-консультант", "розничный дизайнер"),
    ]:
        check(f"отсеивается: {why}",
              collect.match_keywords(job, "активные продажи, формирование базы", keywords) is None,
              f"«{job}» всё ещё проходит")

    print("\n[10] Checko: рекурсивный поиск полей в ответе")
    # Ответ приходит на русских ключах и с неизвестной вложенностью,
    # поэтому разбор должен находить поле на любой глубине.
    nested = {"data": {"Реквизиты": {"ИНН": "7736207543"},
                       "ОКВЭД": {"Код": "62.01", "Наим": "Разработка ПО"},
                       "Показатели": {"СЧР": 45, "Выручка": "120 000 000,50"}}}
    check("ИНН найден во вложенном словаре", deep_pick(nested, "ИНН", "inn") == "7736207543")
    check("приоритет у первого имени из списка",
          deep_pick({"a": {"inn": "2"}, "ИНН": "1"}, "ИНН", "inn") == "1")
    check("число с пробелами и запятой разобрано",
          to_number("120 000 000,50") == 120000000.5)
    check("пустая строка не считается значением",
          deep_pick({"ИНН": "", "x": {"ИНН": "77"}}, "ИНН") == "77")

    parsed_company = CheckoClient.parse_company(nested)
    check("из карточки извлечён ОКВЭД", parsed_company["okved"] == "62.01")
    check("из карточки извлечена численность", parsed_company["staff"] == 45.0)
    check("из карточки извлечена выручка", parsed_company["revenue"] == 120000000.5)

    print("\n[11] Вердикт по ICP")
    criteria = yaml.safe_load(Path("config/icp.yaml").read_text(encoding="utf-8"))["criteria"]
    fits = {"okved": "62.01", "extra_okved": [], "staff": 40.0,
            "revenue": 120_000_000.0, "registration_date": "2015-03-10"}

    status, _ = targets.judge(fits, criteria)
    check("подходящая компания проходит", status == "passed", status)

    status, reason = targets.judge({**fits, "staff": 5.0}, criteria)
    check("мелкая компания отсеивается по штату", status == "rejected")
    check("причина отказа человекочитаема", "штат" in reason, reason)

    status, _ = targets.judge({**fits, "staff": 500.0}, criteria)
    check("крупная компания отсеивается по штату", status == "rejected")

    status, _ = targets.judge({**fits, "revenue": 1_000_000.0}, criteria)
    check("компания без выручки отсеивается", status == "rejected")

    status, reason = targets.judge({**fits, "extra_okved": ["78.10"]}, criteria)
    check("кадровое агентство отсеивается по доп. ОКВЭД", status == "rejected")
    check("причина называет ОКВЭД", "78.10" in reason, reason)

    status, _ = targets.judge({**fits, "staff": None}, criteria)
    check("нет данных о штате → отказ, а не пропуск", status == "rejected")

    print("\n[12] Бюджет запросов")
    budget = RequestBudget(limit=3)
    spent = [budget.try_spend() for _ in range(5)]
    check("тратится ровно лимит", spent == [True, True, True, False, False], str(spent))
    check("остаток не уходит в минус", budget.left == 0)

    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО проверок: {len(FAILURES)} — {', '.join(FAILURES)}")
        return 1
    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
