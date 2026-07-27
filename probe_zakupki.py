#!/usr/bin/env python3
"""Замер: сколько компаний нашего списка вообще есть в госзакупках.

Прежде чем строить сбор, надо узнать покрытие. Ровно этот порядок уберёг
нас от системы поверх trudvsem, где 14 131 вакансия дала ноль компаний
нашего профиля (DECISIONS.md, Р-014).

Что делает: скачивает несколько свежих архивов контрактов по 44-ФЗ для
одного региона и смотрит, встречаются ли там ИНН компаний, прошедших ICP.

Ничего не пишет в базу. К Checko и к моделям не обращается.

    python3 probe_zakupki.py                     # Москва, 3 архива
    python3 probe_zakupki.py --region Moskva --archives 5
"""

from __future__ import annotations

import argparse
import sys

from src import db, log
from src.sources import zakupki


def main() -> int:
    parser = argparse.ArgumentParser(description="Замер покрытия по госзакупкам")
    parser.add_argument("--region", default="Moskva",
                        help="каталог региона в выгрузке ЕИС (по умолчанию Moskva)")
    parser.add_argument("--archives", type=int, default=3,
                        help="сколько свежих архивов скачать")
    args = parser.parse_args()

    logger = log.setup()

    conn = db.connect()
    rows = conn.execute(
        "SELECT inn, name FROM companies WHERE icp_status = 'passed'").fetchall()
    conn.close()

    if not rows:
        logger.error("В базе нет компаний, прошедших ICP. "
                     "Сначала: python3 run.py --stage targets")
        return 1

    wanted = {row["inn"]: row["name"] for row in rows}
    logger.info("Проверяем %d компаний, прошедших ICP", len(wanted))

    try:
        ftp = zakupki.connect()
    except zakupki.ZakupkiError as exc:
        logger.error("%s", exc)
        return 1

    try:
        path = zakupki.find_directory(ftp, args.region)
        logger.info("Каталог: %s", path)

        names = zakupki.list_archives(ftp, path, args.archives)
        if not names:
            logger.error("В каталоге нет ни одного архива — проверьте регион")
            return 1
        logger.info("Скачиваем архивов: %d (%s)", len(names), ", ".join(names))

        archives = []
        for name in names:
            data = zakupki.download(ftp, name)
            logger.info("  %s — %.1f МБ", name, len(data) / 1e6)
            archives.append(data)
    except zakupki.ZakupkiError as exc:
        logger.error("%s", exc)
        return 1
    finally:
        try:
            ftp.quit()
        except OSError:
            pass

    result = zakupki.measure_coverage(archives, set(wanted))

    print("\n" + "=" * 72)
    print("ЗАМЕР ПОКРЫТИЯ ПО ГОСЗАКУПКАМ")
    print("=" * 72)
    print(f"Просмотрено контрактов:      {result['contracts']}")
    print(f"Разных поставщиков в них:    {result['distinct_suppliers']}")
    print(f"Наших компаний найдено:      {len(result['matched'])} из {len(wanted)}")
    print(f"Покрытие:                    {result['coverage']:.0%}")

    if result["matched"]:
        print("\nКто нашёлся:")
        for inn, contracts in result["matched"].items():
            print(f"\n  {wanted[inn]} (ИНН {inn}) — контрактов {len(contracts)}")
            for contract in contracts[:5]:
                price = (f"{contract['price'] / 1e6:.1f} млн ₽"
                         if contract["price"] else "сумма не разобралась")
                end = contract["end_date"] or "дата окончания не разобралась"
                print(f"      до {end}, {price}")

    print("\n" + "-" * 72)
    if result["coverage"] >= 0.3:
        print("ВЫВОД: покрытие достаточное. Источник стоит строить —")
        print("окончание контракта даёт письму настоящий повод написать сегодня.")
    elif result["contracts"] < 500:
        print("ВЫВОД: сделать нельзя. Просмотрено слишком мало контрактов,")
        print("чтобы судить о покрытии. Возьмите больше архивов: --archives 10")
    else:
        print("ВЫВОД: покрытие низкое. Компании нашего профиля в госзакупках")
        print("почти не участвуют — строить сбор поверх этого не стоит,")
        print("как не стоило поверх trudvsem.")
    print("-" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
