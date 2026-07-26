"""Вежливый сбор текста с сайта компании.

Зачем это нужно. Реестровые данные не отличают разработку на заказ от
продуктовой компании: ОКВЭД 62.01 носят и веб-студия, и вендор коробочного
софта, и дистрибьютор. А сайт отличает сразу — см. DECISIONS.md, Р-019.

Правила вежливости, заложенные в код:
  * читаем и соблюдаем robots.txt, включая Crawl-delay;
  * честный User-Agent с контактом, без маскировки под браузер;
  * пауза между запросами, не более 4 страниц с сайта;
  * таймаут, ограничение размера ответа, никаких повторных обходов —
    результат кэшируется в базе.

Это публичный сайт самой компании, а не чужой агрегатор: ситуация
принципиально чище, чем обход площадки вопреки её соглашению.
"""

from __future__ import annotations

import re
import time
import urllib.robotparser
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from .. import log

logger = log.get("website")

# Страницы, где компании обычно рассказывают, чем занимаются.
CANDIDATE_PATHS = [
    "", "/about", "/about-us", "/o-kompanii", "/company",
    "/services", "/uslugi", "/what-we-do", "/solutions",
]

MAX_PAGES = 4
MAX_BYTES = 500_000
MAX_TEXT_CHARS = 12_000


class _TextExtractor(HTMLParser):
    """Достаёт видимый текст, выкидывая скрипты, стили и разметку."""

    SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        joined = " ".join(self.parts)
        return re.sub(r"\s+", " ", joined).strip()


def normalize_url(raw: str) -> str | None:
    """Приводит адрес из ЕГРЮЛ к виду, пригодному для запроса."""
    if not raw:
        return None
    value = raw.strip().strip(",;").split()[0]
    if not value or "." not in value:
        return None
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    parsed = urlparse(value)
    if not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


class WebsiteFetcher:
    def __init__(self, config: dict[str, Any]) -> None:
        self.timeout = int(config.get("timeout_seconds", 15))
        self.pause = float(config.get("pause_seconds", 2.0))
        self.max_pages = int(config.get("max_pages", MAX_PAGES))
        contact = config.get("contact", "")
        self.user_agent = (
            f"leadgen-research/0.1 (+{contact})" if contact
            else "leadgen-research/0.1"
        )
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru,en;q=0.8",
        })

    def _robots(self, base_url: str) -> tuple[urllib.robotparser.RobotFileParser | None, float]:
        """Читает robots.txt. Возвращает парсер и требуемую задержку."""
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(urljoin(base_url, "/robots.txt"))
        try:
            response = self.session.get(
                urljoin(base_url, "/robots.txt"), timeout=self.timeout
            )
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
            else:
                # robots.txt нет — по умолчанию считаем, что можно.
                parser.parse([])
        except requests.RequestException as exc:
            logger.debug("robots.txt недоступен для %s (%s) — считаем разрешённым",
                         base_url, exc)
            parser.parse([])

        delay = self.pause
        try:
            declared = parser.crawl_delay(self.user_agent)
            if declared:
                delay = max(delay, float(declared))
        except Exception:  # noqa: BLE001 — разные версии ведут себя по-разному
            pass
        return parser, delay

    def fetch(self, raw_url: str) -> dict[str, Any]:
        """Собирает текст с нескольких страниц сайта.

        Возвращает словарь с текстом и диагностикой. Никогда не бросает
        исключение наружу: недоступный сайт — это нормальный исход, а не
        авария всего прогона.
        """
        base_url = normalize_url(raw_url)
        if not base_url:
            return {"ok": False, "reason": "некорректный адрес сайта", "text": "", "pages": []}

        robots, delay = self._robots(base_url)
        collected: list[str] = []
        visited: list[str] = []

        for path in CANDIDATE_PATHS:
            if len(visited) >= self.max_pages:
                break
            url = urljoin(base_url, path) if path else base_url

            if robots and not robots.can_fetch(self.user_agent, url):
                logger.debug("robots.txt запрещает %s — пропускаем", url)
                continue

            try:
                response = self.session.get(url, timeout=self.timeout, stream=True)
                if response.status_code != 200:
                    continue
                content_type = response.headers.get("Content-Type", "")
                if "html" not in content_type.lower():
                    continue

                body = response.raw.read(MAX_BYTES, decode_content=True) or b""
                encoding = response.encoding or "utf-8"
                html = body.decode(encoding, errors="replace")
            except requests.RequestException as exc:
                logger.debug("%s недоступен: %s", url, exc)
                continue
            finally:
                time.sleep(delay)

            extractor = _TextExtractor()
            try:
                extractor.feed(html)
            except Exception:  # noqa: BLE001 — кривая вёрстка не должна ронять прогон
                continue

            text = extractor.text()
            if len(text) > 200:
                collected.append(text)
                visited.append(url)

        if not collected:
            return {"ok": False, "reason": "не удалось прочитать ни одной страницы",
                    "text": "", "pages": []}

        combined = " ".join(collected)[:MAX_TEXT_CHARS]
        return {"ok": True, "reason": "", "text": combined, "pages": visited,
                "base_url": base_url}
