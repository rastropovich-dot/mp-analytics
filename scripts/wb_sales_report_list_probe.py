#!/usr/bin/env python3
"""Список отчётов реализации WB (finance-api /api/finance/v1/sales-reports/list) за глубину метода — только чтение.

    venv/bin/python3 scripts/wb_sales_report_list_probe.py --period weekly [--date-from 2024-01-29] [--date-to <сегодня>]

Одно обращение на период (лимит метода 1/мин; limit 1000 — весь список с 2024-01-29 умещается в один ответ, иначе — offset
вторым обращением). Сырьё — data/wb_sales_report_raw/list_<period>_<от>_<до>.json (файл есть — в API не идёт).
Печатает таблицу: reportId, период, создан, тип (1 — основной, 2 — по выкупам), продажа, к перечислению, корректировка ВВ
(additionalPaymentSum), удержания, штрафы; ищет REPORT_OF_INTEREST по reportId и AMOUNT_OF_INTEREST по модулю в суммах.
db_writes = 0.
"""
import argparse
import json
import os
import sys
import time
from datetime import date
from decimal import Decimal

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(".env")
import cabinet  # noqa: E402
_PROFILE = cabinet.profile()   # каталоги данных и логов кабинета (MP_CABINET); guard — у загрузчика / точки входа, здесь только профиль
URL = "https://finance-api.wildberries.ru/api/finance/v1/sales-reports/list"
RAW_DIR = cabinet.data_path("wb_sales_report_raw", prof=_PROFILE)
REPORT_OF_INTEREST = 63466988
AMOUNT_OF_INTEREST = Decimal("7119571.15")
SUM_FIELDS = ("retailAmountSum", "forPaySum", "deliveryServiceSum", "paidStorageSum", "paidAcceptanceSum", "deductionSum",
              "penaltySum", "additionalPaymentSum", "cashbackAmountSum", "cashbackDiscountSum", "cashbackCommissionChangeSum", "bankPaymentSum")


def D(v):
    return Decimal(str(v)) if v not in (None, "") else Decimal(0)


def fetch(period, date_from, date_to):
    path = os.path.join(RAW_DIR, f"list_{period}_{date_from}_{date_to}.json")
    if os.path.exists(path):
        print(f"{path}: файл есть — в API не идём")
        return json.load(open(path, encoding="utf-8")), 0
    items, offset, calls = [], 0, 0
    while True:
        body = {"dateFrom": date_from, "dateTo": date_to, "limit": 1000, "offset": offset, "period": period}
        if calls:
            time.sleep(65)
        for attempt in range(1, 4):   # 429 — лимит 1/мин на метод: пауза 65 с, не более 3 попыток
            resp = requests.post(URL, headers={"Authorization": os.environ["WB_API_KEY"], "Content-Type": "application/json"}, json=body, timeout=120)
            calls += 1
            print(f"list {period} {date_from}…{date_to} offset {offset}: HTTP {resp.status_code}" + (f" (попытка {attempt})" if attempt > 1 else ""))
            if resp.status_code != 429:
                break
            time.sleep(65)
        if resp.status_code == 204:
            break
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        page = json.loads(resp.content.decode("utf-8"), parse_float=Decimal)
        items.extend(page)
        if len(page) < 1000:
            break
        offset += 1000
    os.makedirs(RAW_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, default=str)
    return items, calls


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--period", default="weekly", choices=["weekly", "daily"])
    ap.add_argument("--date-from", default="2024-01-29")
    ap.add_argument("--date-to", default=date.today().isoformat())
    args = ap.parse_args(argv)
    items, calls = fetch(args.period, args.date_from, args.date_to)
    print(f"отчётов {len(items)}, обращений {calls}")
    fields = [f for f in SUM_FIELDS if any(D(r.get(f)) for r in items)]
    print("reportId | период | создан | тип | " + " | ".join(fields))
    for r in sorted(items, key=lambda x: (str(x.get("dateFrom")), int(x["reportId"]))):
        print(f"{r['reportId']} | {r.get('dateFrom')} … {r.get('dateTo')} | {r.get('createDate')} | {r.get('reportType')} | "
              + " | ".join(f"{D(r.get(f)):,.2f}".replace(",", " ") for f in fields))
    hit = [r for r in items if int(r["reportId"]) == REPORT_OF_INTEREST]
    print(f"\nreportId {REPORT_OF_INTEREST}: {'есть — ' + json.dumps(hit[0], ensure_ascii=False, default=str) if hit else 'в списке нет'}")
    amt = [(r["reportId"], r.get("dateFrom"), r.get("dateTo"), f, r.get(f)) for r in items for f in SUM_FIELDS if abs(D(r.get(f))) == AMOUNT_OF_INTEREST]
    print(f"сумма {AMOUNT_OF_INTEREST} по модулю в суммах отчётов: {amt if amt else 'нет'}")
    big = [(r["reportId"], r.get("dateFrom"), r.get("dateTo"), f, str(r.get(f))) for r in items for f in SUM_FIELDS if abs(D(r.get(f))) >= Decimal("1000000") and f not in ("retailAmountSum", "forPaySum", "deliveryServiceSum", "bankPaymentSum")]
    print("суммы ≥ 1 млн вне продаж/к перечислению/доставки/выплат: " + (", ".join(map(str, big)) if big else "нет"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
