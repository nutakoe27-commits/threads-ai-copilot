"""Классификация почтовых адресов компании.

Две задачи, и обе важные.

**Первая — не хранить персональные данные.** Адрес вида
`tatiana.bakurskaya@example.ru` — это персональные данные конкретного
человека со всеми последствиями 152-ФЗ. В схеме базы над полем
`contact_email` с самого начала стоял комментарий «обезличенный
корпоративный адрес, НЕ личный email», но кода, который бы это
обеспечивал, не было: адрес брался из Checko как есть (DECISIONS.md, Р-026).

**Вторая — понимать, куда попадёт письмо.** `tender@` читает тендерный
отдел, `hr@` — кадровик, `host965@mail.ru` вообще неизвестно кто. Письмо
про клиентов компании ни одному из них не адресовано. Отбрасывать такие
адреса нельзя — часто это единственный контакт, — но человек должен видеть
тип адреса до отправки, а не после.
"""

from __future__ import annotations

import re

# Роль, а не человек: такие адреса безличны и хранить их можно.
ROLE_LOCALS = {
    "info", "mail", "office", "sales", "sale", "zakaz", "order", "orders",
    "contact", "contacts", "hello", "hi", "support", "help", "helpdesk",
    "admin", "reception", "secretary", "post", "inbox", "mailbox", "box",
    "company", "corp", "main", "general", "client", "clients", "service",
    "welcome", "request", "requests", "partner", "partners", "director",
    "manager", "shop", "market", "pr", "press", "media", "web", "site",
}

# Тоже безличные, но письмо попадёт не туда, кому оно написано.
WRONG_DESK_LOCALS = {
    "tender": "тендерный отдел",
    "tenders": "тендерный отдел",
    "zakupki": "отдел закупок",
    "hr": "отдел кадров",
    "job": "отдел кадров",
    "jobs": "отдел кадров",
    "rabota": "отдел кадров",
    "vacancy": "отдел кадров",
    "buh": "бухгалтерия",
    "buhgalteria": "бухгалтерия",
    "accounting": "бухгалтерия",
    "finance": "финансовый отдел",
    "legal": "юридический отдел",
    "security": "служба безопасности",
    "abuse": "техническая служба",
    "noreply": "адрес для автоответов",
    "no-reply": "адрес для автоответов",
}

# Бесплатная почта у юрлица — признак того, что адрес заведён кем-то лично
# и читает его неизвестно кто.
FREE_DOMAINS = {
    "mail.ru", "bk.ru", "list.ru", "inbox.ru", "internet.ru",
    "yandex.ru", "ya.ru", "yandex.com", "gmail.com", "googlemail.com",
    "rambler.ru", "lenta.ru", "autorambler.ru", "ro.ru",
    "outlook.com", "hotmail.com", "live.com", "icloud.com", "me.com",
}

# Распространённые имена в латинской транслитерации. Список заведомо
# неполный — он ловит частые случаи, а редкие ловит проверка на разделитель.
FIRST_NAMES = {
    "aleksandr", "alexander", "sasha", "aleksey", "alexey", "alexei", "lesha",
    "anatoly", "andrey", "andrei", "anton", "arkady", "artem", "artyom",
    "boris", "vadim", "valentin", "valery", "vasily", "victor", "viktor",
    "vitaly", "vladimir", "vladislav", "vyacheslav", "gennady", "georgy",
    "grigory", "denis", "dmitry", "dmitriy", "dima", "evgeny", "evgeniy",
    "egor", "ivan", "igor", "ilya", "kirill", "konstantin", "leonid",
    "maxim", "maksim", "mikhail", "misha", "nikita", "nikolay", "nikolai",
    "oleg", "pavel", "petr", "pyotr", "roman", "ruslan", "sergey", "sergei",
    "stanislav", "stepan", "timur", "fedor", "fyodor", "eduard", "yury",
    "yuri", "yaroslav",
    "alena", "alina", "alla", "anastasia", "anna", "antonina", "valeria",
    "vera", "veronika", "victoria", "viktoria", "galina", "daria", "darya",
    "diana", "ekaterina", "katerina", "katya", "elena", "elizaveta",
    "zhanna", "inna", "irina", "karina", "kristina", "ksenia", "kseniya",
    "larisa", "lidia", "lyubov", "lyudmila", "marina", "maria", "mariya",
    "nadezhda", "natalia", "natalya", "nina", "oksana", "olga", "polina",
    "raisa", "svetlana", "sofia", "tamara", "tatiana", "tatyana", "ulyana",
    "yulia", "julia", "yana",
}

_SPLIT_RE = re.compile(r"[._\-]+")
_DIGITS_RE = re.compile(r"\d+")


def classify(email: str) -> tuple[str, str]:
    """Возвращает (вид, пояснение для человека).

    Виды:
      role        — безличный адрес компании, писать можно;
      wrong_desk  — безличный, но читает не тот отдел;
      free        — бесплатная почта, читатель неизвестен;
      personal    — адрес конкретного человека, хранить нельзя;
      unknown     — распознать не удалось.
    """
    value = (email or "").strip().lower()
    if "@" not in value:
        return "unknown", ""

    local, _, domain = value.partition("@")
    bare = _DIGITS_RE.sub("", local)
    parts = [p for p in _SPLIT_RE.split(bare) if p]

    # Личный адрес определяем первым: он важнее всех остальных признаков,
    # потому что дело не в удобстве, а в том, что такие данные не хранятся.
    if any(part in FIRST_NAMES for part in parts):
        return "personal", "адрес конкретного человека"
    # ivanov.ii, a.petrov, s_smirnov — почти всегда человек. Инициал в одну
    # букву считаем частью имени: «a.petrov» ничем не отличается от
    # «alexey.petrov», кроме длины.
    if (len(parts) >= 2 and all(p.isalpha() for p in parts)
            and any(len(p) >= 3 for p in parts)
            and not any(p in ROLE_LOCALS or p in WRONG_DESK_LOCALS for p in parts)):
        return "personal", "похоже на имя и фамилию"

    if bare in WRONG_DESK_LOCALS:
        return "wrong_desk", WRONG_DESK_LOCALS[bare]
    if domain in FREE_DOMAINS:
        return "free", "бесплатная почта, читатель неизвестен"
    if bare in ROLE_LOCALS:
        return "role", ""
    return "unknown", "адрес не распознан, проверьте глазами"


def is_storable(email: str) -> bool:
    """Можно ли сохранять адрес в базу. Личные адреса — нельзя."""
    kind, _ = classify(email)
    return kind != "personal"


def safe(email: str) -> str:
    """Адрес, если его можно хранить, иначе пустая строка."""
    return email if email and is_storable(email) else ""


def label(email: str) -> str:
    """Пометка для утреннего списка: куда именно попадёт письмо."""
    kind, note = classify(email)
    if kind in ("role", "unknown") and not note:
        return ""
    return note
