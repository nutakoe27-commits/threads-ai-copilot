"""Технографика: что стоит у компании на сайте.

ЗАЧЕМ. Мы уже скачиваем HTML их сайта ради классификации и выбрасываем всё,
кроме текста. А в том же HTML видно, чем компания пользуется: считает ли она
трафик, платит ли за него, ловит ли входящие.

Для нашего оффера это важнее, чем кажется. Компания с коллтрекингом и Roistat
уже покупает спрос и умеет считать стоимость лида — с ней можно говорить
цифрами. Компания без единого счётчика живёт на сарафане, и объяснять ей
придётся с нуля. Это два разных письма (DECISIONS.md, Р-044).

КАК ОПРЕДЕЛЯЕТСЯ. Стандартным способом: по адресам скриптов, по мета-тегам,
по характерным строкам в HTML. Никакого исполнения JavaScript — только текст
страницы, который у нас и так есть.

ЧЕГО ЗДЕСЬ НЕТ. Определения рекламных кампаний. Факт, что счётчик Метрики
стоит, виден; крутится ли реклама и с каким бюджетом — нет. Домысливать это
мы не будем.
"""

from __future__ import annotations

import re
from typing import Any

from . import log

logger = log.get("techstack")

# Сигнатуры. Ключ — название инструмента, значение — что искать в HTML.
# Поиск идёт по нижнему регистру, поэтому все образцы строчные.
#
# Группы устроены по смыслу для продаж, а не по типу технологии:
# кто считает, кто платит за трафик, кто ловит входящие.

SIGNATURES: dict[str, dict[str, Any]] = {
    # --- Аналитика: считают ли они хоть что-нибудь -------------------------
    "Яндекс.Метрика": {"group": "analytics",
                       "patterns": ["mc.yandex.ru/metrika", "ym(", "yandex_metrika"]},
    "Google Analytics": {"group": "analytics",
                         "patterns": ["google-analytics.com", "googletagmanager.com/gtag",
                                      "gtag(", "ga('create'"]},
    "Google Tag Manager": {"group": "analytics",
                           "patterns": ["googletagmanager.com/gtm.js"]},
    "Top.Mail.Ru": {"group": "analytics", "patterns": ["top-fwz1.mail.ru", "top.mail.ru/counter"]},

    # --- Платный трафик и его измерение ------------------------------------
    # Самая ценная группа: эти инструменты ставят, когда платят за клики
    # и хотят понимать, во что обходится заявка.
    "Roistat": {"group": "paid", "patterns": ["roistat", "cloud.roistat.com"]},
    "Calltouch": {"group": "paid", "patterns": ["calltouch.ru", "calltouch.js"]},
    "Callibri": {"group": "paid", "patterns": ["callibri.ru", "callibri.js"]},
    "CoMagic": {"group": "paid", "patterns": ["comagic.ru", "comagic.js"]},
    "Mango Office": {"group": "paid", "patterns": ["mango-office.ru", "mangosite"]},
    "Яндекс.Директ (пиксель)": {"group": "paid", "patterns": ["yandex.ru/ads", "an.yandex.ru"]},
    "VK Пиксель": {"group": "paid", "patterns": ["vk.com/js/api/openapi", "top-fwz1", "vk-pixel"]},

    # --- Захват входящих ---------------------------------------------------
    "Jivo": {"group": "capture", "patterns": ["jivosite.com", "jivo.ru", "code.jivosite"]},
    "Envybox": {"group": "capture", "patterns": ["envybox", "envycdn"]},
    "Bitrix24 (виджет)": {"group": "capture",
                          "patterns": ["bitrix24.ru/b", "cdn-ru.bitrix24", "b24-widget"]},
    "amoCRM (форма)": {"group": "capture", "patterns": ["amocrm.ru", "amo-forms"]},
    "Tilda (формы)": {"group": "capture", "patterns": ["tilda.cc", "tildacdn"]},

    # --- Платформа сайта ---------------------------------------------------
    "1С-Битрикс": {"group": "cms", "patterns": ["bitrix/js", "bitrix/templates", "/bitrix/"]},
    "WordPress": {"group": "cms", "patterns": ["wp-content", "wp-includes", "wordpress"]},
    "Tilda": {"group": "cms", "patterns": ["tildacdn.com", "tilda-blocks"]},
    "Drupal": {"group": "cms", "patterns": ["/sites/all/", "drupal.js"]},
    "MODX": {"group": "cms", "patterns": ["modx", "assets/components"]},
    "Next.js": {"group": "cms", "patterns": ["/_next/static", "__next_data__"]},
}

# Как читать набор инструментов. Это не «хорошо/плохо», а «на каком языке
# с ними говорить».
MATURITY = {
    "paid": ("платит за трафик и меряет его",
             "Компания покупает спрос и считает стоимость заявки. С ней можно "
             "говорить цифрами: сколько стоит лид, сколько встреч на сотню писем."),
    "analytics": ("считает трафик, но не покупает",
                  "Аналитика стоит, инструментов платного трафика нет. Скорее всего "
                  "живут на входящих и сарафане, а исходящих не пробовали."),
    "capture": ("ловит входящие",
                "Есть виджеты захвата. Значит, поток на сайт какой-то идёт "
                "и его пытаются превратить в заявки."),
    "none": ("никаких следов маркетинга",
             "Ни счётчиков, ни виджетов. Продажи, скорее всего, целиком на связях "
             "и повторных клиентах. Разговор про исходящие придётся начинать с нуля."),
}

_META_GENERATOR = re.compile(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)',
                             re.IGNORECASE)


def detect(html: str) -> dict[str, list[str]]:
    """Находит инструменты в HTML. Возвращает словарь группа → список названий."""
    if not html:
        return {}

    lowered = html.lower()
    found: dict[str, list[str]] = {}

    for name, spec in SIGNATURES.items():
        if any(pattern in lowered for pattern in spec["patterns"]):
            found.setdefault(spec["group"], []).append(name)

    # Мета-тег generator часто называет CMS прямо, включая те, которых
    # нет в наших сигнатурах.
    match = _META_GENERATOR.search(html)
    if match:
        generator = match.group(1).strip()[:60]
        if generator and not any(generator.lower() in name.lower()
                                 for names in found.values() for name in names):
            found.setdefault("cms", []).append(generator)

    return found


def maturity(stack: dict[str, list[str]]) -> tuple[str, str]:
    """Как читать набор инструментов. Возвращает (краткое, пояснение)."""
    for group in ("paid", "capture", "analytics"):
        if stack.get(group):
            return MATURITY[group]
    return MATURITY["none"]


def as_facts(stack: dict[str, list[str]]) -> list[str]:
    """Факты для досье. Только то, что действительно нашлось.

    В письме эти факты использовать НЕЛЬЗЯ: «я посмотрел, какие у вас стоят
    счётчики» звучит как слежка. Они нужны Михаилу — понять, на каком языке
    говорить, — и потому идут в досье с пометкой «не для письма».
    """
    facts: list[str] = []
    if stack.get("paid"):
        facts.append("Платит за трафик: " + ", ".join(stack["paid"]))
    if stack.get("capture"):
        facts.append("Ловит входящие: " + ", ".join(stack["capture"]))
    if stack.get("analytics"):
        facts.append("Аналитика: " + ", ".join(stack["analytics"]))
    if stack.get("cms"):
        facts.append("Сайт на: " + ", ".join(stack["cms"][:2]))
    return facts


def describe(stack: dict[str, list[str]]) -> str:
    """Одна строка для утреннего списка."""
    short, _ = maturity(stack)
    tools = [name for group in ("paid", "capture") for name in stack.get(group, [])]
    if tools:
        return f"{short} ({', '.join(tools[:3])})"
    return short
