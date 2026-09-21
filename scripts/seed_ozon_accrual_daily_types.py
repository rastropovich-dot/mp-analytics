#!/usr/bin/env python3
"""Посев истории леджера начислений по типам (ozon_accrual_daily_types) из файлов сырья. Запускает владелец.

Сырьё — data/accrual_history/<день>.json (ответы /v1/finance/accrual/by-day). В API Ozon
скрипт НЕ ходит: нет файла — дата называется и пропускается. Строки строит тот же
accrual.build_type_ledger_rows, что и ночной шаг, — правило одно.

Без --apply ничего не пишет (db_writes = 0) и показывает план:

    дат, строк (дата × тип), типов, из них незнакомых TYPE_TO_EXPENSE; суммы по 41, 54, 1, 51, 96, 25, 10
    АРБИТР по каждой дате: Σ леджера по типам, свёрнутая через TYPE_TO_EXPENSE в статьи, со сменой знака,
    = marketplace_expenses по статьям за ту же дату (41 / 54 и 25 / 10 из свёртки исключены — в расходах
    у них строк нет). Что сравнивается: файл — срез на момент его сбора, база — на момент последней ночи,
    когда дата была в 30-дневном окне. Даты моложе двух суток помечаются: начисления доезжают.

--apply: upsert по (accrual_date, type_id) пачками, затем контроль «в таблице = записано» по числу
строк и по Σ amount. Таблица новая, чужих данных в ней нет; ночной шаг перепишет последние 30 дней сам.

    venv/bin/python3 scripts/seed_ozon_accrual_daily_types.py --date-from 2026-03-28 --date-to 2026-09-19
    venv/bin/python3 scripts/seed_ozon_accrual_daily_types.py --date-from 2026-03-28 --date-to 2026-09-19 --apply
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
TABLE = "ozon_accrual_daily_types"
WATCH_TYPES = (41, 54, 1, 51, 96, 25, 10)
YOUNG_DAYS = 2
Z = Decimal(0)
C = Decimal("0.01")
D = lambda v: Decimal(str(v or 0))  # noqa: E731


def sb():
    import loaders.ozon_fbo_orders_loader as fbo
    return fbo.supabase


def days_between(date_from, date_to):
    d, out = date.fromisoformat(date_from), []
    while d <= date.fromisoformat(date_to):
        out.append(d.isoformat()); d += timedelta(days=1)
    return out


def load_ledger(days, type_names):
    """Строки леджера по файлам. Возвращает (строки, даты без файла, {дата: когда снят файл})."""
    rows, missing, fetched = [], [], {}
    for day in days:
        path = os.path.join(RAW_DIR, f"{day}.json")
        if not os.path.exists(path):
            missing.append(day)
            continue
        data = json.load(open(path))
        accruals = data["accruals"] if isinstance(data, dict) else data
        fetched[day] = (data.get("fetched_at") or "")[:10] if isinstance(data, dict) else ""
        built = accrual.build_type_ledger_rows(accruals, type_names)
        foreign = sorted({r["accrual_date"] for r in built} - {day})
        if foreign:
            raise SystemExit(f"{path}: в файле дня {day} начисления с датами {foreign} — файл не того дня, стоп")
        rows.extend(built)
    return rows, missing, fetched


def fold_to_articles(rows):
    """{(дата, статья): расход} — свёртка леджера через TYPE_TO_EXPENSE со сменой знака; 41 / 54 и 25 / 10 вне свёртки."""
    out = defaultdict(Decimal)
    for r in rows:
        t = r["type_id"]
        if t in accrual.AD_TYPE_IDS or t in accrual.UNCLASSIFIED_TYPE_IDS:
            continue
        out[(r["accrual_date"], accrual.TYPE_TO_EXPENSE.get(t, f"unknown_{t}"))] -= D(r["amount"])
    return out


def read_expenses(date_from, date_to):
    """marketplace_expenses (ozon) по статьям и датам; чтение по ключу id > последнего, с сортировкой."""
    out, last, n = defaultdict(Decimal), None, 0
    while True:
        q = (sb().table("marketplace_expenses").select("id,expense_date,expense_type,expense_amount")
             .eq("marketplace_code", "ozon").gte("expense_date", date_from).lte("expense_date", date_to).order("id").limit(1000))
        if last is not None:
            q = q.gt("id", last)
        page = q.execute().data or []
        for r in page:
            t = r["expense_type"]
            if t == "commission" or t.startswith("advertising"):
                continue        # комиссия — не услуга с type_id; реклама в расходах — из Performance API, не из начислений
            out[(r["expense_date"], t)] += D(r["expense_amount"])
        n += len(page)
        if len(page) < 1000:
            return out, n
        last = page[-1]["id"]


def arbiter(rows, expenses, days, today):
    """По датам: свёртка леджера против расходов. Возвращает список расхождений [(дата, статья, леджер, база)]."""
    folded = fold_to_articles(rows)
    diffs = []
    for key in sorted(set(folded) | set(expenses)):
        if key[0] not in days:
            continue
        a, b = folded.get(key, Z).quantize(C), expenses.get(key, Z).quantize(C)
        if a != b:
            diffs.append((key[0], key[1], a, b))
    return diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", default="2026-03-28")
    ap.add_argument("--date-to", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-type-names", action="store_true", help="не ходить в /v1/finance/accrual/types за именами (единственное обращение к API)")
    args = ap.parse_args()
    today = datetime.now(timezone.utc).date().isoformat()
    days = days_between(args.date_from, args.date_to)

    type_names = {} if args.no_type_names else accrual.load_accrual_types()
    print(f"справочник типов: {len(type_names)} имён" + (" (без обращения к API)" if args.no_type_names else " (1 обращение к /v1/finance/accrual/types)"))
    rows, missing, fetched = load_ledger(days, type_names)
    have = sorted({r["accrual_date"] for r in rows})
    types = sorted({r["type_id"] for r in rows})
    known = set(accrual.TYPE_TO_EXPENSE) | accrual.AD_TYPE_IDS | accrual.UNCLASSIFIED_TYPE_IDS
    unknown = [t for t in types if t not in known]
    print(f"\nПЛАН ПОСЕВА {args.date_from} … {args.date_to}: дат в окне {len(days)}, с файлом {len(fetched)}, без файла {len(missing)}"
          + (f" ({', '.join(missing[:8])}{' …' if len(missing) > 8 else ''})" if missing else ""))
    empty = [d for d in fetched if d not in have]
    print(f"  строк (дата × тип) {len(rows)}, дат со строками {len(have)}" + (f", файл есть, а услуг нет: {empty}" if empty else "")
          + f"; типов {len(types)}, из них вне TYPE_TO_EXPENSE / рекламы / компенсаций: {len(unknown)} {unknown if unknown else ''}")
    print(f"  нулевых сумм {sum(1 for r in rows if D(r['amount']) == 0)}; строк услуг всего {sum(r['lines'] for r in rows)}")
    by_fetch = defaultdict(int)
    for d, f in fetched.items():
        by_fetch[f] += 1
    print("  файлы сняты: " + ", ".join(f"{f or '?'} — {n} дн." for f, n in sorted(by_fetch.items())))
    print(f"\n  {'тип':>5} {'имя':34}{'Σ amount (знак Ozon)':>24}{'дат':>6}{'строк услуг':>13}")
    for t in WATCH_TYPES:
        mine = [r for r in rows if r["type_id"] == t]
        print(f"  {t:>5} {(type_names.get(t) or '?')[:33]:34}{sum((D(r['amount']) for r in mine), Z):>24,.2f}{len(mine):>6}{sum(r['lines'] for r in mine):>13}")
    ads = [r for r in rows if r["type_id"] in accrual.AD_TYPE_IDS and r["accrual_date"] <= "2026-09-15"]
    print(f"  41 + 54 за {args.date_from} … 2026-09-15, расходом (знак перевёрнут): {-sum((D(r['amount']) for r in ads), Z):,.2f}")

    expenses, n_exp = read_expenses(args.date_from, args.date_to)
    diffs = arbiter(rows, expenses, set(have), today)
    bad_days = sorted({d for d, _a, _l, _b in diffs})
    young = [d for d in have if (date.fromisoformat(today) - date.fromisoformat(d)).days < YOUNG_DAYS]
    print(f"\nАРБИТР: свёртка леджера в статьи (знак перевёрнут) против marketplace_expenses, {n_exp} строк расходов прочитано")
    print(f"  дат сверено {len(have)}, сошлись до копейки {len(have) - len(bad_days)}, расходятся {len(bad_days)}"
          + (f"; моложе двух суток: {young}" if young else ""))
    if diffs:
        print(f"  {'дата':12}{'статья':16}{'леджер (файл)':>16}{'база':>16}{'база − файл':>14}  файл снят")
        for d, art, a, b in diffs:
            print(f"  {d:12}{art:16}{a:>16,.2f}{b:>16,.2f}{b - a:>14,.2f}  {fetched.get(d, '?')}" + ("  ← моложе двух суток" if d in young else ""))
        print(f"  Σ расхождений (база − файл): {sum((b - a for _d, _art, a, b in diffs), Z):,.2f}")

    if not args.apply:
        print("\ndb_writes = 0")
        return

    written = 0
    for i in range(0, len(rows), 500):
        sb().table(TABLE).upsert(rows[i:i + 500], on_conflict="accrual_date,type_id").execute()
        written += len(rows[i:i + 500])
    # контроль: в таблице за окно — те же ключи и та же сумма
    got, last = [], None
    while True:
        q = sb().table(TABLE).select("accrual_date,type_id,amount").gte("accrual_date", args.date_from).lte("accrual_date", args.date_to).order("accrual_date").order("type_id")
        page = q.range(len(got), len(got) + 999).execute().data or []
        got.extend(page)
        if len(page) < 1000:
            break
    want = {(r["accrual_date"], r["type_id"]): D(r["amount"]).quantize(C) for r in rows}
    have_db = {(r["accrual_date"], r["type_id"]): D(r["amount"]).quantize(C) for r in got}
    only_db = set(have_db) - set(want)
    wrong = [k for k in want if have_db.get(k) != want[k]]
    print(f"\nзаписано {written} строк; в таблице за окно {len(have_db)}; не совпало с планом {len(wrong)}; ключей сверх плана {len(only_db)} "
          f"{'=' if not wrong and len(have_db) - len(only_db) == len(want) else '≠ ОШИБКА'}")
    print(f"db_writes = {written}")


if __name__ == "__main__":
    main()
