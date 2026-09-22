#!/usr/bin/env python3
"""Где кончается глубина flag=1: двоичный поиск между пустой и непустой датой.

Только чтение, в БД не ходит. Повторов нет, на первом 429 — стоп. Каждое
обращение пишется в журнал, сырой ответ — в файл.

Зачем. 2026-09-03 дата 2026-03-01 (186 дней) отдавала 362 строки; 2026-09-21
она же (204 дня) отдаёт пустой список, а 2026-04-02 (172 дня) — полный день.
Значит, у flag=1 есть горизонт, и он едет вперёд: всё, что за ним, уже не
восстановить. Поиск называет первую дату, которая ещё отдаётся.

Допущение поиска — монотонность: глубже горизонта пусто, ближе — данные.
Пустой ответ на дате, где заказов не было вовсе, его нарушил бы, поэтому ищем
на отрезке, где в базе заказы есть на каждой дате (март 2026).

Запуск:
    python3 scripts/wb_flag1_depth_probe.py --out logs/wb_probe_20260921 \
        --empty 2026-03-01 --full 2026-04-02
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
    parser.add_argument("--empty", required=True, help="Дата, которая уже отдаёт пусто.")
    parser.add_argument("--full", required=True, help="Дата, которая ещё отдаёт данные.")
    parser.add_argument("--sleep-seconds", type=int, default=65)
    parser.add_argument("--max-calls", type=int, default=6)
    args = parser.parse_args(argv)

    low = date.fromisoformat(args.empty)   # пусто
    high = date.fromisoformat(args.full)   # данные
    ledger_path = os.path.join(args.out, "depth_calls.json")
    ledger = []

    while (high - low).days > 1 and len(ledger) < args.max_calls:
        if ledger:
            time.sleep(args.sleep_seconds)

        day = (low + timedelta(days=(high - low).days // 2)).isoformat()
        started = datetime.now(timezone.utc)
        response = requests.get(ORDERS_URL, headers={"Authorization": WB_API_KEY},
                                params={"dateFrom": day, "flag": 1}, timeout=180)

        with open(os.path.join(args.out, f"orders_flag1_{day}.json"), "wb") as handle:
            handle.write(response.content)

        rows = None
        if response.status_code == 200:
            rows = len(response.json())

        ledger.append({"name": f"orders_flag1_{day}", "at_utc": started.isoformat(), "method": "GET",
                       "url": ORDERS_URL, "params": {"dateFrom": day, "flag": 1},
                       "status": response.status_code, "bytes": len(response.content), "rows": rows})
        with open(ledger_path, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle, ensure_ascii=False, indent=2)

        print(f"{started.isoformat()} {day}: HTTP {response.status_code}, строк {rows}", flush=True)

        if response.status_code != 200:
            print("Не 200 — поиск остановлен, повторов нет.", flush=True)
            return 1

        if rows:
            high = date.fromisoformat(day)
        else:
            low = date.fromisoformat(day)

    today = date.today()
    print(f"Последняя пустая: {low} ({(today - low).days} дн.), первая с данными: {high} "
          f"({(today - high).days} дн.). Обращений: {len(ledger)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
