#!/usr/bin/env python3
"""Себестоимость выкупов по ШТУКАМ из accrual/postings против позиций by-day, по месяцам. Только чтение.

Позиции — товарные строки by-day с непустой commission (как build_buyout_rows,
знак по sale_amount). Штуки — quantity строки типа 69 SaleCommission из
accrual/postings, сверенной построчно (seller_price × quantity = sale_amount,
accrued = sale_commission; check_accrual_postings_quantity.py). Цена —
article_unit_costs (снимок 1С), article — из marketplace_orders по SKU.

    venv/bin/python3 scripts/cogs_by_units_from_accrual.py --date-from 2026-03-28 --date-to 2026-09-13 \
        --postings data/accrual_postings/2026-03-28_2026-08-30.json data/accrual_postings/2026-08-31_2026-09-15.json

Даты без файла by-day пропускаются и перечисляются. db_writes = 0.
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from check_accrual_postings_quantity import D, load_byday, match_day, sale_index  # noqa: E402

C = Decimal("0.01")


def fetch(sb, table, select, filters, order):
    out, page = [], 0
    while True:
        qb = sb.table(table).select(select)
        for f in filters:
            qb = getattr(qb, f[0])(*f[1:])
        res = qb.order(order).range(page * 1000, page * 1000 + 999).execute()
        out.extend(res.data)
        if len(res.data) < 1000:
            break
        page += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", required=True)
    ap.add_argument("--date-to", required=True)
    ap.add_argument("--postings", nargs="+", required=True)
    ap.add_argument("--snapshot", default="2026-05-20")
    args = ap.parse_args()
    postings = []
    for path in args.postings:
        postings.extend(json.load(open(path)))
    idx = sale_index(postings)

    import loaders.ozon_fbo_orders_loader as fbo
    sb = fbo.supabase
    orders = fetch(sb, "marketplace_orders", "marketplace_sku,article,orders_qty",
                   [("eq", "marketplace_code", "ozon"), ("gte", "order_date", "2026-03-28")], "id")
    sku_art = defaultdict(lambda: defaultdict(Decimal))
    for r in orders:
        sku_art[str(r["marketplace_sku"])][str(r["article"] or "")] += D(r["orders_qty"] or 0)
    sku2art = {s: max(a.items(), key=lambda kv: kv[1])[0] for s, a in sku_art.items()}
    norms = sorted({a.lower() for a in sku2art.values() if a})
    cost = {}
    for i in range(0, len(norms), 500):
        for r in sb.table("article_unit_costs").select("offer_id_norm,unit_cost").eq("snapshot_date", args.snapshot).in_("offer_id_norm", norms[i:i + 500]).execute().data:
            cost[r["offer_id_norm"]] = D(r["unit_cost"])

    def unit_cost(sku):
        art = sku2art.get(str(sku))
        return cost.get(art.lower()) if art else None

    # marketplace_buyouts за окно: старый API (до 2026-08-17) писал buyouts_qty по записям items[], то есть
    # ближе к штукам; accrual (после) — позиции. Показываем третьей колонкой, чтобы видеть, где именно недобор.
    table_rows = fetch(sb, "marketplace_buyouts", "buyout_date,marketplace_sku,buyouts_qty",
                       [("eq", "marketplace_code", "ozon"), ("gte", "buyout_date", args.date_from), ("lte", "buyout_date", args.date_to)], "id")
    table_by_day = defaultdict(list)
    for r in table_rows:
        table_by_day[r["buyout_date"]].append(r)
    M = defaultdict(lambda: defaultdict(Decimal))
    missing_days, unmatched_total = [], 0
    nocost = defaultdict(lambda: [0, 0])
    d, d_to = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
    while d <= d_to:
        day = d.isoformat()
        path = os.path.join("data", "accrual_history", f"{day}.json")
        if not os.path.exists(path):
            missing_days.append(day)
            d += timedelta(days=1)
            continue
        c, units, pos, _bad = match_day(load_byday(day), idx, day)
        unmatched_total += c["unmatched"]
        m = M[day[:7]]
        m["days"] += 1
        for r in table_by_day.get(day, []):  # таблица — только за дни, где есть by-day, иначе сравнение нечестное
            qty = D(r["buyouts_qty"] or 0)
            m["table_qty"] += qty
            uc = unit_cost(r["marketplace_sku"])
            if uc is not None:
                m["cogs_table"] += qty * uc
        m["positions"] += c["positions"]
        m["units"] += c["units"]
        m["rows_q2"] += c["rows_q2"]
        m["units_q2"] += c["units_q2"]
        m["unmatched"] += c["unmatched"]
        for sku in set(units) | set(pos):
            uc = unit_cost(sku)
            if uc is None:
                nocost[day[:7]][0] += pos.get(sku, 0)
                nocost[day[:7]][1] += units.get(sku, 0)
                continue
            m["cogs_pos"] += pos.get(sku, 0) * uc
            m["cogs_units"] += units.get(sku, 0) * uc
        d += timedelta(days=1)

    print(f"снимок цен {args.snapshot}; sku→article {len(sku2art)}, цен найдено {len(cost)} из {len(norms)}; "
          f"файлов accrual/postings {len(args.postings)}, отправлений {len(postings)}")
    if missing_days:
        print(f"нет сырья by-day за {len(missing_days)} дат: {missing_days[:8]}{' …' if len(missing_days) > 8 else ''}")
    print(f"\n{'месяц':9}{'дней':>5}{'qty табл.':>10}{'позиций':>9}{'штук':>8}{'штук−поз':>9}{'q≥2':>6}{'СС таблица':>18}{'СС позиции':>18}{'СС штуки':>18}{'штуки−табл.':>14}{'без цены':>9}")
    T = defaultdict(Decimal)
    for mo in sorted(M):
        m = M[mo]
        print(f"{mo:9}{m['days']:>5}{m['table_qty']:>10.0f}{m['positions']:>9}{m['units']:>8}{m['units'] - m['positions']:>9}{m['rows_q2']:>6}"
              f"{m['cogs_table']:>18,.2f}{m['cogs_pos']:>18,.2f}{m['cogs_units']:>18,.2f}{m['cogs_units'] - m['cogs_table']:>14,.2f}{f'{nocost[mo][0]}/{nocost[mo][1]}':>9}")
        for k, v in m.items():
            T[k] += v
    print(f"{'итого':9}{T['days']:>5}{T['table_qty']:>10.0f}{T['positions']:>9}{T['units']:>8}{T['units'] - T['positions']:>9}{T['rows_q2']:>6}"
          f"{T['cogs_table']:>18,.2f}{T['cogs_pos']:>18,.2f}{T['cogs_units']:>18,.2f}{T['cogs_units'] - T['cogs_table']:>14,.2f}")
    print(f"СС штуки − СС позиции = {T['cogs_units'] - T['cogs_pos']:,.2f}; СС позиции − СС таблица = {T['cogs_pos'] - T['cogs_table']:,.2f}")
    if T["positions"]:
        print(f"\nштук / позиций = {T['units'] / T['positions'] * 100:.2f} %; СС по штукам / СС по позициям = {T['cogs_units'] / T['cogs_pos'] * 100:.2f} %; "
              f"не сошедшихся строк {unmatched_total}")
    print("db_writes = 0")


if __name__ == "__main__":
    main()
