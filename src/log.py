"""Логирование: одновременно в файл logs/YYYY-MM-DD.log и в консоль.

Дополнительно собираем все ошибки прогона в память, чтобы в конце
утреннего списка показать их отдельным блоком. Молчаливых падений быть
не должно: если что-то не сработало, вы узнаёте об этом утром, а не через
неделю по отсутствию лидов.
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

# Ошибки и предупреждения прогона — попадут в конец утреннего отчёта.
_collected_problems: list[str] = []
_error_count = 0


class _ProblemCollector(logging.Handler):
    """Складывает WARNING и ERROR в список, чтобы показать их в отчёте."""

    def emit(self, record: logging.LogRecord) -> None:
        global _error_count
        if record.levelno >= logging.WARNING:
            _collected_problems.append(f"{record.levelname}: {record.getMessage()}")
        if record.levelno >= logging.ERROR:
            _error_count += 1


def setup(verbose: bool = False) -> logging.Logger:
    """Настраивает корневой логгер. Вызывается один раз на старте."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"{date.today().isoformat()}.log"

    logger = logging.getLogger("leadgen")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    # Повторный вызов setup() не должен плодить обработчики.
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    logger.addHandler(_ProblemCollector())
    return logger


def get(name: str) -> logging.Logger:
    """Логгер для конкретного модуля: leadgen.collect, leadgen.report и т.д."""
    return logging.getLogger(f"leadgen.{name}")


def problems() -> list[str]:
    """Все предупреждения и ошибки текущего прогона."""
    return list(_collected_problems)


def error_count() -> int:
    """Сколько было именно ошибок (не предупреждений).

    По этому числу run.py решает, каким кодом завершиться: cron должен
    отличать «сегодня новых компаний нет» (нормально) от «источник
    недоступен» (надо чинить).
    """
    return _error_count
