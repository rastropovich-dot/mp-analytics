#!/usr/bin/env python3
"""Проверка до записи: свежий сбор за окно, строки по новому правилу, сравнение с таблицей. db_writes = 0.

    venv/bin/python3 scripts/ozon_orders_precheck.py --scheme fbo                 # сбор /v3/posting/fbo/list, 30 дней
    venv/bin/python3 scripts/ozon_orders_precheck.py --scheme fbs --raw data/postings_raw/precheck_fbs_<UTC>.json

Печатает: отправлений всего / подтверждённых / отменённых / родителей
разделённых / без даты и проверку, что сумма сходится с общим числом; по датам
— подтверждённые, отменённые и созданные; сравнение с marketplace_orders по
ключу (дата, sku) за полные дни окна:

  FBO: таблица против ПОДТВЕРЖДЁННЫХ. Должны совпасть, кроме (а) ключей, которых
       свежий сбор не вернул — устаревшие, и (б) ключей, где отмены дозрели
       после последней ночной записи (в таблице больше, чем подтверждено).
  FBS: таблица против СОЗДАННЫХ (подтверждённые + отменённые): старый загрузчик
       писал все статусы, поэтому совпадать должна сумма пар, а не старая
       колонка — разница с подтверждёнными и есть доля отменённых внутри строк.
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from loaders import http_retry  # noqa: E402
from loaders import ozon_orders_rows as rules  # noqa: E402

RAW_DIR = os.path.join("data", "postings_raw")
D = lambda v: Decimal(str(v or 0))  # noqa: E731


def fetch(scheme, days_back):
    calls = {"n": 0, "429": 0}
    orig = http_retry.post

    def counted(*a, **kw):
        calls["n"] += 1
        stats = kw.setdefault("stats", {})        # повторы внутри http_retry тоже 429 — считаем и их
        r = orig(*a, **kw)
        calls["429"] += http_retry.count_429(stats, r)
        return r
    http_retry.post = counted
    to = datetime.now(timezone.utc).replace(microsecond=0)
    since = to - timedelta(days=days_back)
    t0 = time.monotonic()
    if scheme == "fbo":
        import loaders.ozon_fbo_orders_loader as fbo
        postings = fbo.get_fbo_postings(days_back=days_back)
    else:
        import loaders.ozon_fbs_orders_loader as fbs
        postings = fbs.get_ozon_fbs_postings(since=since, to=to)
    http_retry.post = orig
    window = {"since": since.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "to": to.strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    os.makedirs(RAW_DIR, exist_ok=True)
    path = os.path.join(RAW_DIR, f"precheck_{scheme}_{to.strftime('%Y%m%dT%H%M%SZ')}.json")
    json.dump({"schema": scheme, "fetched_at": to.isoformat(), "window": window, "postings": postings}, open(path, "w"), ensure_ascii=False)
    print(f"сбор {scheme}: {len(postings)} отправлений, обращений {calls['n']}, 429 — {calls['429']}, {time.monotonic() - t0:.0f} с; окно {window['since']} … {window['to']} → {path}")
    return postings, window


def table_rows(scheme, date_from, date_to):
    import loaders.ozon_fbo_orders_loader as fbo
    out, page = [], 0
    while True:
        res = (fbo.supabase.table("marketplace_orders")
               .select("order_date,marketplace_sku,orders_qty,orders_amount_seller")
               .eq("marketplace_code", "ozon").eq("order_schema", scheme)
               .gte("order_date", date_from).lte("order_date", date_to)
               .order("id").range(page * 1000, page * 1000 + 999).execute())
        out.extend(res.data)
        if len(res.data) < 1000:
            break
        page += 1
    return {(r["order_date"], str(r["marketplace_sku"])): (D(r["orders_qty"]), D(r["orders_amount_seller"])) for r in out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scheme", choices=["fbo", "fbs"], required=True)
    ap.add_argument("--days-back", type=int, default=30)
    ap.add_argument("--raw", help="сохранённый сбор вместо нового обращения к API")
    ap.add_argument("--no-db", action="store_true")
    args = ap.parse_args()
    if args.raw:
        data = json.load(open(args.raw))
        postings, window = data["postings"], data.get("window")
        print(f"сырьё {args.raw}: {len(postings)} отправлений, окно {window}")
    else:
        postings, window = fetch(args.scheme, args.days_back)

    rows, c = rules.build_order_rows(postings, args.scheme, observed_at="precheck")
    rules.print_counters(args.scheme, c)
    total = len(postings)
    accounted = c.get("confirmed_postings", 0) + c.get("cancelled_postings", 0) + c.get("split_parent_skipped", 0) + c.get("no_date", 0)
    print(f"сумма: подтверждённых {c.get('confirmed_postings', 0)} + отменённых {c.get('cancelled_postings', 0)} "
          f"+ родителей {c.get('split_parent_skipped', 0)} + без даты {c.get('no_date', 0)} = {accounted} "
          f"{'=' if accounted == total else '≠'} всего {total}")
    from collections import Counter
    print("статусы:", dict(Counter(p.get("status") for p in postings)))

    per = defaultdict(lambda: [Decimal(0)] * 4)
    for r in rows:
        a = per[r["order_date"]]
        a[0] += D(r["orders_qty"]); a[1] += D(r["orders_amount_seller"]); a[2] += D(r["cancelled_orders_qty"]); a[3] += D(r["cancelled_orders_amount_seller"])
    print(f"\n{'дата':12}{'подтв. шт':>10}{'подтв. ₽':>15}{'отм. шт':>9}{'отм. ₽':>15}{'создано ₽':>15}{'доля отм.':>10}")
    T = [Decimal(0)] * 4
    for d in sorted(per):
        a = per[d]; created = a[1] + a[3]
        print(f"{d:12}{a[0]:>10}{a[1]:>15,.2f}{a[2]:>9}{a[3]:>15,.2f}{created:>15,.2f}{(a[3] / created * 100 if created else 0):>9.1f} %")
        for i in range(4):
            T[i] += a[i]
    created = T[1] + T[3]
    print(f"{'итого':12}{T[0]:>10}{T[1]:>15,.2f}{T[2]:>9}{T[3]:>15,.2f}{created:>15,.2f}{(T[3] / created * 100 if created else 0):>9.1f} %")

    if args.no_db or not window:
        print("db_writes = 0")
        return
    first = rules.to_local_order_date(window["since"])
    last = rules.to_local_order_date(window["to"])
    date_from = (datetime.fromisoformat(first) + timedelta(days=1)).date().isoformat()  # первый локальный день окна неполон
    tbl = table_rows(args.scheme, date_from, last)
    api = {(r["order_date"], r["marketplace_sku"]): r for r in rows if date_from <= r["order_date"] <= last}
    print(f"\nтаблица marketplace_orders {args.scheme} за {date_from} … {last}: ключей {len(tbl)}; из сбора: {len(api)}")
    for label, take in (("подтверждённые (orders_*)", lambda r: (D(r["orders_qty"]), D(r["orders_amount_seller"]))),
                        ("созданные (orders_* + cancelled_*)", lambda r: (D(r["orders_qty"]) + D(r["cancelled_orders_qty"]), D(r["orders_amount_seller"]) + D(r["cancelled_orders_amount_seller"])))):
        same = diff_more = diff_less = 0
        only_db = only_api = 0
        s_only_db = s_more = s_less = Decimal(0)
        for k in set(tbl) | set(api):
            if k in tbl and k in api:
                tq, ta = tbl[k]; aq, aa = take(api[k])
                if tq == aq and abs(ta - aa) <= Decimal("0.01"):
                    same += 1
                elif ta > aa:
                    diff_more += 1; s_more += ta - aa
                else:
                    diff_less += 1; s_less += aa - ta
            elif k in tbl:
                only_db += 1; s_only_db += tbl[k][1]
            else:
                only_api += 1
        print(f"  таблица против {label}: совпало {same}, в таблице больше {diff_more} (на {s_more:,.2f}), "
              f"в таблице меньше {diff_less} (на {s_less:,.2f}), только в таблице {only_db} (на {s_only_db:,.2f}), только в сборе {only_api}")
    print("db_writes = 0")


if __name__ == "__main__":
    main()
