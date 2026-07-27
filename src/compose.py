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
from pathlib import Path
from typing import Any

from . import db, facts as factlib, llm, log

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
        "`why_line` — для внутреннего списка, её читает только Михаил. "
        "Здесь можно называть выручку и численность, в самом письме — нельзя."
    )


# Хвостовые строки, которые модель дописывает сама вопреки запрету.
# Подпись подставляет код, поэтому свою надо убрать.
SIGN_OFFS = {"михаил", "с уважением", "всего доброго", "спасибо",
             "заранее спасибо", "хорошего дня", "до связи"}


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


def finish_letter(body: str, origin: str) -> tuple[str, list[str]]:
    """Дописывает концовку и проверяет письмо механически.

    Всё, что здесь проверяется, — не про вкус, а про соблюдение прямых
    запретов. Просить модель об этом бесполезно: подпись она теряла три
    прогона подряд, а описание системы сочиняла каждый раз заново.
    Возвращает готовый текст и список замечаний для человека.
    """
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

    if "!" in text:
        notes.append("в тексте есть восклицательный знак — уберите руками")
    if not text.lower().startswith("здравствуйте"):
        notes.append("письмо не начинается с приветствия")

    words = len(text.split())
    if words < 90:
        notes.append(f"письмо короткое ({words} слов) — вероятно, "
                     f"не хватило места объяснить, кто пишет и зачем")
    if words > 300:
        notes.append(f"письмо длинное ({words} слов) — такое читают по диагонали")

    return f"{text}\n\n{origin}", notes


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
    for row in rows:
        name = row["name"]

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

        body, notes = finish_letter(result.get("body", ""), origin)
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
                (result.get("subject", ""), body, result.get("why_line", ""),
                 json.dumps(facts_used, ensure_ascii=False), status,
                 db.now(), row["inn"]),
            )

    if not dry_run:
        conn.commit()
    conn.close()

    logger.info("Письма: годных %d, отложено (мало фактов) %d, не удалось %d",
                counters["written"], counters["thin"], counters["failed"])
    if counters["thin"]:
        logger.info("Отложенные письма: sqlite3 data/leads.db < tools/thin.sql")
    return counters
