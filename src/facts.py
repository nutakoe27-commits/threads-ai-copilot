"""Сверка фактов с источником — без обращений к модели.

Зачем это здесь. Модель, которую просят перечислить использованные факты,
перечисляет их так, как ей удобно: в списке оказывается «выручка 496 млн ₽»,
которой в письме нет. Проверка, которую можно пройти, дописав строчку,
ничего не проверяет (DECISIONS.md, Р-025).

Поэтому обе проверки здесь считаются кодом:

* факт из досье засчитывается, только если его цитата действительно есть
  в тексте сайта;
* факт считается использованным в письме, только если его характерные
  слова действительно встречаются в тексте письма.

ПРО СРАВНЕНИЕ СЛОВ. Русский язык не даёт сравнивать слова целиком:
«трубопроводов» и «трубопроводы» — одно слово в разных формах. Сравнивать
фиксированные первые N букв тоже нельзя: «учебном» и «учебный» расходятся
уже на шестой букве, а «трубо» и «трубочист» совпадают на пятой. Поэтому
два слова считаются одним, если у них общее начало не короче PREFIX_LEN
букв: окончания в русском длинные, приставки короткие, и такое правило
переживает склонение, не склеивая разные корни.
"""

from __future__ import annotations

import re

# Сколько букв в начале должны совпасть, чтобы счесть слова одним.
PREFIX_LEN = 5

# Слова короче этого в расчёт не идут: в русском это предлоги, союзы
# и местоимения, они совпадают всегда и ничего не доказывают.
MIN_WORD = 5

# Начала слов, которые встречаются в любом тексте про IT-компанию. Без
# этого списка «разработка систем для бизнеса» совпадает с чем угодно.
NOISE = {
    "компа", "систе", "решен", "услуг", "клиен", "проек", "работ",
    "бизне", "разра", "проду", "техно", "серви", "качес", "внедр",
    "росси", "предп", "инфор", "проце", "задач", "поста", "средс",
    "возмо", "напри", "включ", "позво", "обесп", "различ",
    "котор", "являе", "также", "более", "может", "своих", "наших",
}

_WORD_RE = re.compile(r"[а-яёa-z0-9]+")


def normalize(text: str) -> str:
    """Приводит текст к виду, в котором его можно сравнивать."""
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def words(text: str, drop_noise: bool = True) -> list[str]:
    """Значимые слова текста в нормализованном виде."""
    result = []
    for word in _WORD_RE.findall(normalize(text)):
        if len(word) < MIN_WORD:
            continue
        if drop_noise and word[:PREFIX_LEN] in NOISE:
            continue
        result.append(word)
    return result


def same_word(first: str, second: str) -> bool:
    """Одно ли это слово с точностью до формы."""
    limit = min(len(first), len(second))
    if limit < PREFIX_LEN:
        return False
    shared = 0
    while shared < limit and first[shared] == second[shared]:
        shared += 1
    return shared >= PREFIX_LEN


def coverage(needle: list[str], haystack: list[str]) -> float:
    """Какая доля слов из `needle` нашлась в `haystack`."""
    if not needle:
        return 0.0
    found = sum(1 for word in needle if any(same_word(word, other) for other in haystack))
    return found / len(needle)


def quote_found(quote: str, source: str, threshold: float = 0.75) -> bool:
    """Есть ли цитата в исходном тексте.

    Точное вхождение подстроки — лучший случай, но модель почти всегда
    слегка меняет пунктуацию или падеж, поэтому запасной вариант — доля
    слов цитаты, найденных в источнике. Шум здесь не отбрасываем:
    проверяем дословность, а не содержательность.
    """
    quote_norm, source_norm = normalize(quote), normalize(source)
    if not quote_norm:
        return False
    if quote_norm in source_norm:
        return True
    return coverage(words(quote, drop_noise=False),
                    words(source, drop_noise=False)) >= threshold


def fact_used_in(fact: str, letter: str, threshold: float = 0.5) -> bool:
    """Опирается ли письмо на этот факт.

    Письмо пересказывает факт своими словами, поэтому дословного совпадения
    ждать нельзя. Считаем долю характерных слов факта, попавших в письмо.
    Порог невысокий намеренно: задача — отличить письмо, которое опирается
    на факт, от письма, которое его не упоминает вовсе.
    """
    fact_words = words(fact)
    if not fact_words:
        return False
    return coverage(fact_words, words(letter)) >= threshold


def used_facts(facts: list[str], letter: str) -> list[str]:
    """Факты из досье, которые действительно отражены в письме."""
    return [fact for fact in facts if fact_used_in(fact, letter)]
