#!/usr/bin/env python3
"""Проверка плана пересборки истории заказов Ozon до записи. Только чтение.

`rebuild_ozon_orders_history.py --plan` даёт помесячно деньги «таблица / сбор».
Этого мало, чтобы показать план владельцу: нужны штуки, доля отмен, сравнение
МНОЖЕСТВ ключей, ступенька на границе ночного окна и ключи витрины, которые
останутся без заказов. Скрипт читает те же файлы сырья
(data/postings_raw/history_<схема>_<from>_<to>.json) и в API Ozon не ходит;
нет файла — выходит с ошибкой. В БД только читает (db_writes = 0).

Что измеряет каждая величина:

    таблица, подтв.   orders_* в marketplace_orders. До границы (--boundary, начало
                      30-дневного окна первой ночи на новом коде) это старые правила:
                      FBS — все статусы вместе («созданные»), FBO — подтверждённые
                      на момент последней записи + ключи, чьи заказы отменились позже.
                      С границы — подтверждённые по новому правилу.
    таблица, отм.     cancelled_orders_* — до границы нули (колонки не было).
    план, подтв./отм. build_order_rows() по сегодняшнему сбору: статус на момент сбора.
    создано           подтв. + отм.

    venv/bin/python3 scripts/check_orders_history_plan.py --date-from 2026-03-28 --date-to 2026-09-18
"""
import argparse
import importlib.util
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_spec = importlib.util.spec_from_file_location("rebuild", os.path.join(ROOT, "scripts", "rebuild_ozon_orders_history.py"))
rebuild = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rebuild)
rules = rebuild.rules
D = rebuild.D
Z = Decimal(0)


def pct(part, whole):
    return f"{(part / whole * 100):.1f} %" if whole else "—"


def load_raw(scheme, date_from, date_to):
    path = rebuild.raw_path(scheme, date_from, date_to)
    if not os.path.exists(path):
        raise SystemExit(f"нет файла сырья {path} — сначала rebuild_ozon_orders_history.py --fetch; в API этот скрипт не ходит")
    data = json.load(open(path))
    return path, data


def period_of(order_date, boundary):
    month = order_date[:7]
    if month == boundary[:7] and boundary[8:] != "01":
        return f"{month} до {boundary[8:]}" if order_date < boundary else f"{month} с {boundary[8:]}"
    return month


def agg(rows, key_fn):
    """rows -> {key: [строк, подтв шт, подтв ₽, отм шт, отм ₽]}"""
    out = defaultdict(lambda: [0, Z, Z, Z, Z])
    for r in rows:
        a = out[key_fn(r)]
        a[0] += 1
        a[1] += D(r.get("orders_qty")); a[2] += D(r.get("orders_amount_seller"))
        a[3] += D(r.get("cancelled_orders_qty")); a[4] += D(r.get("cancelled_orders_amount_seller"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", default="2026-03-28")
    ap.add_argument("--date-to", default=date.today().isoformat())
    ap.add_argument("--boundary", default="2026-08-19", help="первая дата, уже переписанная ночным окном по новому правилу")
    ap.add_argument("--step-from", default="2026-08-10")
    ap.add_argument("--step-to", default="2026-08-27")
    ap.add_argument("--dump-keys", help="куда положить JSON с ключами «только в таблице» / «только в плане»")
    args = ap.parse_args()
    df, dt, boundary = args.date_from, args.date_to, args.boundary

    # ---------- A. сырьё ----------
    plan_rows = {}
    print("A. СЫРЬЁ")
    for scheme in ("fbo", "fbs"):
        path, data = load_raw(scheme, df, dt)
        postings = data["postings"]
        numbers = [p["posting_number"] for p in postings]
        statuses = Counter(p.get("status") for p in postings)
        field = "created_at" if scheme == "fbo" else "in_process_at"
        times = sorted(p.get(field) for p in postings if p.get(field))
        print(f"  {scheme}: {path}")
        print(f"     снято {data.get('fetched_at')}, окно {data.get('window')}")
        print(f"     отправлений {len(postings)}, уникальных номеров {len(set(numbers))}; {field}: {times[0]} … {times[-1]}, пусто у {len(postings) - len(times)}")
        print(f"     статусы: {dict(statuses.most_common())}")
        rows, counters = rules.build_order_rows(postings, scheme, observed_at=data.get("fetched_at"))
        rules.print_counters(scheme, counters)
        inside = [r for r in rows if df <= r["order_date"] <= dt]
        print(f"     строк по правилу {len(rows)}, из них в {df} … {dt}: {len(inside)}; вне окна: {sorted(Counter(r['order_date'] for r in rows if not (df <= r['order_date'] <= dt)).items())}")
        plan_rows[scheme] = inside

    # ---------- таблица ----------
    table = rebuild.fetch_all(
        "marketplace_orders", [("eq", "marketplace_code", "ozon"), ("gte", "order_date", df), ("lte", "order_date", dt)],
        select="id,order_date,order_schema,marketplace_sku,orders_qty,orders_amount_seller,cancelled_orders_qty,cancelled_orders_amount_seller,observed_at")
    ids = [r["id"] for r in table]
    print(f"\nтаблица marketplace_orders (ozon, {df} … {dt}): строк {len(table)}, уникальных id {len(set(ids))}")
    table_by_scheme = defaultdict(list)
    for r in table:
        table_by_scheme[r["order_schema"]].append(r)
    other = set(table_by_scheme) - {"fbo", "fbs"}
    if other:
        print(f"ВНИМАНИЕ: в таблице есть схемы вне fbo/fbs: {other} — скрипт пересборки их не трогает")

    # ---------- B. по периодам ----------
    print(f"\nB. ПО ПЕРИОДАМ (граница ночного окна {boundary}: до неё таблица по старым правилам, после — по новому)")
    head = f"{'схема':5} {'период':14}|{'табл строк':>10}{'табл подтв шт':>14}{'табл подтв ₽':>16}{'табл отм ₽':>14} |{'план строк':>10}{'подтв шт':>10}{'подтв ₽':>16}{'отм шт':>9}{'отм ₽':>15}{'создано ₽':>16} |{'отм шт %':>9}{'отм ₽ %':>9} |{'Δ подтв шт':>11}{'Δ подтв ₽':>16}{'Δ пары ₽':>16}"
    print(head)
    totals = {}
    for scheme in ("fbo", "fbs"):
        t = agg(table_by_scheme[scheme], lambda r: period_of(r["order_date"], boundary))
        n = agg(plan_rows[scheme], lambda r: period_of(r["order_date"], boundary))
        tt, nn = [0, Z, Z, Z, Z], [0, Z, Z, Z, Z]
        for key in sorted(set(t) | set(n)):
            a, b = t.get(key, [0, Z, Z, Z, Z]), n.get(key, [0, Z, Z, Z, Z])
            for i in range(5):
                tt[i] += a[i]; nn[i] += b[i]
            print(f"{scheme:5} {key:14}|{a[0]:>10}{a[1]:>14,.0f}{a[2]:>16,.2f}{a[4]:>14,.2f} |{b[0]:>10}{b[1]:>10,.0f}{b[2]:>16,.2f}{b[3]:>9,.0f}{b[4]:>15,.2f}{b[2] + b[4]:>16,.2f} |{pct(b[3], b[1] + b[3]):>9}{pct(b[4], b[2] + b[4]):>9} |{b[1] - a[1]:>11,.0f}{b[2] - a[2]:>16,.2f}{(b[2] + b[4]) - (a[2] + a[4]):>16,.2f}")
        print(f"{scheme:5} {'ИТОГО':14}|{tt[0]:>10}{tt[1]:>14,.0f}{tt[2]:>16,.2f}{tt[4]:>14,.2f} |{nn[0]:>10}{nn[1]:>10,.0f}{nn[2]:>16,.2f}{nn[3]:>9,.0f}{nn[4]:>15,.2f}{nn[2] + nn[4]:>16,.2f} |{pct(nn[3], nn[1] + nn[3]):>9}{pct(nn[4], nn[2] + nn[4]):>9} |{nn[1] - tt[1]:>11,.0f}{nn[2] - tt[2]:>16,.2f}{(nn[2] + nn[4]) - (tt[2] + tt[4]):>16,.2f}")
        totals[scheme] = (tt, nn)
    print("Δ = план − таблица. «Δ пары» = (подтв + отм) плана − (подтв + отм) таблицы.")

    # ---------- C. множества ключей ----------
    print("\nC. МНОЖЕСТВА КЛЮЧЕЙ (order_date, sku, схема): что есть только в таблице и только в плане")
    dump = {}
    for scheme in ("fbo", "fbs"):
        tk = {(r["order_date"], r["marketplace_sku"]): r for r in table_by_scheme[scheme]}
        nk = {(r["order_date"], r["marketplace_sku"]): r for r in plan_rows[scheme]}
        only_t, only_n = sorted(set(tk) - set(nk)), sorted(set(nk) - set(tk))
        dump[scheme] = {"only_table": [list(k) for k in only_t], "only_plan": [list(k) for k in only_n]}
        print(f"  {scheme}: ключей в таблице {len(tk)}, в плане {len(nk)}, общих {len(set(tk) & set(nk))}, только в таблице {len(only_t)}, только в плане {len(only_n)}")
        for label, keys, src in (("только в таблице", only_t, tk), ("только в плане", only_n, nk)):
            by = defaultdict(lambda: [0, Z, Z])
            for k in keys:
                a = by[period_of(k[0], boundary)]
                a[0] += 1; a[1] += D(src[k].get("orders_amount_seller")); a[2] += D(src[k].get("cancelled_orders_amount_seller"))
            for p in sorted(by):
                print(f"      {label:17} {p:14} ключей {by[p][0]:>6}  подтв ₽ {by[p][1]:>15,.2f}  отм ₽ {by[p][2]:>15,.2f}")
        # общие ключи: где подтверждённые плана больше таблицы (невозможно при чистой отмене — значит, в таблице недобор)
        more = [(k, D(nk[k]["orders_amount_seller"]) - D(tk[k]["orders_amount_seller"])) for k in set(tk) & set(nk)
                if D(nk[k]["orders_amount_seller"]) > D(tk[k]["orders_amount_seller"])]
        by = defaultdict(lambda: [0, Z])
        for k, delta in more:
            by[period_of(k[0], boundary)][0] += 1; by[period_of(k[0], boundary)][1] += delta
        print(f"      общие ключи, где подтв. плана БОЛЬШЕ таблицы (в таблице недобор): {len(more)} на {sum((d for _k, d in more), Z):,.2f}")
        for p in sorted(by):
            print(f"          {p:14} ключей {by[p][0]:>6}  на {by[p][1]:>15,.2f}")
    if args.dump_keys:
        json.dump(dump, open(args.dump_keys, "w"), ensure_ascii=False)
        print(f"  ключи → {args.dump_keys}")

    # ---------- D. ступенька ----------
    print(f"\nD. СТУПЕНЬКА: по дням {args.step_from} … {args.step_to}, обе схемы вместе; витрина — daily_marketplace_kpi.orders_amount_seller")
    kpi = rebuild.fetch_all("daily_marketplace_kpi", [("eq", "marketplace_code", "ozon"), ("gte", "kpi_date", args.step_from), ("lte", "kpi_date", args.step_to)],
                            select="id,kpi_date,orders_amount_seller,ad_spend,ad_share_of_orders")
    kpi_by = {r["kpi_date"]: r for r in kpi}
    t_day = agg([r for r in table if args.step_from <= r["order_date"] <= args.step_to], lambda r: r["order_date"])
    n_day = agg([r for s in plan_rows.values() for r in s if args.step_from <= r["order_date"] <= args.step_to], lambda r: r["order_date"])
    print(f"{'дата':12}{'витрина ₽':>16}{'таблица подтв ₽':>18}{'план подтв ₽':>16}{'план отм ₽':>15}{'план создано ₽':>17}{'отм ₽ %':>9}{'реклама ₽':>14}{'ДРР витр.':>10}{'ДРР план':>10}")
    d = date.fromisoformat(args.step_from)
    while d.isoformat() <= args.step_to:
        k = d.isoformat()
        a, b, v = t_day.get(k, [0, Z, Z, Z, Z]), n_day.get(k, [0, Z, Z, Z, Z]), kpi_by.get(k) or {}
        ad = D(v.get("ad_spend"))
        mark = "  ← граница" if k == boundary else ""
        print(f"{k:12}{D(v.get('orders_amount_seller')):>16,.2f}{a[2]:>18,.2f}{b[2]:>16,.2f}{b[4]:>15,.2f}{b[2] + b[4]:>17,.2f}{pct(b[4], b[2] + b[4]):>9}{ad:>14,.2f}{pct(ad, D(v.get('orders_amount_seller'))):>10}{pct(ad, b[2]):>10}{mark}")
        d += timedelta(days=1)

    # ---------- E. ключи витрины, у которых заказов не останется ----------
    print("\nE. ВИТРИНА daily_sku_kpi: ключи (дата, sku), которые есть в таблице заказов сейчас и исчезнут после пересборки")
    t_pairs = {(r["order_date"], r["marketplace_sku"]) for r in table}
    n_pairs = {(r["order_date"], r["marketplace_sku"]) for s in plan_rows.values() for r in s}
    vanish = sorted(t_pairs - n_pairs)
    print(f"  пар (дата, sku) в таблице {len(t_pairs)}, в плане {len(n_pairs)}, исчезает {len(vanish)}, появляется {len(n_pairs - t_pairs)}")
    by_date = defaultdict(list)
    for dte, sku in vanish:
        by_date[dte].append(sku)
    sb = rebuild.sb()
    stale, kept = [], []
    for dte, skus in sorted(by_date.items()):
        found = defaultdict(set)
        for tbl, col, extra in (("marketplace_buyouts", "buyout_date", True), ("marketplace_expenses", "expense_date", True), ("ozon_daily_sku_organic", "sale_date", True)):
            for i in range(0, len(skus), 100):
                res = sb.table(tbl).select("marketplace_sku").eq(col, dte).eq("marketplace_code", "ozon").in_("marketplace_sku", skus[i:i + 100]).order("marketplace_sku").limit(1000).execute()
                if len(res.data) >= 1000:
                    raise SystemExit(f"{tbl} {dte}: 1000 строк в ответе — страница обрезана, проверка недостоверна")
                for r in res.data:
                    found[r["marketplace_sku"]].add(tbl)
        for i in range(0, len(skus), 100):
            res = sb.table("daily_sku_kpi").select("marketplace_sku,orders_qty,orders_amount_seller").eq("kpi_date", dte).eq("marketplace_code", "ozon").in_("marketplace_sku", skus[i:i + 100]).order("marketplace_sku").execute()
            for r in res.data:
                (kept if found.get(r["marketplace_sku"]) else stale).append((dte, r["marketplace_sku"], D(r["orders_qty"]), D(r["orders_amount_seller"])))
    for label, keys in (("останутся в витрине НАВСЕГДА со старыми заказами (нет ни выкупов, ни расходов, ни органики — ночной KPI ключ не построит, upsert не удалит) — под чистку", stale),
                        ("ночной KPI перепишет сам, заказы обнулит (у ключа есть выкупы / расходы / органика)", kept)):
        by = defaultdict(lambda: [0, Z, Z])
        for dte, _sku, q, amt in keys:
            a = by[period_of(dte, boundary)]
            a[0] += 1; a[1] += q; a[2] += amt
        print(f"  {label}: {len(keys)} ключей, заказов в них {sum((k[2] for k in keys), Z):,.0f} шт на {sum((k[3] for k in keys), Z):,.2f}")
        for p in sorted(by):
            print(f"      {p:14} ключей {by[p][0]:>6}  {by[p][1]:>8,.0f} шт  {by[p][2]:>15,.2f}")
    if args.dump_keys:
        path = args.dump_keys.replace(".json", "") + "_kpi_stale.json"
        json.dump([[k[0], k[1], str(k[2]), str(k[3])] for k in stale], open(path, "w"), ensure_ascii=False)
        print(f"  ключи под чистку → {path}")
    print("  daily_marketplace_kpi: ключ (дата, площадка) — дат без заказов не возникает, под чистку 0; суммы пересоберёт ночной KPI из daily_sku_kpi")
    print("\ndb_writes = 0")


if __name__ == "__main__":
    main()
