"""Проверка настроек: что можно крутить свободно, а что защищает.

ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ. Конфиги лежат рядом и выглядят одинаково. Но
`staff_max: 100` — это предпочтение, а `pause_seconds: 2.0` — обязательство
перед чужим сервером. Человек, который правит конфиг, разницы не видит: обе
строки просто числа с комментарием.

Хуже, что нарушение защиты не проявляется сразу. Сайт не откажет, если ходить
к нему раз в секунду вместо раза в две. Проблема появится через месяц и
не у нас.

Поэтому защиты проверяются кодом при каждом запуске. Нарушения делятся на два
вида. Жёсткие останавливают этап: с ними работать нельзя. Мягкие печатаются
предупреждением: так делать не стоит, но решать человеку.

Это же нужно при передаче системы клиенту (HANDOVER.md): коммерческий директор
получает конфиг и право его менять, и должен упереться в стену, если попробует
отключить защиту.
"""

from __future__ import annotations

from typing import Any

from . import log

logger = log.get("guard")


class Violation:
    """Одно нарушение: что нарушено, чем это грозит, жёсткое ли."""

    def __init__(self, setting: str, message: str, hard: bool) -> None:
        self.setting = setting
        self.message = message
        self.hard = hard

    def __str__(self) -> str:
        mark = "СТОП" if self.hard else "внимание"
        return f"[{mark}] {self.setting}: {self.message}"


def check_icp(config: dict[str, Any]) -> list[Violation]:
    """Проверяет настройки сбора данных и писем."""
    problems: list[Violation] = []
    website = config.get("website", {}) or {}
    provider = config.get("provider", {}) or {}
    compose = config.get("compose", {}) or {}

    # --- Жёсткие: вежливость к чужим серверам и закон -----------------------

    pause = float(website.get("pause_seconds", 2.0))
    if pause < 1.0:
        problems.append(Violation(
            "website.pause_seconds",
            f"пауза {pause} с между запросами к чужому сайту. Меньше секунды — "
            "это уже нагрузка на чужой сервер, а не чтение. Минимум 1.0",
            hard=True))

    pages = int(website.get("max_pages", 4))
    if pages > 12:
        problems.append(Violation(
            "website.max_pages",
            f"{pages} страниц с одного сайта за проход. Мы читаем описание "
            "компании, а не выкачиваем сайт. Разумный потолок — 12",
            hard=True))

    if not website.get("contact"):
        problems.append(Violation(
            "website.contact",
            "пустой контакт в User-Agent. Владелец сайта должен понимать, "
            "кто к нему ходит, и иметь возможность связаться. Впишите почту",
            hard=False))

    budget = int(provider.get("daily_request_budget", 100))
    if budget > 1000:
        problems.append(Violation(
            "provider.daily_request_budget",
            f"{budget} запросов в сутки. Похоже на опечатку: на бесплатном "
            "тарифе Checko лимит 100. Проверьте свой тариф",
            hard=False))

    # --- Мягкие: качество, которое легко испортить незаметно ---------------

    min_facts = int(compose.get("min_facts", 2))
    if min_facts < 1:
        problems.append(Violation(
            "compose.min_facts",
            "порог фактов равен нулю. Письма без опоры на досье пойдут "
            "человеку как обычные. Это осознанное решение, но помните: "
            "письмо говорит «вас нашла система», и слабый факт его опровергает",
            hard=False))
    if min_facts > 4:
        problems.append(Violation(
            "compose.min_facts",
            f"порог {min_facts} фактов. Это отсечёт почти всё: по замеру "
            "на компанию приходится 5–7 фактов, а в письмо попадают 2–3",
            hard=False))

    per_run = int(compose.get("per_run", 20))
    if per_run > 50:
        problems.append(Violation(
            "compose.per_run",
            f"{per_run} писем за прогон. Менеджер утонет: пятнадцать точных "
            "лучше пятидесяти сырых, и это правило проверено дороже прочих",
            hard=False))

    return problems


def check_signals(config: dict[str, Any]) -> list[Violation]:
    """Проверяет ключевые слова: не остались ли конфликты со стоп-словами."""
    from .collect import contains_keyword, normalize

    problems: list[Violation] = []
    keywords = config.get("keywords", {}) or {}
    wanted = list(keywords.get("strong", [])) + list(keywords.get("normal", []))
    stops = list(keywords.get("exclude_job", []))

    collisions = [(word, stop) for word in wanted for stop in stops
                  if contains_keyword(normalize(word), stop)]
    if collisions:
        pairs = "; ".join(f"«{w}» ловится стоп-словом «{s}»" for w, s in collisions[:3])
        problems.append(Violation(
            "signals.keywords",
            f"стоп-слово прячется внутри нужного слова: {pairs}. Такие "
            "вакансии будут молча выброшены — так мы однажды потеряли всех "
            "РОПов из-за «водителя» внутри «руководителя»",
            hard=False))
    return problems


def check(config: dict[str, Any], kind: str = "icp") -> list[Violation]:
    """Все проверки для конфига данного вида."""
    if kind == "signals":
        return check_signals(config)
    return check_icp(config)


def enforce(config: dict[str, Any], kind: str = "icp") -> bool:
    """Печатает нарушения. Возвращает False, если есть жёсткое.

    Вызывается перед этапами, которые ходят наружу или пишут письма.
    """
    problems = check(config, kind)
    if not problems:
        return True

    for problem in problems:
        if problem.hard:
            logger.error("%s", problem)
        else:
            logger.warning("%s", problem)

    hard = [p for p in problems if p.hard]
    if hard:
        logger.error("Этап остановлен: %d защитных настроек нарушено. "
                     "Что это значит — в HANDOVER.md, раздел «Что менять нельзя»",
                     len(hard))
        return False
    return True
