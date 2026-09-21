#!/usr/bin/env python3
"""Перезапись истории marketplace_buyouts из accrual/by-day за окно дат. Запускает владелец.

Зачем: до миграции 2026-09-11 выкупы писал старый /v3/finance/transaction/list. На части дат его
строки расходятся с начислениями по деньгам (07-08: +9 562,00), на многих — та же сумма дня иначе
разложена по SKU, а buyouts_qty у него — штуки, тогда как с 08-18 колонка хранит позиции: ряд
смешанный. Правило то же, что 2026-03-31: снимок → delete за даты → запись строк штатного
build_buyout_rows.

Сырьё — data/accrual_history/<день>.json, в API не ходит; нет файла дня — стоп (перезаписать день,
которого нет в сырье, значит стереть его).

Без --apply — план (db_writes = 0): помесячно Σ таблицы и accrual (оборот, позиции, комиссия), даты с
расхождением по деньгам, ключи (дата, sku) только в таблице / только в accrual / с разными
значениями, и ключи daily_sku_kpi, которые после перезаписи останутся без источника.

--apply: снимок всех строк окна (все колонки) → delete помесячно → запись → сверка «таблица = плану»
по ключам и значениям. Витрины не трогает: их пересоберёт ночной KPI. Штуки (buyouts_units) у
перезаписанных строк пропадают — посев штук запускать ПОСЛЕ. В окне 00:15 … 03:15 UTC и около
07:30 UTC не запускать; --date-from и --date-to обязательны.

    venv/bin/python3 scripts/rewrite_buyouts_history.py --date-from 2026-03-28 --date-to 2026-08-17
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from loaders import ozon_finance_accrual as accrual  # noqa: E402

RAW_DIR = os.path.join("data", "accrual_history")
SNAP_DIR = os.path.join("data", "snapshots")
FIELDS = ("buyouts_qty", "buyouts_amount_buyer", "buyouts_amount_seller", "commission_amount", "revenue_after_commission_vat")
BATCH = 500
C = Decimal("0.01")
Z = Decimal(0)
D = lambda v: Decimal(str(v or 0)).quantize(C)  # noqa: E731
NIGHT = ((0, 15), (3, 15))


def sb():
    import loaders.ozon_fbo_orders_loader as fbo
    return fbo.supabase


def execute(query_factory, attempts=3):
    """Чтение с повтором: PostgREST закрывает HTTP/2-соединение после сотни запросов, и httpx отдаёт это
    исключением посреди серии мелких чтений. Повторяем ТОЛЬКО чтения — запись не повторяется никогда."""
    import httpx
    for attempt in range(1, attempts + 1):
        try:
            return query_factory().execute()
        except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError) as exc:
            if attempt == attempts:
                raise
            print(f"  чтение: {type(exc).__name__}, повтор {attempt}/{attempts - 1}", flush=True)


def days_between(date_from, date_to):
    d, out = date.fromisoformat(date_from), []
    while d <= date.fromisoformat(date_to):
        out.append(d.isoformat()); d += timedelta(days=1)
    return out


def month_ranges(date_from, date_to):
    out, cur, end = [], date.fromisoformat(date_from), date.fromisoformat(date_to)
    while cur <= end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        out.append((cur.isoformat(), min(nxt - timedelta(days=1), end).isoformat()))
        cur = nxt
    return out


def accrual_rows(days):
    rows = []
    for day in days:
        path = os.path.join(RAW_DIR, f"{day}.json")
        if not os.path.exists(path):
            raise SystemExit(f"нет сырья {path} — день нельзя перезаписать из ничего, стоп")
        data = json.load(open(path))
        built, _counters = accrual.build_buyout_rows(data["accruals"] if isinstance(data, dict) else data)
        foreign = sorted({r["buyout_date"] for r in built} - {day})
        if foreign:
            raise SystemExit(f"{path}: строки с датами {foreign} — файл не того дня, стоп")
        rows.extend(built)
    return rows


def read_table(table, date_col, date_from, date_to, select="*"):
    out, last = [], None
    while True:
        q = sb().table(table).select(select).eq("marketplace_code", "ozon").gte(date_col, date_from).lte(date_col, date_to).order("id").limit(1000)
        if last is not None:
            q = q.gt("id", last)
        page = q.execute().data or []
        out.extend(page)
        if len(page) < 1000:
            return out
        last = page[-1]["id"]


def compare(table_rows, new_rows):
    t = {(r["buyout_date"], str(r["marketplace_sku"])): r for r in table_rows}
    n = {(r["buyout_date"], str(r["marketplace_sku"])): r for r in new_rows}
    only_t, only_n = sorted(set(t) - set(n)), sorted(set(n) - set(t))
    differ = {f: [k for k in set(t) & set(n) if D(t[k].get(f)) != D(n[k].get(f))] for f in FIELDS}
    return t, n, only_t, only_n, differ


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", required=True)
    ap.add_argument("--date-to", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    minutes = now.hour * 60 + now.minute
    if args.apply and (NIGHT[0][0] * 60 + NIGHT[0][1] <= minutes <= NIGHT[1][0] * 60 + NIGHT[1][1] or 7 * 60 + 20 <= minutes <= 7 * 60 + 45):
        raise SystemExit("окно ночного прогона или утреннего алерта — не стартую")
    days = days_between(args.date_from, args.date_to)
    new_rows = accrual_rows(days)
    table_rows = read_table("marketplace_buyouts", "buyout_date", args.date_from, args.date_to)
    t, n, only_t, only_n, differ = compare(table_rows, new_rows)

    print(f"окно {args.date_from} … {args.date_to}: дат {len(days)}; строк в таблице {len(t)}, из accrual {len(n)}")
    by = defaultdict(lambda: [Z] * 6)
    for k, r in t.items():
        a = by[k[0][:7]]; a[0] += D(r.get("buyouts_amount_seller")); a[1] += D(r.get("buyouts_qty")); a[2] += D(r.get("commission_amount"))
    for k, r in n.items():
        a = by[k[0][:7]]; a[3] += D(r.get("buyouts_amount_seller")); a[4] += D(r.get("buyouts_qty")); a[5] += D(r.get("commission_amount"))
    print(f"\n{'месяц':9}{'оборот табл.':>18}{'оборот accrual':>18}{'Δ оборот':>14}{'qty табл.':>11}{'позиций':>9}{'Δ qty':>7}{'комиссия табл.':>18}{'комиссия accrual':>18}{'Δ комиссия':>14}")
    tot = [Z] * 6
    for m in sorted(by):
        a = by[m]; tot = [tot[i] + a[i] for i in range(6)]
        print(f"{m:9}{a[0]:>18,.2f}{a[3]:>18,.2f}{a[0] - a[3]:>14,.2f}{a[1]:>11,.0f}{a[4]:>9,.0f}{a[1] - a[4]:>7,.0f}{a[2]:>18,.2f}{a[5]:>18,.2f}{a[2] - a[5]:>14,.2f}")
    a = tot
    print(f"{'итого':9}{a[0]:>18,.2f}{a[3]:>18,.2f}{a[0] - a[3]:>14,.2f}{a[1]:>11,.0f}{a[4]:>9,.0f}{a[1] - a[4]:>7,.0f}{a[2]:>18,.2f}{a[5]:>18,.2f}{a[2] - a[5]:>14,.2f}")
    print("  Δ = таблица − accrual. qty в таблице до 2026-08-17 — штуки старого API, у accrual — позиции: Δ qty — не ошибка, а смена меры.")

    day_t, day_n = defaultdict(Decimal), defaultdict(Decimal)
    for k, r in t.items():
        day_t[k[0]] += D(r.get("buyouts_amount_seller"))
    for k, r in n.items():
        day_n[k[0]] += D(r.get("buyouts_amount_seller"))
    bad_days = [(d, day_t[d], day_n[d]) for d in days if day_t[d] != day_n[d]]
    print(f"\nдат с расхождением оборота дня: {len(bad_days)} из {len(days)}; Σ (таблица − accrual) {sum((x[1] - x[2] for x in bad_days), Z):,.2f}")
    for d, a_, b_ in bad_days:
        print(f"    {d}: таблица {a_:>14,.2f}  accrual {b_:>14,.2f}  Δ {a_ - b_:>12,.2f}")
    zero_t = [k for k in only_t if all(D(t[k].get(f)) == 0 for f in FIELDS)]
    print(f"\nключи (дата, sku): общих {len(set(t) & set(n))}; только в таблице {len(only_t)} (из них с нулями во всех полях {len(zero_t)}, "
          f"с деньгами {len(only_t) - len(zero_t)} на {sum((D(t[k].get('buyouts_amount_seller')) for k in only_t), Z):,.2f}); "
          f"только в accrual {len(only_n)} на {sum((D(n[k].get('buyouts_amount_seller')) for k in only_n), Z):,.2f}")
    for f in FIELDS:
        ks = differ[f]
        print(f"    общие ключи с разным {f}: {len(ks)}" + (f", Σ (таблица − accrual) {sum((D(t[k].get(f)) - D(n[k].get(f)) for k in ks), Z):,.2f}" if ks else ""))

    # ключи витрины, которые останутся без источника: пара (дата, sku) уходит из выкупов, а заказов, расходов и органики у неё нет
    stale, by_date = [], defaultdict(list)
    for d, s in only_t:
        by_date[d].append(s)
    for d, skus in sorted(by_date.items()):
        found = set()
        for table, col in (("marketplace_orders", "order_date"), ("marketplace_expenses", "expense_date"), ("ozon_daily_sku_organic", "sale_date")):
            for i in range(0, len(skus), 100):
                part = skus[i:i + 100]
                res = execute(lambda: sb().table(table).select("marketplace_sku").eq(col, d).eq("marketplace_code", "ozon").in_("marketplace_sku", part).order("marketplace_sku").limit(1000))
                found |= {r["marketplace_sku"] for r in res.data}
        stale += [(d, s) for s in skus if s not in found]
    print(f"\nключей daily_sku_kpi, которые останутся БЕЗ источника после перезаписи (чистить отдельно): {len(stale)}" + (f" — {stale[:6]}" if stale else ""))

    if not args.apply:
        print("\ndb_writes = 0")
        return
    os.makedirs(SNAP_DIR, exist_ok=True)
    snap = os.path.join(SNAP_DIR, f"buyouts_rewrite_{args.date_from}_{args.date_to}_{now.strftime('%Y%m%dT%H%M%SZ')}.json")
    json.dump({"date_from": args.date_from, "date_to": args.date_to, "rows": table_rows}, open(snap, "w"), ensure_ascii=False)
    print(f"\nснимок {len(table_rows)} строк (все колонки) → {snap}")
    for d1, d2 in month_ranges(args.date_from, args.date_to):
        sb().table("marketplace_buyouts").delete().eq("marketplace_code", "ozon").gte("buyout_date", d1).lte("buyout_date", d2).execute()
    left = read_table("marketplace_buyouts", "buyout_date", args.date_from, args.date_to, "id")
    if left:
        raise SystemExit(f"после delete осталось {len(left)} строк — стоп, запись не начата; снимок: {snap}")
    for i in range(0, len(new_rows), BATCH):
        sb().table("marketplace_buyouts").upsert(new_rows[i:i + BATCH], on_conflict="buyout_date,marketplace_code,marketplace_sku").execute()
    after = read_table("marketplace_buyouts", "buyout_date", args.date_from, args.date_to)
    t2, _n2, only_t2, only_n2, differ2 = compare(after, new_rows)
    wrong = sum(len(v) for v in differ2.values())
    print(f"было {len(table_rows)}, удалено, записано {len(new_rows)}, в таблице {len(after)}; только в таблице {len(only_t2)}, только в плане {len(only_n2)}, "
          f"значений не совпало {wrong} {'=' if not only_t2 and not only_n2 and not wrong else '≠ ОШИБКА'}")
    print(f"db_writes: −{len(table_rows)} +{len(new_rows)}")


if __name__ == "__main__":
    main()
