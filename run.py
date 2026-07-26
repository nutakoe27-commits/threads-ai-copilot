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
import os
import sys
from pathlib import Path

import yaml

from src import collect, dossier, log, report, targets

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "signals.yaml"
ICP_CONFIG_PATH = ROOT / "config" / "icp.yaml"


def load_env() -> None:
    """Читает .env в переменные окружения. Без внешних зависимостей.

    Уже заданные переменные окружения не перезаписываются — так удобнее
    подменять ключи при отладке, не трогая файл.
    """
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def load_config(path: Path) -> dict:
    if not path.exists():
        print(f"Не найден конфиг {path}", file=sys.stderr)
        sys.exit(1)
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def main() -> int:
    parser = argparse.ArgumentParser(description="GTM-система: сбор сигналов и утренний список")
    parser.add_argument(
        "--stage",
        choices=["targets", "discover", "enrich", "rejudge", "dossier",
                 "collect", "report", "all"],
        default="all",
        help=(
            "targets — построить целевой список через Checko (discover + enrich); "
            "discover/enrich — половинки этого этапа по отдельности; "
            "rejudge — пересмотреть вердикты по уже скачанным данным, без запросов к API; "
            "dossier — обойти сайты прошедших ICP и классифицировать их; "
            "collect/report — сбор сигналов и утренний список"
        ),
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

    load_env()
    logger = log.setup(verbose=args.verbose)
    logger.info("=" * 70)
    logger.info("Запуск: этап=%s dry_run=%s", args.stage, args.dry_run)

    try:
        if args.stage in ("targets", "discover", "enrich", "rejudge"):
            icp_config = load_config(ICP_CONFIG_PATH)
            if args.stage == "rejudge":
                targets.rejudge(icp_config, dry_run=args.dry_run)
            else:
                if args.stage in ("targets", "discover"):
                    targets.discover(icp_config, dry_run=args.dry_run)
                if args.stage in ("targets", "enrich"):
                    targets.enrich(icp_config, dry_run=args.dry_run)

        if args.stage == "dossier":
            dossier.run(load_config(ICP_CONFIG_PATH), dry_run=args.dry_run)

        if args.stage in ("collect", "all"):
            config = load_config(args.config)
            collect.run(config, dry_run=args.dry_run, raw=args.raw)

        if args.stage in ("report", "all"):
            config = load_config(args.config)
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
