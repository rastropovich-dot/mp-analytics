#!/usr/bin/env python3
"""Перепроверка измерений WB от 2026-09-03. Только чтение, в БД не ходит вовсе.

Снимает сырые ответы WB в файлы и ведёт журнал обращений. Разбор — отдельно
(scripts/wb_remeasure_analyze.py), чтобы его можно было повторять без единого
обращения к WB.

Повторов нет намеренно: каждое обращение считается, а на первом же 429 серия
останавливается — лимит statistics-api ~1 запрос в минуту, и повтор в том же
окне только сдвинул бы следующий отказ.

Запуск:
    python3 scripts/wb_remeasure_probe.py --out logs/wb_probe_20260921
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
OLD_STOCKS_URL = "https://statistics-api.wildberries.ru/api/v1/supplier/stocks"
NEW_STOCKS_URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses"

STATISTICS_SLEEP_SECONDS = 65


def build_calls(today, inside_dates, depth_date, eroded_dates):
    window_start = (today - timedelta(days=30)).isoformat()
    calls = [{
        "name": f"orders_flag0_from_{window_start}",
        "method": "GET", "url": ORDERS_URL,
        "params": {"dateFrom": window_start, "flag": 0},
    }]
    for day in inside_dates + [depth_date] + eroded_dates:
        calls.append({
            "name": f"orders_flag1_{day}",
            "method": "GET", "url": ORDERS_URL,
            "params": {"dateFrom": day, "flag": 1},
        })
    calls.append({
        "name": "stocks_old_get",
        "method": "GET", "url": OLD_STOCKS_URL,
        "params": {"dateFrom": (today - timedelta(days=1)).isoformat()},
    })
    calls.append({
        "name": "stocks_new_post",
        "method": "POST", "url": NEW_STOCKS_URL,
        "json": {"limit": 1000, "offset": 0},
    })
    return calls


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--inside", nargs="+", default=["2026-09-05", "2026-08-24"])
    parser.add_argument("--depth", default="2026-03-01")
    parser.add_argument("--eroded", nargs="+",
                        default=["2026-07-21", "2026-06-05", "2026-05-22", "2026-04-02"])
    parser.add_argument("--sleep-seconds", type=int, default=STATISTICS_SLEEP_SECONDS)
    args = parser.parse_args(argv)

    if not WB_API_KEY:
        print("WB_API_KEY не задан")
        return 2

    os.makedirs(args.out, exist_ok=True)
    ledger_path = os.path.join(args.out, "calls.json")
    ledger = []

    calls = build_calls(date.today(), args.inside, args.depth, args.eroded)
    previous_host = None

    for call in calls:
        host = call["url"].split("/")[2]
        if previous_host == host:
            time.sleep(args.sleep_seconds)
        previous_host = host

        started = datetime.now(timezone.utc)
        headers = {"Authorization": WB_API_KEY}
        if call["method"] == "GET":
            response = requests.get(call["url"], headers=headers, params=call["params"], timeout=180)
        else:
            response = requests.post(call["url"], headers=headers, json=call["json"], timeout=180)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()

        body_path = os.path.join(args.out, call["name"] + ".json")
        with open(body_path, "wb") as handle:
            handle.write(response.content)

        entry = {
            "name": call["name"],
            "at_utc": started.isoformat(),
            "method": call["method"],
            "url": call["url"],
            "params": call.get("params") or call.get("json"),
            "status": response.status_code,
            "bytes": len(response.content),
            "seconds": round(elapsed, 1),
            "retry_after": response.headers.get("Retry-After"),
            "ratelimit_remaining": response.headers.get("X-Ratelimit-Remaining"),
        }
        ledger.append(entry)
        with open(ledger_path, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle, ensure_ascii=False, indent=2)

        print(f"{entry['at_utc']} {call['name']}: HTTP {entry['status']}, "
              f"{entry['bytes']} байт, {entry['seconds']} с", flush=True)

        if response.status_code == 429:
            print("429 — серия остановлена, повторов нет.", flush=True)
            return 1

    print(f"Обращений: {len(ledger)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
