"""Единственное место в системе, которое знает про LLM-провайдера.

Смена провайдера — правка этого файла, остальной код её не заметит.
Так задумано с самого начала: если иностранная карта перестанет работать,
переход на GigaChat или YandexGPT не потребует переписывать конвейер
(DECISIONS.md, Р-005).

Модели:
  * claude-haiku-4-5 — классификация (дёшево, много вызовов);
  * claude-sonnet-5  — генерация письма (качество русского текста важнее цены).
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import log

logger = log.get("llm")

MODEL_CLASSIFY = "claude-haiku-4-5"
MODEL_WRITE = "claude-sonnet-5"


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
             max_tokens: int = 1024) -> dict[str, Any]:
    """Классификация со строгой схемой ответа.

    Схема гарантирует, что модель вернёт валидный JSON нужной формы —
    разбирать свободный текст и угадывать формат не приходится.
    """
    client = _client()
    try:
        response = client.messages.create(
            model=MODEL_CLASSIFY,
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
