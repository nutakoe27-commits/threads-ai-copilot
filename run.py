#!/usr/bin/env python3
"""Оркестратор: запускает этапы системы по порядку.

Примеры:
    python run.py                          # полный прогон: сбор + утренний список
    python run.py --stage collect          # только сбор сигналов
    python run.py --stage report           # только пересобрать список
    python run.py --dry-run                # ничего не пишем, только смотрим
    python run.py --stage collect --raw    # показать сырой JSON первой вакансии
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from src import collect, db, log, report

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "signals.yaml"


def load_config(path: Path) -> dict:
    if not path.exists():
        print(f"Не найден конфиг {path}", file=sys.stderr)
        sys.exit(1)
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def main() -> int:
    parser = argparse.ArgumentParser(description="GTM-система: сбор сигналов и утренний список")
    parser.add_argument(
        "--stage", choices=["collect", "report", "all"], default="all",
        help="какой этап запустить (по умолчанию все)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="ничего не записывать: ни в базу, ни в файл отчёта",
    )
    parser.add_argument(
        "--raw", action="store_true",
        help="показать сырой JSON первой вакансии — для сверки имён полей API",
    )
    parser.add_argument("--verbose", action="store_true", help="подробный лог")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args()

    logger = log.setup(verbose=args.verbose)
    logger.info("=" * 70)
    logger.info("Запуск: этап=%s dry_run=%s", args.stage, args.dry_run)

    config = load_config(args.config)

    try:
        if args.stage in ("collect", "all"):
            collect.run(config, dry_run=args.dry_run, raw=args.raw)

        if args.stage in ("report", "all"):
            path = report.build(config, dry_run=args.dry_run)
            if path:
                logger.info("Открыть список: %s", path)

    except KeyboardInterrupt:
        logger.warning("Прервано пользователем")
        return 130
    except Exception:
        # Полный стек — в лог-файл, чтобы разбираться постфактум.
        logger.exception("Прогон упал с необработанной ошибкой")
        return 1

    errors = log.error_count()
    problems = log.problems()

    if errors:
        # Код 2 — чтобы cron прислал письмо: что-то сломалось по-настоящему.
        logger.error("Прогон завершён с ошибками: %d (всего замечаний %d)", errors, len(problems))
        return 2
    if problems:
        logger.warning("Прогон завершён с замечаниями: %d шт. (см. лог и конец отчёта)",
                       len(problems))
    else:
        logger.info("Прогон завершён без замечаний")

    return 0


if __name__ == "__main__":
    sys.exit(main())
