#!/usr/bin/env python3
"""Цена покупателя в marketplace_orders (Ozon) за прошедшие дни — план из файлов на диске, запись по слову владельца.

    venv/bin/python3 scripts/backfill_ozon_orders_buyer.py --date-from 2026-09-01 --date-to 2026-09-22
    venv/bin/python3 scripts/backfill_ozon_orders_buyer.py --date-from … --date-to … --apply --approve-ozon-orders-buyer-write

До 2026-09-24 в orders_amount_buyer / cancelled_orders_amount_buyer у Ozon писалась цена продавца (дубль *_amount_seller).
Ночной сбор с мержем coinvest-columns пишет оплачено покупателем × кол-во (FBS — customer_price списка /v4, FBO — отчёт ЛК),
а история остаётся дублем. Этот скрипт строит строки за окно ТЕМ ЖЕ правилом (loaders.ozon_orders_rows.build_order_rows с
buyer_prices) из уже снятых файлов и переписывает в таблице ТОЛЬКО две колонки покупателя — у ключей, где создано (шт и ₽
продавца, подтверждённые и отменённые) совпало с таблицей до копейки. Разошлось — состояние заказов уехало с ночи снятия
файла — ключ пропускается и считается; цены нет хоть у одного товара ключа — ключ не трогается и считается.

Источники (в API не ходит; нет файла — стоп с именем):
  FBS  data/postings_raw/history_fbs_<from>_<to>.json      список /v4 с financial_data.products[].customer_price
  FBO  data/postings_raw/history_fbo_<from>_<to>.json      список /v3 (цены покупателя в нём нет)
       data/ozon_report_postings/fbo_<from>_<to>.csv        отчёт ЛК: «Оплачено покупателем» по (отправление, SKU)
  [FBS data/ozon_report_postings/fbs_<from>_<to>.csv        запас для строк FBS без customer_price, если файл есть]
Границы: файлы сняты по UTC-окну, наша дата заказа — МСК; крайние дни окна в файлах неполны — их ключи не совпадут с
таблицей и будут пропущены (это видно в плане по дню, не чинится подгонкой).

--apply (только по слову владельца, после плана с числами; отказ в окне ночного прогона и утреннего алерта): снимок старых
значений двух колонок по id → upsert по ключу (order_date, marketplace_code, marketplace_sku, order_schema) с полезной
нагрузкой из ключа и двух колонок (PostgREST при конфликте меняет только присланные колонки; ключей вне таблицы в плане
нет по построению) → контроль чтением. Без --apply не пишет ничего.
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
from loaders import ozon_orders_rows as rules  # noqa: E402
from loaders import ozon_postings_report as report  # noqa: E402
from loaders.pipeline_window import in_morning_alert_window, in_nightly_run_window, window_text  # noqa: E402

TABLE = "marketplace_orders"
KEY = ("order_date", "marketplace_code", "marketplace_sku", "order_schema")
BUYER = ("orders_amount_buyer", "cancelled_orders_amount_buyer")
GUARD = ("orders_qty", "orders_amount_seller", "cancelled_orders_qty", "cancelled_orders_amount_seller")
RAW_DIR = os.path.join(ROOT, "data", "postings_raw")
REPORTS_DIR = os.path.join(ROOT, "data", "ozon_report_postings")
SNAP_DIR = os.path.join(ROOT, "data", "snapshots")
C = Decimal("0.01")
Z = Decimal(0)


def D(v):
    return Decimal(str(v)) if v is not None else None


def q(v):
    return None if v is None else Decimal(str(v)).quantize(C)


def key_of(r):
    return tuple(str(r[k]) for k in KEY)


def load_postings(path):
    if not os.path.exists(path):
        raise SystemExit(f"нет файла {path} — снять scripts/rebuild_ozon_orders_history.py --fetch (в API этот скрипт не ходит)")
    data = json.load(open(path))
    if isinstance(data, list):
        return data
    for k in ("postings", "result", "items"):
        if isinstance(data.get(k), list):
            return data[k]
        if isinstance(data.get(k), dict) and isinstance(data[k].get("postings"), list):
            return data[k]["postings"]
    raise SystemExit(f"{path}: не нашёл списка отправлений в файле (ключи {list(data)[:6]})")


def load_report_prices(path, required):
    if not os.path.exists(path):
        if required:
            raise SystemExit(f"нет файла {path} — отчёт ЛК /v1/report/postings/create за окно (в API этот скрипт не ходит)")
        return {}, 0
    return report.parse_report(open(path, encoding="utf-8").read())


def build(date_from, date_to, paths):
    """Строки обеих схем по файлам окна ТЕМ ЖЕ правилом, что ночью. Возвращает (строки, счётчики по схемам)."""
    rows, counters = [], {}
    fbo_prices, fbo_skipped = load_report_prices(paths["fbo_report"], required=True)
    fbs_prices, _ = load_report_prices(paths["fbs_report"], required=False)
    for scheme, prices in (("fbs", fbs_prices), ("fbo", fbo_prices)):
        postings = load_postings(paths[scheme])
        built, c = rules.build_order_rows(postings, scheme, observed_at=None, buyer_prices=prices)
        c["postings"] = len(postings); c["report_prices"] = len(prices)
        if scheme == "fbo":
            c["report_rows_without_price"] = fbo_skipped
        counters[scheme] = c
        rows.extend(r for r in built if date_from <= r["order_date"] <= date_to)
    return rows, counters


def sb():
    import loaders.ozon_fbo_orders_loader as fbo   # тот же клиент, что у ночных шагов и генератора
    return fbo.supabase


def read_existing(client, date_from, date_to):
    out, page = [], 0
    while True:
        res = (client.table(TABLE).select("id," + ",".join(KEY + GUARD + BUYER)).eq("marketplace_code", "ozon")
               .gte("order_date", date_from).lte("order_date", date_to)
               .order("order_date").order("marketplace_code").order("marketplace_sku").order("order_schema")
               .range(page * 1000, page * 1000 + 999).execute())
        out.extend(res.data)
        if len(res.data) < 1000:
            return out
        page += 1


def plan(existing, built):
    """Что переписать: ключи, где создано совпало с таблицей и цена покупателя известна. Остальное — по причинам, с числами."""
    table = {key_of(r): r for r in existing}
    updates, skipped = [], defaultdict(list)
    per_day = defaultdict(lambda: defaultdict(Decimal))
    seen = set()
    for r in built:
        k = key_of(r)
        seen.add(k)
        old = table.get(k)
        d = r["order_date"]
        per_day[d]["built"] += 1
        if old is None:
            skipped["нет в таблице"].append(k); per_day[d]["not_in_table"] += 1; continue
        if any(q(old[g]) != q(r[g]) for g in GUARD):
            skipped["создано разошлось с таблицей (состояние уехало)"].append(k); per_day[d]["mismatch"] += 1; continue
        if any(r[b] is None for b in BUYER):
            skipped["цена покупателя неизвестна"].append(k); per_day[d]["no_price"] += 1; continue
        per_day[d]["planned"] += 1
        per_day[d]["created_seller"] += D(r["orders_amount_seller"]) + D(r["cancelled_orders_amount_seller"])
        per_day[d]["created_buyer"] += D(r["orders_amount_buyer"]) + D(r["cancelled_orders_amount_buyer"])
        if all(q(old[b]) == q(r[b]) for b in BUYER):
            per_day[d]["already_equal"] += 1
            continue                                   # уже так — писать нечего
        if all(q(old[b]) == q(old[g]) for b, g in zip(BUYER, ("orders_amount_seller", "cancelled_orders_amount_seller"))):
            per_day[d]["old_dup"] += 1                 # старая запись: покупатель = продавец
        updates.append({"id": old["id"], **{k2: r[k2] for k2 in KEY}, **{b: r[b] for b in BUYER}, "_old": {b: old[b] for b in BUYER}})
    for k, old in table.items():
        if k not in seen:
            skipped["в таблице, в файлах нет (вне окна файлов или ключ устарел)"].append(k); per_day[old["order_date"]]["table_only"] += 1
    return {"updates": updates, "skipped": dict(skipped), "per_day": per_day, "table_rows": len(table)}


def print_plan(p, counters):
    for scheme, c in counters.items():
        print(f"{scheme.upper()}: отправлений в файле {c['postings']}, строк {c['rows']}, цена покупателя известна у {c.get('buyer_price_known_products', 0)} товаров, "
              f"неизвестна у {c.get('buyer_price_unknown_products', 0)}; цен из отчёта ЛК {c['report_prices']}"
              + (f", строк отчёта без цены {c['report_rows_without_price']}" if "report_rows_without_price" in c else ""))
    print(f"\n{'день':10}{'в табл.':>8}{'из файлов':>10}{'план':>7}{'уже так':>8}{'дубль':>7}{'уехало':>7}{'без цены':>9}{'нет в табл.':>12}"
          f"{'создано, ₽ продавца':>21}{'оплачено покупателем':>21}{'соинвест %':>11}")
    tot = defaultdict(Decimal)
    for d in sorted(p["per_day"]):
        a = p["per_day"][d]
        in_table = sum(1 for _k in ()) or int(a["built"] - a["not_in_table"] + a["table_only"])
        share = ((a["created_seller"] - a["created_buyer"]) / a["created_seller"] * 100) if a["created_seller"] else None
        print(f"{d:10}{in_table:>8}{int(a['built']):>10}{int(a['planned']):>7}{int(a['already_equal']):>8}{int(a['old_dup']):>7}{int(a['mismatch']):>7}"
              f"{int(a['no_price']):>9}{int(a['not_in_table']):>12}{a['created_seller']:>21,.2f}{a['created_buyer']:>21,.2f}"
              f"{'' if share is None else f'{share:.2f}':>11}")
        for k in ("built", "planned", "already_equal", "old_dup", "mismatch", "no_price", "not_in_table", "table_only", "created_seller", "created_buyer"):
            tot[k] += a[k]
    share = ((tot["created_seller"] - tot["created_buyer"]) / tot["created_seller"] * 100) if tot["created_seller"] else None
    print(f"{'итого':10}{p['table_rows']:>8}{int(tot['built']):>10}{int(tot['planned']):>7}{int(tot['already_equal']):>8}{int(tot['old_dup']):>7}{int(tot['mismatch']):>7}"
          f"{int(tot['no_price']):>9}{int(tot['not_in_table']):>12}{tot['created_seller']:>21,.2f}{tot['created_buyer']:>21,.2f}{'' if share is None else f'{share:.2f}':>11}")
    print(f"\nк записи (upsert двух колонок по ключу): {len(p['updates'])} ключей; в таблице, в файлах нет: {int(tot['table_only'])}")
    for why, keys in p["skipped"].items():
        print(f"  пропущено — {why}: {len(keys)}" + (f"; например {keys[:3]}" if keys else ""))


def apply(p, client):
    now = datetime.now(timezone.utc)
    if in_nightly_run_window(now) or in_morning_alert_window(now):
        raise SystemExit(f"окно ночного прогона {window_text()} или утреннего алерта — не пишу")
    os.makedirs(SNAP_DIR, exist_ok=True)
    snap = os.path.join(SNAP_DIR, f"orders_buyer_backfill_{now.strftime('%Y%m%dT%H%M%SZ')}.json")
    json.dump({"table": TABLE, "columns": list(BUYER), "rows_before": [{"id": u["id"], **{k: u[k] for k in KEY}, **u["_old"]} for u in p["updates"]]},
              open(snap, "w"), ensure_ascii=False, indent=1, default=str)
    print(f"снимок → {snap}")
    payload = [{**{k: u[k] for k in KEY}, **{b: u[b] for b in BUYER}} for u in p["updates"]]
    written = 0
    for i in range(0, len(payload), 500):
        client.table(TABLE).upsert(payload[i:i + 500], on_conflict=",".join(KEY)).execute()
        written += len(payload[i:i + 500])
    want = {key_of(u): tuple(q(u[b]) for b in BUYER) for u in p["updates"]}
    days = sorted({u["order_date"] for u in p["updates"]})
    have = {key_of(r): tuple(q(r[b]) for b in BUYER) for r in read_existing(client, days[0], days[-1])} if days else {}
    wrong = [k for k in want if have.get(k) != want[k]]
    print(f"записано (upsert) {written}; контроль чтением: не совпало с планом {len(wrong)} {'=' if not wrong else '≠ ОШИБКА'}")
    print(f"db_writes = {written}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", required=True); ap.add_argument("--date-to", required=True)
    ap.add_argument("--fbs-raw"); ap.add_argument("--fbo-raw"); ap.add_argument("--fbo-report"); ap.add_argument("--fbs-report")
    ap.add_argument("--apply", action="store_true"); ap.add_argument("--approve-ozon-orders-buyer-write", action="store_true")
    args = ap.parse_args(argv)
    if args.apply and not args.approve_ozon_orders_buyer_write:
        raise SystemExit("--apply требует --approve-ozon-orders-buyer-write (слово владельца по плану с числами)")
    d1, d2 = args.date_from, args.date_to
    date.fromisoformat(d1); date.fromisoformat(d2)
    paths = {"fbs": args.fbs_raw or os.path.join(RAW_DIR, f"history_fbs_{d1}_{d2}.json"),
             "fbo": args.fbo_raw or os.path.join(RAW_DIR, f"history_fbo_{d1}_{d2}.json"),
             "fbo_report": args.fbo_report or os.path.join(REPORTS_DIR, f"fbo_{d1}_{d2}.csv"),
             "fbs_report": args.fbs_report or os.path.join(REPORTS_DIR, f"fbs_{d1}_{d2}.csv")}
    built, counters = build(d1, d2, paths)
    client = sb()
    existing = read_existing(client, d1, d2)
    p = plan(existing, built)
    print_plan(p, counters)
    if args.apply:
        apply(p, client)
    else:
        print("db_writes = 0 (план; запись — --apply --approve-ozon-orders-buyer-write по слову владельца)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
