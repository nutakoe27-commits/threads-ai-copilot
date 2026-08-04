"""Прогоны, запущенные из панели: один за раз, с прогрессом и остановкой.

ПОЧЕМУ ОДИН ЗА РАЗ. Все этапы пишут в одну базу SQLite и тратят один общий
дневной запас запросов к Checko. Два одновременных прогона поделили бы
и то и другое, а результат стал бы невоспроизводимым: «вчера прошло 13
компаний, сегодня 4» без объяснимой причины. Очередь из одного — не
упрощение, а требование.

КАК УСТРОЕН ПРОГРЕСС. Этапы зовут `log.progress(сделано, всего, что_именно)`
внутри своих циклов. Пока панель не запущена, эта функция не делает ничего.
Когда прогон идёт из панели, она обновляет состояние задачи, а браузер
раз в секунду его спрашивает.

КАК УСТРОЕНА ОСТАНОВКА. Убить поток в Python нельзя, и это к лучшему:
прогон, убитый посреди записи, оставил бы базу в непонятном состоянии.
Вместо этого приёмник прогресса бросает исключение, когда нажата «стоп».
Оно выходит наружу через тот же путь, что и любая ошибка, — прогон
завершается прерванным на границе компании, а не посреди неё.

Из этого следует ограничение, которое честно показано в панели: остановка
срабатывает не мгновенно, а на следующей компании. Этап без циклов
(например, сборка утреннего списка) остановить нельзя вовсе — он и длится
секунды.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import deque
from typing import Any

from . import log, pipeline

logger = log.get("jobs")

# Сколько последних строк лога держать при задаче. Панель показывает их
# во время прогона; полная история всё равно в файле и в базе.
TAIL_LINES = 400


class JobCancelled(RuntimeError):
    """Человек нажал «остановить». Не ошибка, а решение."""


class Job:
    """Один прогон одного этапа."""

    def __init__(self, stage: str, dry_run: bool = False) -> None:
        self.stage = stage
        self.dry_run = dry_run
        self.status = "running"          # running | done | failed | cancelled
        self.started = time.time()
        self.finished: float | None = None
        self.done = 0
        self.total = 0
        self.label = ""
        # Шаг составного прогона. У одиночного этапа шаг один из одного.
        self.step = 1
        self.steps = 1
        self.step_title = ""
        self.result: dict[str, Any] = {}
        self.error = ""
        self.lines: deque[str] = deque(maxlen=TAIL_LINES)
        self._stop = threading.Event()

    # --- прогресс -------------------------------------------------------

    def report(self, done: int, total: int, label: str = "") -> None:
        """Приёмник для log.progress. Здесь же ловится нажатие «стоп»."""
        if self._stop.is_set():
            raise JobCancelled(f"Прогон остановлен на {done} из {total}")
        self.done, self.total, self.label = done, total, label

    def report_phase(self, index: int, total: int, title: str) -> None:
        """Начался следующий этап составного прогона.

        Счётчик компаний обнуляется: он относится к этапу, а не к прогону.
        Здесь же ловится «стоп» — иначе между этапами остановка не сработала бы,
        а как раз между ними прогон и стоит дольше всего.
        """
        if self._stop.is_set():
            raise JobCancelled(f"Прогон остановлен перед этапом «{title}»")
        self.step, self.steps, self.step_title = index, total, title
        self.done = self.total = 0
        self.label = ""

    def stop(self) -> None:
        self._stop.set()
        self.lines.append("Остановка запрошена — прогон прервётся "
                          "на следующей компании")

    # --- то, что видит браузер ------------------------------------------

    def state(self) -> dict[str, Any]:
        seconds = (self.finished or time.time()) - self.started
        inside = (self.done / self.total) if self.total else 0.0

        # У составного прогона полоса показывает весь прогон, а не текущий
        # этап: иначе она четыре раза откатывается к нулю, и человек решает,
        # что всё зависло.
        if self.status == "done":
            # Дошли до конца — полоса обязана это показать. Без этого она
            # замирает на последнем шаге: он засчитан только когда начался
            # следующий, а следующего нет.
            percent = 100
        elif self.steps > 1:
            percent = int((self.step - 1 + inside) * 100 / self.steps)
        else:
            percent = int(inside * 100)

        return {
            "stage": self.stage,
            "title": pipeline.describe(self.stage)["title"],
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "step": self.step,
            "steps": self.steps,
            "step_title": self.step_title,
            "percent": percent,
            "label": self.label,
            "seconds": round(seconds, 1),
            "dry_run": self.dry_run,
            "result": self.result,
            "error": self.error,
            "lines": list(self.lines),
        }


class _TailHandler(logging.Handler):
    """Складывает строки лога в задачу, чтобы показать их в панели живьём."""

    def __init__(self, job: Job) -> None:
        super().__init__(level=logging.INFO)
        self.job = job

    def emit(self, record: logging.LogRecord) -> None:
        try:
            mark = {"WARNING": "⚠ ", "ERROR": "✗ ", "CRITICAL": "✗ "}.get(
                record.levelname, "")
            self.job.lines.append(f"{mark}{record.getMessage()}")
        except Exception:  # noqa: BLE001 — лог не имеет права ронять прогон
            pass


_current: Job | None = None
_lock = threading.Lock()


def current() -> Job | None:
    return _current


def busy() -> bool:
    return _current is not None and _current.status == "running"


def start(stage: str, dry_run: bool = False) -> Job:
    """Запускает этап в отдельном потоке. Бросает RuntimeError, если занято."""
    global _current

    with _lock:
        if busy():
            raise RuntimeError(
                f"Уже идёт другой прогон: {_current.stage}. "
                f"Дождитесь конца или остановите его.")
        job = Job(stage, dry_run)
        _current = job

    thread = threading.Thread(target=_execute, args=(job,),
                              name=f"job-{stage}", daemon=True)
    thread.start()
    return job


def _execute(job: Job) -> None:
    """Тело потока. Всё, что может упасть, падает сюда и попадает в задачу."""
    root = logging.getLogger("leadgen")
    tail = _TailHandler(job)
    root.addHandler(tail)
    log.set_progress_sink(job.report)
    log.set_phase_sink(job.report_phase)

    try:
        logger.info("Панель запустила этап %s%s", job.stage,
                    " (пробный прогон)" if job.dry_run else "")
        job.result = pipeline.run_stage(job.stage, dry_run=job.dry_run) or {}
        job.status = "done"
        logger.info("Этап %s завершён: %s", job.stage, _summary(job.result))
    except JobCancelled as exc:
        job.status = "cancelled"
        job.error = str(exc)
        logger.warning("Этап %s остановлен человеком", job.stage)
    except pipeline.StageError as exc:
        job.status = "failed"
        job.error = str(exc)
        logger.error("Этап %s не запустился: %s", job.stage, exc)
    except Exception as exc:  # noqa: BLE001 — иначе поток умрёт молча
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        logger.error("Этап %s упал: %s", job.stage, job.error)
        logger.debug("%s", traceback.format_exc())
    finally:
        job.finished = time.time()
        log.set_progress_sink(None)
        log.set_phase_sink(None)
        root.removeHandler(tail)


def _summary(result: dict[str, Any]) -> str:
    if not result:
        return "без счётчиков"
    return ", ".join(f"{key} {value}" for key, value in result.items())
