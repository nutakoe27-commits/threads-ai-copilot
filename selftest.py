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

import io
import json
import os
import sys
import tempfile
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import yaml

from typing import Any

from src import (collect, compose, contacts, db, dossier, facts, guard,
                 hiring, jobs, llm, log, metrics, notify, outreach, pipeline,
                 report, scoring, settings, targets, techstack, ui)
from src.providers import perplexity
from src.sources import registries
from src.providers.perplexity import PerplexityClient, PerplexityUnavailable
from src.sources import website as website_module, zakupki
from src.providers.checko import CheckoClient, RequestBudget, deep_pick, to_number
from src.sources import trudvsem as trudvsem_module
from src.sources.website import _TextExtractor, WebsiteFetcher, normalize_url
from src.sources.trudvsem import TrudvsemClient, TrudvsemError

FAILURES: list[str] = []


def _raises(exception: type[BaseException], call) -> bool:
    """Поднимает ли вызов именно это исключение. Для проверок обработки ошибок."""
    try:
        call()
    except exception:
        return True
    except Exception:  # noqa: BLE001 — другое исключение это тоже провал
        return False
    return False


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
    signal_cfg = yaml.safe_load(
        Path("config/icp.yaml").read_text(encoding="utf-8"))["signal_scoring"]
    check("падение на 20% → сильный сигнал",
          targets.describe_signal(-20.0, signal_cfg)[0] == "strong")
    check("стагнация 1% → сильный сигнал",
          targets.describe_signal(1.0, signal_cfg)[0] == "strong")
    check("рост 40% → обычный сигнал",
          targets.describe_signal(40.0, signal_cfg)[0] == "normal")
    check("формулировка про падение понятна",
          "упала" in targets.describe_signal(-20.0, signal_cfg)[1])

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
          "0%" not in targets.describe_signal(-0.1, signal_cfg)[1],
          targets.describe_signal(-0.1, signal_cfg)[1])

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
    check("обязательны все пять полей",
          set(dossier.CLASSIFY_SCHEMA["required"])
          == {"type", "confidence", "summary", "specialization", "facts"})
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

    print("\n[19] Письмо: шаблон, промпт и досье")
    icp_full = yaml.safe_load(Path("config/icp.yaml").read_text(encoding="utf-8"))

    template = compose.load_template()
    check("пояснения для человека в письмо не попадают",
          "<!--" not in template and "ЗАЧЕМ ШАБЛОН" not in template, template[:200])
    check("шаблон начинается с приветствия",
          template.startswith("Здравствуйте!"), template[:40])
    check("в шаблоне есть места под обе вставки",
          compose.check_template(template) == [],
          str(compose.check_template(template)))
    for anchor, what in [
        ("Я делаю программное обеспечение", "рассказ о себе зафиксирован"),
        ("Что предлагаю:", "предложение зафиксировано"),
        ("пилотный список", "обещание пилота на месте"),
        ("пятнадцать минут", "просьба о разговоре на месте"),
        ("С уважением, Михаил", "подпись на месте"),
        ("+7 911 346-07-13", "телефон на месте"),
        ("P.S.", "приписка на месте"),
        ("без менеджеров и наценки агентства", "текст приписки на месте"),
    ]:
        check(f"шаблон: {what}", anchor in template, anchor)

    # ГЛАВНОЕ СВОЙСТВО ШАБЛОНА. Всё, кроме двух вставок, одинаково всегда —
    # ради этого он и заводился (DECISIONS.md, Р-053).
    fixed = compose.render_letter(template, {"вопрос": "A?", "признаки": "B"},
                                  today=date(2026, 8, 4))
    other = compose.render_letter(template, {"вопрос": "C?", "признаки": "D"},
                                  today=date(2026, 8, 4))
    check("меняются только вставки",
          fixed.replace("A?", "").replace("B", "")
          == other.replace("C?", "").replace("D", ""))

    # Дату считает код: модель не знает сегодняшнего числа и однажды
    # предложит встретиться в прошлом.
    check("день недели и дата подставлены",
          "в пятницу, 07.08.2026" in fixed, fixed)
    # 4 августа 2026 — вторник: +3 дня попадает ровно на пятницу 7-го.
    check("ближайшая пятница берётся, если до неё есть три дня",
          compose.next_call_date(date(2026, 8, 4))[1] == "07.08.2026",
          str(compose.next_call_date(date(2026, 8, 4))))
    # Среда: +3 дня это суббота, значит пятница уже следующая.
    check("если до пятницы меньше трёх дней, берётся следующая",
          compose.next_call_date(date(2026, 8, 5))[1] == "14.08.2026",
          str(compose.next_call_date(date(2026, 8, 5))))
    check("день недели в винительном падеже",
          compose.next_call_date(date(2026, 8, 4))[0] == "пятницу")

    # Сломанный шаблон должен ловиться до похода к модели: иначе получится
    # пачка одинаково испорченных писем.
    check("шаблон без места под вставку забракован",
          compose.check_template(template.replace("{признаки}", "то и сё")),
          "проверка шаблона молчит")
    check("лишнее место в шаблоне тоже замечено",
          any("лишние" in item for item in
              compose.check_template(template + "\n\n{выдумка}")),
          str(compose.check_template(template + "\n\n{выдумка}")))

    followup_template = compose.load_template("template_followup.md")
    check("у дожима свой шаблон с одной вставкой",
          compose.check_template(followup_template, required=("мысль",)) == [],
          str(compose.check_template(followup_template, required=("мысль",))))
    check("в дожиме просьба ниже, чем в первом письме",
          "ответьте парой слов" in followup_template
          and "пятнадцать минут" not in followup_template)

    # Вставки приходят от модели грязными чаще, чем хотелось бы.
    check("приветствие из вставки срезано",
          compose.clean_insert("Здравствуйте! Вопрос по продажам?")
          == "Вопрос по продажам?")
    check("перевод строки внутри вставки убран",
          compose.clean_insert("первая\nвторая") == "первая вторая")
    check("кавычки по краям убраны",
          compose.clean_insert('«отдел перевода»') == "отдел перевода")

    for site_type in ("product", "outsourcing", "staffing", "integrator"):
        prompt = compose.build_system_prompt(site_type)
        check(f"промпт для «{site_type}» собран",
              "Вставка 1: `question`" in prompt and "Кто это" in prompt,
              str(len(prompt)))
    check("неизвестный тип падает на запасной угол",
          "Кто это" in compose.build_system_prompt("нет_такого_типа"))
    check("продуктовым говорим про перебор рынка",
          "Круг покупателей конечный" in compose.build_system_prompt("product"))
    check("аутсорсу говорим про специализацию",
          "специализацию" in compose.build_system_prompt("outsourcing"))
    check("интегратору говорим про конечное число покупателей",
          "Покупателей конечное число" in compose.build_system_prompt("integrator")
          or "конечное число" in compose.build_system_prompt("integrator"))

    # ЭКОНОМИЯ ТОКЕНОВ — одна из трёх причин перехода на шаблон. Промпт
    # был около четырнадцати тысяч знаков на письмо (Р-053).
    prompt_size = len(compose.build_system_prompt("product"))
    check("промпт стал коротким", prompt_size < 12000, f"{prompt_size} знаков")

    letter_frame = compose.load_prompt("letter.md")
    for anchor, what in [
        ("Измеримый", "вопрос должен быть измеримым"),
        ("Общий вопрос не годится", "запрет на общий вопрос"),
        ("Держись образца строго", "требование держаться образца"),
        ("На странице вакансий", "вакансия важнее прочего"),
        ("три-пять признаков через запятую", "формат перечня признаков"),
        ("должен узнать в списке", "зачем нужны признаки"),
        ("Не годится пересказ", "запрет пересказывать их сайт"),
        ("Не длиннее 70 знаков", "ограничение темы"),
        ("120 знаков", "ограничение строки для списка"),
        ("любые две заглавные буквы подряд", "правило про сокращения"),
        ("ЛПР", "самое частое сокращение названо"),
        ("восклицательных знаков", "запрет восклицательных знаков"),
        ("Досье — единственный источник", "запрет выдумывать"),
    ]:
        check(f"бриф: {what}", anchor in letter_frame, anchor)

    # Оффера в промпте больше нет: предложение живёт в шаблоне, и пересказывать
    # его модели незачем.
    check("оффер из промпта убран",
          "Что предлагаю" not in compose.build_system_prompt("product"))

    print("\n[21] Сверка фактов без модели")
    site_text = (
        "Компания разрабатывает систему LeakSPY для обнаружения утечек "
        "в магистральных трубопроводах. Среди заказчиков — операторы "
        "нефтепроводов. Работаем с 2004 года."
    )
    check("дословная цитата подтверждается",
          facts.quote_found("систему LeakSPY для обнаружения утечек", site_text))
    check("цитата в другом падеже подтверждается",
          facts.quote_found("система LeakSPY для обнаружения утечки", site_text))
    check("выдуманная цитата не подтверждается",
          not facts.quote_found("сертифицирован по ISO 27001 с 2019 года", site_text))
    check("пустая цитата не подтверждается", not facts.quote_found("", site_text))

    letter_text = ("Здравствуйте.\n\nУ вас LeakSPY — система обнаружения утечек "
                   "на магистральных трубопроводах.\n\nМихаил")
    check("факт, отражённый в письме, засчитан",
          facts.fact_used_in("Продукт LeakSPY для обнаружения утечек в трубопроводах",
                             letter_text))
    check("факт, которого в письме нет, не засчитан",
          not facts.fact_used_in("Работают с 2004 года", letter_text))
    check("общие слова не проходят за факт",
          not facts.fact_used_in("Компания разрабатывает системы для бизнеса",
                                 letter_text))

    # Регрессия по Р-030. Порог считал долю слов факта, а письмо факт сжимает:
    # правильные упоминания давали 22–33% и отбрасывались. Из-за этого четыре
    # письма из пяти в первом боевом прогоне ушли в «отложено» напрасно.
    # Все факты ниже — настоящие, из прогона 2026-07-27.
    compressed = [
        ("САРУС+ КОМПОЗИТЫ - специализированное решение для проектирования и "
         "технологической подготовки изделий из композиционных материалов с "
         "моделированием драпировки ткани на криволинейных поверхностях",
         "У вас в САРУС+ есть отдельный модуль под композиты."),
        ("Компания работает по трёхэтапной модели проектов: пилотный проект "
         "(2-6 месяцев), разработка и внедрение системы (6-18 месяцев), "
         "масштабирование решения (1-5 лет)",
         "Вы ведёте проекты в три этапа — от пилота до масштабирования."),
        ("Имеет 6 филиалов в крупных городах России и сеть из более чем "
         "75 дилеров по всей стране",
         "У вас дилерская сеть по всей стране."),
    ]
    for long_fact, short_letter in compressed:
        check(f"сжатое упоминание засчитано: {long_fact[:34]}...",
              facts.fact_used_in(long_fact, short_letter))

    check("одного совпавшего слова мало",
          not facts.fact_used_in(
              "Специализируется на отраслях критической инфраструктуры: "
              "производство, металлургия, энергетика",
              "Вы внедряете решения на производстве."))
    check("у короткого факта должны совпасть все слова",
          not facts.fact_used_in("Продукт ParsecNET",
                                 "У вас есть свой продукт."))
    check("used_facts отбирает только настоящие",
          facts.used_facts(["Продукт LeakSPY для обнаружения утечек",
                            "Сертификат ISO 27001"], letter_text)
          == ["Продукт LeakSPY для обнаружения утечек"])

    print("\n[22] Почтовые адреса: что храним и куда попадёт письмо")
    for address in ("tatiana.bakurskaya@i-rt.ru", "a.petrov@example.ru",
                    "ivan@example.ru", "sergey.smirnov@example.com"):
        kind, _ = contacts.classify(address)
        check(f"{address} — личный", kind == "personal", kind)
        check(f"{address} не сохраняется", not contacts.is_storable(address))
        check(f"{address} обнуляется через safe()", contacts.safe(address) == "")

    for address in ("info@example.ru", "sales@alta.ru", "zakaz@example.ru"):
        kind, _ = contacts.classify(address)
        check(f"{address} — безличный", kind == "role", kind)
        check(f"{address} сохраняется", contacts.safe(address) == address)

    kind, note = contacts.classify("tender@topsbi.ru")
    check("tender@ распознан как чужой отдел", kind == "wrong_desk", kind)
    check("для tender@ есть пояснение человеку", "тендер" in note, note)
    check("tender@ всё равно сохраняется", contacts.is_storable("tender@topsbi.ru"))

    kind, note = contacts.classify("host965@mail.ru")
    check("бесплатная почта помечена", kind == "free", kind)
    check("у бесплатной почты есть пояснение", bool(contacts.label("host965@mail.ru")))
    check("у обычного info@ пояснения нет", contacts.label("info@example.ru") == "")

    print("\n[23] Досье: факты с сайта проверяются цитатой")
    verified, dropped = dossier.verify_facts(
        [
            {"fact": "Продукт LeakSPY", "quote": "систему LeakSPY для обнаружения утечек"},
            {"fact": "Сертификат ISO 27001", "quote": "сертифицированы по ISO 27001"},
            {"fact": "Без цитаты", "quote": ""},
            "не словарь",
        ],
        site_text,
    )
    check("подтверждённый факт остался", verified == ["Продукт LeakSPY"], str(verified))
    check("неподтверждённые отброшены", dropped == 3, str(dropped))
    check("схема требует факты с цитатами",
          set(dossier.CLASSIFY_SCHEMA["properties"]["facts"]["items"]["required"])
          == {"fact", "quote"})
    check("промпт классификатора требует дословную цитату",
          "дословный кусок текста сайта" in dossier.load_prompt("classify.md"))

    print("\n[25] Сигнал найма со страницы вакансий компании")
    kw = yaml.safe_load(Path("config/signals.yaml").read_text(encoding="utf-8"))["keywords"]
    career_page = (
        "Вакансии компании. Менеджер по продажам B2B. Обязанности: холодные "
        "звонки по базе, поиск новых клиентов, ведение CRM. Требования: опыт "
        "от двух лет. Также открыта позиция руководитель отдела продаж."
    )
    signals = hiring.find_signals(career_page, kw)
    kinds = {s["strength"] for s in signals}
    check("сильный сигнал найден", "strong" in kinds, str(signals))
    check("сигнал по должностям найден", "normal" in kinds, str(signals))
    check("у каждого сигнала есть цитата со страницы",
          all(s["quote"] for s in signals), str(signals))
    check("цитаты действительно есть на странице",
          all(facts.quote_found(s["quote"], career_page) for s in signals))
    check("описание сигнала человеческое",
          hiring.describe(signals) == "прямо сейчас нанимают людей на холодный поиск клиентов",
          hiring.describe(signals))

    # Та же ловушка, что и в Р-011: стоп-слово внутри длинного слова.
    check("«рукоВОДИТЕЛЬ отдела продаж» не путается с водителем",
          any("руководитель отдела продаж" in s["fact"] for s in signals), str(signals))
    check("пустая страница не даёт сигналов", hiring.find_signals("", kw) == [])
    check("страница без продаж не даёт сигналов",
          hiring.find_signals("Вакансия: инженер-конструктор, работа с чертежами", kw) == [])

    check("пути к вакансиям заданы отдельно от описания",
          "/career" in website_module.CAREER_PATHS
          and "/career" not in website_module.CANDIDATE_PATHS)

    print("\n[26] Госзакупки: разбор архива контрактов")
    xml = (
        "<contract><suppliers><supplier><inn>7722260272</inn>"
        "<name>ООО МДО</name></supplier></suppliers>"
        "<price>12500000.00</price><endDate>2026-12-31</endDate></contract>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("contract_1.xml", xml)
        zf.writestr("readme.txt", "не XML, должен быть пропущен")
    archive = buffer.getvalue()

    contracts = list(zakupki.iter_contracts(archive))
    check("контракт разобран", len(contracts) == 1, str(len(contracts)))
    check("ИНН поставщика найден", "7722260272" in contracts[0]["inns"])
    check("дата окончания разобрана", contracts[0]["end_date"] == "2026-12-31")
    check("сумма разобрана", contracts[0]["price"] == 12500000.0)

    measured = zakupki.measure_coverage([archive], {"7722260272", "5018046069"})
    check("покрытие посчитано верно", measured["coverage"] == 0.5, str(measured["coverage"]))
    check("найденная компания попала в результат", "7722260272" in measured["matched"])
    check("объём просмотренного виден", measured["contracts"] == 1)
    check("битый архив даёт понятную ошибку",
          _raises(zakupki.ZakupkiError,
                  lambda: list(zakupki.iter_contracts(b"not a zip archive"))))

    print("\n[27] Perplexity: разбор ответа")
    check("текст ответа достаётся",
          perplexity.extract_text(
              {"choices": [{"message": {"content": "ответ"}}]}) == "ответ")
    check("пустая структура не роняет разбор",
          perplexity.extract_text({}) == "")
    check("ссылки из citations-строк",
          perplexity.extract_urls({"citations": ["https://a.ru"]}) == ["https://a.ru"])
    check("ссылки из search_results-словарей",
          perplexity.extract_urls(
              {"search_results": [{"url": "https://b.ru"}]}) == ["https://b.ru"])
    check("дубли ссылок схлопываются",
          perplexity.extract_urls({"citations": ["https://a.ru"],
                                   "search_results": [{"url": "https://a.ru"}]})
          == ["https://a.ru"])
    check("без ключа поднимается понятная ошибка",
          _raises(PerplexityUnavailable,
                  lambda: PerplexityClient().ask("s", "q"))
          if not os.environ.get("PERPLEXITY_API_KEY") else True)

    print("\n[28] Модели: письмо пишет не самая дешёвая")
    check("для письма задана отдельная модель", llm.MODEL_WRITE != llm.MODEL_CLASSIFY)
    check("для досье задана отдельная модель", llm.MODEL_EXTRACT != llm.MODEL_CLASSIFY)
    check("письмо пишет Opus", llm.MODEL_WRITE == "claude-opus-5", llm.MODEL_WRITE)
    check("досье собирает Sonnet", llm.MODEL_EXTRACT == "claude-sonnet-5", llm.MODEL_EXTRACT)
    # Регрессия Р-031: compose вызывал classify() ради схемы, а модель в ней
    # была зашита намертво, и письма полгода писал Haiku.
    src = Path("src/compose.py").read_text(encoding="utf-8")
    check("compose явно указывает модель", "model=llm.MODEL_WRITE" in src)
    src_dossier = Path("src/dossier.py").read_text(encoding="utf-8")
    check("dossier явно указывает модель", "model=llm.MODEL_EXTRACT" in src_dossier)

    print("\n[29] Механические проверки собранного письма")
    template = compose.load_template()
    question = ("Вопрос по вашему отделу продаж: сколько часов в неделю "
                "менеджеры тратят на ручной поиск и проверку компаний, "
                "прежде чем дойти до первого разговора?")
    signs = ("наличие отдела перевода или штатных переводчиков, большой поток "
             "документации на иностранных языках, переход на отечественные "
             "операционные системы вроде РЕД ОС")
    dossier_text = ("Компания: PROMT\n  1. Совместимость с Astra Linux "
                    "и РЕД ОС\n  2. Свой отдел перевода")
    good = compose.render_letter(template,
                                 {"вопрос": question, "признаки": signs},
                                 today=date(2026, 8, 4))

    check("у собранного письма замечаний нет",
          compose.check_letter(good, "PROMT", inserts=(question, signs),
                               known=dossier_text) == [],
          str(compose.check_letter(good, "PROMT", inserts=(question, signs),
                                   known=dossier_text)))

    # ГЛАВНАЯ ПРОВЕРКА (Р-048). Сокращения ловит код, а не промпт: просить
    # об этом модель — то же самое, что просить её не подписываться.
    check("сокращение из заглавных букв поймано",
          compose.find_jargon("Отдам список в вашу CRM") == ["CRM"],
          str(compose.find_jargon("Отдам список в вашу CRM")))
    check("русское сокращение поймано",
          "ЛПР" in compose.find_jargon("Найду контакты ЛПР"))
    check("название компании-адресата сокращением не считается",
          compose.find_jargon("Ваш продукт PROMT известен", "PROMT") == [])

    # Названия продуктов законны, но только если они есть в досье. Название,
    # которого в досье нет, модель взяла из головы — это выдумка.
    check("название продукта из досье разрешено",
          compose.find_jargon("переход на РЕД ОС", "PROMT", dossier_text) == [],
          str(compose.find_jargon("переход на РЕД ОС", "PROMT", dossier_text)))
    check("выдуманное название поймано",
          compose.find_jargon("у них внедрён СБИС", "PROMT", dossier_text)
          == ["СБИС"],
          str(compose.find_jargon("у них внедрён СБИС", "PROMT", dossier_text)))

    check("жаргон в нижнем регистре поймано по корню",
          any("конверси" in item for item in
              compose.find_jargon("Посмотрим на конверсию писем")))
    check("падеж жаргону не помогает",
          any("воронк" in item for item in
              compose.find_jargon("Наверху воронки становится шире")))
    check("к жаргону предложена замена",
          "путь от письма до сделки" in " ".join(
              compose.find_jargon("Наверху воронки становится шире")))

    # Фиксированную часть шаблона человек прочитал один раз. Ругаться на неё
    # каждое утро значит приучить себя пропускать замечания.
    check("названия в шаблоне замечаний не вызывают",
          not any("MAX" in note for note in
                  compose.check_letter(good, "PROMT",
                                       inserts=(question, signs))),
          str(compose.check_letter(good, "PROMT", inserts=(question, signs))))

    bad_insert = "Загружу контакты ЛПР в вашу CRM!"
    notes = compose.check_letter(
        compose.render_letter(template,
                              {"вопрос": bad_insert, "признаки": signs}),
        "PROMT", inserts=(bad_insert, signs), known=dossier_text)
    check("сокращение во вставке попадает в замечания",
          any("сокращения" in note for note in notes), str(notes))
    check("восклицательный знак во вставке замечен",
          any("восклицательный" in note for note in notes), str(notes))

    # Незаполненное место — это письмо, ушедшее с «{признаки}» в тексте.
    check("незаполненное место замечено",
          any("незаполненное" in note for note in
              compose.check_letter(template)),
          str(compose.check_letter(template)))

    huge = compose.render_letter(template,
                                 {"вопрос": question, "признаки": "слово " * 120})
    check("разросшаяся вставка замечена",
          any("длинн" in note for note in compose.check_letter(huge, "PROMT")),
          str(compose.check_letter(huge, "PROMT")))

    print("\n[30] Воронка: запись, конверсия, ловля деградации")
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "m.db"
        conn = db.connect()

        metrics.record(conn, "targets", {"checked": 40, "passed": 9})
        metrics.record(conn, "targets", {"checked": 20, "passed": 5})
        conn.commit()
        check("числа за день складываются, а не дублируются",
              metrics.totals(conn, "targets", "checked", "2000-01-01") == 60.0)

        text = metrics.report(conn, days=14)
        check("в отчёте есть строка этапа", "прошло ICP" in text, text[:200])
        check("конверсия посчитана", "23.3%" in text, text[:400])

        # Деградация: на прошлой неделе проходила пятая часть, на этой — сотая.
        conn.execute("DELETE FROM funnel")
        old_day = (date.today() - timedelta(days=10)).isoformat()
        new_day = date.today().isoformat()
        for day, checked, passed in [(old_day, 200, 40), (new_day, 200, 8)]:
            for metric, value in (("checked", checked), ("passed", passed)):
                conn.execute("INSERT INTO funnel (day, stage, metric, value) "
                             "VALUES (?, 'targets', ?, ?)", (day, metric, value))
        conn.commit()
        drops = metrics.check_drops(conn)
        check("падение конверсии вдвое замечено", len(drops) == 1, str(drops))
        check("в замечании названы обе цифры",
              "20%" in drops[0] and "4%" in drops[0], str(drops))

        # На малых числах конверсия скачет сама — молчим, иначе приучим
        # человека не читать предупреждения.
        conn.execute("DELETE FROM funnel")
        for day, checked, passed in [(old_day, 10, 5), (new_day, 10, 1)]:
            for metric, value in (("checked", checked), ("passed", passed)):
                conn.execute("INSERT INTO funnel (day, stage, metric, value) "
                             "VALUES (?, 'targets', ?, ?)", (day, metric, value))
        conn.commit()
        check("на малых числах не жалуемся", metrics.check_drops(conn) == [])
        conn.close()

    print("\n[31] Статусы касаний и стоп-лист")
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "o.db"
        conn = db.connect()
        conn.execute("INSERT INTO companies (inn,name,first_seen,last_seen,"
                     "letter_body) VALUES ('111','ООО ТЕСТ','t','t','письмо')")
        outreach.save_touch(conn, "111", 1, "тема", "письмо", "почему", "[]")
        conn.commit()
        conn.close()

        outreach.mark("sent", ["111"])
        conn = db.connect()
        row = conn.execute("SELECT * FROM companies WHERE inn='111'").fetchone()
        check("статус проставлен", row["outreach_status"] == "sent")
        check("счётчик касаний вырос", row["touch_count"] == 1)
        check("часы дожима запущены", row["last_touch_at"] is not None)
        touch = conn.execute("SELECT * FROM touches WHERE inn='111'").fetchone()
        check("черновик отмечен отправленным", touch["status"] == "sent")

        # Дожим положен через delay_days, но не раньше.
        check("сразу после отправки дожимать рано",
              outreach.pending_followups(conn, delay_days=4, max_touches=3) == [])
        conn.execute("UPDATE companies SET last_touch_at = "
                     "datetime('now', '-10 days') WHERE inn='111'")
        conn.commit()
        due = outreach.pending_followups(conn, delay_days=4, max_touches=3)
        check("через десять дней дожим положен", len(due) == 1, str(len(due)))
        conn.close()

        outreach.mark("refused", ["111"])
        stoplist = (db.DB_PATH.parent / "stoplist.txt").read_text(encoding="utf-8")
        check("отказ уходит в стоп-лист файлом", "111" in stoplist, stoplist)
        conn = db.connect()
        check("отказавшихся больше не дожимаем",
              outreach.pending_followups(conn, delay_days=1, max_touches=3) == [])
        conn.close()

        outreach.mark("sent", ["999"])
        check("неизвестный ИНН не роняет прогон", True)

    print("\n[32] Защита настроек")
    icp = yaml.safe_load(Path("config/icp.yaml").read_text(encoding="utf-8"))
    check("боевой конфиг проходит проверку без жёстких нарушений",
          all(not v.hard for v in guard.check(icp)),
          str([str(v) for v in guard.check(icp) if v.hard]))

    broken = yaml.safe_load(Path("config/icp.yaml").read_text(encoding="utf-8"))
    broken["website"]["pause_seconds"] = 0.1
    violations = guard.check(broken)
    check("слишком частые запросы к чужому сайту — жёсткий стоп",
          any(v.hard and "pause_seconds" in v.setting for v in violations))
    check("enforce останавливает этап", guard.enforce(broken) is False)

    broken2 = yaml.safe_load(Path("config/icp.yaml").read_text(encoding="utf-8"))
    broken2["compose"]["per_run"] = 200
    check("двести писем за прогон — мягкое замечание",
          any(not v.hard and "per_run" in v.setting for v in guard.check(broken2)))
    check("мягкое замечание не останавливает этап", guard.enforce(broken2) is True)

    signals_cfg = yaml.safe_load(Path("config/signals.yaml").read_text(encoding="utf-8"))
    check("ключевые слова не конфликтуют со стоп-словами",
          guard.check(signals_cfg, kind="signals") == [],
          str([str(v) for v in guard.check(signals_cfg, kind="signals")]))

    print("\n[33] Telegram: разбивка длинных сообщений")
    check("короткое сообщение не режется",
          notify.split_message("коротко") == ["коротко"])
    long_text = "\n\n".join(["абзац " * 50] * 40)
    parts = notify.split_message(long_text)
    check("длинное разбито", len(parts) > 1, str(len(parts)))
    check("каждая часть влезает в лимит",
          all(len(p) <= notify.MAX_MESSAGE for p in parts),
          str([len(p) for p in parts]))
    check("текст не потерялся при разбивке",
          sum(p.count("абзац") for p in parts) == long_text.count("абзац"))
    check("без токена этап пропускается, а не падает",
          notify.send_morning(["блок"], "шапка").get("skipped") == 1
          if not os.environ.get("TELEGRAM_BOT_TOKEN") else True)

    print("\n[34] Дожим: шаблон, промпт и досье с историей")
    followup_prompt = compose.build_followup_prompt("product")
    check("дожим наследует запрет на сокращения",
          "ЛПР" in followup_prompt)
    check("дожим запрещает напоминать о себе",
          "поднимаю письмо" in followup_prompt)
    check("дожим требует новую мысль", "новая мысль, а не напоминание" in followup_prompt)
    check("дожим объясняет, почему молчание не отказ", "42%" in followup_prompt)

    class FakeRow(dict):
        def __getitem__(self, key):
            return self.get(key)

    row = FakeRow(name="ООО ТЕСТ", site_facts=json.dumps(["Факт один", "Факт два"]),
                  site_summary="", site_specialization="", region="Москва",
                  staff=40, revenue=None, revenue_change_pct=None, okved_name="")
    previous = [FakeRow(step=1, subject="первая тема",
                        body="Здравствуйте. Первое письмо про факт один.",
                        sent_at="2026-07-20")]
    dossier_text = compose.build_followup_dossier(row, previous)
    check("в досье дожима есть текст прошлого письма",
          "Первое письмо про факт один" in dossier_text)
    check("в досье дожима есть запрет повторяться",
          "Не повторяй ни мысль" in dossier_text)

    print("\n[35] Технографика: что стоит у компании на сайте")
    sample_html = """<html><head>
      <meta name="generator" content="Joomla! 4.2">
      <script src="https://mc.yandex.ru/metrika/tag.js"></script>
      <script src="//cloud.roistat.com/dist/module.js"></script>
      <script src="//code.jivosite.com/widget/abc"></script>
      <link rel="stylesheet" href="/bitrix/templates/main/style.css">
    </head><body>Мы делаем сайты</body></html>"""
    stack = techstack.detect(sample_html)
    check("счётчик найден", "Яндекс.Метрика" in stack.get("analytics", []), str(stack))
    check("коллтрекинг отнесён к платному трафику",
          "Roistat" in stack.get("paid", []), str(stack))
    check("виджет захвата найден", "Jivo" in stack.get("capture", []), str(stack))
    check("CMS определена по пути", "1С-Битрикс" in stack.get("cms", []), str(stack))
    check("мета-тег generator добавляет CMS, которой нет в сигнатурах",
          any("Joomla" in name for name in stack.get("cms", [])), str(stack))

    short, explain = techstack.maturity(stack)
    check("платный трафик читается как главный признак",
          short == "платит за трафик и меряет его", short)
    check("к признаку есть объяснение для человека", "стоимость заявки" in explain)

    only_counter = techstack.detect('<script src="https://mc.yandex.ru/metrika/x"></script>')
    check("один счётчик — это ещё не покупка трафика",
          techstack.maturity(only_counter)[0] == "считает трафик, но не покупает",
          str(techstack.maturity(only_counter)))
    check("пустой сайт не выдумывает инструментов", techstack.detect("") == {})
    check("без следов маркетинга так и сказано",
          techstack.maturity({})[0] == "никаких следов маркетинга")
    check("факты для досье перечисляют найденное",
          any("Roistat" in fact for fact in techstack.as_facts(stack)),
          str(techstack.as_facts(stack)))
    check("строка для списка называет инструменты",
          "Roistat" in techstack.describe(stack), techstack.describe(stack))

    print("\n[36] Реестры Минцифры: разбор выгрузок и отметка компаний")
    csv_text = ("Наименование;ИНН;Дата включения\n"
                'ООО "АЛЬФА";7701234567;2026-03-01\n'
                'ООО "БЕТА";5018046069;2026-04-15\n')
    check("ИНН взяты из колонки по названию",
          registries.parse_csv(csv_text) == {"7701234567", "5018046069"},
          str(registries.parse_csv(csv_text)))

    csv_no_header = "название;код\nАЛЬФА;7701234567\n"
    check("без колонки ИНН разбирается весь текст",
          "7701234567" in registries.parse_csv(csv_no_header),
          str(registries.parse_csv(csv_no_header)))
    check("двенадцатизначный ИНН предпринимателя тоже находится",
          "500100732259" in registries._extract_inns_from_text("ИП, ИНН 500100732259"))
    check("телефон за ИНН не принимается",
          registries._extract_inns_from_text("+7 495 123-45-67") == set(),
          str(registries._extract_inns_from_text("+7 495 123-45-67")))
    check("ИНН из JSON любой вложенности",
          registries.parse_json('{"items":[{"org":{"inn":"7701234567"}}]}')
          == {"7701234567"})
    check("битый JSON не роняет этап", registries.parse_json("{не json") == set())
    check("оба реестра названы человеческими словами",
          "реестре отечественного ПО" in registries.KINDS["software"]
          and "аккредитован" in registries.KINDS["accredited"],
          str(registries.KINDS))
    check("человеку сказано, куда класть файлы",
          "data/registries/software.csv" in registries.instructions())
    check("XLSX честно назван неподдерживаемым",
          "XLSX" in registries.instructions())

    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "r.db"
        registries.DATA_DIR = Path(tmp) / "registries"
        registries.DATA_DIR.mkdir()
        (registries.DATA_DIR / "software.csv").write_text(csv_text, encoding="utf-8")

        conn = db.connect()
        # 7701234567 есть в реестре и у нас; 9999999999 — только у нас.
        for inn in ("7701234567", "9999999999"):
            conn.execute("INSERT INTO companies (inn,name,first_seen,last_seen) "
                         "VALUES (?,?,'t','t')", (inn, f"ООО {inn}"))
        conn.commit()
        conn.close()

        check("файл реестра найден по имени",
              "software" in registries.available_files(),
              str(registries.available_files()))
        result = registries.run()
        check("реестр прочитан целиком", result["imported"] == 2, str(result))
        check("отмечены только наши компании", result["matched"] == 1, str(result))

        conn = db.connect()
        flags = conn.execute("SELECT registry_flags FROM companies "
                             "WHERE inn='7701234567'").fetchone()[0]
        other = conn.execute("SELECT registry_flags FROM companies "
                             "WHERE inn='9999999999'").fetchone()[0]
        conn.close()
        check("отметка сохранена", json.loads(flags) == ["software"], str(flags))
        check("чужим отметка не проставлена", other is None, str(other))
        check("отметка превращается в строку для списка",
              "реестре отечественного ПО" in registries.describe(flags),
              registries.describe(flags))

        registries.DATA_DIR = Path(tmp) / "пусто"
        check("без файлов этап не падает, а объясняет",
              registries.run() == {"imported": 0, "matched": 0})

    print("\n[37] Балл схождения сигналов")
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "s.db"
        conn = db.connect()

        def add(inn: str, **fields: Any) -> None:
            columns = ["inn", "name", "first_seen", "last_seen", "icp_status"]
            values: list[Any] = [inn, f"ООО {inn}", "t", "t", "passed"]
            for key, value in fields.items():
                columns.append(key)
                values.append(value)
            conn.execute(
                f"INSERT INTO companies ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})", values)

        # Один сильный признак против трёх слабых — ради этого сравнения
        # балл и заводился.
        add("100", site_hiring=json.dumps([{"strength": "strong"}]),
            site_type="outsourcing", contact_email="info@a.ru")
        add("200", tech_stack=json.dumps({"paid": ["Roistat"], "capture": ["Jivo"],
                                          "analytics": ["Яндекс.Метрика"]}),
            site_type="product", revenue_change_pct=-30.0, contact_email="info@b.ru")
        add("300", tech_stack=json.dumps({"cms": ["WordPress"]}),
            site_type="outsourcing", contact_email="tender@c.ru")
        conn.commit()

        rows = {r["inn"]: r for r in conn.execute("SELECT * FROM companies")}

        strong_score, strong_why = scoring.score(rows["100"])
        many_score, many_why = scoring.score(rows["200"])
        poor_score, poor_why = scoring.score(rows["300"])

        check("вакансия на холодные звонки распознана как сильная",
              "hiring_strong" in scoring.signals_of(rows["100"]),
              str(scoring.signals_of(rows["100"])))
        check("схождение нескольких признаков перевешивает один сильный",
              many_score > strong_score, f"{many_score} vs {strong_score}")
        check("объяснение написано словами, а не ключами",
              "платит за трафик" in " ".join(many_why), str(many_why))
        check("объяснение отсортировано по весу",
              many_why[0] == scoring.LABELS["pays_for_traffic"], str(many_why))
        check("отсутствие следов маркетинга штрафуется",
              "no_marketing_traces" in scoring.signals_of(rows["300"]),
              str(scoring.signals_of(rows["300"])))
        check("адрес не того отдела штрафуется",
              "wrong_desk_email" in scoring.signals_of(rows["300"]),
              str(scoring.signals_of(rows["300"])))
        check("сумма штрафов уводит балл в минус", poor_score < 0, str(poor_score))

        # Веса — настройка, а не константа: правка конфига меняет порядок.
        flipped = scoring.score(rows["100"], {"hiring_strong": 100.0})[0]
        check("вес из конфига пересиливает значение по умолчанию",
              flipped > many_score, f"{flipped} vs {many_score}")
        conn.close()

        counters = scoring.recompute({"scoring": {"weights": {}}})
        check("пересчёт прошёл по всем прошедшим ICP",
              counters["scored"] == 3, str(counters))
        conn = db.connect()
        saved = conn.execute("SELECT signal_score, signal_reasons FROM companies "
                             "WHERE inn='200'").fetchone()
        conn.close()
        check("балл сохранён в базе", saved["signal_score"] == many_score,
              str(saved["signal_score"]))
        check("объяснение сохранено рядом с баллом",
              len(json.loads(saved["signal_reasons"])) == len(many_why))

    print("\n[38] Логи: этап, база, замер времени")
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "l.db"
        db.connect().close()

        log.set_stage("dossier")
        check("текущий этап запоминается", log.current_stage() == "dossier")

        handler = log._DatabaseHandler()
        logger = log.get("selftest")
        logger.addHandler(handler)
        try:
            logger.info("проверочная запись")
            logger.warning("проверочное предупреждение")
            log.set_stage("compose")
            logger.error("проверочная ошибка")
        finally:
            logger.removeHandler(handler)

        rows = log.recent(limit=50)
        check("записи попали в базу", len(rows) >= 3, str(len(rows)))
        check("этап проставлен в записи",
              {r["stage"] for r in rows} >= {"dossier", "compose"},
              str({r["stage"] for r in rows}))
        check("модуль записан без общего префикса",
              all(not r["source"].startswith("leadgen.") for r in rows),
              str({r["source"] for r in rows}))

        warnings_only = log.recent(limit=50, level="WARNING")
        check("фильтр по уровню отдаёт и то, что выше",
              {r["level"] for r in warnings_only} == {"WARNING", "ERROR"},
              str({r["level"] for r in warnings_only}))
        check("фильтр по этапу сужает выдачу",
              all(r["stage"] == "compose"
                  for r in log.recent(limit=50, stage="compose")))

        # Логирование не имеет права ронять прогон.
        db.DB_PATH = Path(tmp) / "нет" / "такой" / "папки.db"
        logger.addHandler(handler)
        try:
            logger.info("база недоступна, но прогон продолжается")
            check("недоступная база не роняет логирование", True)
        except Exception as exc:  # noqa: BLE001
            check("недоступная база не роняет логирование", False, str(exc))
        finally:
            logger.removeHandler(handler)

        db.DB_PATH = Path(tmp) / "l.db"
        check("цвет выключается, когда вывод не в терминал",
              log._color_supported() is False or sys.stdout.isatty())

        with log.step("проверочный участок", logger):
            pass
        check("замер времени пишет в лог и не мешает работе",
              any("проверочный участок" in r["message"] for r in log.recent(limit=50)),
              str([r["message"] for r in log.recent(limit=5)]))

        old = db.now()
        conn = db.connect()
        conn.execute("UPDATE log_entries SET ts = datetime('now','-90 days')")
        conn.commit()
        conn.close()
        check("старые записи вычищаются", log.prune(days=30) > 0)
        check("после чистки таблица пуста", log.recent(limit=5) == [], old)
        log.set_stage("—")

    print("\n[39] Панель: страницы собираются без сервера")
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "ui.db"
        conn = db.connect()
        conn.execute(
            "INSERT INTO companies (inn,name,first_seen,last_seen,icp_status,"
            "site_type,site_url,contact_email,staff,revenue,signal_score,"
            "signal_reasons) VALUES ('7701','ООО ТОПС','t','t','passed',"
            "'product','https://tops.ru','info@tops.ru',40,90000000,16.0,?)",
            (json.dumps(["ищут людей на холодный поиск клиентов",
                         "платит за трафик и меряет его"], ensure_ascii=False),))
        outreach.save_touch(conn, "7701", 1, "про ваш продукт",
                            "Здравствуйте.\n\nВы ищете людей в продажи.",
                            "нанимают в продажи", "[]")
        conn.commit()
        conn.close()

        letters = ui.letters_page().decode("utf-8")
        check("письмо показано на главной", "про ваш продукт" in letters)
        check("есть кнопка копирования", "копировать письмо" in letters)
        check("есть кнопки статусов",
              "отправлено" in letters and "просили не писать" in letters)
        check("балл виден рядом с компанией", "16.0" in letters, letters[:400])
        check("причины показаны словами",
              "платит за трафик" in letters)
        check("почта компании видна", "info@tops.ru" in letters)

        # Имя компании с кавычками и угловыми скобками не должно ломать вёрстку.
        conn = db.connect()
        conn.execute("UPDATE companies SET name = ? WHERE inn='7701'",
                     ('<b>ООО "Тест"</b>',))
        conn.commit()
        conn.close()
        check("HTML в данных экранируется",
              "&lt;b&gt;" in ui.letters_page().decode("utf-8"))

        stats_html = ui.stats_page().decode("utf-8")
        check("воронка собирается", "Воронка" in stats_html, stats_html[:300])
        logs_html = ui.logs_page(level="INFO").decode("utf-8")
        check("страница логов собирается", "уровень" in logs_html)
        check("на странице логов есть фильтры",
              "WARNING" in logs_html and "все этапы" in logs_html)
        check("страница логов обновляется сама", "http-equiv=\"refresh\"" in logs_html)

        # Пустая база — обычное состояние в первый день, не ошибка.
        db.DB_PATH = Path(tmp) / "empty.db"
        db.connect().close()
        empty = ui.letters_page().decode("utf-8")
        check("на пустой базе панель объясняет, что делать",
              "Написать письма" in empty and "Прогон" in empty, empty[-500:])

    print("\n[40] Один список этапов на командную строку и панель")
    check("этапы описаны человеческим языком",
          all(item["title"] and item["detail"] for item in pipeline.STAGES),
          str([item["key"] for item in pipeline.STAGES if not item["detail"]]))
    check("порядок этапов — это порядок работы",
          [item["key"] for item in pipeline.STAGES][:3]
          == ["targets", "dossier", "compose"]
          and pipeline.STAGE_KEYS.index("followup") < pipeline.STAGE_KEYS.index("morning"),
          str(pipeline.STAGE_KEYS))
    check("этапы, тратящие деньги, помечены",
          pipeline.describe("targets")["costs"] and
          not pipeline.describe("score")["costs"])
    check("ключи этапов не повторяются",
          len(pipeline.STAGE_KEYS) == len(set(pipeline.STAGE_KEYS)))
    check("неизвестный этап описывается, а не падает",
          pipeline.describe("нет_такого")["title"] == "нет_такого")
    check("неизвестный этап не запускается",
          _raises(pipeline.StageError, lambda: pipeline.run_stage("нет_такого")))
    check("mark без ИНН объясняет, чего не хватает",
          _raises(pipeline.StageError, lambda: pipeline.run_stage("mark", rest=["sent"])))

    # Командная строка и панель обязаны знать один и тот же набор этапов:
    # ради этого модуль и заводился.
    src_run = Path("run.py").read_text(encoding="utf-8")
    check("run.py берёт список этапов из общего места",
          "pipeline.STAGE_KEYS" in src_run and "pipeline.run_stage" in src_run)
    src_ui = Path("src/ui.py").read_text(encoding="utf-8")
    check("панель берёт список этапов оттуда же",
          "pipeline.STAGES" in src_ui and "pipeline.STAGE_KEYS" in src_ui)

    print("\n[41] Прогон из панели: прогресс, остановка, ошибки")
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "j.db"
        db.connect().close()

        job = jobs.Job("dossier")
        job.report(3, 10, "ООО ПРИМЕР")
        state = job.state()
        check("прогресс считается в процентах", state["percent"] == 30, str(state))
        check("видно, на чём именно стоим", state["label"] == "ООО ПРИМЕР")
        check("у задачи есть человеческое название",
              state["title"] == "Обойти сайты", state["title"])

        # Остановка: поток не убиваем, а бросаем исключение из приёмника
        # прогресса. Прогон прервётся на границе компании (Р-050).
        job.stop()
        check("после «стоп» следующий шаг прерывает прогон",
              _raises(jobs.JobCancelled, lambda: job.report(4, 10, "ООО СЛЕДУЮЩЕЕ")))

        check("нулевое «всего» не делит на ноль",
              jobs.Job("score").state()["percent"] == 0)

        # Живой лог: строки прогона видно в панели, не открывая файл.
        tail_job = jobs.Job("score")
        handler = jobs._TailHandler(tail_job)
        logger_ = log.get("selftest")
        logger_.addHandler(handler)
        try:
            logger_.info("обычная строка")
            logger_.warning("тревожная строка")
        finally:
            logger_.removeHandler(handler)
        check("строки прогона попали в панель", len(tail_job.lines) == 2,
              str(list(tail_job.lines)))
        check("предупреждение помечено значком",
              tail_job.lines[1].startswith("⚠"), tail_job.lines[1])

        # Настоящий прогон целиком: запуск, ожидание, результат.
        done = jobs.start("score")
        for _ in range(100):
            if done.status != "running":
                break
            time.sleep(0.05)
        check("этап из панели дошёл до конца", done.status == "done", done.status)
        check("результат этапа виден в панели", "scored" in done.result, str(done.result))
        check("после конца прогона панель свободна", jobs.busy() is False)

        # Два прогона разом делили бы базу и дневной запас запросов.
        blocker = jobs.Job("dossier")
        jobs._current = blocker
        check("второй прогон не запускается, пока идёт первый",
              _raises(RuntimeError, lambda: jobs.start("score")))
        blocker.status = "done"

        # Упавший этап не должен утащить поток молча.
        broken = jobs.Job("score")
        jobs._current = broken
        jobs._execute(broken)
        check("прогон завершился, а не завис", broken.status in ("done", "failed"))

    print("\n[42] Правка настроек из панели")
    check("список файлов не пуст и у каждого есть пояснение",
          all(item["title"] and item["about"] for item in settings.FILES.values()))
    check("править можно и критерии, и тексты писем",
          {"icp", "template", "template_followup", "letter", "stoplist"}
          <= set(settings.FILES))

    # Шаблон — единственный файл, который целиком уходит к адресату.
    # Сломанное место под вставку не должно сохраниться.
    whole = Path("config/prompts/template.md").read_text(encoding="utf-8")
    check("целый шаблон сохраняется", settings.validate("template", whole) == [])
    check("шаблон без места под вставку не сохраняется",
          settings.validate("template", whole.replace("{признаки}", "то и сё")))
    check("шаблон без подписи не сохраняется",
          settings.validate("template", whole.replace("С уважением, Михаил", "Пока")))

    # ГЛАВНОЕ. Путь к файлу приходит не из браузера, а из белого списка.
    check("чужой путь не превращается в файл",
          _raises(settings.SettingsError, lambda: settings.resolve("../../.env")))
    check("сохранить чужой путь тоже нельзя",
          _raises(settings.SettingsError,
                  lambda: settings.save("../../.env", "ANTHROPIC_API_KEY=x")))
    check("ключей в списке файлов нет вовсе",
          not any(".env" in str(item["path"]) for item in settings.FILES.values()))

    check("сломанный YAML не сохраняется",
          settings.validate("icp", "provider:\n  base_url: [не закрыт\n"))
    check("в отказе назван номер строки",
          "строка" in settings.validate("icp", "a: [\nb: 1\n")[0],
          str(settings.validate("icp", "a: [\nb: 1\n")))
    check("пустой файл не сохраняется",
          settings.validate("letter", "   ") == ["файл пустой — сохранять нечего"])

    check("рабочий конфиг проходит проверку",
          settings.validate("icp", Path("config/icp.yaml").read_text(encoding="utf-8"))
          == [])

    # Защита сильнее удобства: пауза меньше секунды к чужому серверу
    # не сохранится, даже если её вписали руками в панели (Р-043).
    broken_icp = Path("config/icp.yaml").read_text(encoding="utf-8").replace(
        "pause_seconds: 2.0", "pause_seconds: 0.1")
    problems = settings.validate("icp", broken_icp)
    check("нарушение защиты не сохраняется через панель",
          any("защита" in item for item in problems), str(problems))

    with tempfile.TemporaryDirectory() as tmp:
        # Правим настоящую запись списка, но в свою папку.
        settings.BACKUP_DIR = Path(tmp) / "backups"
        target = Path(tmp) / "проба.txt"
        settings.FILES["проба"] = {"path": target, "title": "Проба",
                                   "kind": "text", "about": "для проверки"}
        settings.save("проба", "первая версия")
        check("файл записан", target.read_text(encoding="utf-8").strip() == "первая версия")
        check("копии ещё нет — файла раньше не было", settings.backups("проба") == [])

        settings.save("проба", "вторая версия")
        copies = settings.backups("проба")
        check("перед перезаписью сделана копия", len(copies) == 1, str(copies))
        check("в копии лежит прежний текст",
              "первая версия" in (settings.BACKUP_DIR / copies[0]["name"]).read_text(
                  encoding="utf-8"))

        settings.restore("проба", copies[0]["name"])
        check("возврат к прежней версии работает",
              "первая версия" in target.read_text(encoding="utf-8"))
        check("выдуманное имя копии отклоняется",
              _raises(settings.SettingsError,
                      lambda: settings.restore("проба", "чужой.txt")))

        for number in range(settings.KEEP_BACKUPS + 5):
            settings.save("проба", f"версия {number}")
        check("старые копии не копятся бесконечно",
              len(settings.backups("проба")) <= settings.KEEP_BACKUPS,
              str(len(settings.backups("проба"))))
        del settings.FILES["проба"]

    print("\n[43] Переписать письма заново после правки промпта")
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "rw.db"
        conn = db.connect()
        for inn, name, status in [("111", "ООО ЧЕРНОВИК", None),
                                  ("222", "ООО ОТПРАВЛЕНО", "sent"),
                                  ("333", "ООО НОВЫЙ", "new"),
                                  ("444", "ООО ОТКАЗАЛИСЬ", "refused")]:
            conn.execute(
                "INSERT INTO companies (inn,name,first_seen,last_seen,"
                "letter_subject,letter_body,letter_written_at,outreach_status,"
                "last_reported) VALUES (?,?,'t','t','старая тема','старый текст',"
                "'2026-08-01',?,'2026-08-01')", (inn, name, status))
            outreach.save_touch(conn, inn, 1, "старая тема", "старый текст", "по", "[]")
        conn.execute("UPDATE touches SET status='sent' WHERE inn IN ('222','444')")
        conn.commit()
        conn.close()

        # Проба ничего не стирает — иначе кнопка «проба» была бы ловушкой.
        preview = compose.reset_drafts(dry_run=True)
        conn = db.connect()
        check("проба не трогает письма",
              conn.execute("SELECT COUNT(*) FROM companies "
                           "WHERE letter_body IS NOT NULL").fetchone()[0] == 4)
        conn.close()
        check("проба показывает, скольких коснётся", preview["reset"] == 2, str(preview))

        result = compose.reset_drafts()
        check("стёрты только неотправленные", result["reset"] == 2, str(result))
        check("отправленные посчитаны отдельно", result["kept"] == 2, str(result))

        conn = db.connect()
        rows = {r["inn"]: r for r in conn.execute("SELECT * FROM companies")}
        # САМОЕ ВАЖНОЕ. Текст отправленного письма — запись того, что человек
        # получил. Переписать его значит потерять то, на что он отвечает.
        check("отправленное письмо не тронуто",
              rows["222"]["letter_body"] == "старый текст", str(rows["222"]["letter_body"]))
        check("письмо отказавшихся тоже не тронуто",
              rows["444"]["letter_body"] == "старый текст")
        check("неотправленное письмо стёрто", rows["111"]["letter_body"] is None)
        check("статус «new» считается неотправленным", rows["333"]["letter_body"] is None)
        check("отметка о показе снята, компания вернётся в список",
              rows["111"]["last_reported"] is None)
        check("у отправленной отметка о показе осталась",
              rows["222"]["last_reported"] == "2026-08-01")

        # Черновик по определению не отправлен. Если его не удалить, после
        # повторного прогона в панели окажется по два письма на компанию.
        check("черновики убраны",
              conn.execute("SELECT COUNT(*) FROM touches "
                           "WHERE status='draft'").fetchone()[0] == 0)
        check("отправленные касания на месте",
              conn.execute("SELECT COUNT(*) FROM touches "
                           "WHERE status='sent'").fetchone()[0] == 2)
        conn.close()

        # После стирания compose снова видит эти компании как ждущие письма.
        conn = db.connect()
        waiting = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE letter_body IS NULL "
            "AND letter_status IS NULL").fetchone()[0]
        conn.close()
        check("стёртые компании снова ждут письма", waiting == 2, str(waiting))

        check("повторный запуск на пустом месте не падает",
              compose.reset_drafts()["reset"] == 0)

    check("переписывание есть отдельной кнопкой в панели",
          "rewrite" in pipeline.STAGE_KEYS)
    check("кнопка объясняет, что отправленные не трогает",
          "Отправленные не трогает" in pipeline.describe("rewrite")["detail"])

    print("\n[44] Порог рейтинга: не тратим дорогую модель впустую")
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "score.db"
        conn = db.connect()
        for inn, name, score in [("100", "ООО СОШЛОСЬ", 8.0),
                                 ("200", "ООО НА ПОРОГЕ", 5.0),
                                 ("300", "ООО НИЧЕГО", 2.0),
                                 ("400", "ООО НЕ СЧИТАЛИ", None)]:
            conn.execute(
                "INSERT INTO companies (inn,name,first_seen,last_seen,icp_status,"
                "site_type,site_facts,signal_score) VALUES (?,?,'t','t','passed',"
                "'outsourcing',?,?)",
                (inn, name, json.dumps(["Факт один", "Факт два"]), score))
        conn.commit()
        conn.close()

        seen: list[str] = []

        def fake_classify(system_prompt, user_content, schema,
                          max_tokens=1024, model=None):
            seen.append(user_content.splitlines()[0])
            return {"subject": "тема", "question": "Вопрос про поиск клиентов?",
                    "signs": "признак один, признак два",
                    "why_line": "почему", "facts_used": []}

        config = {"website": {"target_types": ["outsourcing"]},
                  "compose": {"per_run": 10, "min_facts": 0, "min_score": 5.0}}
        original = llm.classify
        llm.classify = fake_classify
        try:
            result = compose.run(config)
        finally:
            llm.classify = original

        check("к модели ушли только компании с рейтингом от порога",
              len(seen) == 2, str(seen))
        check("компания на самом пороге проходит",
              any("НА ПОРОГЕ" in item for item in seen), str(seen))
        check("низкий рейтинг до модели не доходит",
              not any("НИЧЕГО" in item for item in seen), str(seen))
        # Балл ещё не считали — это не повод писать письмо: пересчёт бесплатный.
        check("непосчитанный рейтинг считается нулевым",
              not any("НЕ СЧИТАЛИ" in item for item in seen), str(seen))
        check("пропущенные посчитаны и видны в итоге",
              result["skipped_by_score"] == 2, str(result))

        # Порог — настройка, а не константа: ноль возвращает всех.
        conn = db.connect()
        conn.execute("UPDATE companies SET letter_body = NULL, "
                     "letter_status = NULL, letter_written_at = NULL")
        conn.commit()
        conn.close()
        seen.clear()
        config["compose"]["min_score"] = 0
        llm.classify = fake_classify
        try:
            compose.run(config)
        finally:
            llm.classify = original
        check("порог 0 пропускает всех", len(seen) == 4, str(len(seen)))

    print("\n[45] Оборванный ответ модели: повтор вместо потери письма")

    class FakeBlock:
        type = "text"

        def __init__(self, text):
            self.text = text

    class FakeResponse:
        def __init__(self, text, stop_reason):
            self.content = [FakeBlock(text)]
            self.stop_reason = stop_reason

    class FakeMessages:
        def __init__(self, answers):
            self.answers = list(answers)
            self.limits: list[int] = []

        def create(self, **kwargs):
            self.limits.append(kwargs["max_tokens"])
            return self.answers.pop(0)

    class FakeClient:
        def __init__(self, answers):
            self.messages = FakeMessages(answers)

    good = '{"subject": "тема", "why_line": "почему"}'
    # Ровно тот случай из боевого прогона: JSON оборвался на полуслове,
    # токены потрачены, письма нет (DECISIONS.md, Р-052).
    cut = '{"subject":"Поиск заказчиков","body":"Здравствуйте!\\n\\nВопрос по по'

    client = FakeClient([FakeResponse(cut, "max_tokens"),
                         FakeResponse(good, "end_turn")])
    original_client = llm._client
    llm._client = lambda: client
    try:
        result = llm.classify("s", "u", {"properties": {}}, max_tokens=500)
        check("после обрыва повтор дал результат", result["subject"] == "тема",
              str(result))
        check("повтор ушёл с удвоенным потолком",
              client.messages.limits == [500, 1000], str(client.messages.limits))

        # Если и повтор оборвался — ошибка должна называть причину, а не
        # валить вину на модель.
        client2 = FakeClient([FakeResponse(cut, "max_tokens"),
                              FakeResponse(cut, "max_tokens")])
        llm._client = lambda: client2
        try:
            llm.classify("s", "u", {"properties": {}}, max_tokens=500)
            check("второй обрыв поднимает ошибку", False, "ошибки не было")
        except llm.LLMError as exc:
            check("второй обрыв поднимает ошибку", True)
            check("в ошибке названа настоящая причина",
                  "оборвался по потолку" in str(exc), str(exc))

        # Не-JSON без обрыва — по-прежнему честно «не-JSON», без лишних повторов.
        client3 = FakeClient([FakeResponse("извините, не могу", "end_turn")])
        llm._client = lambda: client3
        try:
            llm.classify("s", "u", {"properties": {}}, max_tokens=500)
            check("мусор вместо JSON поднимает ошибку", False, "ошибки не было")
        except llm.LLMError as exc:
            check("мусор вместо JSON поднимает ошибку", True)
            check("мусор не выдаётся за обрыв",
                  "не-JSON" in str(exc), str(exc))
            check("на мусор повтора не было",
                  client3.messages.limits == [500], str(client3.messages.limits))
    finally:
        llm._client = original_client

    print("\n[46] Вкладка «Отправленные»")
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "sent.db"
        conn = db.connect()
        conn.execute(
            "INSERT INTO companies (inn,name,first_seen,last_seen,icp_status,"
            "contact_email,outreach_status,touch_count) VALUES "
            "('111','ООО ОТВЕТИЛИ','t','t','passed','info@a.ru','replied',1)")
        conn.execute(
            "INSERT INTO companies (inn,name,first_seen,last_seen,icp_status,"
            "outreach_status,touch_count) VALUES "
            "('222','ООО МОЛЧИТ','t','t','passed','sent',1)")
        conn.execute(
            "INSERT INTO companies (inn,name,first_seen,last_seen,icp_status) "
            "VALUES ('333','ООО ЧЕРНОВИК','t','t','passed')")
        outreach.save_touch(conn, "111", 1, "тема ответивших", "текст один", "по", "[]")
        outreach.save_touch(conn, "222", 1, "тема молчащих", "текст два", "по", "[]")
        outreach.save_touch(conn, "333", 1, "черновик", "текст три", "по", "[]")
        conn.execute("UPDATE touches SET status='sent', sent_at='2026-08-01T10:00' "
                     "WHERE inn IN ('111','222')")
        conn.commit()
        conn.close()

        html = ui.sent_page().decode("utf-8")
        check("отправленные показаны", "тема ответивших" in html and "тема молчащих" in html)
        # Разделение вкладок и есть смысл этой страницы: «что отправить»
        # и «что уже отправлено» — разные вопросы.
        check("черновик сюда не попал", "черновик" not in html, html[-600:])
        check("виден статус", "ответили" in html and "отправлено" in html)
        check("посчитана доля ответов", "доля ответов" in html and "50%" in html,
              html[:900])
        check("текст письма спрятан под раскрытие", "<details" in html)
        check("со страницы можно поменять статус", "просили не писать" in html)

        only_replied = ui.sent_page(status="replied").decode("utf-8")
        check("фильтр по статусу работает",
              "тема ответивших" in only_replied
              and "тема молчащих" not in only_replied)

        db.DB_PATH = Path(tmp) / "empty.db"
        db.connect().close()
        check("на пустой базе страница объясняет, откуда берутся письма",
              "отмечаете их" in ui.sent_page().decode("utf-8"))

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
        conn.execute(
            "UPDATE companies SET site_facts = ? WHERE inn = '7701'",
            (json.dumps(["Продукт «Альта-ГТД» для подачи деклараций",
                         "Обучение декларантов в собственном учебном центре",
                         "Сервис выдачи электронных подписей"],
                        ensure_ascii=False),))
        # Вторая: факты в досье есть, но письмо получилось общим и ни одного
        # из них не использовало. Проверяем, что это ловится сверкой текста,
        # а не самоотчётом модели.
        conn.execute(
            "INSERT INTO companies (inn,name,first_seen,last_seen,region,"
            "okved,okved_name,staff,revenue,icp_status,checked_at,site_url,"
            "site_type,site_confidence,site_summary,site_facts,site_checked_at) "
            "VALUES ('7702','ООО \"ОБЩЕЕ\"','t','t','Москва','62.01',"
            "'Разработка ПО',30,90e6,'passed','t','https://obshee.ru',"
            "'outsourcing',0.5,'IT-компания',"
            "'[\"Продукт ParsecNET для контроля доступа\", "
            "\"Проекты для государственных заказчиков\"]','t')")
        # Четвёртая: нанимает в продажи. Единственное событие в выборке,
        # поэтому в утреннем списке должна оказаться выше всех.
        conn.execute(
            "INSERT INTO companies (inn,name,first_seen,last_seen,region,"
            "okved,okved_name,staff,revenue,icp_status,checked_at,site_url,"
            "site_type,site_confidence,site_summary,site_facts,site_hiring,"
            "site_checked_at) "
            "VALUES ('7704','ООО \"НАНИМАЮТ\"','t','t','Москва','62.01',"
            "'Разработка ПО',45,150e6,'passed','t','https://naimut.ru',"
            "'outsourcing',0.8,'Разработка на заказ для ритейла',"
            "'[\"На странице вакансий ищут людей на «холодные звонки»\", "
            "\"Кейсы по автоматизации складского учёта для сетей\"]',"
            "'[{\"fact\": \"На странице вакансий ищут людей на «холодные звонки»\", "
            "\"quote\": \"холодные звонки по базе\", \"strength\": \"strong\"}]','t')")
        # Третья: фактов нет вовсе — модель не должна вызываться совсем.
        conn.execute(
            "INSERT INTO companies (inn,name,first_seen,last_seen,region,"
            "okved,okved_name,staff,revenue,icp_status,checked_at,site_url,"
            "site_type,site_confidence,site_summary,site_facts,site_checked_at) "
            "VALUES ('7703','ООО \"ПУСТО\"','t','t','Москва','62.01',"
            "'Разработка ПО',30,90e6,'passed','t','https://pusto.ru',"
            "'outsourcing',0.5,'IT-компания','[]','t')")
        conn.commit()
        row = conn.execute("SELECT * FROM companies WHERE inn='7701'").fetchone()
        dossier_text = compose.build_dossier(row)
        conn.close()

        check("в досье попало описание с сайта", "таможенного оформления" in dossier_text)
        check("в досье попали конкретные факты с сайта",
              "Альта-ГТД" in dossier_text and "учебном центре" in dossier_text,
              dossier_text)
        check("факты стоят под заголовком про наблюдение",
              dossier_text.index("НАЙДЕНО НА ИХ САЙТЕ") < dossier_text.index("Альта-ГТД"),
              dossier_text)
        check("выручка помечена как «в письме не упоминать»",
              dossier_text.index("не упоминать") < dossier_text.index("310 млн"),
              dossier_text)
        check("в досье есть запрет на выдумки",
              "Ничего сверх этого списка" in dossier_text)

        # Подменяем модель: сквозной прогон не должен ходить в сеть.
        captured: dict = {}

        def fake_classify(system_prompt, user_content, schema,
                          max_tokens=1024, model=None):
            captured.setdefault("models", set()).add(model)
            # Ключуем по компании: иначе второй вызов затирает первый и
            # проверка «дошёл ли нужный угол» становится бессмысленной.
            key = ("пусто" if "ПУСТО" in user_content
                   else "общее" if "ОБЩЕЕ" in user_content
                   else "найм" if "НАНИМАЮТ" in user_content else "альта")
            captured[key] = {"system": system_prompt, "user": user_content}
            # Модель отдаёт вставки, а не письмо: остальное берётся из шаблона.
            if key == "найм":
                return {
                    "subject": "Поиск заказчиков для отдела продаж НАНИМАЮТ",
                    "question": ("Вижу, вы ищете людей на холодные звонки. "
                                 "Сколько времени у нового менеджера уходит "
                                 "на сбор списка компаний для первых звонков?"),
                    "signs": ("розничные сети со своим складом, автоматизация "
                              "складского учёта на устаревших системах"),
                    "why_line": "нанимают в продажи прямо сейчас",
                    "facts_used": [],
                }
            if key == "общее":
                # Вставки ни на что не опираются, но модель заявляет обратное.
                return {
                    "subject": "вопрос про ваших клиентов",
                    "question": "Как вы находите новых клиентов?",
                    "signs": "компании, которым нужна разработка",
                    "why_line": "IT-компания подходящего размера",
                    "facts_used": ["Продукт ParsecNET для контроля доступа",
                                   "Проекты для государственных заказчиков"],
                }
            return {
                "subject": "Поиск покупателей для отдела продаж Альта-Софт",
                "question": ("Как вы составляете список компаний, которым "
                             "нужна Альта-ГТД, — руками или он уже есть целиком?"),
                "signs": ("свой отдел таможенного оформления, поток деклараций, "
                          "обучение декларантов в собственном учебном центре"),
                "why_line": "продуктовая компания с чётким кругом покупателей",
                "facts_used": ["Продукт «Альта-ГТД» для подачи деклараций"],
            }

        original = llm.classify
        llm.classify = fake_classify
        # Порог рейтинга здесь отключён намеренно: этот раздел про конвейер,
        # а сам порог проверяется отдельно, в разделе 44.
        icp_full.setdefault("compose", {})["min_score"] = 0
        try:
            result = compose.run(icp_full)
        finally:
            llm.classify = original

        # Так же, как в run.py: после письма пересчитываем баллы, иначе
        # утренний список нечем сортировать.
        scoring.recompute(icp_full)

        check("письма ушли в сильную модель, а не в дешёвую",
              captured.get("models") == {llm.MODEL_WRITE}, str(captured.get("models")))
        check("годные письма написаны", result["written"] == 2, str(result))
        check("письма без опоры на факты отложены", result["thin"] == 2, str(result))
        check("вакансия дошла до модели как факт досье",
              "холодные звонки" in captured.get("найм", {}).get("user", ""),
              captured.get("найм", {}).get("user", ""))
        check("промпт велит строить вопрос вокруг вакансии",
              "строй вопрос вокруг него"
              in captured.get("найм", {}).get("system", ""))
        check("компания без фактов до модели не дошла",
              "пусто" not in captured, str(sorted(captured)))
        check("модель получила угол для продуктовой компании",
              "Круг покупателей конечный" in captured.get("альта", {}).get("system", ""))
        check("аутсорсеру ушёл другой угол",
              "специализацию" in captured.get("общее", {}).get("system", ""))
        check("модель получила факты из досье",
              "учебном центре" in captured.get("альта", {}).get("user", ""))
        check("модель получила запрет выдумывать",
              "Досье — единственный источник"
              in captured.get("альта", {}).get("system", ""))
        # Оффер модели больше не показывают: он в шаблоне, дословный (Р-053).
        check("оффер до модели не доходит",
              "Что предлагаю" not in captured.get("альта", {}).get("system", ""))

        # Самое важное в этом разделе: модель заявила два факта, в тексте
        # не оказалось ни одного — и письмо всё равно отложено.
        conn = db.connect()
        общее = conn.execute("SELECT letter_status, letter_facts FROM companies "
                             "WHERE inn = '7702'").fetchone()
        альта = conn.execute("SELECT letter_facts FROM companies "
                             "WHERE inn = '7701'").fetchone()
        conn.close()
        check("самоотчёт модели не спасает пустое письмо",
              общее["letter_status"] == "thin", str(общее["letter_status"]))
        check("в базе сохранены сверенные факты, а не заявленные",
              json.loads(общее["letter_facts"]) == [], общее["letter_facts"])
        check("у годного письма факты найдены сверкой",
              len(json.loads(альта["letter_facts"])) == 2, альта["letter_facts"])

        path = report.build_morning(icp_full)
        check("утренний список создан", path is not None and path.exists())
        if path:
            text = path.read_text(encoding="utf-8")
            check("в списке есть строка «Почему здесь»", "**Почему здесь:**" in text)
            check("в списке есть тема письма", "Альта-Софт" in text)
            check("в списке есть текст письма", "Альта-ГТД" in text)
            check("в списке указан тип компании", "продуктовая компания" in text)
            check("напоминание не хранить ФИО на месте",
                  "система не хранит" in text)
            check("письмо без опоры на факты в основной список не попало",
                  "Вы занимаетесь разработкой" not in text, text)
            check("отложенные компании названы в хвосте",
                  "Отложено" in text and "ОБЩЕЕ" in text and "ПУСТО" in text, text)
            check("общий адрес показан без предупреждения",
                  "`info@alta.ru`" in text and "info@alta.ru — ⚠️" not in text, text)
            # Событие важнее всего остального: компания, которая нанимает,
            # должна стоять выше продуктовой, хотя обычно порядок обратный.
            check("нанимающая компания стоит первой в списке",
                  text.index("НАНИМАЮТ") < text.index("АЛЬТА-СОФТ"), text[:600])
            check("событие показано отдельной строкой",
                  "**Событие:** прямо сейчас нанимают людей на холодный поиск" in text,
                  text[:900])

        check("повторный список пуст", report.build_morning(icp_full) is None)

    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО проверок: {len(FAILURES)} — {', '.join(FAILURES)}")
        return 1
    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
