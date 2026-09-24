#!/usr/bin/env python3
"""Глубокая проверка застрявших ключей — таблицы против сырья by-day на диске за всю глубину (тридцать седьмая §5).

    venv/bin/python3 scripts/stale_keys_deep_check.py --date-from 2026-03-28 --date-to 2026-09-22            план, обращений 0
    venv/bin/python3 scripts/stale_keys_deep_check.py … --fetch-missing                                       дни без файла — живым by-day
    venv/bin/python3 scripts/stale_keys_deep_check.py … --apply --approve-stale-deep-delete                   удаление по слову

Класс дефекта: Ozon убирает начисления задним числом и дальше 30 дней (11 ключей выкупов 08-19 … 08-23 нашлись 24.09), а ночной
механизм (loaders/stale_keys.py) видит только окно ночи. Здесь та же логика — «ключ есть в таблице, среди построенных из сырья нет —
застрял», — но на всю глубину data/accrual_history/ (файл на день, с 03-28), тремя таблицами:

    marketplace_buyouts          ключ (buyout_date, sku)                       строится accrual.build_buyout_rows
    marketplace_expenses         ключ (expense_date, sku, expense_type)         accrual.build_expense_rows текущей свёрткой;
                                 реклама Performance (advertising*) не из сырья — не сравнивается
    ozon_accrual_daily_types     ключ (accrual_date, type_id)                   accrual.build_type_ledger_rows

День без файла сырья не сравнивается и называется (--fetch-missing — снять живым by-day, ~2 обращения на день, цена печатается).
День моложе двух суток не сравнивается: начисления доезжают (CLAUDE.md §2). План — таблицей по месяцам (ключей, ₽) и полным списком
в data/snapshots/stale_deep_plan_<UTC>.json. Расходы в окне ночи (последние 31 день) могут быть старой свёрткой до её первой ночи —
ключи с другой статьёй попадут в план как застрявшие; их снимет сама ночь (порог механизма 200), в плане они помечены.

--apply (по слову владельца): каждый затронутый день перепроверяется ЖИВЫМ by-day (не файлом) — удаляется только то, чего свежий
полный сбор дня не строит; сбор с повторами/отказами — стоп; снимок удаляемых строк → data/snapshots/stale_deep_<UTC>.json → delete
по id (выкупы, расходы) и по (дата, тип) (леджер) → контроль «осталось 0». Не в окнах ночного прогона и утреннего алерта.
Раз в неделю — предложение в отчёте, не шаг конвейера.
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
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from loaders import ozon_finance_accrual as accrual  # noqa: E402
from loaders import stale_keys  # noqa: E402
from loaders.pipeline_window import in_morning_alert_window, in_nightly_run_window  # noqa: E402

RAW_DIR = os.path.join(ROOT, "data", "accrual_history")
SNAP_DIR = os.path.join(ROOT, "data", "snapshots")
TYPES_JSON = os.path.join(ROOT, "knowledge", "ozon", "accrual_types_2026-09-23.json")
YOUNG_DAYS = 2
NIGHT_WINDOW_DAYS = 31
D = lambda v: Decimal(str(v or 0))  # noqa: E731

TABLES = {
    "buyouts": {"table": "marketplace_buyouts", "date": "buyout_date", "money": "buyouts_amount_seller",
                "select": "id,buyout_date,marketplace_code,marketplace_sku,buyouts_amount_seller,commission_amount,created_at",
                "order": ["buyout_date", "marketplace_code", "marketplace_sku"], "ozon_only": True,
                "key": lambda r: (r["buyout_date"], str(r["marketplace_sku"] or "")), "delete": "id"},
    "expenses": {"table": "marketplace_expenses", "date": "expense_date", "money": "expense_amount",
                 "select": "id,expense_date,marketplace_code,marketplace_sku,expense_type,expense_amount,created_at",
                 "order": ["expense_date", "marketplace_code", "marketplace_sku", "expense_type"], "ozon_only": True,
                 "key": lambda r: (r["expense_date"], str(r["marketplace_sku"] or ""), r["expense_type"]), "delete": "id",
                 "skip": lambda r: str(r["expense_type"] or "").startswith("advertising")},
    "ledger": {"table": "ozon_accrual_daily_types", "date": "accrual_date", "money": "amount",
               "select": "accrual_date,type_id,amount", "order": ["accrual_date", "type_id"], "ozon_only": False,
               "key": lambda r: (r["accrual_date"], int(r["type_id"])), "delete": ("accrual_date", "type_id")},
}


def days_between(d1, d2):
    d, out = date.fromisoformat(d1), []
    while d <= date.fromisoformat(d2):
        out.append(d.isoformat()); d += timedelta(days=1)
    return out


def type_names():
    if os.path.exists(TYPES_JSON):
        return {t["id"]: t.get("name") or "" for t in json.load(open(TYPES_JSON))}
    return {}


def built_keys_for_day(day, accruals, names):
    """{таблица: множество ключей дня из сырья}."""
    buyouts, _c = accrual.build_buyout_rows(accruals)
    expenses, _c2, _u = accrual.build_expense_rows(accruals, names)
    ledger = accrual.build_type_ledger_rows(accruals, names)
    return {"buyouts": {(day, str(r["marketplace_sku"])) for r in buyouts if r["buyout_date"] == day},
            "expenses": {(day, str(r["marketplace_sku"] or ""), r["expense_type"]) for r in expenses if r["expense_date"] == day},
            "ledger": {(day, int(r["type_id"])) for r in ledger if r["accrual_date"] == day}}


def load_raw_day(day):
    path = os.path.join(RAW_DIR, f"{day}.json")
    if not os.path.exists(path):
        return None
    data = json.load(open(path))
    return data["accruals"] if isinstance(data, dict) else data


def read_table(sb, spec, d1, d2):
    filters = ([("eq", "marketplace_code", "ozon")] if spec["ozon_only"] else []) + [("gte", spec["date"], d1), ("lte", spec["date"], d2)]
    rows = stale_keys.read_window_rows(sb, spec["table"], spec["select"], filters, spec["order"])
    skip = spec.get("skip")
    return [r for r in rows if not (skip and skip(r))]


def deep_plan(tables_rows, built_by_day, days_with_raw, today, night_from):
    """{таблица: [застрявшие строки с пометками]} и счётчики. Сравниваются только дни с сырьём и старше двух суток."""
    young_from = (date.fromisoformat(today) - timedelta(days=YOUNG_DAYS)).isoformat()
    plan, counters = {}, defaultdict(int)
    for name, rows in tables_rows.items():
        spec = TABLES[name]
        out = []
        for r in rows:
            day = r[spec["date"]]
            if day >= young_from:
                counters[f"{name}:young_rows"] += 1; continue
            if day not in days_with_raw:
                counters[f"{name}:rows_without_raw"] += 1; continue
            if spec["key"](r) in built_by_day[day][name]:
                continue
            out.append(dict(r, _table=spec["table"], _money=str(D(r[spec["money"]])), _in_night_window=day >= night_from))
        plan[name] = out
    return plan, dict(counters)


def print_plan(plan, counters, days, days_with_raw, missing_days):
    print(f"дней в окне {len(days)}, с сырьём {len(days_with_raw)}, без файла {len(missing_days)}" + (f": {', '.join(missing_days[:12])}{' …' if len(missing_days) > 12 else ''}" if missing_days else ""))
    for name, rows in plan.items():
        spec = TABLES[name]
        by_month = defaultdict(lambda: [0, Decimal(0), 0])
        for r in rows:
            m = by_month[r[spec["date"]][:7]]
            m[0] += 1; m[1] += D(r[spec["money"]]); m[2] += 1 if r["_in_night_window"] else 0
        total = sum((D(r[spec["money"]]) for r in rows), Decimal(0))
        print(f"\n{spec['table']}: застрявших ключей {len(rows)} на {total:,.2f} ₽ (в окне ночи {sum(1 for r in rows if r['_in_night_window'])} — их снимет ночь); "
              f"строк моложе двух суток пропущено {counters.get(name + ':young_rows', 0)}, дней без сырья {counters.get(name + ':rows_without_raw', 0)} строк")
        for m in sorted(by_month):
            n, amt, night = by_month[m]
            print(f"   {m}: ключей {n:>5}, ₽ {amt:>16,.2f}" + (f", в окне ночи {night}" if night else ""))
        for r in rows[:6]:
            key = spec["key"](r)
            print(f"      например {key} — {D(r[spec['money']]):,.2f}" + (f", создана {str(r.get('created_at'))[:19]}" if r.get("created_at") else ""))


def verify_live(plan, stats):
    """Перепроверка каждого затронутого дня живым by-day: ключ удаляется, только если свежий сбор дня его не строит."""
    names = type_names()
    days = sorted({r[TABLES[n]["date"]] for n, rows in plan.items() for r in rows})
    confirmed = {n: [] for n in plan}
    kept = 0
    for day in days:
        accruals = accrual.fetch_day(day, stats)
        built = built_keys_for_day(day, accruals, names)
        for n, rows in plan.items():
            for r in rows:
                if r[TABLES[n]["date"]] != day:
                    continue
                if TABLES[n]["key"](r) in built[n]:
                    kept += 1
                else:
                    confirmed[n].append(r)
    return confirmed, kept, len(days)


def apply(sb, plan, stats_out):
    now = datetime.now(timezone.utc)
    if in_nightly_run_window(now) or in_morning_alert_window(now):
        raise SystemExit("окно ночного прогона или утреннего алерта — не удаляю")
    stats = {}
    confirmed, kept, ndays = verify_live(plan, stats)
    print(f"перепроверка живым by-day: дней {ndays}, обращений {stats.get('requests', 0)}, повторов {stats.get('retries', 0)}, отказов {stats.get('failures', 0)}; "
          f"подтверждено к удалению {sum(len(v) for v in confirmed.values())}, оставлено (свежий сбор строит ключ) {kept}")
    if stats.get("retries") or stats.get("failures"):
        raise SystemExit("сбор был не гладким (повторы/отказы) — не удаляю")
    os.makedirs(SNAP_DIR, exist_ok=True)
    snap = os.path.join(SNAP_DIR, f"stale_deep_{now.strftime('%Y%m%dT%H%M%SZ')}.json")
    json.dump({n: rows for n, rows in confirmed.items()}, open(snap, "w"), ensure_ascii=False, default=str)
    print(f"снимок → {snap}")
    total = 0
    for n, rows in confirmed.items():
        if not rows:
            continue
        spec = TABLES[n]
        if spec["delete"] == "id":
            deleted = stale_keys.delete_by_id(sb, spec["table"], rows)
            left = len(sb.table(spec["table"]).select("id").in_("id", [r["id"] for r in rows]).execute().data or [])
        else:
            dc, col = spec["delete"]
            deleted = stale_keys.delete_by_date_and_column(sb, spec["table"], rows, dc, col)
            left = 0
            for r in rows:
                left += len(sb.table(spec["table"]).select(col).eq(dc, r[dc]).eq(col, r[col]).execute().data or [])
        print(f"{spec['table']}: удалено {deleted} из {len(rows)}, осталось {left} {'=' if left == 0 and deleted == len(rows) else '≠ ОШИБКА'}")
        total += deleted
    stats_out["db_writes"] = total
    print(f"db_writes = {total}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", default="2026-03-28"); ap.add_argument("--date-to", default=(date.today() - timedelta(days=3)).isoformat())
    ap.add_argument("--tables", default="buyouts,expenses,ledger")
    ap.add_argument("--fetch-missing", action="store_true", help="дни без файла сырья снять живым by-day (в файл не пишет)")
    ap.add_argument("--plan-out", help="куда положить план (по умолчанию data/snapshots/stale_deep_plan_<UTC>.json)")
    ap.add_argument("--apply", action="store_true"); ap.add_argument("--approve-stale-deep-delete", action="store_true")
    args = ap.parse_args(argv)
    if args.apply and not args.approve_stale_deep_delete:
        raise SystemExit("--apply требует --approve-stale-deep-delete (слово владельца по плану)")
    today = date.today().isoformat()
    days = days_between(args.date_from, args.date_to)
    names = type_names()
    built_by_day, missing = {}, []
    stats = {}
    for day in days:
        accruals = load_raw_day(day)
        if accruals is None:
            if args.fetch_missing:
                accruals = accrual.fetch_day(day, stats)
            else:
                missing.append(day); continue
        built_by_day[day] = built_keys_for_day(day, accruals, names)
    if missing and not args.fetch_missing:
        print(f"дней без файла сырья {len(missing)} — не сравниваются; снять живым: --fetch-missing, ~{2 * len(missing)} обращений")
    elif args.fetch_missing:
        print(f"живой by-day за дни без файла: обращений {stats.get('requests', 0)}, повторов {stats.get('retries', 0)}, отказов {stats.get('failures', 0)}")
    from loaders.ozon_fbo_orders_loader import supabase as sb
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    rows = {n: read_table(sb, TABLES[n], args.date_from, args.date_to) for n in tables}
    print("строк в таблицах за окно: " + ", ".join(f"{TABLES[n]['table']} {len(v)}" for n, v in rows.items()))
    night_from = (date.today() - timedelta(days=NIGHT_WINDOW_DAYS)).isoformat()
    plan, counters = deep_plan(rows, built_by_day, set(built_by_day), today, night_from)
    print_plan(plan, counters, days, set(built_by_day), missing)
    os.makedirs(SNAP_DIR, exist_ok=True)
    out = args.plan_out or os.path.join(SNAP_DIR, f"stale_deep_plan_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
    json.dump({"window": [args.date_from, args.date_to], "today": today, "missing_days": missing, "plan": plan}, open(out, "w"), ensure_ascii=False, default=str)
    print(f"план → {out}")
    if args.apply:
        stats_out = {}
        apply(sb, plan, stats_out)
    else:
        n_days = len({r[TABLES[n]['date']] for n, rs in plan.items() for r in rs})
        print(f"db_writes = 0 (удаление — --apply --approve-stale-deep-delete по слову; перепроверка живым by-day ~{2 * n_days} обращений на {n_days} дн.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
