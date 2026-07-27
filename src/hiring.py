"""Сигнал найма: что компания пишет на своей странице вакансий.

Исходная идея всей системы была про вакансии: компания, которая ищет людей
в продажи, решает проблему потока клиентов прямо сейчас, и это событие,
а не описание бизнеса. Идея не менялась — менялся источник. «Работа России»
нашего ICP не содержит (Р-014), hh.ru закрыт на уровне соглашения (Р-016),
а собственный сайт компании открыт и вопросов не вызывает: мы туда и так
ходим за описанием и соблюдаем их robots.txt.

Ключевые слова берутся из config/signals.yaml — тех самых списков, которые
уже отлажены на четырнадцати тысячах вакансий, включая историю со стоп-словом
«водитель» внутри «рукоВОДИТЕЛЬ отдела продаж» (Р-011).

Каждый найденный сигнал сопровождается цитатой с самой страницы, поэтому
проверяется тем же способом, что и остальные факты досье (Р-025).
"""

from __future__ import annotations

from typing import Any

from .collect import contains_keyword, normalize

# Сколько символов вокруг совпадения брать в цитату. Достаточно, чтобы
# человек понял контекст, и мало, чтобы цитата осталась цитатой.
CONTEXT_CHARS = 140


def _excerpt(text: str, keyword: str) -> str:
    """Кусок текста вокруг ключевого слова — для проверки и для письма."""
    normalized = normalize(text)
    position = normalized.find(normalize(keyword))
    if position < 0:
        return ""
    start = max(0, position - CONTEXT_CHARS // 2)
    end = min(len(normalized), position + len(keyword) + CONTEXT_CHARS // 2)
    return normalized[start:end].strip()


def find_signals(hiring_text: str, keywords: dict[str, list[str]]) -> list[dict[str, str]]:
    """Ищет признаки найма в продажи на странице вакансий.

    Возвращает список фактов в том же виде, что и классификатор сайта:
    {"fact": ..., "quote": ...}. Цитата — реальный кусок страницы, поэтому
    факт проходит ту же сверку, что и все остальные.
    """
    if not hiring_text:
        return []

    signals: list[dict[str, str]] = []
    seen: set[str] = set()

    # Сильные слова — компания прямым текстом описывает холодный поиск
    # клиентов. Это самый ценный сигнал: у неё уже болит ровно то, о чём
    # написано в письме.
    for keyword in keywords.get("strong", []):
        if not contains_keyword(hiring_text, keyword):
            continue
        if keyword in seen:
            continue
        seen.add(keyword)
        signals.append({
            "fact": f"На странице вакансий ищут людей на «{keyword}»",
            "quote": _excerpt(hiring_text, keyword),
            "strength": "strong",
        })

    # Обычные — названия должностей. Само по себе слабее, но несколько
    # продажных вакансий сразу — это уже перестройка отдела.
    roles: list[str] = []
    for keyword in keywords.get("normal", []):
        if contains_keyword(hiring_text, keyword) and keyword not in seen:
            seen.add(keyword)
            roles.append(keyword)

    if roles:
        listed = ", ".join(roles[:4])
        signals.append({
            "fact": f"На странице вакансий открыты позиции в продажи: {listed}",
            "quote": _excerpt(hiring_text, roles[0]),
            "strength": "normal",
        })

    return signals


def describe(signals: list[dict[str, Any]]) -> str:
    """Строка для утреннего списка: почему найм делает компанию интереснее."""
    if not signals:
        return ""
    strong = [s for s in signals if s.get("strength") == "strong"]
    if strong:
        return "прямо сейчас нанимают людей на холодный поиск клиентов"
    return "прямо сейчас нанимают в отдел продаж"
