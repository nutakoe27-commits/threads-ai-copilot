"""Правка настроек и промптов из панели.

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ ТЕКСТОВОЕ ПОЛЕ В ПАНЕЛИ. Настройки —
единственное место системы, где неверная правка портит не текущий прогон,
а все следующие. Поэтому между текстовым полем и файлом на диске стоят
три вещи:

**Белый список.** Панель никогда не получает путь к файлу от браузера.
Она получает ключ из списка ниже, и ключ превращается в путь здесь.
Иначе поле «какой файл править» стало бы способом прочитать `.env`.

**Проверка перед записью.** Сломанный YAML не сохраняется вовсе: файл
на диске остаётся прежним, а человек видит, в какой строке ошибка. Это
важнее, чем кажется: сломанный конфиг обнаружился бы утром следующего дня.

**Копия предыдущей версии.** Перед каждой записью старое содержимое
уходит в `data/config-backups/`. Восстановление — это копирование файла
обратно, без git и без объяснений.

ЧТО СЮДА НЕ ПОПАДАЕТ. Файл `.env`. Ключи правятся руками, в редакторе,
и через панель не показываются даже на чтение (DECISIONS.md, Р-051).
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from . import guard, log

logger = log.get("settings")

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / "data" / "config-backups"

# Сколько копий держать на файл. Больше не нужно: если ошибка не заметилась
# за десять правок, дело не в правке.
KEEP_BACKUPS = 10


class SettingsError(ValueError):
    """Правка не сохранена. Текст объясняет человеку, что не так."""


# Белый список. Ключ → что это за файл. Всё, чего здесь нет, панель
# не покажет и не сохранит.
FILES: dict[str, dict[str, Any]] = {
    "icp": {
        "path": ROOT / "config" / "icp.yaml",
        "title": "Критерии отбора и лимиты",
        "kind": "yaml",
        "guard": "icp",
        "about": "Кого ищем: отрасль, регион, размер, выручка. Здесь же веса "
                 "баллов, лимиты запросов и настройки панели.",
    },
    "signals": {
        "path": ROOT / "config" / "signals.yaml",
        "title": "Ключевые слова сигналов",
        "kind": "yaml",
        "guard": "signals",
        "about": "Слова, по которым вакансия считается сигналом, и стоп-слова. "
                 "Проверяется на конфликты между теми и другими.",
    },
    "letter": {
        "path": ROOT / "config" / "prompts" / "letter.md",
        "title": "Правила письма",
        "kind": "text",
        "about": "Устройство письма из пяти частей, запреты, язык, длина. "
                 "Самый важный файл системы.",
    },
    "offer": {
        "path": ROOT / "config" / "prompts" / "offer.md",
        "title": "Что предлагаем",
        "kind": "text",
        "about": "Что делает система, что обещаем и о чём просим. Правится "
                 "чаще остальных — по мере того, как понятно, на что отвечают.",
    },
    "followup": {
        "path": ROOT / "config" / "prompts" / "followup.md",
        "title": "Правила дожима",
        "kind": "text",
        "about": "Второе и третье письмо: новая мысль, а не напоминание.",
    },
    "origin": {
        "path": ROOT / "config" / "prompts" / "origin.md",
        "title": "Подпись и контакты",
        "kind": "text",
        "about": "Подставляется в конец каждого письма дословно. Проверяйте "
                 "опечатки в телефоне: их некому заметить.",
    },
    "angle_product": {
        "path": ROOT / "config" / "prompts" / "angles" / "product.md",
        "title": "Угол: продуктовая компания",
        "kind": "text",
        "about": "О чём спросить и какие признаки покупателя назвать.",
    },
    "angle_outsourcing": {
        "path": ROOT / "config" / "prompts" / "angles" / "outsourcing.md",
        "title": "Угол: разработка на заказ",
        "kind": "text",
        "about": "О чём спросить и какие признаки покупателя назвать.",
    },
    "angle_staffing": {
        "path": ROOT / "config" / "prompts" / "angles" / "staffing.md",
        "title": "Угол: аутстафф",
        "kind": "text",
        "about": "О чём спросить и какие признаки покупателя назвать.",
    },
    "angle_integrator": {
        "path": ROOT / "config" / "prompts" / "angles" / "integrator.md",
        "title": "Угол: системный интегратор",
        "kind": "text",
        "about": "О чём спросить и какие признаки покупателя назвать.",
    },
    "angle_default": {
        "path": ROOT / "config" / "prompts" / "angles" / "default.md",
        "title": "Угол: тип не определён",
        "kind": "text",
        "about": "Запасной угол, когда по сайту непонятно, чем компания живёт.",
    },
    "stoplist": {
        "path": ROOT / "data" / "stoplist.txt",
        "title": "Стоп-лист",
        "kind": "text",
        "about": "ИНН компаний, которым больше не пишем никогда. Сюда же "
                 "система сама дописывает отказавшихся.",
    },
}


def resolve(key: str) -> Path:
    """Ключ → путь. Единственный способ получить путь; иначе ошибка."""
    entry = FILES.get(key)
    if entry is None:
        raise SettingsError(f"Неизвестный файл настроек: {key}")
    return entry["path"]


def read(key: str) -> str:
    path = resolve(key)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def validate(key: str, text: str) -> list[str]:
    """Что не так с новым содержимым. Пустой список — можно сохранять.

    Первым делом сверяет ключ с белым списком: иначе неизвестное имя дошло бы
    до чтения файла и вылезло наружу голым KeyError вместо объяснения.
    """
    resolve(key)
    entry = FILES[key]
    problems: list[str] = []

    if not text.strip():
        return ["файл пустой — сохранять нечего"]

    if entry["kind"] == "yaml":
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f", строка {mark.line + 1}" if mark else ""
            return [f"это не YAML{where}: {getattr(exc, 'problem', exc)}"]

        if not isinstance(data, dict):
            return ["на верхнем уровне должны быть разделы, а не список"]

        kind = entry.get("guard")
        if kind:
            for violation in guard.check(data, kind=kind):
                if violation.hard:
                    problems.append(f"защита: {violation}")
                else:
                    logger.warning("Замечание к настройкам: %s", violation)

    if key == "origin" and "михаил" not in text.lower():
        problems.append("в подписи нет имени — письма уйдут без неё")

    return problems


def save(key: str, text: str) -> dict[str, Any]:
    """Проверяет и записывает. При жёстком нарушении файл не трогается."""
    problems = validate(key, text)
    if problems:
        raise SettingsError("; ".join(problems))

    path = resolve(key)
    backup = _backup(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    # Перевод строки в конце — чтобы файл не «слипался» при следующей правке
    # в обычном редакторе.
    path.write_text(text.rstrip() + "\n", encoding="utf-8")

    logger.info("Настройки сохранены: %s (%s)", FILES[key]["title"], path.name)
    return {"saved": key, "backup": backup.name if backup else ""}


def _backup(path: Path) -> Path | None:
    """Копия предыдущей версии. Без неё правка настроек необратима."""
    if not path.exists():
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"{path.stem}.{stamp}{path.suffix}"
    shutil.copy2(path, target)

    # Старые копии чистим сразу: иначе через год здесь будет тысяча файлов
    # и никто не поймёт, какой из них нужен.
    same = sorted(BACKUP_DIR.glob(f"{path.stem}.*{path.suffix}"))
    for stale in same[:-KEEP_BACKUPS]:
        stale.unlink(missing_ok=True)
    return target


def backups(key: str) -> list[dict[str, Any]]:
    """Копии этого файла, свежие сверху."""
    path = resolve(key)
    if not BACKUP_DIR.exists():
        return []
    found = sorted(BACKUP_DIR.glob(f"{path.stem}.*{path.suffix}"), reverse=True)
    return [{"name": item.name,
             "when": datetime.fromtimestamp(item.stat().st_mtime).strftime("%d.%m %H:%M"),
             "size": item.stat().st_size}
            for item in found]


def restore(key: str, name: str) -> dict[str, Any]:
    """Возвращает файл к сохранённой копии. Имя копии сверяется со списком."""
    known = {item["name"] for item in backups(key)}
    if name not in known:
        raise SettingsError(f"Нет такой копии: {name}")

    text = (BACKUP_DIR / name).read_text(encoding="utf-8")
    return save(key, text)
