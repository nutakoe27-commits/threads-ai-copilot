Сюда кладутся выгрузки реестров Минцифры.

  software*.csv    — реестр отечественного ПО
  accredited*.csv  — реестр аккредитованных IT-компаний

Импорт:  python3 run.py --stage registries
Подробности:  python3 -c "from src.sources import registries; print(registries.instructions())"
