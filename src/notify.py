"""Доставка утреннего списка в Telegram.

ЗАЧЕМ. Список должен приходить туда, куда человек и так заходит каждое утро.
Файл в папке `morning/` требует помнить о нём и открывать. Telegram —
не требует.

КАК УСТРОЕНО. Каждая компания уходит отдельным сообщением. Так письмо можно
скопировать одним нажатием, не выделяя кусок из простыни. Текст письма идёт
блоком кода — в Telegram по нему есть кнопка «копировать».

Токен бота и адрес чата берутся из переменных окружения:
    TELEGRAM_BOT_TOKEN — выдаёт @BotFather за две минуты;
    TELEGRAM_CHAT_ID   — ваш ID, узнаётся у @userinfobot.

Если переменных нет, этап просто пропускается. Это не авария: файл списка
всё равно лежит на диске.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from . import log

logger = log.get("notify")

API = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram режет сообщения длиннее этого. Режем сами и по границе абзаца.
MAX_MESSAGE = 3900

# Пауза между сообщениями: у Bot API лимит примерно 30 сообщений в секунду,
# но при 20 компаниях спешить некуда, а лимит ловить незачем.
PAUSE_SECONDS = 0.4


class TelegramUnavailable(RuntimeError):
    """Нет токена или чата. Повод пропустить этап, а не упасть."""


def credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        raise TelegramUnavailable(
            "Не заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID. "
            "Токен — у @BotFather, свой ID — у @userinfobot. "
            "Положите их в .env"
        )
    return token, chat


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Режет длинный текст по границам абзацев, а не посередине слова."""
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        # Абзац сам по себе длиннее лимита — режем по строкам.
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            parts.append(block[:cut])
            block = block[cut:].lstrip("\n")
        current = block
    if current:
        parts.append(current)
    return parts


def send(text: str, timeout: int = 20) -> bool:
    """Отправляет одно сообщение. Возвращает False, если не получилось."""
    token, chat = credentials()
    for chunk in split_message(text):
        try:
            response = requests.post(
                API.format(token=token),
                json={"chat_id": chat, "text": chunk,
                      "parse_mode": "Markdown",
                      "disable_web_page_preview": True},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            logger.warning("Telegram недоступен: %s", exc)
            return False

        if response.status_code != 200:
            # Частая причина — Markdown не разобрался из-за символов в тексте
            # компании. Пробуем ещё раз без разметки: лучше без неё, чем никак.
            plain = requests.post(
                API.format(token=token),
                json={"chat_id": chat, "text": chunk,
                      "disable_web_page_preview": True},
                timeout=timeout,
            )
            if plain.status_code != 200:
                logger.warning("Telegram отказал: HTTP %d %s",
                               response.status_code, response.text[:200])
                return False
        time.sleep(PAUSE_SECONDS)
    return True


def send_morning(blocks: list[str], header: str) -> dict[str, int]:
    """Отправляет утренний список: шапка, потом по сообщению на компанию."""
    counters = {"sent": 0, "failed": 0}
    try:
        credentials()
    except TelegramUnavailable as exc:
        logger.info("%s", exc)
        return {"sent": 0, "failed": 0, "skipped": 1}

    if send(header):
        counters["sent"] += 1
    else:
        counters["failed"] += 1

    for block in blocks:
        if send(block):
            counters["sent"] += 1
        else:
            counters["failed"] += 1

    logger.info("Telegram: отправлено сообщений %d, не удалось %d",
                counters["sent"], counters["failed"])
    return counters
