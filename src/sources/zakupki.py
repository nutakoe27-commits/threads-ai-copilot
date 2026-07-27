"""Госзакупки: контракты по 44-ФЗ из официальной выгрузки ЕИС.

ПОЧЕМУ ИМЕННО ЭТОТ ИСТОЧНИК. Всё, чем мы располагали до сих пор, —
состояния, а не события: выручка за позапрошлый год, описание с сайта,
вид деятельности по реестру. Из состояния не следует «почему сегодня».
Контракт же имеет дату начала и дату окончания, а тендер — дату итогов
и проигравших. Это события с точностью до дня.

Что из этого получается для нашего ICP:

  * компания участвовала в тендере и проиграла — выручка нужна сейчас;
  * у компании заканчивается контракт через N месяцев — дыра в выручке
    видна заранее, и письмо приходит до того, как она случилась;
  * компания выиграла первый крупный контракт — растёт, нужен поток.

ЗАКОННОСТЬ. Публикация этих данных прямо предписана 44-ФЗ, а выгрузка
существует ровно для машинного чтения: FTP `ftp.zakupki.gov.ru`, логин
и пароль `free`. Это не обход чужих условий, а использование площадки
по назначению — принципиально иная ситуация, чем с hh.ru (Р-016).

ОГРАНИЧЕНИЕ. По 223-ФЗ сведения о поставщиках ЕИС не публикует с 2019
года, поэтому рабочая часть — только 44-ФЗ.

ПРО ОБЪЁМ. Архивы большие: регион за месяц — это десятки мегабайт.
Поэтому здесь не «скачать всё», а «скачать один регион за один период
и посмотреть, есть ли там наши ИНН». Сначала замер покрытия, и только
если покрытие есть — постоянный сбор. Тот же порядок, который уже
уберёг нас от постройки системы поверх trudvsem (Р-014).
"""

from __future__ import annotations

import io
import re
import zipfile
from ftplib import FTP, error_perm
from typing import Any, Iterator

from .. import log

logger = log.get("zakupki")

FTP_HOST = "ftp.zakupki.gov.ru"
FTP_USER = "free"
FTP_PASSWORD = "free"

# Каталог с контрактами по 44-ФЗ. Структура ЕИС менялась не раз, поэтому
# каталог ищется перебором известных вариантов, а не задаётся жёстко.
CONTRACT_DIRS = [
    "/fcs_regions/{region}/contracts/currMonth",
    "/fcs_regions/{region}/contracts/prevMonth",
    "/fcs_regions/{region}/contracts",
]

# ИНН поставщика и даты в XML контракта. Полноценный разбор XML здесь
# избыточен: файлы огромные, а для замера покрытия нужно ровно два поля.
_INN_RE = re.compile(r"<inn>(\d{10,12})</inn>", re.IGNORECASE)
_END_DATE_RE = re.compile(r"<endDate>([\d-]{10})</endDate>", re.IGNORECASE)
_PRICE_RE = re.compile(r"<price>([\d.]+)</price>", re.IGNORECASE)


class ZakupkiError(RuntimeError):
    pass


def connect(timeout: int = 60) -> FTP:
    """Открывает анонимное соединение с выгрузкой ЕИС."""
    try:
        ftp = FTP(FTP_HOST, timeout=timeout)
        ftp.login(FTP_USER, FTP_PASSWORD)
        ftp.set_pasv(True)
        return ftp
    except OSError as exc:
        raise ZakupkiError(
            f"не удалось подключиться к {FTP_HOST}: {exc}. "
            "Если сеть закрыта, проверьте, что исходящий FTP разрешён"
        ) from exc


def find_directory(ftp: FTP, region: str) -> str:
    """Первый существующий каталог с контрактами региона."""
    for template in CONTRACT_DIRS:
        path = template.format(region=region)
        try:
            ftp.cwd(path)
            return path
        except error_perm:
            continue
    raise ZakupkiError(
        f"не нашёл каталог контрактов для региона «{region}». "
        f"Проверял: {', '.join(t.format(region=region) for t in CONTRACT_DIRS)}"
    )


def list_archives(ftp: FTP, path: str, limit: int) -> list[str]:
    """Имена zip-архивов в каталоге, свежие первыми."""
    ftp.cwd(path)
    names = [name for name in ftp.nlst() if name.lower().endswith(".zip")]
    names.sort(reverse=True)
    return names[:limit]


def download(ftp: FTP, name: str) -> bytes:
    buffer = io.BytesIO()
    ftp.retrbinary(f"RETR {name}", buffer.write)
    return buffer.getvalue()


def iter_contracts(archive: bytes) -> Iterator[dict[str, Any]]:
    """Достаёт из архива минимум, нужный для замера: ИНН, дата, сумма.

    Каждый XML внутри архива — один контракт. Разбираем регулярками, а не
    парсером: файлы большие, схема ЕИС огромная, а нам нужны три поля.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            for info in zf.infolist():
                if not info.filename.lower().endswith(".xml"):
                    continue
                try:
                    text = zf.read(info).decode("utf-8", errors="replace")
                except (OSError, RuntimeError) as exc:
                    logger.debug("%s не читается: %s", info.filename, exc)
                    continue

                inns = _INN_RE.findall(text)
                if not inns:
                    continue
                end_dates = _END_DATE_RE.findall(text)
                prices = _PRICE_RE.findall(text)
                yield {
                    "file": info.filename,
                    "inns": set(inns),
                    "end_date": end_dates[-1] if end_dates else None,
                    "price": float(prices[0]) if prices else None,
                }
    except zipfile.BadZipFile as exc:
        raise ZakupkiError(f"архив повреждён: {exc}") from exc


def measure_coverage(archives: list[bytes], wanted: set[str]) -> dict[str, Any]:
    """Сколько наших компаний встретилось в скачанных архивах.

    Возвращает не только совпадения, но и общий объём просмотренного:
    «ноль совпадений в трёх контрактах» и «ноль в сорока тысячах» —
    выводы разной силы, и путать их нельзя.
    """
    found: dict[str, list[dict[str, Any]]] = {}
    contracts = 0
    all_inns: set[str] = set()

    for archive in archives:
        for contract in iter_contracts(archive):
            contracts += 1
            all_inns |= contract["inns"]
            for inn in contract["inns"] & wanted:
                found.setdefault(inn, []).append({
                    "end_date": contract["end_date"],
                    "price": contract["price"],
                    "file": contract["file"],
                })

    return {
        "contracts": contracts,
        "distinct_suppliers": len(all_inns),
        "matched": found,
        "coverage": len(found) / len(wanted) if wanted else 0.0,
    }
