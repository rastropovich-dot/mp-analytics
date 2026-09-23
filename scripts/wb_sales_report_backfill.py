#!/usr/bin/env python3
"""История отчёта реализации WB в wb_sales_report_rows: сырьё → план → запись по слову.

    python3 scripts/wb_sales_report_backfill.py --fetch --date-from 2026-02-01     снять сырьё в файлы (только чтение WB)
    python3 scripts/wb_sales_report_backfill.py --plan                             план из файлов, 0 обращений, db_writes = 0
    python3 scripts/wb_sales_report_backfill.py --apply --approve-wb-sales-report-write   запись из файлов по слову владельца

Сырьё — data/wb_sales_report_raw/daily_<от>_<до>.json, куски по CHUNK_DAYS дней,
period=daily (те же rrdId, что у недельных отчётов — loaders/wb_sales_report_loader.py).
Файл есть — в API не идёт. Между обращениями 65 с (лимит 1/мин); ночное окно
прогона — стоп (loaders/pipeline_window.py).

План: кусков, строк, обращений; Σ retailPriceWithDisc (Продажа − Возврат) по
месяцам против marketplace_buyouts.buyouts_amount_seller по месяцам — оборот
листа обязан сойтись с базой там, где база полна (с 2026-03-26), а до неё показать
разницу вслух. Запись — upsert по rrd_id из файлов, затем SQL-контроль печатает
Σ по rr_date за 1…21 сентября (ожидание 16 667 220,00) и по дням недели 09-01…07.
"""
import argparse
import glob
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")

import loaders.wb_sales_report_loader as loader  # noqa: E402
from loaders.pipeline_window import in_nightly_run_window, window_text  # noqa: E402

RAW_DIR = os.path.join("data", "wb_sales_report_raw")
CHUNK_DAYS = 14
CALLS_PATH = os.path.join(RAW_DIR, "calls.json")
COLLECTION_START = "2026-03-26"  # с этой даты marketplace_buyouts по WB полна (docs/wb_data_integrity.md)


def chunks(date_from, date_to, size=CHUNK_DAYS):
    d, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    while d <= end:
        stop = min(d + timedelta(days=size - 1), end)
        yield d.isoformat(), stop.isoformat()
        d = stop + timedelta(days=1)


def raw_path(date_from, date_to):
    return os.path.join(RAW_DIR, f"daily_{date_from}_{date_to}.json")


def fetch(date_from, date_to, sleep_fn=time.sleep):
    """Снять куски в файлы. Есть файл — пропуск. Пустой ответ (204) — файл с пустым списком, это тоже знание."""
    os.makedirs(RAW_DIR, exist_ok=True)
    ledger = json.load(open(CALLS_PATH, encoding="utf-8")) if os.path.exists(CALLS_PATH) else []
    counters = {"requests": 0, "429": 0, "transient": 0}
    for d1, d2 in chunks(date_from, date_to):
        path = raw_path(d1, d2)
        if os.path.exists(path):
            print(f"{d1}…{d2}: есть, пропуск", flush=True)
            continue
        if in_nightly_run_window(datetime.now(timezone.utc)):
            print(f"{d1}…{d2}: ночное окно {window_text()} — стоп; повторный запуск продолжит", flush=True)
            return 3
        before = counters["requests"]
        started = datetime.now(timezone.utc)
        items = loader.fetch_period(d1, d2, counters, sleep_fn=sleep_fn)
        loader.check_items(items, d1, d2)
        with open(path, "w", encoding="utf-8") as h:
            json.dump(items, h, ensure_ascii=False, default=str)
        ledger.append({"chunk": f"{d1}_{d2}", "at_utc": started.isoformat(), "requests": counters["requests"] - before,
                       "rows": len(items), "429": counters["429"], "transient": counters["transient"]})
        json.dump(ledger, open(CALLS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"{d1}…{d2}: строк {len(items)}, файл {path}", flush=True)
    print(f"Обращений: {counters['requests']}, 429 — {counters['429']}, сетевых отказов {counters['transient']}", flush=True)
    return 0


def load_files(date_from=None, date_to=None):
    """Все строки из файлов сырья (Decimal), с проверкой дублей rrdId между файлами."""
    rows, seen, files = [], {}, []
    for path in sorted(glob.glob(os.path.join(RAW_DIR, "daily_*.json"))):
        name = os.path.basename(path)[len("daily_"):-len(".json")]
        d1, d2 = name.split("_")
        if (date_from and d2 < date_from) or (date_to and d1 > date_to):
            continue
        items = json.load(open(path, encoding="utf-8"), parse_float=Decimal)
        for x in items:
            rid = int(x["rrdId"])
            if rid in seen:
                raise RuntimeError(f"rrdId {rid} и в {seen[rid]}, и в {name} — куски пересеклись")
            seen[rid] = name
        rows.extend(items)
        files.append((name, len(items)))
    return rows, files


def turnover_by_month(items):
    by = defaultdict(lambda: {"rows": 0, "turnover": Decimal(0), "sales": 0, "returns": 0})
    for x in items:
        m = str(x["rrDate"])[:7]
        by[m]["rows"] += 1
        op = x.get("sellerOperName")
        if op in ("Продажа", "Возврат") and x.get("retailPriceWithDisc") not in (None, ""):
            sign = -1 if op == "Возврат" else 1
            by[m]["turnover"] += sign * Decimal(str(x["retailPriceWithDisc"]))
            by[m]["sales" if sign > 0 else "returns"] += 1
    return dict(sorted(by.items()))


def buyouts_by_month(sb, date_from, date_to):
    """marketplace_buyouts по WB по месяцам — постранично, с сортировкой по ключу."""
    from loaders import stale_keys
    rows = stale_keys.read_window_rows(
        sb, "marketplace_buyouts", "buyout_date,marketplace_sku,buyouts_amount_seller,buyouts_qty",
        [("eq", "marketplace_code", "wb"), ("gte", "buyout_date", date_from), ("lte", "buyout_date", date_to)],
        ["buyout_date", "marketplace_sku"])
    by = defaultdict(lambda: {"amount": Decimal(0), "qty": Decimal(0)})
    for r in rows:
        m = str(r["buyout_date"])[:7]
        by[m]["amount"] += Decimal(str(r["buyouts_amount_seller"] or 0))
        by[m]["qty"] += Decimal(str(r["buyouts_qty"] or 0))
    return dict(sorted(by.items()))


def plan(sb, date_from, date_to):
    items, files = load_files(date_from, date_to)
    if not items:
        print("файлов сырья нет — сначала --fetch")
        return None
    days = sorted({str(x["rrDate"])[:10] for x in items})
    print(f"Сырьё: файлов {len(files)}, строк {len(items)}, дней {len(days)} ({days[0]} … {days[-1]}); обращений за съём — "
          f"{sum(e['requests'] for e in json.load(open(CALLS_PATH))) if os.path.exists(CALLS_PATH) else '—'}")
    ours = turnover_by_month(items)
    theirs = buyouts_by_month(sb, days[0], days[-1])
    print(f"\n{'месяц':9}{'строк':>8}{'продаж':>8}{'возвр.':>7}{'оборот по отчёту':>20}{'marketplace_buyouts':>22}{'разница':>16}")
    tot = {"o": Decimal(0), "t": Decimal(0)}
    for m in sorted(set(ours) | set(theirs)):
        o = ours.get(m, {"rows": 0, "turnover": Decimal(0), "sales": 0, "returns": 0})
        t = theirs.get(m, {"amount": Decimal(0)})["amount"]
        diff = o["turnover"] - t
        tot["o"] += o["turnover"]; tot["t"] += t
        note = "" if diff == 0 else ("   база до 03-26 неполна" if m < COLLECTION_START[:7] or (m == COLLECTION_START[:7]) else "   ← разница")
        print(f"{m:9}{o['rows']:>8}{o['sales']:>8}{o['returns']:>7}{o['turnover']:>20,.2f}{t:>22,.2f}{diff:>16,.2f}{note}")
    print(f"{'итого':9}{len(items):>8}{'':>15}{tot['o']:>20,.2f}{tot['t']:>22,.2f}{tot['o'] - tot['t']:>16,.2f}")
    print("db_writes = 0")
    return items


def apply(sb, items):
    observed_at = datetime.now(timezone.utc).isoformat()
    rows = [loader.build_row(x, observed_at) for x in items]
    written = loader.upsert_rows(sb, rows)
    print(f"✅ {loader.TABLE}: upsert {written} строк ({observed_at})")
    return written


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--approve-wb-sales-report-write", action="store_true")
    ap.add_argument("--date-from", default="2026-02-01")
    ap.add_argument("--date-to", default=(date.today() - timedelta(days=1)).isoformat())
    args = ap.parse_args(argv)
    if args.fetch:
        return fetch(args.date_from, args.date_to)
    sb = loader._client()
    if args.plan or args.apply:
        items = plan(sb, args.date_from, args.date_to)
        if args.apply:
            if not args.approve_wb_sales_report_write:
                print("--apply без --approve-wb-sales-report-write: не пишу")
                return 2
            if not items:
                return 2
            apply(sb, items)
    return 0


if __name__ == "__main__":
    sys.exit(main())
