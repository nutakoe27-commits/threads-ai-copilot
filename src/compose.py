"""Этап 3б: генерация письма по досье.

Промпт собирается из двух частей:
  * config/prompts/letter.md — оффер, первый шаг, запреты, тон. Общее для всех.
  * config/prompts/angles/<тип>.md — угол под тип компании.

Разделение не косметическое. Запреты и тон одинаковы всегда, а разговор с
продуктовой компанией и с аутсорсером идёт о разном: у первой ICP острый и
ей интересно ловить покупателя по событию, у второй боль в предсказуемости
потока. Один текст на обоих превращается в рассылку — см. DECISIONS.md, Р-021.

Письмо не отправляется. Оно попадает в утренний список, где вы его читаете,
правите и отправляете руками.

Письмо, не набравшее минимума фактов из досье (compose.min_facts в icp.yaml),
в утренний список не идёт вовсе — см. DECISIONS.md, Р-024.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import db, facts as factlib, llm, log, metrics, outreach

logger = log.get("compose")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "config" / "prompts"

LETTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "why_line": {"type": "string"},
        "facts_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["subject", "body", "why_line", "facts_used"],
    "additionalProperties": False,
}


def load_prompt(relative: str) -> str:
    path = PROMPTS_DIR / relative
    if not path.exists():
        raise FileNotFoundError(f"Не найден промпт {path}")
    return path.read_text(encoding="utf-8")


def build_system_prompt(site_type: str) -> str:
    """Собирает промпт из трёх частей: бриф, оффер, угол под тип компании.

    Разделение по частоте изменений, а не по красоте. Бриф (кто пишет, кому,
    что считается хорошей работой) устойчив. Оффер переписывается каждый раз,
    когда вы поймёте, на что люди отвечают, — и правится отдельно, не задевая
    остального. Угол объясняет, чем разговор с продуктовой компанией
    отличается от разговора с аутсорсером (DECISIONS.md, Р-021).
    """
    base = load_prompt("letter.md")
    offer = load_prompt("offer.md")
    angle_file = f"angles/{site_type}.md"
    try:
        angle = load_prompt(angle_file)
    except FileNotFoundError:
        angle = load_prompt("angles/default.md")

    return (
        base
        + "\n\n---\n\n" + offer
        + "\n\n---\n\n" + angle
        + "\n\n---\n\n## Формат ответа\n\n"
        "Верни JSON: `subject` — тема письма; `body` — текст письма; "
        "`why_line` — одна строка для утреннего списка, объясняющая, почему "
        "эта компания здесь; `facts_used` — список фактов из досье, на "
        "которые ты реально сослался в письме.\n\n"
        "`facts_used` — не украшение, а самопроверка. Перечисляй только то, "
        "что действительно попало в текст письма, дословно или близко. "
        "Общие слова вроде «IT-компания» фактом не считаются. Письмо, "
        "не опирающееся хотя бы на два факта из досье, человеку не "
        "показывается: лучше не написать письма вовсе, чем написать такое, "
        "которое подошло бы кому угодно.\n\n"
        "`why_line` — одна короткая строка для внутреннего списка, не длиннее "
        "120 знаков. По ней Михаил за секунду решает, читать ли письмо целиком, "
        "поэтому это не пересказ письма и не досье: одна причина, почему "
        "компания здесь. Выручку и численность тут называть можно — "
        "в самом письме нельзя."
    )


# Хвостовые строки, которые модель дописывает сама вопреки запрету.
# Подпись подставляет код, поэтому свою надо убрать.
SIGN_OFFS = {"михаил", "с уважением", "всего доброго", "спасибо",
             "заранее спасибо", "хорошего дня", "до связи"}

# Тема длиннее этого обрезается в списке писем на телефоне.
#
# Раньше стояло 50. Поднято до 70, потому что тема теперь называет действие
# и компанию сразу: «Автоматизация поиска клиентов для отдела продаж PROMT» —
# это 53 знака. Обрезание на телефоне при этом никуда не делось: первые
# 35–40 знаков должны нести смысл сами по себе (DECISIONS.md, Р-048).
SUBJECT_MAX = 70

# ЖАРГОН И СОКРАЩЕНИЯ. Письмо читает генеральный директор, а не специалист
# по продажам. Слово, которого он не понял, — это секунда задержки, а у
# письма их всего две.
#
# Проверка живёт в коде, а не только в промпте, намеренно. Просить модель
# «не использовать сокращения» бесполезно ровно так же, как было бесполезно
# просить её не подписываться: она соглашается и через письмо забывает.
#
# Слова в нижнем регистре — по корню, чтобы ловились все падежи.
JARGON_WORDS = {
    "лидоген": "поиск клиентов",
    "лидген": "поиск клиентов",
    "конверси": "сколько из них ответили",
    "воронк": "путь от письма до сделки",
    "пайплайн": "список сделок в работе",
    "аутрич": "исходящие письма",
    "скоринг": "оценка компании по признакам",
    "квалификац": "проверка, подходит ли компания",
    "ретеншн": "удержание клиентов",
    "чурн": "уход клиентов",
    "фоллоу": "второе письмо",
    "оффер": "предложение",
}

# Сокращения из заглавных букв ловятся правилом, а не списком: список всегда
# отстаёт от изобретательности модели. Два и больше подряд — уже сокращение.
_ACRONYM_RE = re.compile(r"\b[A-ZА-ЯЁ]{2,}\b")

# Что разрешено, хотя и написано заглавными.
ACRONYM_ALLOWED = {"P.S.", "PS", "SMS"}


def find_jargon(text: str, company: str = "") -> list[str]:
    """Сокращения и профессиональные слова в тексте письма.

    `company` — название компании-адресата: его сокращения разрешены,
    иначе письмо в PROMT ругалось бы на слово PROMT.
    """
    allowed = set(ACRONYM_ALLOWED)
    for token in _ACRONYM_RE.findall((company or "").upper()):
        allowed.add(token)

    found: list[str] = []
    for token in _ACRONYM_RE.findall(text):
        if token not in allowed and token not in found:
            found.append(token)

    lowered = text.lower()
    for root, replacement in JARGON_WORDS.items():
        if root in lowered:
            found.append(f"{root}… (лучше: {replacement})")
    return found


def load_origin() -> str:
    """Концовка письма: откуда оно и подпись. Дословно из конфига.

    Комментарии в начале файла (HTML-вида) выбрасываются — они для человека,
    а не для письма.
    """
    text = load_prompt("origin.md")
    while "<!--" in text and "-->" in text:
        head, _, rest = text.partition("<!--")
        _, _, tail = rest.partition("-->")
        text = head + tail
    return text.strip()


def finish_letter(body: str, origin: str, company: str = "",
                  short: bool = False) -> tuple[str, list[str]]:
    """Дописывает концовку и проверяет письмо механически.

    Всё, что здесь проверяется, — не про вкус, а про соблюдение прямых
    запретов. Просить модель об этом бесполезно: подпись она теряла три
    прогона подряд, а описание системы сочиняла каждый раз заново.
    Возвращает готовый текст и список замечаний для человека.

    `short=True` — для дожима. У него своя длина и три абзаца вместо пяти:
    знакомиться заново не нужно, нужна одна новая мысль.
    """
    min_words, max_words = (55, 140) if short else (95, 220)
    min_paragraphs = 3 if short else 4
    text = (body or "").strip()
    notes: list[str] = []

    # Модель нет-нет да и подпишется сама, вопреки запрету. Срезаем хвостовые
    # строки с подписью и прощанием, иначе подпись окажется в письме дважды.
    # Обрезать по последнему вопросу, как раньше, больше нельзя: письмо
    # заканчивается просьбой, а она не обязана быть вопросом.
    lines = text.splitlines()
    while lines:
        last = lines[-1].strip().rstrip(",.").lower()
        if not last:
            lines.pop()
            continue
        if last in SIGN_OFFS or (len(last) < 30 and last.startswith("с уважением")):
            lines.pop()
            continue
        break
    text = "\n".join(lines).strip()

    if not text.lower().startswith("здравствуйте"):
        notes.append("письмо не начинается с приветствия")

    # Восклицательный знак разрешён ровно один раз — в приветствии. Дальше
    # по тексту он читается как напор, а напор в холодном письме не работает.
    body_after_greeting = "\n\n".join(text.split("\n\n")[1:])
    if "!" in body_after_greeting:
        notes.append("восклицательный знак в тексте письма — он разрешён "
                     "только в «Здравствуйте!»")

    # СОКРАЩЕНИЯ. Главная проверка этого блока: письмо читает генеральный
    # директор, а не специалист по продажам (DECISIONS.md, Р-048).
    jargon = find_jargon(text, company)
    if jargon:
        notes.append("сокращения и жаргон — распишите словами: "
                     + ", ".join(jargon[:6]))

    # Длина. Письмо теперь объясняет, что делает система и по каким признакам
    # ищет, — на это нужно место. Потолок держим: получатель сканирует.
    words = len(text.split())
    if words < min_words:
        notes.append(f"письмо короткое ({words} слов) — вероятно, "
                     f"не хватило места на признаки покупателя")
    if words > max_words:
        notes.append(f"письмо длинное ({words} слов при потолке {max_words}) — "
                     f"первое лицо такое сканирует, а не читает")

    # Абзац длиннее четырёх строк на телефоне выглядит стеной, а с телефона
    # эти письма и открывают чаще всего.
    longest = max((len(p.split()) for p in text.split("\n\n")), default=0)
    if longest > 70:
        notes.append(f"самый длинный абзац — {longest} слов, разбейте его")

    # Пять частей письма: приветствие, вопрос, что делает Михаил,
    # что предлагает, просьба. Меньше четырёх абзацев — какая-то выпала.
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) < min_paragraphs:
        notes.append(f"абзацев {len(paragraphs)} — проверьте, что на месте "
                     f"вопрос, рассказ о системе, предложение и просьба")
    if "?" not in text and not short:
        notes.append("в письме нет ни одного вопроса — первый абзац должен "
                     "спрашивать про их работу")

    return f"{text}\n\n{origin}", notes


def build_followup_prompt(site_type: str) -> str:
    """Промпт дожима: общий бриф плюс правила второго письма.

    Запреты, тон, язык и длина берутся из letter.md без изменений. Меняется
    только то, что письмо должно сделать: не познакомиться, а дать новую
    мысль тому, кто уже получил первое (DECISIONS.md, Р-041).
    """
    return (build_system_prompt(site_type)
            + "\n\n---\n\n" + load_prompt("followup.md"))


def build_followup_dossier(row: Any, previous: list[Any]) -> str:
    """Досье для дожима: те же факты плюс то, что уже было написано."""
    lines = [build_dossier(row), ""]
    lines.append("=" * 60)
    lines.append("ЧТО ЭТОЙ КОМПАНИИ УЖЕ ПИСАЛИ. Не повторяй ни мысль, ни "
                 "формулировки. Возьми другой факт и сделай другой вывод.")
    for touch in previous:
        lines.append("")
        lines.append(f"--- Письмо {touch['step']}, отправлено {(touch['sent_at'] or '')[:10]}")
        lines.append(f"Тема: {touch['subject']}")
        lines.append(touch["body"] or "")
    return "\n".join(lines)


def site_facts(row: Any) -> list[str]:
    """Проверенные факты с сайта компании. На них строится наблюдение."""
    try:
        values = json.loads(row["site_facts"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(value) for value in values if str(value).strip()]


def build_dossier(row: Any) -> str:
    """Собирает досье компании — единственный источник фактов для письма.

    Досье разделено на две части намеренно. Наверху — конкретика с сайта,
    из которой пишется наблюдение. Внизу — реестровые данные: они нужны,
    чтобы модель понимала масштаб компании, но писать о них в письме
    нельзя, иначе получается «я посмотрел вашу отчётность».
    """
    lines: list[str] = [f"Компания: {row['name']}"]

    facts = site_facts(row)
    if facts:
        lines.append("")
        lines.append("НАЙДЕНО НА ИХ САЙТЕ — из этого пишется наблюдение:")
        lines.extend(f"  {index}. {fact}" for index, fact in enumerate(facts, start=1))

    if row["site_summary"]:
        lines.append("")
        lines.append(f"Чем занимается в целом: {row['site_summary']}")
    if row["site_specialization"]:
        try:
            spec = json.loads(row["site_specialization"])
            if spec:
                lines.append(f"Специализация: {', '.join(spec)}")
        except (json.JSONDecodeError, TypeError):
            pass

    background: list[str] = []
    if row["region"]:
        background.append(f"регион: {row['region']}")
    if row["staff"]:
        background.append(f"численность: {row['staff']:.0f} человек")
    if row["revenue"]:
        line = f"выручка: {row['revenue'] / 1e6:.0f} млн ₽"
        if row["revenue_change_pct"] is not None:
            line += f" ({row['revenue_change_pct']:+.0f}% год к году)"
        background.append(line)
    if row["okved_name"]:
        background.append(f"вид деятельности по реестру: {row['okved_name']}")

    # Технографика идёт сюда же, к «не упоминать». Она нужна, чтобы понять,
    # на каком языке говорить с компанией, но фраза «я посмотрел, какие
    # у вас стоят счётчики» звучит как слежка (DECISIONS.md, Р-044).
    try:
        stack = json.loads(row["tech_stack"] or "{}")
    except (json.JSONDecodeError, TypeError):
        stack = {}
    if stack:
        from . import techstack
        _, explanation = techstack.maturity(stack)
        background.append(f"маркетинг: {explanation}")

    if background:
        lines.append("")
        lines.append("ДЛЯ ПОНИМАНИЯ МАСШТАБА — в тексте письма не упоминать "
                     "(в why_line можно):")
        lines.extend(f"  {item}" for item in background)

    lines.append("")
    lines.append(
        "ВНИМАНИЕ: это все факты, которые есть. Ничего сверх этого списка "
        "в письме быть не должно."
    )
    return "\n".join(lines)


def run(config: dict[str, Any], dry_run: bool = False, limit: int | None = None) -> dict[str, int]:
    """Генерирует письма для компаний, прошедших классификацию по сайту."""
    site_cfg = config.get("website", {})
    target_types = site_cfg.get("target_types") or ["outsourcing", "staffing"]
    compose_cfg = config.get("compose", {})
    per_run = limit or int(compose_cfg.get("per_run", 20))
    min_facts = int(compose_cfg.get("min_facts", 2))

    placeholders = ",".join("?" for _ in target_types)
    conn = db.connect()
    rows = conn.execute(
        f"""
        SELECT * FROM companies
        WHERE icp_status = 'passed'
          AND site_type IN ({placeholders})
          AND letter_body IS NULL
          AND letter_status IS NULL
        ORDER BY
            CASE WHEN revenue_change_pct IS NULL THEN 1 ELSE 0 END,
            revenue_change_pct
        LIMIT ?
        """,
        (*target_types, per_run),
    ).fetchall()

    if not rows:
        logger.info("Нет компаний, готовых к письму. "
                    "Сначала: run.py --stage targets, затем --stage dossier")
        conn.close()
        return {"written": 0}

    logger.info("Компаний к написанию письма: %d (модель %s)", len(rows), llm.MODEL_WRITE)

    try:
        origin = load_origin()
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        conn.close()
        return {"written": 0}

    counters = {"written": 0, "failed": 0, "thin": 0}
    for index, row in enumerate(rows, start=1):
        name = row["name"]
        log.progress(index, len(rows), name)

        # Если фактов с сайта меньше минимума, письмо всё равно окажется
        # шаблонным — модель не может опереться на то, чего нет. Не тратим
        # на это ни токенов, ни времени.
        if len(site_facts(row)) < min_facts:
            counters["thin"] += 1
            logger.info("— %s: фактов с сайта %d из %d — письмо не пишем",
                        name, len(site_facts(row)), min_facts)
            if not dry_run:
                conn.execute(
                    "UPDATE companies SET letter_status = 'thin', "
                    "letter_why = ?, letter_written_at = ? WHERE inn = ?",
                    ("на сайте не нашлось конкретики для наблюдения",
                     db.now(), row["inn"]),
                )
            continue

        try:
            system_prompt = build_system_prompt(row["site_type"] or "default")
        except FileNotFoundError as exc:
            logger.error("%s", exc)
            break

        try:
            result = llm.classify(system_prompt, build_dossier(row), LETTER_SCHEMA,
                                  max_tokens=2048, model=llm.MODEL_WRITE)
        except llm.LLMUnavailable as exc:
            logger.error("%s", exc)
            break
        except llm.LLMError as exc:
            counters["failed"] += 1
            logger.warning("— %s: письмо не написалось (%s)", name, exc)
            continue

        subject = result.get("subject", "")
        body, notes = finish_letter(body_text := result.get("body", ""), origin, name)
        if len(subject) > SUBJECT_MAX:
            notes.append(f"тема длинная ({len(subject)} знаков) — "
                         f"на телефоне обрежется, укоротите до {SUBJECT_MAX}")
        del body_text
        for note in notes:
            logger.warning("  ↳ %s: %s", name, note)

        # Проверку считаем сами. Список `facts_used`, который возвращает
        # модель, — это её самоотчёт: в него попадает то, что она собиралась
        # использовать, а не то, что оказалось в тексте. Проверка, которую
        # можно пройти, дописав строчку, ничего не проверяет (Р-025).
        available = site_facts(row)
        facts_used = factlib.used_facts(available, body)
        claimed = result.get("facts_used") or []
        if len(claimed) > len(facts_used):
            logger.debug("%s: модель заявила фактов %d, в тексте нашлось %d",
                         name, len(claimed), len(facts_used))

        # Письмо говорит адресату, что его компанию и факт о ней нашла
        # система. Если факта в тексте нет, письмо опровергает само себя.
        thin = len(facts_used) < min_facts
        status = "thin" if thin else "ok"

        if thin:
            counters["thin"] += 1
            # Уровень INFO, а не WARNING: это штатный исход, а не поломка.
            # В блок «Проблемы прогона» ему попадать незачем — отложенные
            # письма и так перечислены в хвосте отчёта отдельным разделом.
            logger.info(
                "— %s: фактов %d из %d — письмо отложено, в утренний список не пойдёт",
                name, len(facts_used), min_facts,
            )
        else:
            counters["written"] += 1
            logger.info("✓ %s — %s", name, result.get("why_line", ""))

        if not dry_run:
            conn.execute(
                """
                UPDATE companies SET
                    letter_subject = ?, letter_body = ?, letter_why = ?,
                    letter_facts = ?, letter_status = ?, letter_written_at = ?
                WHERE inn = ?
                """,
                (subject, body, result.get("why_line", ""),
                 json.dumps(facts_used, ensure_ascii=False), status,
                 db.now(), row["inn"]),
            )
            if not thin:
                outreach.save_touch(
                    conn, row["inn"], 1, subject, body,
                    result.get("why_line", ""),
                    json.dumps(facts_used, ensure_ascii=False))

    if not dry_run:
        metrics.record(conn, "compose", {
            "attempted": len(rows),
            "written": counters["written"],
            "thin": counters["thin"],
            "failed": counters["failed"],
        })
        conn.commit()
    conn.close()

    logger.info("Письма: годных %d, отложено (мало фактов) %d, не удалось %d",
                counters["written"], counters["thin"], counters["failed"])
    if counters["thin"]:
        logger.info("Отложенные письма: sqlite3 data/leads.db < tools/thin.sql")
    return counters


def run_followups(config: dict[str, Any], dry_run: bool = False) -> dict[str, int]:
    """Пишет второе и третье письмо тем, кто не ответил.

    Дожим отличается от первого письма одним: он обязан нести новую мысль.
    Поэтому модель получает не только досье, но и текст того, что уже
    отправили, с прямым запретом повторяться (DECISIONS.md, Р-041).

    Черновики складываются в историю касаний и попадают в утренний список
    отдельным разделом. Отправляет их человек, как и первые письма.
    """
    followup_cfg = config.get("followup", {})
    if not followup_cfg.get("enabled", True):
        logger.info("Дожимы выключены в конфиге (followup.enabled)")
        return {"written": 0}

    delay_days = int(followup_cfg.get("delay_days", 4))
    max_touches = int(followup_cfg.get("max_touches", 3))
    per_run = int(followup_cfg.get("per_run", 10))

    conn = db.connect()
    rows = outreach.pending_followups(conn, delay_days, max_touches)[:per_run]

    if not rows:
        logger.info("Дожимать некого. Условия: письмо отправлено, ответа нет, "
                    "прошло %d дней, касаний меньше %d", delay_days, max_touches)
        conn.close()
        return {"written": 0}

    logger.info("Компаний к дожиму: %d (модель %s)", len(rows), llm.MODEL_WRITE)

    try:
        origin = load_origin()
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        conn.close()
        return {"written": 0}

    counters = {"written": 0, "failed": 0}
    for index, row in enumerate(rows, start=1):
        name = row["name"]
        log.progress(index, len(rows), name)
        step = int(row["touch_count"] or 1) + 1
        previous = outreach.previous_touches(conn, row["inn"])

        if not previous:
            # Письмо отмечено отправленным, но текста в истории нет. Такое
            # бывает у компаний, обработанных до появления таблицы touches.
            logger.warning("— %s: нет текста прошлых писем, дожим пропущен", name)
            continue

        try:
            result = llm.classify(
                build_followup_prompt(row["site_type"] or "default"),
                build_followup_dossier(row, previous),
                LETTER_SCHEMA, max_tokens=2048, model=llm.MODEL_WRITE,
            )
        except llm.LLMUnavailable as exc:
            logger.error("%s", exc)
            break
        except llm.LLMError as exc:
            counters["failed"] += 1
            logger.warning("— %s: дожим не написался (%s)", name, exc)
            continue

        body, notes = finish_letter(result.get("body", ""), origin, name, short=True)
        for note in notes:
            logger.warning("  ↳ %s: %s", name, note)

        # Проверка, которой нет у первого письма: не повторяет ли дожим
        # предыдущее. Сравниваем со всеми уже отправленными текстами.
        repeated = any(factlib.coverage(factlib.words(body),
                                        factlib.words(touch["body"] or "")) > 0.6
                       for touch in previous)
        if repeated:
            logger.warning("  ↳ %s: дожим сильно повторяет прошлое письмо", name)

        counters["written"] += 1
        logger.info("✓ %s — письмо %d: %s", name, step, result.get("subject", ""))

        if not dry_run:
            outreach.save_touch(
                conn, row["inn"], step, result.get("subject", ""), body,
                result.get("why_line", ""),
                json.dumps(result.get("facts_used") or [], ensure_ascii=False))

    if not dry_run:
        metrics.record(conn, "followup", {"attempted": len(rows),
                                          "written": counters["written"]})
        conn.commit()
    conn.close()

    logger.info("Дожимов написано: %d, не удалось %d",
                counters["written"], counters["failed"])
    return counters
