#!/usr/bin/env python3
"""Снимок витрины daily_sku_kpi по Ozon по датам и сравнение с прежним снимком. Только чтение.

    venv/bin/python3 scripts/kpi_ozon_snapshot.py --take                       → data/snapshots/kpi_ozon_by_date_<UTC>.json
    venv/bin/python3 scripts/kpi_ozon_snapshot.py --compare data/snapshots/kpi_ozon_by_date_20260922T07Z.json --until 2026-08-23

Зачем. 2026-09-22 WB-сессия пишет WB-строки в marketplace_orders (восстановление истории, 176 дат), ночь пересчитывает
витрину по обеим площадкам. Витрина по Ozon от WB-записи не должна измениться ни на рубль — но ночь законно меняет
даты своего окна (заказы 30 дней, начисления 31 день). Поэтому сравнение делится: даты СТРОГО ДО --until (начало
окна ночи) обязаны совпасть по всем суммам и числу строк; даты от --until — печатаются с разницей, это работа ночи.

Чтение — постранично с сортировкой по полному ключу (kpi_date, marketplace_code, marketplace_sku); повтор ключа — RuntimeError.
db_writes = 0.
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

COLS = ("orders_qty", "orders_amount_seller", "buyouts_qty", "buyouts_amount_seller", "ad_spend", "commission_amount", "logistics_amount", "other_expenses_amount")
OUT_DIR = os.path.join(ROOT, "data", "snapshots")


def take(sb):
    from report_ozon_month import fetch
    rows = fetch(sb, "daily_sku_kpi", "id,kpi_date,marketplace_sku,article," + ",".join(COLS), [("eq", "marketplace_code", "ozon")],
                 ["kpi_date", "marketplace_code", "marketplace_sku"])
    keys, by = set(), defaultdict(lambda: defaultdict(Decimal))
    for r in rows:
        k = (r["kpi_date"], r["marketplace_sku"])
        if k in keys:
            raise RuntimeError(f"повтор ключа {k} — чтение без порядка?")
        keys.add(k)
        a = by[r["kpi_date"]]
        a["rows"] += 1
        a["with_article"] += 1 if (r.get("article") or "") else 0
        for c in COLS:
            a[c] += Decimal(str(r.get(c) or 0))
    return {"taken_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "marketplace_code": "ozon", "rows": len(rows), "dates": len(by),
            "by_date": {d: {k: str(v) for k, v in a.items()} for d, a in sorted(by.items())},
            "totals": {c: str(sum((a[c] for a in by.values()), Decimal(0))) for c in COLS}}


def compare(old, new, until):
    """[(дата, колонка, было, стало)] для дат строго до until; дальше — только печать."""
    fields = ("rows", "with_article") + COLS
    diffs, night = [], []
    for d in sorted(set(old["by_date"]) | set(new["by_date"])):
        a, b = old["by_date"].get(d, {}), new["by_date"].get(d, {})
        for f in fields:
            x, y = Decimal(a.get(f, "0")), Decimal(b.get(f, "0"))
            if x != y:
                (diffs if d < until else night).append((d, f, x, y))
    return diffs, night


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--take", action="store_true")
    ap.add_argument("--compare", help="прежний снимок")
    ap.add_argument("--until", help="начало окна ночи: даты строго до него обязаны совпасть")
    args = ap.parse_args()
    import loaders.ozon_fbo_orders_loader as fbo
    t = time.time()
    snap = take(fbo.supabase)
    print(f"снимок: строк {snap['rows']}, дат {snap['dates']}, {time.time() - t:.0f} с; итоги {snap['totals']}")
    if args.take or not args.compare:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, f"kpi_ozon_by_date_{snap['taken_at'].replace('-', '').replace(':', '')[:13]}Z.json")
        json.dump(snap, open(path, "w"), ensure_ascii=False, indent=1)
        print(f"записан {path}")
    if args.compare:
        if not args.until:
            raise SystemExit("--compare требует --until")
        old = json.load(open(args.compare))
        diffs, night = compare(old, snap, args.until)
        print(f"\nсравнение с {args.compare} (снят {old['taken_at']}): даты до {args.until} — расхождений {len(diffs)}"
              + ("" if not diffs else " ОБЯЗАНЫ СОВПАСТЬ"))
        for d, f, x, y in diffs:
            print(f"  {d} {f}: было {x} стало {y} ({y - x:+})")
        print(f"даты от {args.until} (окно ночи, меняются законно): изменений {len(night)}")
        for d, f, x, y in night:
            if f in ("rows", "orders_amount_seller", "buyouts_amount_seller", "ad_spend", "other_expenses_amount"):
                print(f"  {d} {f}: {x} → {y} ({y - x:+})")
        print("db_writes = 0")
        sys.exit(1 if diffs else 0)
    print("db_writes = 0")


if __name__ == "__main__":
    main()
