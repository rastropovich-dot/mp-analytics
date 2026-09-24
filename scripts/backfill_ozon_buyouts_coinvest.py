#!/usr/bin/env python3
"""Соинвест по выкупам Ozon за историю — план из сырья by-day на диске, запись по слову владельца.

    venv/bin/python3 scripts/backfill_ozon_buyouts_coinvest.py --date-from 2026-03-28 --date-to 2026-09-23
    venv/bin/python3 scripts/backfill_ozon_buyouts_coinvest.py --date-from … --date-to … --apply --approve-ozon-buyouts-coinvest-write

Что пишется (три колонки marketplace_buyouts, остальное не трогается): buyouts_amount_buyer = Σ commission.sale_price
(оплачено покупателем, за строку), bonus_amount = Σ bonus (баллы за скидки), coinvestment_amount = Σ coinvestment (зелёные
цены) — тем же правилом, что ночью (loaders.ozon_finance_accrual.build_buyout_rows), из data/accrual_history/<день>.json.
Тождество построчно seller − buyer = bonus + coinvestment — счётчик coinvest_identity_broken по строкам сырья, печатается.

Переписываются только ключи (дата, ozon, sku), у которых buyouts_amount_seller и commission_amount совпали с таблицей до
копейки (файл дня и таблица описывают одно состояние); разошлось — пропуск со счётом (начисления доехали после файла —
переснимать день, не подгонять). Дня без файла — нет в плане, он назван. Без колонок bonus_amount / coinvestment_amount
(миграция sql/20260924_add_buyouts_bonus_coinvestment.sql не применена) план печатается, --apply отказывает.

--apply (только по слову владельца, после плана; отказ в окне ночного прогона и утреннего алерта): снимок старых значений
трёх колонок по id → upsert по ключу (buyout_date, marketplace_code, marketplace_sku) с полезной нагрузкой из ключа и трёх
колонок → контроль чтением. Без --apply не пишет ничего; в API не ходит.
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
from loaders.pipeline_window import in_morning_alert_window, in_nightly_run_window, window_text  # noqa: E402

TABLE = "marketplace_buyouts"
KEY = ("buyout_date", "marketplace_code", "marketplace_sku")
COLS = ("buyouts_amount_buyer", "bonus_amount", "coinvestment_amount")
GUARD = ("buyouts_amount_seller", "commission_amount")
RAW_DIR = os.path.join(ROOT, "data", "accrual_history")
SNAP_DIR = os.path.join(ROOT, "data", "snapshots")
C = Decimal("0.01")
Z = Decimal(0)


def q(v):
    return None if v is None else Decimal(str(v)).quantize(C)


def key_of(r):
    return tuple(str(r[k]) for k in KEY)


def days_between(d1, d2):
    a, b = date.fromisoformat(d1), date.fromisoformat(d2)
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def build(days):
    """Строки выкупов по файлам дней тем же правилом, что ночью. Возвращает (строки, дни без файла, счётчики по месяцам)."""
    rows, missing = [], []
    identity = defaultdict(int)
    for day in days:
        path = os.path.join(RAW_DIR, f"{day}.json")
        if not os.path.exists(path):
            missing.append(day); continue
        data = json.load(open(path))
        built, counters = accrual.build_buyout_rows(data["accruals"] if isinstance(data, dict) else data)
        foreign = sorted({r["buyout_date"] for r in built} - {day})
        if foreign:
            raise SystemExit(f"{path}: строки с датами {foreign} — файл не того дня, стоп")
        identity[day[:7]] += counters.get("coinvest_identity_broken", 0)
        rows.extend(built)
    return rows, missing, dict(identity)


def sb():
    import loaders.ozon_fbo_orders_loader as fbo   # тот же клиент, что у ночных шагов и генератора
    return fbo.supabase


def read_existing(client, d1, d2, with_cols):
    """Таблица за окно помесячно (statement_timeout 8 с), с сортировкой по ключу. with_cols=False — без колонок соинвеста."""
    select = "id," + ",".join(KEY + GUARD + (COLS if with_cols else COLS[:1]))
    out = []
    for m1, m2 in month_ranges(d1, d2):
        page = 0
        while True:
            res = (client.table(TABLE).select(select).eq("marketplace_code", "ozon").gte("buyout_date", m1).lte("buyout_date", m2)
                   .order("buyout_date").order("marketplace_code").order("marketplace_sku").range(page * 1000, page * 1000 + 999).execute())
            out.extend(res.data)
            if len(res.data) < 1000:
                break
            page += 1
    return out


def month_ranges(d1, d2):
    out, cur, last = [], date.fromisoformat(d1), date.fromisoformat(d2)
    while cur <= last:
        end = (cur.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        out.append((cur.isoformat(), min(end, last).isoformat()))
        cur = end + timedelta(days=1)
    return out


def columns_present(client):
    try:
        client.table(TABLE).select(",".join(COLS)).limit(1).execute()
        return True
    except Exception as exc:
        if any(c in str(exc) for c in COLS[1:]):
            return False
        raise


def plan(existing, built, with_cols):
    table = {key_of(r): r for r in existing}
    updates, skipped = [], defaultdict(list)
    months = defaultdict(lambda: defaultdict(Decimal))
    seen = set()
    for r in built:
        k = key_of(r); seen.add(k); m = r["buyout_date"][:7]
        old = table.get(k)
        months[m]["built"] += 1
        if old is None:
            skipped["нет в таблице"].append(k); months[m]["not_in_table"] += 1; continue
        if any(q(old[g]) != q(r[g]) for g in GUARD):
            skipped["оборот или комиссия разошлись с таблицей (начисления доехали после файла)"].append(k); months[m]["mismatch"] += 1; continue
        months[m]["planned"] += 1
        months[m]["seller"] += Decimal(str(r["buyouts_amount_seller"])); months[m]["buyer"] += Decimal(str(r["buyouts_amount_buyer"]))
        months[m]["bonus"] += Decimal(str(r["bonus_amount"])); months[m]["coinvest"] += Decimal(str(r["coinvestment_amount"]))
        if q(old["buyouts_amount_buyer"]) == q(old["buyouts_amount_seller"]):
            months[m]["old_dup"] += 1
        new_vals = tuple(q(r[c]) for c in COLS)
        old_vals = tuple(q(old.get(c)) for c in COLS) if with_cols else (q(old["buyouts_amount_buyer"]), None, None)
        if new_vals == old_vals:
            months[m]["already_equal"] += 1; continue
        updates.append({"id": old["id"], **{k2: r[k2] for k2 in KEY}, **{c: r[c] for c in COLS}, "_old": {c: old.get(c) for c in COLS}})
    for k, old in table.items():
        if k not in seen:
            skipped["в таблице, в сырье нет (устаревший ключ или день без файла)"].append(k); months[old["buyout_date"][:7]]["table_only"] += 1
    return {"updates": updates, "skipped": dict(skipped), "months": months, "table_rows": len(table)}


def print_plan(p, missing_days, identity):
    print(f"{'месяц':8}{'в табл.':>8}{'из сырья':>9}{'план':>7}{'уже так':>8}{'дубль':>7}{'уехало':>7}{'нет в табл.':>12}"
          f"{'оборот (seller)':>18}{'оплачено (buyer)':>18}{'баллы':>15}{'зелёные цены':>15}{'тожд. нарушено':>15}")
    tot = defaultdict(Decimal)
    for m in sorted(p["months"]):
        a = p["months"][m]
        in_table = int(a["built"] - a["not_in_table"] + a["table_only"])
        print(f"{m:8}{in_table:>8}{int(a['built']):>9}{int(a['planned']):>7}{int(a['already_equal']):>8}{int(a['old_dup']):>7}{int(a['mismatch']):>7}"
              f"{int(a['not_in_table']):>12}{a['seller']:>18,.2f}{a['buyer']:>18,.2f}{a['bonus']:>15,.2f}{a['coinvest']:>15,.2f}{identity.get(m, 0):>15}")
        for k in ("built", "planned", "already_equal", "old_dup", "mismatch", "not_in_table", "table_only", "seller", "buyer", "bonus", "coinvest"):
            tot[k] += a[k]
    gap = tot["seller"] - tot["buyer"] - tot["bonus"] - tot["coinvest"]
    print(f"{'итого':8}{p['table_rows']:>8}{int(tot['built']):>9}{int(tot['planned']):>7}{int(tot['already_equal']):>8}{int(tot['old_dup']):>7}{int(tot['mismatch']):>7}"
          f"{int(tot['not_in_table']):>12}{tot['seller']:>18,.2f}{tot['buyer']:>18,.2f}{tot['bonus']:>15,.2f}{tot['coinvest']:>15,.2f}{sum(identity.values()):>15}")
    print(f"тождество по плану: seller − buyer − bonus − coinvestment = {gap:,.2f}"
          + ("" if q(gap) == 0 else " — НЕ НОЛЬ: смотреть строки со счётчиком coinvest_identity_broken по месяцам"))
    print(f"к записи (upsert трёх колонок по ключу): {len(p['updates'])} ключей; в таблице, в сырье нет: {int(tot['table_only'])}"
          + (f"; дней без файла сырья: {len(missing_days)} ({missing_days[0]} … {missing_days[-1]})" if missing_days else ""))
    for why, keys in p["skipped"].items():
        print(f"  пропущено — {why}: {len(keys)}" + (f"; например {keys[:3]}" if keys else ""))


def apply(p, client, with_cols):
    if not with_cols:
        raise SystemExit("в marketplace_buyouts нет колонок bonus_amount / coinvestment_amount — применить миграцию sql/20260924_add_buyouts_bonus_coinvestment.sql")
    now = datetime.now(timezone.utc)
    if in_nightly_run_window(now) or in_morning_alert_window(now):
        raise SystemExit(f"окно ночного прогона {window_text()} или утреннего алерта — не пишу")
    os.makedirs(SNAP_DIR, exist_ok=True)
    snap = os.path.join(SNAP_DIR, f"buyouts_coinvest_backfill_{now.strftime('%Y%m%dT%H%M%SZ')}.json")
    json.dump({"table": TABLE, "columns": list(COLS), "rows_before": [{"id": u["id"], **{k: u[k] for k in KEY}, **u["_old"]} for u in p["updates"]]},
              open(snap, "w"), ensure_ascii=False, indent=1, default=str)
    print(f"снимок → {snap}")
    payload = [{**{k: u[k] for k in KEY}, **{c: u[c] for c in COLS}} for u in p["updates"]]
    written = 0
    for i in range(0, len(payload), 500):
        client.table(TABLE).upsert(payload[i:i + 500], on_conflict=",".join(KEY)).execute()
        written += len(payload[i:i + 500])
    days = sorted({u["buyout_date"] for u in p["updates"]})
    want = {key_of(u): tuple(q(u[c]) for c in COLS) for u in p["updates"]}
    have = {key_of(r): tuple(q(r.get(c)) for c in COLS) for r in read_existing(client, days[0], days[-1], True)} if days else {}
    wrong = [k for k in want if have.get(k) != want[k]]
    print(f"записано (upsert) {written}; контроль чтением: не совпало с планом {len(wrong)} {'=' if not wrong else '≠ ОШИБКА'}")
    print(f"db_writes = {written}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", required=True); ap.add_argument("--date-to", required=True)
    ap.add_argument("--apply", action="store_true"); ap.add_argument("--approve-ozon-buyouts-coinvest-write", action="store_true")
    args = ap.parse_args(argv)
    if args.apply and not args.approve_ozon_buyouts_coinvest_write:
        raise SystemExit("--apply требует --approve-ozon-buyouts-coinvest-write (слово владельца по плану с числами)")
    days = days_between(args.date_from, args.date_to)
    built, missing, identity = build(days)
    client = sb()
    with_cols = columns_present(client)
    print("колонки bonus_amount / coinvestment_amount в таблице: " + ("есть" if with_cols else "НЕТ — миграция не применена, план сравнивает только buyouts_amount_buyer"))
    existing = read_existing(client, args.date_from, args.date_to, with_cols)
    p = plan(existing, built, with_cols)
    print_plan(p, missing, identity)
    if args.apply:
        apply(p, client, with_cols)
    else:
        print("db_writes = 0 (план; запись — --apply --approve-ozon-buyouts-coinvest-write по слову владельца)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
