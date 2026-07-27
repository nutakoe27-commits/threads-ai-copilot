"""Единственное место в системе, которое знает про LLM-провайдера.

Смена провайдера — правка этого файла, остальной код её не заметит.
Так задумано с самого начала: если иностранная карта перестанет работать,
переход на GigaChat или YandexGPT не потребует переписывать конвейер
(DECISIONS.md, Р-005).

Модели подобраны по цене ошибки, а не по цене запроса:

  * claude-haiku-4-5 — простые переборы, где ошибка ничего не стоит;
  * claude-sonnet-5  — досье: качество фактов задаёт качество всего дальше;
  * claude-opus-5    — письмо: его читает живой человек, и второго шанса нет.

ИСТОРИЯ ОДНОЙ ОШИБКИ. До 2026-07-27 письма писал Haiku: `compose` вызывал
`classify()` ради схемы ответа, а модель в ней была зашита намертво.
Функция `write()` с более сильной моделью не вызывалась ни разу.
Отсюда и лесть вместо наблюдений, и выдуманные описания системы
(DECISIONS.md, Р-031). Теперь модель — параметр, а не константа внутри.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import log

logger = log.get("llm")

MODEL_CLASSIFY = "claude-haiku-4-5"
MODEL_EXTRACT = "claude-sonnet-5"
MODEL_WRITE = "claude-opus-5"


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """Нет ключа или библиотеки. Не авария — повод пропустить шаг."""


def _client() -> Any:
    try:
        import anthropic
    except ImportError as exc:
        raise LLMUnavailable(
            "Не установлен пакет anthropic. Выполните: pip install -r requirements.txt"
        ) from exc

    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise LLMUnavailable(
            "Не задан ANTHROPIC_API_KEY. Положите ключ в .env "
            "(шаблон — в .env.example)"
        )
    return anthropic.Anthropic()


def classify(system_prompt: str, user_content: str, schema: dict[str, Any],
             max_tokens: int = 1024, model: str | None = None) -> dict[str, Any]:
    """Ответ по строгой схеме. Модель задаёт вызывающий.

    Схема гарантирует, что модель вернёт валидный JSON нужной формы —
    разбирать свободный текст и угадывать формат не приходится.

    `model` обязателен по смыслу, хотя и не по сигнатуре: значение по
    умолчанию — самая дешёвая модель, и именно из-за него письма полгода
    писал Haiku. Каждый вызывающий указывает модель явно.
    """
    client = _client()
    chosen = model or MODEL_CLASSIFY
    logger.debug("Запрос к %s, схема из %d полей",
                 chosen, len(schema.get("properties", {})))
    try:
        response = client.messages.create(
            model=chosen,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except Exception as exc:  # noqa: BLE001 — наверх отдаём один тип ошибки
        raise LLMError(f"Запрос к модели не удался: {exc}") from exc

    if response.stop_reason == "refusal":
        raise LLMError("Модель отказалась отвечать на этот запрос")

    text = next((block.text for block in response.content if block.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Модель вернула не-JSON: {text[:200]}") from exc


def write(system_prompt: str, user_content: str, max_tokens: int = 2048) -> str:
    """Генерация текста письма. Возвращает готовый текст."""
    client = _client()
    try:
        response = client.messages.create(
            model=MODEL_WRITE,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"Запрос к модели не удался: {exc}") from exc

    if response.stop_reason == "refusal":
        raise LLMError("Модель отказалась писать этот текст")

    return "".join(block.text for block in response.content if block.type == "text").strip()
