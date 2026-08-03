"""Государственные реестры Минцифры: аккредитация IT и отечественное ПО.

ЧТО ЭТО ДАЁТ.

**Реестр отечественного ПО** — лучшее событие из всех найденных. Компания
попала в реестр, значит ей открылся рынок госзаказчиков и корпораций под
импортозамещение. Новый рынок, к которому у неё нет ни одного контакта.
Это буквально «у вас появились покупатели, которых вы пока не знаете» —
то есть ровно наш оффер, и повод написать именно в этом месяце.

**Реестр аккредитованных IT-компаний** — скорее фильтр, чем сигнал.
Он отделяет настоящие IT-компании от тех, кто просто носит ОКВЭД 62.01.
Дешевле, чем выяснять то же самое через Checko.

КАК ЗАГРУЖАЕТСЯ. Оба реестра — открытые данные, но их выгрузки то переезжают,
то отдаются только через браузер. Поэтому поддерживаются два пути:

1. Автоматический: скачать по адресу из конфига.
2. Ручной: положить файл в `data/registries/` и запустить импорт.

Второй путь не запасной, а равноправный. Реестр обновляется раз в месяц,
скачать файл руками — минута работы, и это надёжнее любой ссылки, которая
через полгода переедет.

ФОРМАТЫ. CSV и JSON. XLSX намеренно не поддерживается: он потребовал бы
внешней библиотеки ради файла, который открывается и пересохраняется
в CSV за десять секунд.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

from .. import log

logger = log.get("registries")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "registries"

# Как называется реестр внутри системы и что означает попадание в него.
KINDS = {
    "software": "продукт в реестре отечественного ПО",
    "accredited": "аккредитованная IT-компания",
}

_INN_RE = re.compile(r"\b(\d{10}|\d{12})\b")


def _extract_inns_from_text(text: str) -> set[str]:
    """Достаёт ИНН из произвольного текста.

    Работает для любого формата выгрузки, включая те, где колонки названы
    неожиданно. ИНН — это 10 или 12 цифр подряд, и спутать их в таком файле
    практически не с чем.
    """
    return set(_INN_RE.findall(text))


def parse_csv(content: str) -> set[str]:
    """ИНН из CSV. Ищет подходящую колонку, иначе разбирает весь текст."""
    try:
        sample = content[:4096]
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"

    inns: set[str] = set()
    reader = csv.reader(io.StringIO(content), dialect)
    try:
        header = next(reader)
    except StopIteration:
        return inns

    # Ищем колонку с ИНН по названию.
    index = None
    for position, title in enumerate(header):
        if "инн" in title.strip().lower():
            index = position
            break

    if index is None:
        logger.debug("В заголовке CSV нет колонки ИНН — разбираю весь текст")
        return _extract_inns_from_text(content)

    for row in reader:
        if len(row) > index:
            inns |= _extract_inns_from_text(row[index])
    return inns


def parse_json(content: str) -> set[str]:
    """ИНН из JSON любой вложенности."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("Файл не разобрался как JSON: %s", exc)
        return set()
    return _extract_inns_from_text(json.dumps(data, ensure_ascii=False))


def load_file(path: Path) -> set[str]:
    """Читает файл реестра и возвращает множество ИНН."""
    if not path.exists():
        return set()

    content = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".json":
        inns = parse_json(content)
    elif path.suffix.lower() in (".csv", ".txt"):
        inns = parse_csv(content)
    else:
        logger.warning("Формат %s не поддерживается. Пересохраните в CSV или JSON",
                       path.suffix)
        return set()

    logger.info("%s: найдено ИНН %d", path.name, len(inns))
    return inns


def available_files() -> dict[str, Path]:
    """Какие файлы реестров лежат в data/registries/.

    Имя файла определяет реестр: всё, что начинается на `software`, —
    отечественное ПО, на `accredited` — аккредитация.
    """
    found: dict[str, Path] = {}
    if not DATA_DIR.exists():
        return found

    for path in sorted(DATA_DIR.iterdir()):
        if path.is_dir() or path.name.startswith("."):
            continue
        for kind in KINDS:
            if path.name.lower().startswith(kind):
                found[kind] = path
    return found


def instructions() -> str:
    """Что сказать человеку, когда файлов нет."""
    return (
        f"Файлы реестров не найдены. Положите их в {DATA_DIR} и запустите заново.\n"
        "\n"
        "  Реестр отечественного ПО — reestr.digital.gov.ru, кнопка выгрузки.\n"
        "    Сохраните как data/registries/software.csv\n"
        "\n"
        "  Реестр аккредитованных IT-компаний — digital.gov.ru, раздел\n"
        "    «Аккредитация IT-компаний». Сохраните как\n"
        "    data/registries/accredited.csv\n"
        "\n"
        "Формат — CSV или JSON. Если скачался XLSX, откройте и пересохраните\n"
        "в CSV: внешнюю библиотеку ради этого ставить не будем.\n"
        "\n"
        "Реестры обновляются примерно раз в месяц. Обновлять файлы чаще смысла нет."
    )


def run(dry_run: bool = False) -> dict[str, int]:
    """Импортирует реестры и проставляет отметки компаниям в базе."""
    from .. import db

    files = available_files()
    if not files:
        logger.warning("%s", instructions())
        return {"imported": 0, "matched": 0}

    conn = db.connect()
    counters = {"imported": 0, "matched": 0}
    marks: dict[str, list[str]] = {}

    for kind, path in files.items():
        inns = load_file(path)
        counters["imported"] += len(inns)
        if not inns:
            continue

        # Отмечаем только те компании, которые у нас уже есть. Реестр
        # целиком в базу не тянем: там десятки тысяч записей, из которых
        # нашему ICP соответствуют единицы.
        placeholders = ",".join("?" for _ in inns)
        rows = conn.execute(
            f"SELECT inn FROM companies WHERE inn IN ({placeholders})",
            tuple(inns),
        ).fetchall()
        for row in rows:
            marks.setdefault(row["inn"], []).append(kind)
        logger.info("Реестр «%s»: совпало с нашей базой %d компаний",
                    KINDS[kind], len(rows))

    for inn, kinds in marks.items():
        counters["matched"] += 1
        if not dry_run:
            conn.execute("UPDATE companies SET registry_flags = ? WHERE inn = ?",
                         (json.dumps(kinds, ensure_ascii=False), inn))

    if not dry_run:
        conn.commit()
    conn.close()

    logger.info("Реестры: прочитано записей %d, отмечено наших компаний %d",
                counters["imported"], counters["matched"])
    return counters


def describe(flags_json: str | None) -> str:
    """Строка для утреннего списка."""
    try:
        kinds = json.loads(flags_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return ""
    return ", ".join(KINDS.get(kind, kind) for kind in kinds)
