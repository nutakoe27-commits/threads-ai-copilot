"""Логи: в файл, в консоль с цветом и в базу для интерфейса.

Три получателя, у каждого своя задача.

**Файл** `logs/ГГГГ-ММ-ДД.log` — полная история, включая отладку. Туда же
падают стеки исключений. Это то, что читают, когда что-то сломалось.

**Консоль** — то, что видно во время прогона. С цветом по уровню: ошибку
надо замечать, не вчитываясь. Цвет отключается сам, если вывод идёт не
в терминал (например, в файл через `>` или в cron).

**База** — таблица `log_entries`. Нужна интерфейсу: он показывает последние
записи с фильтром по уровню и этапу, не читая файл построчно.

Отдельно собираем WARNING и ERROR в память, чтобы показать их блоком в конце
утреннего списка. Молчаливых падений быть не должно: если что-то не
сработало, вы узнаёте утром, а не через неделю по отсутствию лидов.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

# Ошибки и предупреждения прогона — попадут в конец утреннего отчёта.
_collected_problems: list[str] = []
_error_count = 0

# Текущий этап. Проставляется в каждую запись, чтобы в интерфейсе можно было
# отфильтровать «покажи только то, что было на этапе dossier».
_current_stage = "—"

# Цвета ANSI. Работают в Терминале macOS, iTerm и любом современном терминале.
COLORS = {
    "DEBUG": "\033[90m",     # серый
    "INFO": "",              # обычный
    "WARNING": "\033[33m",   # жёлтый
    "ERROR": "\033[31m",     # красный
    "CRITICAL": "\033[1;31m",
}
RESET = "\033[0m"


def set_stage(stage: str) -> None:
    """Запоминает текущий этап для всех последующих записей."""
    global _current_stage
    _current_stage = stage


def current_stage() -> str:
    return _current_stage


# Куда сообщать о ходе работы. По умолчанию — никуда: в терминале прогресс
# и так виден построчно, а вот панели нужна доля выполненного.
#
# Приёмник сообщений о прогрессе имеет право бросить исключение — так
# устроена остановка прогона из панели. Этапы его не ловят, оно выходит
# наружу и прогон честно завершается прерванным (DECISIONS.md, Р-050).
_progress_sink: Any = None


def set_progress_sink(sink: Any) -> None:
    """Назначает приёмник прогресса. None — отключить."""
    global _progress_sink
    _progress_sink = sink


def progress(done: int, total: int, label: str = "") -> None:
    """Сообщает, сколько сделано из скольких.

    Вызывается внутри циклов этапов. Когда панель не запущена, не делает
    ничего и стоит один вызов функции — этого не жалко.
    """
    if _progress_sink is not None:
        _progress_sink(done, total, label)


class _ProblemCollector(logging.Handler):
    """Складывает WARNING и ERROR в список, чтобы показать их в отчёте."""

    def emit(self, record: logging.LogRecord) -> None:
        global _error_count
        if record.levelno >= logging.WARNING:
            _collected_problems.append(f"{record.levelname}: {record.getMessage()}")
        if record.levelno >= logging.ERROR:
            _error_count += 1


class _DatabaseHandler(logging.Handler):
    """Пишет записи в SQLite, чтобы интерфейс их показывал.

    Импорт базы отложен внутрь метода: `db` сам пользуется логированием,
    и импорт на уровне модуля создал бы кольцо.

    Ошибки записи проглатываются намеренно. Логирование не имеет права
    ронять прогон: если база занята, лучше потерять строку лога, чем
    потерять результат работы.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.INFO:
            return
        try:
            from . import db

            conn = db.connect()
            conn.execute(
                "INSERT INTO log_entries (ts, level, stage, source, message) "
                "VALUES (?, ?, ?, ?, ?)",
                (db.now(), record.levelname, _current_stage,
                 record.name.replace("leadgen.", ""), record.getMessage()),
            )
            conn.commit()
            conn.close()
        except Exception:  # noqa: BLE001 — см. docstring
            pass


class _ColorFormatter(logging.Formatter):
    """Формат для консоли: время, уровень с цветом, источник, сообщение."""

    def __init__(self, use_color: bool) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        stamp = self.formatTime(record, self.datefmt)
        source = record.name.replace("leadgen.", "")
        level = record.levelname
        message = record.getMessage()

        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)

        line = f"{stamp}  {level:<7}  {source:<9}  {message}"
        if self.use_color and COLORS.get(level):
            return f"{COLORS[level]}{line}{RESET}"
        return line


def _color_supported() -> bool:
    """Цвет включаем, только если вывод действительно идёт в терминал."""
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def setup(verbose: bool = False, to_db: bool = True) -> logging.Logger:
    """Настраивает корневой логгер. Вызывается один раз на старте."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"{date.today().isoformat()}.log"

    logger = logging.getLogger("leadgen")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    # Повторный вызов setup() не должен плодить обработчики.
    if logger.handlers:
        return logger

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)-18s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_ColorFormatter(_color_supported()))
    logger.addHandler(console)

    logger.addHandler(_ProblemCollector())
    if to_db:
        logger.addHandler(_DatabaseHandler())
    return logger


def get(name: str) -> logging.Logger:
    """Логгер для конкретного модуля: leadgen.collect, leadgen.report и т.д."""
    return logging.getLogger(f"leadgen.{name}")


class step:
    """Замер длительности участка работы. Пишет в лог, сколько занял этап.

    Используется как контекст:

        with log.step("обход сайтов", logger):
            ...

    Зачем. Когда прогон вдруг стал занимать сорок минут вместо пяти, надо
    сразу видеть, какой именно кусок распух, а не гадать по времени строк.
    """

    def __init__(self, name: str, logger: logging.Logger | None = None) -> None:
        self.name = name
        self.logger = logger or get("run")
        self.started = 0.0

    def __enter__(self) -> "step":
        self.started = time.monotonic()
        self.logger.info("▸ %s", self.name)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        seconds = time.monotonic() - self.started
        if exc_type is None:
            self.logger.info("✓ %s — %.1f с", self.name, seconds)
        else:
            self.logger.error("✗ %s — упало через %.1f с", self.name, seconds)
        return False


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


def recent(limit: int = 200, level: str = "", stage: str = "") -> list[Any]:
    """Последние записи из базы — для интерфейса."""
    from . import db

    conn = db.connect()
    query = "SELECT * FROM log_entries WHERE 1=1"
    params: list[Any] = []
    if level:
        order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        allowed = order[order.index(level):] if level in order else [level]
        query += f" AND level IN ({','.join('?' for _ in allowed)})"
        params.extend(allowed)
    if stage:
        query += " AND stage = ?"
        params.append(stage)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def prune(days: int = 30) -> int:
    """Чистит старые записи из базы. Файлы логов не трогает."""
    from . import db

    conn = db.connect()
    cur = conn.execute(
        "DELETE FROM log_entries WHERE ts < datetime('now', ?)", (f"-{days} days",))
    conn.commit()
    conn.close()
    return cur.rowcount
