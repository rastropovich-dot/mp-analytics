#!/usr/bin/env python3
"""Пересборка marketplace_expenses (Ozon, статьи начислений) из сырья by-day текущей свёрткой TYPE_TO_EXPENSE.

    venv/bin/python3 scripts/rebuild_ozon_expenses_from_raw.py --plan  --date-from 2026-03-28 --date-to 2026-08-24
    venv/bin/python3 scripts/rebuild_ozon_expenses_from_raw.py --apply --approve-ozon-expenses-write --date-from … --date-to …

Зачем. 2026-09-23 свёртка типов переложена по справочнику владельца (`6fb657b`: 16, 17, 45, 62, 78, 82 → other; 46, 63 →
logistics). Ночь пишет новой свёрткой только окно 31 дня; всё, что раньше, лежит старой — ряд «логистика / прочее»
разорван на границе окна. Сырьё by-day на каждый день с 2026-03-28 лежит в data/accrual_history/ — строки строятся
ТЕМ ЖЕ кодом, что ночной шаг (`loaders.ozon_finance_accrual.build_expense_rows`), не копией.

План (без записи, db_writes = 0): по ключу (expense_date, marketplace_code, marketplace_sku, expense_type) —
добавить / переписать / удалить (ключи старой статьи, которых новой свёрткой нет) / без изменений; по месяцам —
строки и Σ по статьям до → после. КОНТРОЛЬ: Σ всех статей за день до = после на каждом дне — это перекладка между
статьями, не изменение суммы; день, где не так (сырьё дня неполное, начисления доехали или ушли после ночи, когда
день писался), в план НЕ входит и называется с разницей. Реклама (advertising_*) — чужие строки Performance,
чистка их не видит ни в чтении, ни в удалении.

--apply (только по слову владельца, после плана с числами; отказ в окне ночного прогона): снимок затрагиваемых строк
(добавить / переписать / удалить, все колонки) в data/snapshots/expenses_rebuild_<UTC>.json → upsert построенных строк
дней плана → удаление ключей списка по id → контроль «в таблице за окно = плану» по числу строк и Σ по статьям.
Витрина daily_sku_kpi не трогается — пересчитается ночью.
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
from loaders.pipeline_window import in_nightly_run_window, window_text  # noqa: E402

RAW_DIR = os.path.join(ROOT, "data", "accrual_history")
SNAP_DIR = os.path.join(ROOT, "data", "snapshots")
TYPES_JSON = os.path.join(ROOT, "knowledge", "ozon", "accrual_types_2026-09-23.json")
TABLE = "marketplace_expenses"
OWN_ARTICLES_PREFIX_EXCLUDED = "advertising"
Z = Decimal(0)
C = Decimal("0.01")
D = lambda v: Decimal(str(v or 0))  # noqa: E731


def sb():
    import loaders.ozon_fbo_orders_loader as fbo
    return fbo.supabase


def days_between(d1, d2):
    d, out = date.fromisoformat(d1), []
    while d <= date.fromisoformat(d2):
        out.append(d.isoformat()); d += timedelta(days=1)
    return out


def type_names():
    if os.path.exists(TYPES_JSON):
        return {t["id"]: t.get("name") or "" for t in json.load(open(TYPES_JSON))}
    return {}


def build_from_raw(days, names):
    """{день: [строки новой свёрткой]}; дни без файла — отдельным списком."""
    built, missing = {}, []
    for day in days:
        path = os.path.join(RAW_DIR, f"{day}.json")
        if not os.path.exists(path):
            missing.append(day); continue
        data = json.load(open(path))
        accruals = data["accruals"] if isinstance(data, dict) else data
        rows, _counters, _unknown = accrual.build_expense_rows(accruals, names)
        foreign = sorted({r["expense_date"] for r in rows} - {day})
        if foreign:
            raise SystemExit(f"{path}: в файле дня {day} строки с датами {foreign} — файл не того дня, стоп")
        built[day] = rows
    return built, missing


def read_existing(d1, d2):
    """Строки таблицы за окно, свои статьи (реклама Performance исключена); чтение по id > последнего с сортировкой."""
    out, last = [], None
    while True:
        q = (sb().table(TABLE).select("id,expense_date,marketplace_code,marketplace_sku,expense_type,expense_amount,article,created_at")
             .eq("marketplace_code", "ozon").gte("expense_date", d1).lte("expense_date", d2).order("id").limit(1000))
        if last is not None:
            q = q.gt("id", last)
        page = q.execute().data or []
        out.extend(r for r in page if not str(r["expense_type"] or "").startswith(OWN_ARTICLES_PREFIX_EXCLUDED))
        if len(page) < 1000:
            return out
        last = page[-1]["id"]


def key_of(r):
    return (r["expense_date"], r["marketplace_code"], str(r["marketplace_sku"] or ""), r["expense_type"])


def plan(existing, built, days):
    """Возвращает словарь плана: по дням — add / rewrite / delete / same, суммы до/после, дни с нарушенным контролем."""
    by_day_existing = defaultdict(dict)
    for r in existing:
        by_day_existing[r["expense_date"]][key_of(r)] = r
    out = {"add": [], "rewrite": [], "delete": [], "same": 0, "skipped_days": [], "days_ok": [], "no_raw": [],
           "by_month": defaultdict(lambda: {"before": defaultdict(Decimal), "after": defaultdict(Decimal), "rows_before": 0, "rows_after": 0})}
    for day in days:
        month = day[:7]
        old = by_day_existing.get(day, {})
        if day not in built:
            out["no_raw"].append(day)
            continue
        new = {key_of(r): r for r in built[day]}
        sum_before = sum((D(r["expense_amount"]) for r in old.values()), Z).quantize(C)
        sum_after = sum((D(r["expense_amount"]) for r in new.values()), Z).quantize(C)
        if sum_before != sum_after:
            out["skipped_days"].append((day, sum_before, sum_after, sum_after - sum_before))
            continue
        out["days_ok"].append(day)
        m = out["by_month"][month]
        for r in old.values():
            m["before"][r["expense_type"]] += D(r["expense_amount"]); m["rows_before"] += 1
        for r in new.values():
            m["after"][r["expense_type"]] += D(r["expense_amount"]); m["rows_after"] += 1
        for k, r in new.items():
            if k not in old:
                out["add"].append(r)
            elif D(old[k]["expense_amount"]).quantize(C) != D(r["expense_amount"]).quantize(C):
                out["rewrite"].append((old[k], r))
            else:
                out["same"] += 1
        for k, r in old.items():
            if k not in new:
                out["delete"].append(r)
    return out


def print_plan(p, d1, d2):
    print(f"ПЛАН пересборки marketplace_expenses (Ozon, свои статьи) {d1} … {d2}: дней {len(p['days_ok']) + len(p['skipped_days']) + len(p['no_raw'])}, "
          f"в плане {len(p['days_ok'])}, без сырья {len(p['no_raw'])}, с нарушенным контролем суммы дня {len(p['skipped_days'])}")
    if p["no_raw"]:
        print(f"  без файла сырья (не трогаю): {', '.join(p['no_raw'][:12])}{' …' if len(p['no_raw']) > 12 else ''}")
    for day, b, a, diff in p["skipped_days"]:
        print(f"  {day}: Σ статей до {b:,.2f} ≠ после {a:,.2f} ({diff:+,.2f}) — день не трогаю: сырьё дня и таблица разошлись (начисления доехали или ушли после ночи, когда день писался)")
    print(f"  ключей: добавить {len(p['add'])}, переписать {len(p['rewrite'])}, удалить {len(p['delete'])}, без изменений {p['same']}")
    arts = sorted({a for m in p["by_month"].values() for a in list(m["before"]) + list(m["after"])})
    print(f"\n  {'месяц':8}{'строк до→после':>16}  " + "".join(f"{a[:22]:>24}" for a in arts))
    for month in sorted(p["by_month"]):
        m = p["by_month"][month]
        print(f"  {month:8}{m['rows_before']:>7} → {m['rows_after']:<6}  " + "".join(f"{m['before'].get(a, Z):>11,.0f}→{m['after'].get(a, Z):<11,.0f}" for a in arts))
    tot_b = sum((v for m in p["by_month"].values() for v in m["before"].values()), Z)
    tot_a = sum((v for m in p["by_month"].values() for v in m["after"].values()), Z)
    print(f"  Σ всех статей по дням плана: до {tot_b:,.2f}, после {tot_a:,.2f}, разница {tot_a - tot_b:,.2f} (обязана быть 0,00 — контроль по дням)")
    moved = defaultdict(Decimal)
    for m in p["by_month"].values():
        for a in arts:
            moved[a] += m["after"].get(a, Z) - m["before"].get(a, Z)
    print("  перекладка по статьям за окно: " + ", ".join(f"{a} {v:+,.2f}" for a, v in sorted(moved.items()) if v))
    for title, rows in (("добавить", p["add"]), ("удалить", p["delete"])):
        if rows:
            print(f"  {title}, первые 5: " + "; ".join(f"{r['expense_date']} sku {r['marketplace_sku']} {r['expense_type']} {D(r['expense_amount']):,.2f}" for r in rows[:5]))


def apply(p, built, d1, d2):
    now = datetime.now(timezone.utc)
    if in_nightly_run_window(now):
        raise SystemExit(f"окно ночного прогона {window_text()} — не пишу")
    os.makedirs(SNAP_DIR, exist_ok=True)
    snap = os.path.join(SNAP_DIR, f"expenses_rebuild_{now.strftime('%Y%m%dT%H%M%SZ')}.json")
    json.dump({"window": [d1, d2], "days": p["days_ok"], "skipped_days": [d for d, *_ in p["skipped_days"]],
               "rewrite_before": [o for o, _n in p["rewrite"]], "delete": p["delete"], "add_keys": [key_of(r) for r in p["add"]]},
              open(snap, "w"), ensure_ascii=False, indent=1, default=str)
    print(f"снимок → {snap}")
    client = sb()
    rows = [r for day in p["days_ok"] for r in built[day]]
    written = 0
    for i in range(0, len(rows), 500):
        client.table(TABLE).upsert(rows[i:i + 500], on_conflict="expense_date,marketplace_code,marketplace_sku,expense_type").execute()
        written += len(rows[i:i + 500])
    deleted = 0
    ids = [r["id"] for r in p["delete"]]
    for i in range(0, len(ids), 200):
        res = client.table(TABLE).delete().in_("id", ids[i:i + 200]).execute()
        deleted += len(res.data or [])
    # контроль: в таблице за дни плана — те же ключи и Σ по статьям, что в плане
    after = [r for r in read_existing(d1, d2) if r["expense_date"] in set(p["days_ok"])]
    want = {key_of(r): D(r["expense_amount"]).quantize(C) for r in rows}
    have = {key_of(r): D(r["expense_amount"]).quantize(C) for r in after}
    wrong = [k for k in want if have.get(k) != want[k]]
    extra = set(have) - set(want)
    print(f"записано (upsert) {written}, удалено {deleted} из {len(ids)}; в таблице за дни плана {len(have)} строк, не совпало с планом {len(wrong)}, "
          f"сверх плана {len(extra)} {'=' if not wrong and not extra else '≠ ОШИБКА'}")
    print(f"db_writes = {written + deleted}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", required=True); ap.add_argument("--date-to", required=True)
    ap.add_argument("--plan", action="store_true"); ap.add_argument("--apply", action="store_true")
    ap.add_argument("--approve-ozon-expenses-write", action="store_true")
    args = ap.parse_args(argv)
    if args.apply and not args.approve_ozon_expenses_write:
        raise SystemExit("--apply требует --approve-ozon-expenses-write (слово владельца по плану с числами)")
    days = days_between(args.date_from, args.date_to)
    built, missing = build_from_raw(days, type_names())
    print(f"сырьё: дней {len(days)}, файлов {len(built)}, без файла {len(missing)}; строк построено {sum(len(v) for v in built.values())}; "
          f"свёртка: {len(accrual.TYPE_TO_EXPENSE)} типов TYPE_TO_EXPENSE")
    existing = read_existing(args.date_from, args.date_to)
    print(f"таблица: строк своих статей за окно {len(existing)}")
    p = plan(existing, built, days)
    print_plan(p, args.date_from, args.date_to)
    if not args.apply:
        print("\ndb_writes = 0")
        return 0
    apply(p, built, args.date_from, args.date_to)
    return 0


if __name__ == "__main__":
    sys.exit(main())
