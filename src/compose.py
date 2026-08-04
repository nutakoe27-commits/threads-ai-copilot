"""Этап 3б: письмо по досье.

КАК УСТРОЕНО. Письмо не генерируется целиком. Оно собирается из шаблона
`config/prompts/template.md`, в котором модель заполняет две вставки:

  {вопрос}    первый абзац — вопрос про то, как у них устроен поиск клиентов;
  {признаки}  перечень признаков ИХ покупателя.

Остальное — предложение, просьба, подпись — фиксированный текст. Он не
обязан меняться от компании к компании, а то, что модель пишет каждый раз
заново, она каждый раз пишет чуть иначе (DECISIONS.md, Р-053).

Промпт собирается из двух частей:
  * config/prompts/letter.md — бриф на вставки: что в них должно быть;
  * config/prompts/angles/<тип>.md — угол под тип компании.

Разделение не косметическое: запреты и язык одинаковы всегда, а разговор
с продуктовой компанией и с аутсорсером идёт о разном (Р-021).

Письмо не отправляется. Оно попадает в утренний список и в панель, где вы
его читаете, правите и отправляете руками.

Два порога отсева, оба до похода к модели:
  * `compose.min_score` — рейтинг схождения сигналов (Р-054);
  * `compose.min_facts` — сколько фактов из досье должно попасть в текст (Р-024).
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import db, facts as factlib, llm, log, metrics, outreach

logger = log.get("compose")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "config" / "prompts"

# ЧТО ОТДАЁТ МОДЕЛЬ. Не письмо, а две вставки в него. Само письмо лежит
# в config/prompts/template.md и не меняется от компании к компании
# (DECISIONS.md, Р-053).
LETTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "question": {"type": "string"},
        "signs": {"type": "string"},
        "why_line": {"type": "string"},
        "facts_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["subject", "question", "signs", "why_line", "facts_used"],
    "additionalProperties": False,
}

# У дожима вставка одна: новая мысль. Просьба и подпись тоже в шаблоне.
FOLLOWUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "thought": {"type": "string"},
        "why_line": {"type": "string"},
        "facts_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["subject", "thought", "why_line", "facts_used"],
    "additionalProperties": False,
}

# Потолок ответа. Раньше стояло 2048 под письмо целиком, и ответы в него
# упирались: обрыв выглядел как «модель вернула не-JSON», а токены были
# потрачены (DECISIONS.md, Р-052). Вставки короче письма впятеро, но запас
# оставлен — он ничего не стоит, пока не понадобился.
ANSWER_TOKENS = 1500


def load_prompt(relative: str) -> str:
    path = PROMPTS_DIR / relative
    if not path.exists():
        raise FileNotFoundError(f"Не найден промпт {path}")
    return path.read_text(encoding="utf-8")


def build_system_prompt(site_type: str) -> str:
    """Собирает промпт из двух частей: бриф на вставки и угол под тип компании.

    Оффера здесь больше нет. Раньше он был отдельным файлом, который модель
    читала, чтобы пересказать своими словами. Теперь предложение — это
    фиксированный абзац шаблона, и пересказывать его незачем: модель его
    даже не видит (DECISIONS.md, Р-053).
    """
    base = load_prompt("letter.md")
    try:
        angle = load_prompt(f"angles/{site_type}.md")
    except FileNotFoundError:
        angle = load_prompt("angles/default.md")

    return (
        base
        + "\n\n---\n\n# Угол под эту компанию\n\n" + angle
        + "\n\n---\n\n## Формат ответа\n\n"
        "Верни JSON с четырьмя полями: `subject`, `question`, `signs`, "
        "`why_line`, плюс `facts_used`.\n\n"
        "`facts_used` — самопроверка: перечисляй только те факты из досье, "
        "на которые ты действительно опёрся в `question` и `signs`. Общие "
        "слова вроде «IT-компания» фактом не считаются. Письмо, не опирающееся "
        "хотя бы на два факта, человеку не показывается — лучше не написать "
        "вовсе, чем написать подходящее кому угодно."
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


def find_jargon(text: str, company: str = "", known: str = "") -> list[str]:
    """Сокращения и профессиональные слова в тексте письма.

    `company` — название компании-адресата: его сокращения разрешены,
    иначе письмо в PROMT ругалось бы на слово PROMT.

    `known` — досье компании. Сокращение, которое есть в досье, — это
    название продукта или системы, взятое с их сайта: «РЕД ОС», «САРУС»,
    «1С». Такие слова законны.

    Обратное тоже верно и полезно: сокращение, которого в досье нет,
    модель откуда-то взяла сама. Либо это жаргон, либо выдуманное название —
    и то и другое надо увидеть.
    """
    allowed = set(ACRONYM_ALLOWED)
    for source in (company or "", known or ""):
        allowed.update(_ACRONYM_RE.findall(source.upper()))

    found: list[str] = []
    for token in _ACRONYM_RE.findall(text):
        if token not in allowed and token not in found:
            found.append(token)

    lowered = text.lower()
    for root, replacement in JARGON_WORDS.items():
        if root in lowered:
            found.append(f"{root}… (лучше: {replacement})")
    return found


# День недели для разговора и через сколько дней его предлагать.
#
# Пятница — не случайный выбор: письмо уходит в начале недели, и к пятнице
# у человека уже понятно, что за неделя. Три дня форы — чтобы предложенная
# дата не оказалась «завтра», на которое никто не соглашается.
CALL_WEEKDAY = 4          # 0 — понедельник, 4 — пятница
CALL_LEAD_DAYS = 3
WEEKDAY_NAMES = ["понедельник", "вторник", "среду", "четверг",
                 "пятницу", "субботу", "воскресенье"]

# Что модель обязана заполнить в шаблоне и что заполняет код.
MODEL_SLOTS = ("вопрос", "признаки")
CODE_SLOTS = ("день", "дата")

_SLOT_RE = re.compile(r"\{([а-яё_]+)\}")


def strip_comments(text: str) -> str:
    """Убирает пояснения для человека — они не должны попасть в письмо."""
    while "<!--" in text and "-->" in text:
        head, _, rest = text.partition("<!--")
        _, _, tail = rest.partition("-->")
        text = head + tail
    return text.strip()


def load_template(name: str = "template.md") -> str:
    """Шаблон письма без пояснений. Всё, что в нём, попадает в письмо дословно."""
    return strip_comments(load_prompt(name))


def next_call_date(today: date | None = None,
                   weekday: int = CALL_WEEKDAY,
                   lead_days: int = CALL_LEAD_DAYS) -> tuple[str, str]:
    """Ближайший подходящий день для разговора: («пятницу», «14.08.2026»).

    Считает код, а не модель. Дату модель называть не должна вовсе: она
    не знает сегодняшнего числа и однажды предложит встретиться в прошлом.
    """
    day = (today or date.today()) + timedelta(days=lead_days)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return WEEKDAY_NAMES[weekday], day.strftime("%d.%m.%Y")


def render_letter(template: str, slots: dict[str, str],
                  today: date | None = None) -> str:
    """Подставляет вставки в шаблон. Дату и день недели добавляет сама."""
    day_name, day_date = next_call_date(today)
    filled = {**slots, "день": day_name, "дата": day_date}

    def replace(match: re.Match[str]) -> str:
        return filled.get(match.group(1), match.group(0))

    return _SLOT_RE.sub(replace, template).strip()


def check_template(template: str, required: tuple[str, ...] = MODEL_SLOTS) -> list[str]:
    """Есть ли в шаблоне все места под вставки. Проверяется до похода к модели.

    Иначе правка шаблона в панели, где случайно стёрли `{признаки}`, дала бы
    пачку одинаковых писем без главного абзаца — и заметить это можно было бы
    только глазами.
    """
    found = set(_SLOT_RE.findall(template))
    missing = [f"{{{name}}}" for name in required if name not in found]
    unknown = [f"{{{name}}}" for name in sorted(found)
               if name not in required and name not in CODE_SLOTS]

    problems = []
    if missing:
        problems.append("в шаблоне нет мест под вставки: " + ", ".join(missing))
    if unknown:
        problems.append("в шаблоне лишние места, их некому заполнить: "
                        + ", ".join(unknown))
    return problems


def check_letter(text: str, company: str = "",
                 inserts: tuple[str, ...] = (), known: str = "") -> list[str]:
    """Механические проверки готового письма. Возвращает замечания человеку.

    Проверяется не вкус, а прямые запреты. Большая часть письма теперь
    фиксирована шаблоном, поэтому проверок стало меньше: следить нужно
    за вставками и за тем, что шаблон заполнился целиком.

    `inserts` — то, что написала модель. Если передано, сокращения ищутся
    только там. Фиксированный текст шаблона писал человек, он его прочитал,
    и названия вроде «РЕД ОС» или «MAX» в нём законны. Проверять шаблон
    каждое утро заново — значит приучить себя пропускать замечания.
    """
    notes: list[str] = []

    left = _SLOT_RE.findall(text)
    if left:
        notes.append("в письме осталось незаполненное место: "
                     + ", ".join(f"{{{name}}}" for name in left))

    # СОКРАЩЕНИЯ — главная проверка (DECISIONS.md, Р-048). Живёт в коде,
    # потому что просить об этом модель бесполезно: соглашается и забывает.
    jargon = find_jargon(" ".join(inserts) if inserts else text, company, known)
    if jargon:
        notes.append("сокращения и жаргон во вставках — распишите словами: "
                     + ", ".join(jargon[:6]))

    # Восклицательный знак: в шаблоне он ровно один, в приветствии.
    # Значит, любой лишний пришёл из вставки.
    checked = " ".join(inserts) if inserts else "\n\n".join(text.split("\n\n")[1:])
    if "!" in checked:
        notes.append("восклицательный знак во вставке — он разрешён "
                     "только в «Здравствуйте!»")

    words = len(text.split())
    if words > 260:
        notes.append(f"письмо длинное ({words} слов) — скорее всего, "
                     f"вставка признаков разрослась")

    longest = max((len(p.split()) for p in text.split("\n\n")), default=0)
    if longest > 90:
        notes.append(f"самый длинный абзац — {longest} слов, разбейте его")

    return notes


def clean_insert(value: str) -> str:
    """Приводит вставку в порядок перед подстановкой в шаблон.

    Модель нет-нет да и вернёт вставку с приветствием, кавычками по краям
    или переводом строки внутри. В шаблоне это ломает вёрстку письма,
    а править руками каждое утро никто не станет.
    """
    text = " ".join((value or "").split())
    text = text.strip('"«»')
    for prefix in ("Здравствуйте!", "Здравствуйте,", "Здравствуйте."):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text


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


def reset_drafts(dry_run: bool = False) -> dict[str, int]:
    """Стирает неотправленные письма, чтобы написать их заново по новым правилам.

    ЗАЧЕМ ОТДЕЛЬНЫЙ ЭТАП. Правка `letter.md` или `offer.md` не меняет письма,
    которые уже лежат в базе готовым текстом. `compose` их тоже не тронет —
    он берёт только тех, у кого письма ещё нет. Без этого этапа новые правила
    доходят только до компаний, найденных завтра.

    ЧТО НЕ ТРОГАЕТСЯ — отправленные письма. Их текст ушёл живому человеку
    и остаётся записью того, что он получил. Переписывать его — значит
    потерять то, на что он отвечает, и сломать дожимы, которые обязаны
    не повторять первое письмо.

    Черновики удаляются все, включая дожимы: черновик по определению
    не отправлен (отметка «отправлено» переводит его в статус sent).
    Иначе после повторного прогона в панели оказалось бы по два письма
    на компанию.

    Досье, вердикты по ICP и сами компании не трогаются вовсе, поэтому
    повторный прогон не стоит ни одного запроса к Checko — только к модели.
    """
    conn = db.connect()

    # Компании, письмо которым ещё не уходило. Статус 'new' и пустой статус —
    # одно и то же: разные ветки кода ставили то одно, то другое.
    rows = conn.execute(
        """
        SELECT inn, name FROM companies
        WHERE letter_written_at IS NOT NULL
          AND (outreach_status IS NULL OR outreach_status = 'new')
        """
    ).fetchall()

    kept = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE letter_written_at IS NOT NULL "
        "AND outreach_status IS NOT NULL AND outreach_status != 'new'"
    ).fetchone()[0]

    counters = {"reset": len(rows), "kept": int(kept), "drafts_removed": 0}

    if not rows and not kept:
        logger.info("Писем в базе нет — стирать нечего")
        conn.close()
        return counters

    drafts = conn.execute(
        "SELECT COUNT(*) FROM touches WHERE status = 'draft'").fetchone()[0]
    counters["drafts_removed"] = int(drafts)

    if not dry_run:
        conn.execute("DELETE FROM touches WHERE status = 'draft'")
        conn.execute(
            """
            UPDATE companies SET
                letter_subject = NULL, letter_body = NULL, letter_why = NULL,
                letter_facts = NULL, letter_status = NULL,
                letter_written_at = NULL, last_reported = NULL
            WHERE letter_written_at IS NOT NULL
              AND (outreach_status IS NULL OR outreach_status = 'new')
            """
        )
        conn.commit()
    conn.close()

    logger.info("Стёрто писем: %d, черновиков убрано: %d", len(rows), drafts)
    if kept:
        logger.info("Не тронуто отправленных писем: %d — их текст уже "
                    "у адресата", kept)
    if rows:
        logger.info("Дальше — этап «Написать письма»: он напишет их заново "
                    "по текущим правилам")
    return counters


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
    min_score = float(compose_cfg.get("min_score", 5.0))

    placeholders = ",".join("?" for _ in target_types)
    conn = db.connect()

    # ПОРОГ РЕЙТИНГА. Письмо стоит запроса к самой дорогой модели, и тратить
    # его на компанию, у которой не сошлось ни одного признака, незачем.
    # Балл считает scoring.py по тому, что уже в базе, — сам по себе он
    # бесплатный (DECISIONS.md, Р-054).
    skipped = conn.execute(
        f"""
        SELECT COUNT(*) FROM companies
        WHERE icp_status = 'passed' AND site_type IN ({placeholders})
          AND letter_body IS NULL AND letter_status IS NULL
          AND COALESCE(signal_score, 0) < ?
        """,
        (*target_types, min_score),
    ).fetchone()[0]

    rows = conn.execute(
        f"""
        SELECT * FROM companies
        WHERE icp_status = 'passed'
          AND site_type IN ({placeholders})
          AND letter_body IS NULL
          AND letter_status IS NULL
          AND COALESCE(signal_score, 0) >= ?
        ORDER BY COALESCE(signal_score, 0) DESC, name
        LIMIT ?
        """,
        (*target_types, min_score, per_run),
    ).fetchall()

    if skipped:
        logger.info("Пропущено по рейтингу ниже %.1f: %d компаний "
                    "(порог — compose.min_score)", min_score, skipped)

    if not rows:
        logger.info("Нет компаний, готовых к письму. Либо их ещё не собрали "
                    "(этапы «Найти компании» и «Обойти сайты»), либо ни одна "
                    "не набрала рейтинг %.1f", min_score)
        conn.close()
        return {"written": 0, "skipped_by_score": skipped}

    logger.info("Компаний к написанию письма: %d (модель %s, рейтинг от %.1f)",
                len(rows), llm.MODEL_WRITE, min_score)

    # Шаблон читается один раз на прогон и проверяется до похода к модели:
    # сломанный шаблон дал бы пачку одинаково испорченных писем.
    try:
        template = load_template()
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        conn.close()
        return {"written": 0}

    broken = check_template(template)
    if broken:
        for problem in broken:
            logger.error("Шаблон письма: %s", problem)
        logger.error("Письма не пишем: сначала почините шаблон "
                     "(«Настройки» → «Шаблон письма»)")
        conn.close()
        return {"written": 0}

    counters = {"written": 0, "failed": 0, "thin": 0, "skipped_by_score": skipped}
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

        dossier = build_dossier(row)
        try:
            result = llm.classify(system_prompt, dossier, LETTER_SCHEMA,
                                  max_tokens=ANSWER_TOKENS, model=llm.MODEL_WRITE)
        except llm.LLMUnavailable as exc:
            logger.error("%s", exc)
            break
        except llm.LLMError as exc:
            counters["failed"] += 1
            logger.warning("— %s: письмо не написалось (%s)", name, exc)
            continue

        subject = result.get("subject", "")
        body = render_letter(template, {
            "вопрос": clean_insert(result.get("question", "")),
            "признаки": clean_insert(result.get("signs", "")).rstrip("."),
        })
        notes = check_letter(body, name, known=dossier, inserts=(
            clean_insert(result.get("question", "")),
            clean_insert(result.get("signs", "")),
        ))
        if len(subject) > SUBJECT_MAX:
            notes.append(f"тема длинная ({len(subject)} знаков) — "
                         f"на телефоне обрежется, укоротите до {SUBJECT_MAX}")
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
        template = load_template("template_followup.md")
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        conn.close()
        return {"written": 0}

    broken = check_template(template, required=("мысль",))
    if broken:
        for problem in broken:
            logger.error("Шаблон дожима: %s", problem)
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

        followup_dossier = build_followup_dossier(row, previous)
        try:
            result = llm.classify(
                build_followup_prompt(row["site_type"] or "default"),
                followup_dossier,
                FOLLOWUP_SCHEMA, max_tokens=ANSWER_TOKENS, model=llm.MODEL_WRITE,
            )
        except llm.LLMUnavailable as exc:
            logger.error("%s", exc)
            break
        except llm.LLMError as exc:
            counters["failed"] += 1
            logger.warning("— %s: дожим не написался (%s)", name, exc)
            continue

        body = render_letter(template,
                             {"мысль": clean_insert(result.get("thought", ""))})
        notes = check_letter(body, name, known=followup_dossier,
                             inserts=(clean_insert(result.get("thought", "")),))
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
