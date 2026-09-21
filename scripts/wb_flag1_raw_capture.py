#!/usr/bin/env python3
"""Снять сырые ответы flag=1 по датам в файлы. Только чтение, в БД не ходит.

Зачем отдельно от восстановления. У flag=1 есть горизонт — 190 дней на
2026-09-21 (2026-03-14 пусто, 2026-03-15 отдаёт 218 строк), и он едет вперёд на
день в сутки. Запись в marketplace_orders ждёт слова владельца и мержа окна
записи, а горизонт не ждёт. Сырьё на диске развязывает одно с другим: снятый
день восстановим когда угодно и без единого обращения к WB.

Повторов нет, на первом не-200 — стоп. Уже снятые даты пропускаются.

Запуск:
    python3 scripts/wb_flag1_raw_capture.py --out logs/wb_flag1_raw --dates 2026-03-16 2026-03-18
    python3 scripts/wb_flag1_raw_capture.py --out logs/wb_flag1_raw --date-from 2026-03-15 --date-to 2026-04-30
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

WB_API_KEY = os.getenv("WB_API_KEY")
ORDERS_URL = "https://statistics-api.wildberries.ru/api/v1/supplier/orders"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--dates", nargs="*", default=[])
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--sleep-seconds", type=int, default=65)
    args = parser.parse_args(argv)

    days = list(args.dates)
    if args.date_from and args.date_to:
        day = date.fromisoformat(args.date_from)
        while day <= date.fromisoformat(args.date_to):
            days.append(day.isoformat())
            day += timedelta(days=1)
    days = sorted(set(days))

    os.makedirs(args.out, exist_ok=True)
    ledger_path = os.path.join(args.out, "capture_calls.json")
    ledger = json.load(open(ledger_path, encoding="utf-8")) if os.path.exists(ledger_path) else []
    calls = 0

    for day in days:
        path = os.path.join(args.out, f"orders_flag1_{day}.json")
        if os.path.exists(path):
            print(f"{day}: уже снято, пропуск", flush=True)
            continue
        if calls:
            time.sleep(args.sleep_seconds)

        started = datetime.now(timezone.utc)
        response = requests.get(ORDERS_URL, headers={"Authorization": WB_API_KEY},
                                params={"dateFrom": day, "flag": 1}, timeout=180)
        calls += 1

        rows = foreign = None
        if response.status_code == 200:
            items = response.json()
            rows = len(items)
            foreign = sorted({str(x.get("date"))[:10] for x in items} - {day})
            # Пустой или чужой ответ в сырьё не кладём: файл с таким именем читался
            # бы потом как «день снят», а это не так.
            if rows and not foreign:
                with open(path, "wb") as handle:
                    handle.write(response.content)

        ledger.append({"name": f"orders_flag1_{day}", "at_utc": started.isoformat(), "status": response.status_code,
                       "bytes": len(response.content), "rows": rows, "foreign_dates": foreign})
        with open(ledger_path, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle, ensure_ascii=False, indent=2)
        print(f"{started.isoformat()} {day}: HTTP {response.status_code}, строк {rows}, чужих дат {foreign}", flush=True)

        if response.status_code != 200:
            print("Не 200 — серия остановлена, повторов нет.", flush=True)
            return 1

    print(f"Обращений: {calls}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
