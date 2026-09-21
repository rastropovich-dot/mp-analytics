#!/usr/bin/env python3
"""Штуки в accrual/postings против позиций by-day и против realization/by-day. Только чтение.

Строка типа 69 SaleCommission в accrual/postings несёт seller_price (цена за
единицу) и quantity. Проверка построчная и со знаком: для каждой товарной
строки by-day (sale_amount, sale_commission) ищется строка 69 того же
отправления, SKU и даты с seller_price × quantity = sale_amount и
accrued = sale_commission. У возвратов seller_price отрицательный, тождество
держится со знаком. Штуки нетто = Σ quantity × sign(sale_amount).

Третий источник — ozon_realization_by_day (штуки Ozon, 08-14 … 09-13):
сравнение по дню и по SKU, side='sale' минус side='return'.

    venv/bin/python3 scripts/check_accrual_postings_quantity.py \
        --postings data/accrual_postings/2026-08-31_2026-09-15.json --date-from 2026-08-31 --date-to 2026-09-15
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

BYDAY_DIR = os.path.join("data", "accrual_history")
SALE_TYPE = 69


def D(v):
    return Decimal(str(v))


def sale_index(postings):
    """(posting, sku, date) -> строки типа 69."""
    idx = defaultdict(list)
    for p in postings:
        for a in p.get("accruals") or []:
            if a.get("type_id") == SALE_TYPE:
                idx[(p["posting_number"], str(a.get("sku")), a.get("accrual_date"))].append(a)
    return idx


def load_byday(day):
    data = json.load(open(os.path.join(BYDAY_DIR, f"{day}.json")))
    return data["accruals"] if isinstance(data, dict) else data


def match_day(accruals, idx, day):
    """Построчная сверка одного дня. Возвращает счётчики и штуки/позиции по SKU."""
    c = defaultdict(int)
    per_sku_units, per_sku_pos = defaultdict(int), defaultdict(int)
    used, bad_rows = set(), []
    for r in accruals:
        if r.get("accrued_category") != "POSTING" or not r.get("posting"):
            continue
        pn = r.get("unit_number")
        for pr in r["posting"].get("products") or []:
            cm = pr.get("commission") or {}
            if not cm:
                continue
            sa, sc = D(cm["sale_amount"]["amount"]), D(cm["sale_commission"]["amount"])
            if sa == 0 and sc == 0:
                c["zero"] += 1
                continue
            sign = 1 if sa >= 0 else -1
            sku = str(pr.get("sku"))
            c["rows"] += 1
            c["positions"] += sign
            per_sku_pos[sku] += sign
            hit = None
            for a in idx.get((pn, sku, day), []):
                if id(a) in used:
                    continue
                q, sp = a.get("quantity") or 0, a.get("seller_price")
                if sp is not None and D(sp["amount"]) * q == sa and D(a["accrued"]["amount"]) == sc:
                    hit = a
                    break
            if hit is None:
                c["unmatched"] += 1
                bad_rows.append((pn, sku, str(sa), str(sc)))
                continue
            used.add(id(hit))
            q = hit["quantity"]
            c["matched"] += 1
            c["units"] += sign * q
            per_sku_units[sku] += sign * q
            if q >= 2:
                c["rows_q2"] += 1
                c["units_q2"] += q
    return c, per_sku_units, per_sku_pos, bad_rows


def realization_by_sku(sb, day):
    out, page = [], 0
    while True:
        res = (sb.table("ozon_realization_by_day").select("sku,side,quantity")
               .eq("realization_date", day).order("row_number").order("side")
               .range(page * 1000, page * 1000 + 999).execute())
        out.extend(res.data)
        if len(res.data) < 1000:
            break
        page += 1
    per_sku = defaultdict(int)
    sale = ret = 0
    for r in out:
        q = int(r["quantity"])
        if r["side"] == "sale":
            per_sku[str(r["sku"])] += q
            sale += q
        else:
            per_sku[str(r["sku"])] -= q
            ret += q
    return per_sku, sale, ret, len(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--postings", required=True)
    ap.add_argument("--date-from", required=True)
    ap.add_argument("--date-to", required=True)
    ap.add_argument("--no-db", action="store_true", help="без сравнения с realization/by-day")
    args = ap.parse_args()
    idx = sale_index(json.load(open(args.postings)))
    sb = None
    if not args.no_db:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
        import loaders.ozon_fbo_orders_loader as fbo
        sb = fbo.supabase
    tot = defaultdict(int)
    print(f"{'день':12}{'позиций':>8}{'штук':>6}{'строк':>7}{'q≥2':>5}{'сошлось':>8}{'не сошл.':>9}"
          f"{'реализ. нетто':>14}{'прод/возв':>11}{'штук−реал.':>11}{'SKU =':>7}{'SKU ≠':>7}")
    d, d_to = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
    while d <= d_to:
        day = d.isoformat()
        c, units, pos, bad = match_day(load_byday(day), idx, day)
        line = (f"{day:12}{c['positions']:>8}{c['units']:>6}{c['rows']:>7}{c['rows_q2']:>5}{c['matched']:>8}{c['unmatched']:>9}")
        if sb is not None:
            rz, sale, ret, nrows = realization_by_sku(sb, day)
            if nrows:
                net = sale - ret
                skus = set(units) | set(rz)
                eq = sum(1 for s in skus if units.get(s, 0) == rz.get(s, 0))
                ne = len(skus) - eq
                line += f"{net:>14}{f'{sale}/{ret}':>11}{c['units'] - net:>11}{eq:>7}{ne:>7}"
                tot["real_net"] += net; tot["sku_eq"] += eq; tot["sku_ne"] += ne; tot["real_days"] += 1
                if ne:
                    diffs = sorted(((s, units.get(s, 0), rz.get(s, 0)) for s in skus if units.get(s, 0) != rz.get(s, 0)), key=lambda x: -abs(x[1] - x[2]))
                    line += "  " + "; ".join(f"sku {s}: штук {u}, реал. {r}" for s, u, r in diffs[:4])
            else:
                line += f"{'—':>14}{'—':>11}{'—':>11}{'—':>7}{'—':>7}"
        print(line)
        for b in bad[:5]:
            print("   не сошлось:", day, *b)
        for k, v in c.items():
            tot[k] += v
        d += timedelta(days=1)
    print(f"{'итого':12}{tot['positions']:>8}{tot['units']:>6}{tot['rows']:>7}{tot['rows_q2']:>5}{tot['matched']:>8}{tot['unmatched']:>9}"
          + (f"{tot['real_net']:>14}{'':>11}{'':>11}{tot['sku_eq']:>7}{tot['sku_ne']:>7}  (реализация есть за {tot['real_days']} дн.)" if sb is not None else ""))
    if tot["positions"]:
        print(f"штук / позиций нетто = {tot['units'] / tot['positions'] * 100:.2f} %, штук − позиций = {tot['units'] - tot['positions']}, "
              f"строк с quantity ≥ 2: {tot['rows_q2']} на {tot['units_q2']} штук")
    print("db_writes = 0")


if __name__ == "__main__":
    main()
