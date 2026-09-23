#!/usr/bin/env python3
"""Проверка доступа текущего токена WB к источникам WB-листа. Только чтение, в БД не ходит.

По одному обращению на эндпоинт (задача WB-4, §2–§3): детализация отчёта реализации
за неделю, её горизонт (самые ранние строки одним запросом с limit), эквайринг за
неделю, список кампаний, история затрат на рекламу, статистика кампаний за день
(если кампании есть), лента заказов за день. Ответы — в файлы, журнал — рядом.
Повторов нет; finance-api — 1 запрос в минуту, между его вызовами пауза 65 с.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv(".env")
KEY = os.getenv("WB_API_KEY")
OUT = sys.argv[1] if len(sys.argv) > 1 else "logs/wb_api_probe_20260922"
os.makedirs(OUT, exist_ok=True)
H = {"Authorization": KEY, "Content-Type": "application/json"}
ledger = []


def call(name, method, url, *, params=None, body=None):
    started = datetime.now(timezone.utc)
    r = requests.request(method, url, headers=H, params=params, json=body, timeout=300)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    with open(os.path.join(OUT, f"{name}.json"), "wb") as h:
        h.write(r.content)
    try:
        data = r.json() if r.content else None
    except ValueError:
        data = None
    rows = len(data) if isinstance(data, list) else (len((data or {}).get("data", {}).get("orders", [])) if isinstance(data, dict) and isinstance(data.get("data"), dict) else None)
    entry = {"name": name, "at_utc": started.isoformat(), "method": method, "url": url, "params": params or body,
             "status": r.status_code, "bytes": len(r.content), "seconds": round(elapsed, 1), "rows": rows,
             "error": (data if r.status_code >= 400 and isinstance(data, dict) else None)}
    ledger.append(entry)
    with open(os.path.join(OUT, "calls.json"), "w", encoding="utf-8") as h:
        json.dump(ledger, h, ensure_ascii=False, indent=2)
    print(f"{entry['at_utc'][:19]} {name}: HTTP {r.status_code}, {len(r.content)} байт, строк {rows}, {elapsed:.1f} с"
          + (f" | {json.dumps(entry['error'], ensure_ascii=False)[:300]}" if entry["error"] else ""), flush=True)
    return r.status_code, data


FIN = "https://finance-api.wildberries.ru"
ADV = "https://advert-api.wildberries.ru"
ANA = "https://seller-analytics-api.wildberries.ru"

# 1. детализация отчёта реализации, неделя сентября
call("fin_detailed_2026-09-01_07", "POST", f"{FIN}/api/finance/v1/sales-reports/detailed",
     body={"dateFrom": "2026-09-01", "dateTo": "2026-09-07", "limit": 100000, "rrdId": 0})
time.sleep(65)
# 2. горизонт: самые ранние строки от документированного начала
call("fin_detailed_horizon_from_2024-01-29", "POST", f"{FIN}/api/finance/v1/sales-reports/detailed",
     body={"dateFrom": "2024-01-29", "dateTo": "2026-03-31", "limit": 1000, "rrdId": 0})
time.sleep(65)
# 3. эквайринг за ту же неделю
call("fin_acquiring_2026-09-01_07", "POST", f"{FIN}/api/finance/v1/acquiring/detailed",
     body={"dateFrom": "2026-09-01", "dateTo": "2026-09-07", "limit": 100000, "rrdId": 0})
# 4. кампании
status, adverts = call("adv_promotion_count", "GET", f"{ADV}/adv/v1/promotion/count")
ids = []
if status == 200 and isinstance(adverts, dict):
    for group in adverts.get("adverts") or []:
        if group.get("status") in (7, 9, 11):
            ids += [a["advertId"] for a in group.get("advert_list") or []]
print("кампаний в статусах 7/9/11:", len(ids), flush=True)
time.sleep(1)
# 5. история затрат за сентябрь
call("adv_upd_2026-09-01_21", "GET", f"{ADV}/adv/v1/upd", params={"from": "2026-09-01", "to": "2026-09-21"})
# 6. статистика кампаний за один день — только если есть кампании
if ids:
    time.sleep(21)
    call("adv_fullstats_2026-09-21", "GET", f"{ADV}/adv/v3/fullstats",
         params={"ids": ",".join(str(i) for i in ids[:50]), "beginDate": "2026-09-21", "endDate": "2026-09-21"})
else:
    print("fullstats пропущен: кампаний нет", flush=True)
# 7. лента заказов за один день сентября (по времени текущего статуса)
call("ana_order_feed_2026-09-15", "POST", f"{ANA}/api/analytics/v1/order-feed",
     body={"selectedPeriod": {"start": "2026-09-15T00:00:00+03:00", "end": "2026-09-16T00:00:00+03:00"},
           "pagination": {"offset": 0, "limit": 1000}})
print(f"Обращений: {len(ledger)}", flush=True)
