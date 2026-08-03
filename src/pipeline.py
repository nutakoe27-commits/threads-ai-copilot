"""Один список этапов на всю систему.

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ. Этапы запускаются из двух мест: из командной строки
(`run.py`) и из панели. Если бы каждое место знало порядок вызовов само,
они бы разъехались через месяц — в панели забыли бы пересчитать баллы после
письма, и разница вылезла бы не сразу, а в виде «почему-то список
отсортирован не так».

Поэтому список этапов, их описания и порядок вызовов лежат здесь, а `run.py`
и панель одинаково зовут `run_stage()`.

ЗДЕСЬ ЖЕ ЧЕЛОВЕЧЕСКИЕ ОПИСАНИЯ. Панель показывает их на кнопках, командная
строка — в справке. Одно место, один текст.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import (collect, compose, dossier, guard, log, metrics, outreach,
               report, scoring, targets)
from .sources import registries

logger = log.get("pipeline")

ROOT = Path(__file__).resolve().parent.parent
SIGNALS_CONFIG = ROOT / "config" / "signals.yaml"
ICP_CONFIG = ROOT / "config" / "icp.yaml"


class StageError(RuntimeError):
    """Этап не запустился. Текст предназначен человеку, а не в лог."""


# Порядок здесь — это порядок ежедневной работы. Панель рисует кнопки
# в этом же порядке, поэтому «сверху вниз» на экране и есть правильный
# порядок запуска.
STAGES: list[dict[str, Any]] = [
    {
        "key": "targets",
        "title": "Найти компании",
        "detail": "Поиск в реестре по отрасли и региону, проверка по критериям. "
                  "Тратит запросы Checko.",
        "minutes": "2–5 минут",
        "costs": "запросы Checko",
    },
    {
        "key": "dossier",
        "title": "Обойти сайты",
        "detail": "Читает сайты прошедших проверку, собирает факты с цитатами, "
                  "определяет тип компании и следы маркетинга.",
        "minutes": "3–8 минут",
        "costs": "запросы к модели",
    },
    {
        "key": "compose",
        "title": "Написать письма",
        "detail": "По каждому досье пишется письмо. Письма без опоры на факты "
                  "откладываются.",
        "minutes": "2–6 минут",
        "costs": "запросы к модели",
    },
    {
        "key": "rewrite",
        "title": "Переписать письма заново",
        "detail": "Стирает неотправленные письма и пишет их заново по текущим "
                  "правилам. Отправленные не трогает. Запускать после правки "
                  "текстов в «Настройках».",
        "minutes": "2–6 минут",
        "costs": "запросы к модели",
    },
    {
        "key": "followup",
        "title": "Написать дожимы",
        "detail": "Второе и третье письмо тем, кто не ответил. Каждое несёт "
                  "новую мысль, а не напоминание.",
        "minutes": "1–4 минуты",
        "costs": "запросы к модели",
    },
    {
        "key": "morning",
        "title": "Собрать утренний список",
        "detail": "Файл в morning/ и отправка в Telegram, если он подключён.",
        "minutes": "секунды",
        "costs": "",
    },
    {
        "key": "score",
        "title": "Пересчитать баллы",
        "detail": "Пересобирает баллы схождения сигналов по тому, что уже "
                  "в базе. Наружу не ходит.",
        "minutes": "секунды",
        "costs": "",
    },
    {
        "key": "registries",
        "title": "Импорт реестров",
        "detail": "Читает выгрузки Минцифры из data/registries/ и отмечает "
                  "наши компании.",
        "minutes": "секунды",
        "costs": "",
    },
    {
        "key": "stats",
        "title": "Воронка и самодиагностика",
        "detail": "Печатает конверсии за две недели и жалуется, если "
                  "что-то просело.",
        "minutes": "секунды",
        "costs": "",
    },
    {
        "key": "rejudge",
        "title": "Пересмотреть вердикты",
        "detail": "Заново применяет критерии к уже скачанным данным. "
                  "Запускать после правки критериев. Наружу не ходит.",
        "minutes": "секунды",
        "costs": "",
    },
    {
        "key": "cleanup",
        "title": "Убрать личные адреса",
        "detail": "Удаляет из базы личные почтовые адреса, если они попали "
                  "туда до появления фильтра.",
        "minutes": "секунды",
        "costs": "",
    },
]

STAGE_KEYS = [item["key"] for item in STAGES]

# Этапы, которых нет на кнопках панели: они либо разбивают другой этап
# на половинки, либо не имеют смысла вне командной строки.
EXTRA_KEYS = ["discover", "enrich", "telegram", "collect", "report", "mark", "ui", "all"]


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise StageError(f"Не найден конфиг {path}")
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def describe(stage: str) -> dict[str, Any]:
    for item in STAGES:
        if item["key"] == stage:
            return item
    return {"key": stage, "title": stage, "detail": "", "minutes": "", "costs": ""}


def _icp() -> dict[str, Any]:
    """Конфиг ICP с проверкой защит. Жёсткое нарушение — этап не пойдёт."""
    config = load_config(ICP_CONFIG)
    if not guard.enforce(config):
        raise StageError(
            "Настройки нарушают защиту, этап остановлен. Что именно — "
            "в логах. Разбор каждой строки — в HANDOVER.md.")
    return config


def run_stage(stage: str, dry_run: bool = False, days: int = 14,
              rest: list[str] | None = None, raw: bool = False) -> dict[str, Any]:
    """Запускает один этап. Единственная точка входа для всех запускающих.

    Возвращает счётчики этапа — то, что панель показывает как итог.
    Исключения не глушит: их ловит вызывающий и решает, что с ними делать.
    """
    rest = rest or []
    log.set_stage(stage)

    if stage in ("targets", "discover", "enrich", "rejudge"):
        config = _icp()
        if stage == "rejudge":
            return targets.rejudge(config, dry_run=dry_run)
        result: dict[str, Any] = {}
        if stage in ("targets", "discover"):
            result.update(targets.discover(config, dry_run=dry_run) or {})
        if stage in ("targets", "enrich"):
            result.update(targets.enrich(config, dry_run=dry_run) or {})
        return result

    if stage == "dossier":
        config = _icp()
        result = dossier.run(config, dry_run=dry_run) or {}
        # Досье добавляет признаки — баллы после него всегда устарели.
        scoring.recompute(config, dry_run=dry_run)
        return result

    if stage == "compose":
        config = _icp()
        result = compose.run(config, dry_run=dry_run) or {}
        scoring.recompute(config, dry_run=dry_run)
        return result

    if stage == "rewrite":
        # Порядок важен: сначала стереть, потом писать. Иначе compose
        # не увидит эти компании — у них уже есть письма.
        config = _icp()
        result = compose.reset_drafts(dry_run=dry_run)
        if dry_run:
            # В пробе стирать нечего, значит и писать нечего: показываем
            # только, скольких это коснулось бы.
            return result
        result.update(compose.run(config) or {})
        scoring.recompute(config)
        return result

    if stage == "followup":
        return compose.run_followups(_icp(), dry_run=dry_run) or {}

    if stage == "morning":
        config = load_config(ICP_CONFIG)
        path = report.build_morning(config, dry_run=dry_run)
        if path and config.get("report", {}).get("telegram", True):
            report.send_to_telegram(config)
        return {"файл": str(path) if path else "список пуст"}

    if stage == "telegram":
        report.send_to_telegram(load_config(ICP_CONFIG))
        return {}

    if stage == "score":
        return scoring.recompute(load_config(ICP_CONFIG), dry_run=dry_run)

    if stage == "registries":
        return registries.run(dry_run=dry_run)

    if stage == "stats":
        metrics.run(days)
        return {}

    if stage == "cleanup":
        return targets.cleanup_contacts(dry_run=dry_run) or {}

    if stage == "mark":
        if len(rest) < 2:
            raise StageError(
                "Нужны статус и хотя бы один ИНН: mark sent 7703283933")
        outreach.mark(rest[0], rest[1:], dry_run=dry_run)
        return {"отмечено": len(rest) - 1}

    if stage in ("collect", "report", "all"):
        config = load_config(SIGNALS_CONFIG)
        result = {}
        if stage in ("collect", "all"):
            guard.enforce(config, kind="signals")
            result.update(collect.run(config, dry_run=dry_run, raw=raw) or {})
        if stage in ("report", "all"):
            path = report.build(config, dry_run=dry_run)
            result["файл"] = str(path) if path else "список пуст"
        return result

    if stage == "ui":
        # Импорт отложен: панель импортирует этот модуль, и импорт на уровне
        # файла замкнул бы кольцо.
        from . import ui

        ui.serve(config=load_config(ICP_CONFIG))
        return {}

    raise StageError(f"Неизвестный этап: {stage}")
