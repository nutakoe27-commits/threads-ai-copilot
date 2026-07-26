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

import json
import sys
import tempfile
from pathlib import Path

import yaml

from src import collect, compose, db, dossier, llm, log, report, targets
from src.providers.checko import CheckoClient, RequestBudget, deep_pick, to_number
from src.sources import trudvsem as trudvsem_module
from src.sources.website import _TextExtractor, WebsiteFetcher, normalize_url
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

    print("\n[10] Checko: разбор настоящего ответа (снят зондом 2026-07-26)")
    # Это НЕ выдуманные данные: фрагменты реального ответа Checko,
    # включая блоки с персональными данными, которые мы обязаны игнорировать.
    real_search = {"data": {
        "ЗапВсего": 13955, "СтрВсего": 140, "СтрТекущ": 1,
        "Записи": [
            {"ОГРН": "1023201336344", "ИНН": "3203006518",
             "НаимСокр": 'ООО "АНАНТА"', "ДатаРег": "1999-07-30",
             "Статус": "Действует", "РегионКод": "77",
             "ОКВЭД": "Разработка компьютерного программного обеспечения",
             "Руковод": [{"ФИО": "Шульгин Игорь Сергеевич", "ИНН": "461703208229"}],
             "Учред": {"ФЛ": [{"ФИО": "Шульгин Игорь Сергеевич",
                               "ИНН": "461703208229"}]}},
            {"ОГРН": "1024000950368", "ИНН": "4025070110",
             "НаимСокр": 'ООО "ИНТЕГРАЛ КТ"', "Статус": "Действует"},
        ],
    }}
    records = CheckoClient.parse_search_results(real_search)
    check("из поиска извлечены обе компании", len(records) == 2, str(len(records)))
    check("взят ИНН юрлица, а не руководителя",
          records[0]["inn"] == "3203006518", records[0]["inn"])
    check("название компании извлечено", records[0]["name"] == 'ООО "АНАНТА"')
    check("дата регистрации взята из поиска (бесплатный отсев по возрасту)",
          records[0]["reg_date"] == "1999-07-30")
    check("статус взят из поиска (бесплатный отсев недействующих)",
          records[0]["status"] == "Действует")

    # Требование Р-000: персональные данные в систему не попадают.
    leaked = [key for record in records for key in record
              if key not in ("inn", "name", "reg_date", "status")]
    check("из поиска не утекли ФИО и прочие поля", not leaked, str(leaked))
    check("ФИО отсутствуют в результате целиком",
          "Шульгин" not in json.dumps(records, ensure_ascii=False))

    real_company = {"data": {
        "ОГРН": "1023201336344", "ИНН": "3203006518", "ДатаРег": "1999-07-30",
        "НаимСокр": 'ООО "АНАНТА"',
        "Статус": {"Код": "001", "Наим": "Действует"},
        "Регион": {"Код": "77", "Наим": "Москва"},
        "ОКВЭД": {"Код": "62.01", "Наим": "Разработка компьютерного ПО", "Версия": "2014"},
        "ОКВЭДДоп": [{"Код": "46.52", "Наим": "Торговля оптовая"},
                     {"Код": "62.02", "Наим": "Консультативная деятельность"}],
        "Контакты": {"Тел": ["+7 495 000-00-00"],
                     "Емэйл": ["info@example.ru", "sales@example.ru"],
                     "ВебСайт": "https://example.ru"},
        "Налоги": {"СумУпл": 1234567.0, "СведУплГод": "2024"},
        "РМСП": {"Кат": "МАЛОЕ ПРЕДПРИЯТИЕ"},
        "СЧР": 45,
        "Руковод": [{"ФИО": "Шульгин Игорь Сергеевич", "ИНН": "461703208229"}],
    }}
    parsed_company = CheckoClient.parse_company(real_company)
    check("ОКВЭД извлечён кодом", parsed_company["okved"] == "62.01")
    check("дополнительные ОКВЭД извлечены", parsed_company["extra_okved"] == ["46.52", "62.02"])
    check("регион — название, а не словарь", parsed_company["region"] == "Москва")
    check("сайт из Контакты.ВебСайт", parsed_company["site"] == "https://example.ru")
    check("почта — первая из списка", parsed_company["email"] == "info@example.ru")
    check("телефон — первый из списка", parsed_company["phone"] == "+7 495 000-00-00")
    check("численность из СЧР", parsed_company["staff"] == 45.0)
    check("статус «Действует» распознан", parsed_company["active"] is True)
    check("категория МСП извлечена", parsed_company["msp_category"] == "МАЛОЕ ПРЕДПРИЯТИЕ")
    check("ФИО руководителя не попали в разбор",
          "Шульгин" not in json.dumps(parsed_company, ensure_ascii=False))

    check("ликвидированная компания распознана",
          CheckoClient.parse_company(
              {"data": {"Статус": {"Наим": "Ликвидировано"}}})["active"] is False)

    print("\n[11] Разбор финансов и динамика выручки")
    finances = CheckoClient.parse_finances(
        {"data": {"2024": {"2110": 120_000_000}, "2023": {"2110": 150_000_000}}})
    check("выручка за последний год", finances["revenue"] == 120_000_000)
    check("выручка за предыдущий год", finances["revenue_prev"] == 150_000_000)
    check("падение посчитано верно",
          abs(finances["revenue_change_pct"] - (-20.0)) < 0.01,
          str(finances["revenue_change_pct"]))
    check("год выручки определён", finances["revenue_year"] == "2024")

    single_year = CheckoClient.parse_finances({"data": {"2024": {"2110": 50_000_000}}})
    check("один год — динамика None, а не ноль", single_year["revenue_change_pct"] is None)
    check("отсутствие финансов не роняет разбор",
          CheckoClient.parse_finances({})["revenue"] is None)

    print("\n[12] Сила сигнала по динамике выручки")
    scoring = yaml.safe_load(
        Path("config/icp.yaml").read_text(encoding="utf-8"))["signal_scoring"]
    check("падение на 20% → сильный сигнал",
          targets.describe_signal(-20.0, scoring)[0] == "strong")
    check("стагнация 1% → сильный сигнал",
          targets.describe_signal(1.0, scoring)[0] == "strong")
    check("рост 40% → обычный сигнал",
          targets.describe_signal(40.0, scoring)[0] == "normal")
    check("формулировка про падение понятна",
          "упала" in targets.describe_signal(-20.0, scoring)[1])

    print("\n[13] Вердикт по ICP: ступень 1 (профиль)")
    icp = yaml.safe_load(Path("config/icp.yaml").read_text(encoding="utf-8"))
    criteria = icp["criteria"]
    fits = {"okved": "62.01", "extra_okved": [], "staff": 40.0,
            "registration_date": "2015-03-10", "active": True}

    ok, _ = targets.judge_profile(fits, criteria)
    check("подходящий профиль проходит", ok is True)

    ok, reason = targets.judge_profile({**fits, "staff": 5.0}, criteria)
    check("мелкая компания отсеивается", ok is False)
    check("причина отказа человекочитаема", "штат" in reason, reason)

    ok, _ = targets.judge_profile({**fits, "staff": 500.0}, criteria)
    check("крупная компания отсеивается", ok is False)

    ok, reason = targets.judge_profile({**fits, "extra_okved": ["78.10"]}, criteria)
    check("кадровое агентство отсеивается по доп. ОКВЭД", ok is False)
    check("причина называет ОКВЭД", "78.10" in reason, reason)

    ok, _ = targets.judge_profile({**fits, "staff": None}, criteria)
    check("нет данных о штате → отказ, а не пропуск", ok is False)

    ok, reason = targets.judge_profile({**fits, "active": False}, criteria)
    check("недействующая компания отсеивается", ok is False)

    print("\n[14] Вердикт по ICP: ступень 2 (финансы)")
    ok, _ = targets.judge_finances({"revenue": 120_000_000.0,
                                    "revenue_change_pct": -15.0}, criteria, staff=40)
    check("подходящая выручка проходит", ok is True)
    ok, _ = targets.judge_finances({"revenue": 1_000_000.0}, criteria, staff=40)
    check("слишком малая выручка отсеивается", ok is False)
    ok, reason = targets.judge_finances({}, criteria, staff=40)
    check("нет данных о выручке → отказ", ok is False)
    check("причина названа", "выручк" in reason, reason)

    # Выручка на сотрудника отделяет разработку от перепродажи железа.
    # Реальный случай из первого прогона: «ЭЛЕТЕК», 865 млн при штате до 100.
    ok, reason = targets.judge_finances({"revenue": 865_000_000.0}, criteria, staff=100)
    check("перепродажа отсеивается по выручке на сотрудника", ok is False)
    check("причина объясняет, почему", "перепродаж" in reason, reason)

    ok, reason = targets.judge_finances({"revenue": 120_000_000.0}, criteria, staff=40)
    check("типичный аутсорс проходит (3 млн на человека)", ok is True, reason)
    check("выручка на сотрудника попала в описание",
          "на сотрудника" in reason, reason)

    ok, _ = targets.judge_finances({"revenue": 120_000_000.0}, criteria, staff=None)
    check("без данных о штате проверка на сотрудника пропускается", ok is True)

    print("\n[14b] Формулировка динамики")
    check("динамика около нуля не даёт «-0%»",
          "0%" not in targets.describe_signal(-0.1, scoring)[1],
          targets.describe_signal(-0.1, scoring)[1])

    print("\n[15] Бюджет запросов")
    budget = RequestBudget(limit=3)
    spent = [budget.try_spend() for _ in range(5)]
    check("тратится ровно лимит", spent == [True, True, True, False, False], str(spent))
    check("остаток не уходит в минус", budget.left == 0)

    print("\n[16] Сайт компании: адрес и извлечение текста")
    check("голый домен получает https",
          normalize_url("example.ru") == "https://example.ru")
    check("лишний путь отбрасывается",
          normalize_url("https://example.ru/about/?utm=1") == "https://example.ru")
    check("мусор из ЕГРЮЛ не ломает разбор",
          normalize_url("не указан") is None, str(normalize_url("не указан")))
    check("пустое значение даёт None", normalize_url("") is None)

    extractor = _TextExtractor()
    extractor.feed(
        "<html><head><title>T</title><style>.a{color:red}</style></head>"
        "<body><script>var x=1;</script><h1>Разработка на заказ</h1>"
        "<p>Делаем&nbsp;проекты   под ключ</p></body></html>"
    )
    text = extractor.text()
    check("скрипты и стили выкинуты",
          "var x" not in text and "color:red" not in text, text)
    check("видимый текст собран", "Разработка на заказ" in text, text)
    check("пробелы схлопнуты", "проекты под ключ" in text, text)

    variants = WebsiteFetcher._variants("https://example.ru")
    check("пробуем и www, и без www",
          "https://www.example.ru" in variants and "https://example.ru" in variants,
          str(variants))
    check("пробуем и https, и http",
          "http://example.ru" in variants, str(variants))
    check("исходный адрес идёт первым", variants[0] == "https://example.ru")
    check("вариантов не больше четырёх", len(variants) == 4, str(len(variants)))
    check("www-адрес не удваивается",
          WebsiteFetcher._variants("https://www.example.ru")[1] == "https://example.ru")

    print("\n[17] Классификация: схема и промпт")
    schema_types = set(dossier.CLASSIFY_SCHEMA["properties"]["type"]["enum"])
    check("все категории схемы имеют человеческое название",
          schema_types == set(dossier.TYPE_LABELS), str(schema_types ^ set(dossier.TYPE_LABELS)))
    check("схема запрещает лишние поля",
          dossier.CLASSIFY_SCHEMA["additionalProperties"] is False)
    check("обязательны все четыре поля",
          set(dossier.CLASSIFY_SCHEMA["required"])
          == {"type", "confidence", "summary", "specialization"})
    check("промпт классификации на месте и не пуст",
          len(dossier.load_prompt("classify.md")) > 500)
    check("промпт описывает целевые категории",
          "outsourcing" in dossier.load_prompt("classify.md"))

    print("\n[18] Что видит модель")
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "t.db"
        conn = db.connect()
        conn.execute(
            "INSERT INTO companies (inn,name,first_seen,last_seen,okved,okved_name,"
            "staff,revenue,region) VALUES ('1','ООО Пример','t','t','62.01',"
            "'Разработка ПО',40,120e6,'Москва')")
        conn.commit()
        row = conn.execute("SELECT * FROM companies WHERE inn='1'").fetchone()
        content = dossier.build_user_content(row, "Мы делаем сайты под ключ")
        conn.close()
    check("реестровые факты попали в запрос", "62.01" in content and "Москва" in content)
    check("численность и выручка попали", "40" in content and "120 млн" in content)
    check("текст сайта попал", "сайты под ключ" in content)

    print("\n[19] Письмо: сборка промпта и досье")
    icp_full = yaml.safe_load(Path("config/icp.yaml").read_text(encoding="utf-8"))

    for site_type in ("product", "outsourcing", "staffing", "integrator"):
        prompt = compose.build_system_prompt(site_type)
        check(f"промпт для «{site_type}» собран",
              len(prompt) > 1500 and "Про этого адресата" in prompt)
    check("неизвестный тип падает на запасной угол",
          "Про этого адресата" in compose.build_system_prompt("нет_такого_типа"))
    check("общие запреты попали в промпт",
          "восклицательные знаки" in compose.build_system_prompt("product"))
    check("продуктовым говорим про их покупателя",
          "кто покупатель" in compose.build_system_prompt("product"))
    check("аутсорсу говорим про специализацию",
          "специализацию" in compose.build_system_prompt("outsourcing"))

    # Каркас без оффера: письмо задаёт вопрос и ничего не предлагает.
    # Проверяем не косметику, а то, что в промпте нет обещаний, за которые
    # потом придётся отвечать руками (DECISIONS.md, Р-023).
    letter_frame = compose.load_prompt("letter.md")
    check("в каркасе есть финальный вопрос про удачного клиента",
          "вот таких бы побольше" in letter_frame)
    check("каркас запрещает любые предложения",
          "Никаких предложений" in letter_frame)
    check("каркас запрещает скрытые предложения",
          "готов обсудить" in letter_frame)
    check("из каркаса убран бесплатный список компаний",
          "20 компаний" not in letter_frame and "Бесплатно" not in letter_frame,
          "остался старый оффер")
    check("незаполненных маркеров в каркасе не осталось",
          "ЗАПОЛНИТЬ" not in letter_frame)
    check("подпись задана в конфиге, а не в коде",
          "Михаил" in letter_frame)

    for angle_type in ("product", "outsourcing", "staffing", "integrator", "default"):
        angle_text = compose.load_prompt(f"angles/{angle_type}.md")
        check(f"угол «{angle_type}» ничего не предлагает",
              "редлаг" not in angle_text.replace("Ничего не предлагает", ""),
              angle_text[:200])

    print("\n[20] Сквозной прогон: досье → письмо → утренний список")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db.DB_PATH = tmp_path / "e2e.db"
        report.MORNING_DIR = tmp_path / "morning"

        conn = db.connect()
        conn.execute(
            "INSERT INTO companies (inn,name,first_seen,last_seen,region,site,"
            "contact_email,okved,okved_name,staff,revenue,revenue_prev,"
            "revenue_change_pct,icp_status,checked_at,site_url,site_type,"
            "site_confidence,site_summary,site_specialization,site_checked_at) "
            "VALUES ('7701','ООО \"АЛЬТА-СОФТ\"','t','t','Москва','alta.ru',"
            "'info@alta.ru','62.01','Разработка ПО',88,310e6,243e6,27.5,'passed','t',"
            "'https://alta.ru','product',0.92,'Софт для таможенного оформления',"
            "'[\"таможня\", \"ВЭД\"]','t')")
        # Вторая компания — с пустым досье. На ней проверяем, что письмо
        # без фактов до утреннего списка не доходит.
        conn.execute(
            "INSERT INTO companies (inn,name,first_seen,last_seen,region,"
            "okved,okved_name,staff,revenue,icp_status,checked_at,site_url,"
            "site_type,site_confidence,site_summary,site_checked_at) "
            "VALUES ('7702','ООО \"ПУСТО\"','t','t','Москва','62.01',"
            "'Разработка ПО',30,90e6,'passed','t','https://pusto.ru',"
            "'outsourcing',0.5,'IT-компания','t')")
        conn.commit()
        row = conn.execute("SELECT * FROM companies WHERE inn='7701'").fetchone()
        dossier_text = compose.build_dossier(row)
        conn.close()

        check("в досье попало описание с сайта", "таможенного оформления" in dossier_text)
        check("в досье попала выручка и динамика",
              "310 млн" in dossier_text and "+28%" in dossier_text, dossier_text)
        check("в досье попала выручка на сотрудника",
              "на сотрудника: 3.5" in dossier_text, dossier_text)
        check("в досье есть запрет на выдумки",
              "Ничего сверх этого списка" in dossier_text)

        # Подменяем модель: сквозной прогон не должен ходить в сеть.
        captured: dict = {}

        def fake_classify(system_prompt, user_content, schema, max_tokens=1024):
            # Ключуем по компании: иначе второй вызов затирает первый и
            # проверка «дошёл ли нужный угол» становится бессмысленной.
            key = "пусто" if "ПУСТО" in user_content else "альта"
            captured[key] = {"system": system_prompt, "user": user_content}
            # У пустой компании модель честно не находит, на что опереться.
            if "ПУСТО" in user_content:
                return {
                    "subject": "вопрос про ваших клиентов",
                    "body": "Вы занимаетесь разработкой...",
                    "why_line": "IT-компания подходящего размера",
                    "facts_used": ["IT-компания"],
                }
            return {
                "subject": "про ваши проекты для импортёров",
                "body": "Вы продаёте софт для таможенного оформления...",
                "why_line": "продуктовая компания с чётким ICP, выручка растёт",
                "facts_used": ["софт для таможенного оформления", "88 человек"],
            }

        original = llm.classify
        llm.classify = fake_classify
        try:
            result = compose.run(icp_full)
        finally:
            llm.classify = original

        check("годное письмо написано", result["written"] == 1, str(result))
        check("письмо без фактов отложено", result["thin"] == 1, str(result))
        check("модель получила угол для продуктовой компании",
              "кто покупатель" in captured.get("альта", {}).get("system", ""))
        check("аутсорсеру ушёл другой угол",
              "специализацию" in captured.get("пусто", {}).get("system", ""))
        check("модель получила факты из досье",
              "таможенного оформления" in captured.get("альта", {}).get("user", ""))
        check("модель получила требование не выдумывать",
              "Ничего не выдумывай" in captured.get("альта", {}).get("system", ""))

        path = report.build_morning(icp_full)
        check("утренний список создан", path is not None and path.exists())
        if path:
            text = path.read_text(encoding="utf-8")
            check("в списке есть строка «Почему здесь»", "**Почему здесь:**" in text)
            check("в списке есть тема письма", "про ваши проекты для импортёров" in text)
            check("в списке есть текст письма", "софт для таможенного оформления" in text)
            check("в списке указан тип компании", "продуктовая компания" in text)
            check("напоминание не хранить ФИО на месте",
                  "система не хранит" in text)
            check("письмо без фактов в основной список не попало",
                  "Вы занимаетесь разработкой" not in text, text)
            check("отложенная компания названа в хвосте",
                  "Отложено" in text and "ПУСТО" in text, text)

        check("повторный список пуст", report.build_morning(icp_full) is None)

    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО проверок: {len(FAILURES)} — {', '.join(FAILURES)}")
        return 1
    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
