#!/usr/bin/env python3
"""Оркестратор командной строки: запускает этапы системы.

Всё то же самое есть в панели (`--stage ui`), и запускает она те же самые
функции: список этапов и порядок вызовов лежат в `src/pipeline.py`, чтобы
командная строка и панель не разъехались.

Примеры:
    python3 run.py --stage ui               # панель: этапы, письма, настройки
    python3 run.py --stage targets          # найти компании
    python3 run.py --stage morning          # пересобрать утренний список
    python3 run.py --dry-run --stage dossier  # ничего не пишем, только смотрим
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from src import log, pipeline

ROOT = Path(__file__).resolve().parent


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


def stage_help() -> str:
    """Справка по этапам собирается из того же списка, что и кнопки панели."""
    lines = [f"{item['key']} — {item['detail']}" for item in pipeline.STAGES]
    lines.append("ui — панель в браузере: те же этапы кнопками, плюс письма, "
                 "воронка, настройки и логи")
    lines.append("discover/enrich — половинки этапа targets по отдельности")
    lines.append("mark — отметить статус: mark sent ИНН ИНН")
    lines.append("telegram — отправить свежий утренний список в Telegram")
    lines.append("collect/report — старый сбор сигналов и его отчёт")
    return "; ".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GTM-система: поиск компаний, досье, письма, утренний список")
    parser.add_argument(
        "--stage",
        choices=pipeline.STAGE_KEYS + pipeline.EXTRA_KEYS,
        default="all",
        help=stage_help(),
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
    parser.add_argument("--days", type=int, default=14,
                        help="глубина отчёта для --stage stats")
    parser.add_argument(
        "rest", nargs="*",
        help="аргументы этапа mark: сначала статус, потом ИНН через пробел",
    )
    args = parser.parse_args()

    load_env()
    logger = log.setup(verbose=args.verbose)
    logger.info("=" * 70)
    logger.info("Запуск: этап=%s dry_run=%s", args.stage, args.dry_run)

    try:
        result = pipeline.run_stage(args.stage, dry_run=args.dry_run,
                                    days=args.days, rest=args.rest, raw=args.raw)
        if result:
            logger.info("Итог: %s", ", ".join(f"{k} {v}" for k, v in result.items()))
    except pipeline.StageError as exc:
        logger.error("%s", exc)
        return 2
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
        logger.error("Прогон завершён с ошибками: %d (всего замечаний %d)",
                     errors, len(problems))
        return 2
    if problems:
        logger.warning("Прогон завершён с замечаниями: %d шт. (см. лог и конец отчёта)",
                       len(problems))
    else:
        logger.info("Прогон завершён без замечаний")

    return 0


if __name__ == "__main__":
    sys.exit(main())
