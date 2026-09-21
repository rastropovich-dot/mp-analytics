#!/usr/bin/env python3
"""Удаление ключей daily_sku_kpi, у которых после пересборки истории заказов не осталось источника.

Ночной reports_daily_sku_kpi строит витрину из заказов, выкупов, расходов и органики и
пишет upsert-ом: ключ (дата, площадка, sku), у которого не осталось ни одной строки-источника,
он не построит и НЕ удалит — старые заказы останутся в витрине навсегда. После пересборки
2026-09-18 такими стали ключи старой датировки (до 15fb56f FBS датировал заказ по
shipment_date, FBO — по UTC-дате): те же заказы теперь лежат под верной датой, и апрель задвоен.

Список ключей — JSON от scripts/check_orders_history_plan.py, раздел E. Список — не приказ:
перед удалением каждый ключ перепроверяется по живой БД, и удаляются только те, у которых
СЕЙЧАС нет строки ни в marketplace_orders, ни в marketplace_buyouts, ни в marketplace_expenses,
ни в ozon_daily_sku_organic. Ключ, у которого источник нашёлся, не трогается и называется вслух.

Без --apply ничего не пишет (db_writes = 0). С --apply: снимок удаляемых строк (все колонки)
в data/snapshots/kpi_stale_keys_<UTC>.json → delete по id пачками → контроль «осталось 0».

    venv/bin/python3 scripts/delete_stale_kpi_keys.py --keys data/orders_history_kpi_stale_keys_20260918.json
    venv/bin/python3 scripts/delete_stale_kpi_keys.py --keys data/orders_history_kpi_stale_keys_20260918.json --apply
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

SNAP_DIR = os.path.join("data", "snapshots")
MARKETPLACE = "ozon"
SOURCES = (("marketplace_orders", "order_date"), ("marketplace_buyouts", "buyout_date"),
           ("marketplace_expenses", "expense_date"), ("ozon_daily_sku_organic", "sale_date"))
CHUNK = 100
D = lambda v: Decimal(str(v or 0))  # noqa: E731


def sb():
    import loaders.ozon_fbo_orders_loader as fbo
    return fbo.supabase


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    listed = json.load(open(args.keys))["keys"]
    want = {(k["kpi_date"], str(k["marketplace_sku"])): (D(k["orders_qty"]), D(k["orders_amount_seller"])) for k in listed}
    if len(want) != len(listed):
        raise SystemExit(f"в списке {len(listed)} записей, уникальных ключей {len(want)} — список с повторами, стоп")
    print(f"список {args.keys}: {len(want)} ключей, {sum(v[0] for v in want.values()):,.0f} шт, {sum(v[1] for v in want.values()):,.2f} ₽, "
          f"даты {min(k[0] for k in want)} … {max(k[0] for k in want)}")

    by_date = defaultdict(list)
    for kpi_date, sku in want:
        by_date[kpi_date].append(sku)

    rows, with_source = {}, defaultdict(set)
    for kpi_date, skus in sorted(by_date.items()):
        for i in range(0, len(skus), CHUNK):
            part = skus[i:i + CHUNK]
            res = sb().table("daily_sku_kpi").select("*").eq("kpi_date", kpi_date).eq("marketplace_code", MARKETPLACE).in_("marketplace_sku", part).order("id").execute()
            for r in res.data:
                rows[(r["kpi_date"], r["marketplace_sku"])] = r
            for table, col in SOURCES:
                res = sb().table(table).select("marketplace_sku").eq(col, kpi_date).eq("marketplace_code", MARKETPLACE).in_("marketplace_sku", part).order("marketplace_sku").limit(1000).execute()
                if len(res.data) >= 1000:
                    raise SystemExit(f"{table} {kpi_date}: 1000 строк в ответе — страница обрезана, проверка недостоверна")
                for r in res.data:
                    with_source[(kpi_date, r["marketplace_sku"])].add(table)

    absent = sorted(k for k in want if k not in rows)
    blocked = sorted(k for k in rows if with_source.get(k))
    differ = sorted(k for k in rows if k not in with_source and (D(rows[k]["orders_qty"]), D(rows[k]["orders_amount_seller"])) != want[k])
    to_delete = sorted(k for k in rows if not with_source.get(k))
    nonzero_other = [k for k in to_delete if any(D(rows[k].get(f)) != 0 for f in
                     ("buyouts_qty", "buyouts_amount_seller", "ad_spend", "commission_amount", "logistics_amount", "other_expenses_amount",
                      "ad_orders_revenue", "organic_orders_revenue"))]

    print(f"в daily_sku_kpi найдено {len(rows)} из {len(want)}; нет в витрине {len(absent)}")
    print(f"с живым источником (НЕ удаляются, ночной KPI перестроит сам): {len(blocked)}")
    for k in blocked[:20]:
        print(f"    {k[0]} {k[1]}: {sorted(with_source[k])}")
    print(f"заказы в витрине отличаются от списка (удаляются — источника всё равно нет): {len(differ)}")
    for k in differ[:20]:
        print(f"    {k[0]} {k[1]}: витрина {D(rows[k]['orders_qty'])} шт / {D(rows[k]['orders_amount_seller']):,.2f}, список {want[k][0]} шт / {want[k][1]:,.2f}")
    print(f"у удаляемых ключей ненулевые выкупы / расходы / реклама / органика в самой строке витрины: {len(nonzero_other)}")
    for k in nonzero_other[:20]:
        print(f"    {k[0]} {k[1]}:", {f: rows[k][f] for f in ("buyouts_amount_seller", "ad_spend", "commission_amount", "logistics_amount", "other_expenses_amount")})
    print(f"К УДАЛЕНИЮ: {len(to_delete)} ключей, заказов {sum(D(rows[k]['orders_qty']) for k in to_delete):,.0f} шт на "
          f"{sum(D(rows[k]['orders_amount_seller']) for k in to_delete):,.2f} ₽, даты {to_delete[0][0] if to_delete else '—'} … {to_delete[-1][0] if to_delete else '—'}")

    if not args.apply:
        print("db_writes = 0")
        return
    if not to_delete:
        print("удалять нечего; db_writes = 0")
        return

    os.makedirs(SNAP_DIR, exist_ok=True)
    snap = os.path.join(SNAP_DIR, f"kpi_stale_keys_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
    json.dump({"table": "daily_sku_kpi", "keys_file": args.keys, "rows": [rows[k] for k in to_delete]}, open(snap, "w"), ensure_ascii=False)
    print(f"снимок {len(to_delete)} строк (все колонки) → {snap}")

    ids = [rows[k]["id"] for k in to_delete]
    deleted = 0
    for i in range(0, len(ids), CHUNK):
        res = sb().table("daily_sku_kpi").delete().in_("id", ids[i:i + CHUNK]).execute()
        deleted += len(res.data or [])
    left = 0
    for i in range(0, len(ids), CHUNK):
        left += len(sb().table("daily_sku_kpi").select("id").in_("id", ids[i:i + CHUNK]).execute().data)
    print(f"удалено {deleted} из {len(ids)}, осталось в таблице {left} {'=' if left == 0 and deleted == len(ids) else '≠ ОШИБКА'}")
    print(f"db_writes = {deleted}")


if __name__ == "__main__":
    main()
