#!/usr/bin/env python3
"""Посев истории штук выкупов (marketplace_buyouts.buyouts_units) из файлов сырья. Запускает владелец.

Сырьё: data/accrual_history/<день>.json (by-day) и data/accrual_postings/*.json (начисления по
номерам отправлений). В API не ходит: нет файла дня — дата называется и пропускается. Сведение —
тем же loaders/ozon_buyout_units.units_by_key, что у ночного шага.

Без --apply — план (db_writes = 0): строк со штуками, штуки и позиции по месяцам против таблицы,
ключи не измеренные, штуки без строки выкупа, строки выкупа без штук. --apply пишет ТОЛЬКО колонку
buyouts_units у существующих строк, затем сверяет таблицу с планом по ключам.

Порядок с перезаписью истории выкупов: СНАЧАЛА rewrite_buyouts_history.py --apply, ПОТОМ этот посев —
перезапись удаляет строки вместе с их штуками.

    venv/bin/python3 scripts/seed_buyout_units.py --date-from 2026-03-28 --date-to 2026-09-19
    venv/bin/python3 scripts/seed_buyout_units.py --date-from 2026-03-28 --date-to 2026-09-19 --apply
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from loaders import ozon_buyout_units as bu  # noqa: E402

BYDAY_DIR = os.path.join("data", "accrual_history")
POSTINGS_DIR = os.path.join("data", "accrual_postings")


def load_inputs(date_from, date_to):
    accruals, missing = [], []
    d = date.fromisoformat(date_from)
    while d <= date.fromisoformat(date_to):
        path = os.path.join(BYDAY_DIR, f"{d.isoformat()}.json")
        if os.path.exists(path):
            data = json.load(open(path))
            accruals += data["accruals"] if isinstance(data, dict) else data
        else:
            missing.append(d.isoformat())
        d += timedelta(days=1)
    postings, files = {}, sorted(glob.glob(os.path.join(POSTINGS_DIR, "*.json")))
    for f in files:
        for p in json.load(open(f)):
            # одно отправление может лежать в двух файлах (окна пересекаются): берём версию с большим числом строк
            old = postings.get(p["posting_number"])
            if old is None or len(p.get("accruals") or []) > len(old.get("accruals") or []):
                postings[p["posting_number"]] = p
    return accruals, missing, list(postings.values()), files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", required=True)
    ap.add_argument("--date-to", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    accruals, missing, postings, files = load_inputs(args.date_from, args.date_to)
    numbers = bu.sold_postings(accruals)
    have = {p["posting_number"] for p in postings}
    print(f"окно {args.date_from} … {args.date_to}: начислений {len(accruals)}, дат без файла by-day {len(missing)} {missing[:6]}; "
          f"файлов accrual/postings {len(files)}, отправлений в них {len(postings)}; отправлений с продажей в окне {len(numbers)}, из них нет в сырье {len([n for n in numbers if n not in have])}")
    units, c, unmatched, positions = bu.units_by_key(accruals, postings)
    print(f"сведение: строк by-day {c['rows']}, сведено {c['matched_rows']}, не сведено {c.get('unmatched_rows', 0)}; "
          f"ключей {c['keys']}, измерено {c['keys_measured']}, не измерено {c['keys_unmeasured']}; строк с quantity ≥ 2 — {c.get('rows_with_2_plus', 0)}")
    if unmatched:
        print("  не сведены: " + ", ".join(f"{d} sku {s} × {n}" for (d, s), n in sorted(unmatched.items())[:10]))

    import loaders.ozon_fbo_orders_loader as fbo
    sb = fbo.supabase
    try:
        existing = bu.read_existing_keys(sb, args.date_from, args.date_to)
    except Exception as exc:
        if "buyouts_units" not in str(exc):
            raise
        if args.apply:
            raise SystemExit("колонки marketplace_buyouts.buyouts_units нет — миграция sql/20260921_add_buyouts_units.sql не применена, писать некуда")
        print("КОЛОНКИ buyouts_units ЕЩЁ НЕТ (миграция не применена): план строится по ключам строк, «уже верно» = 0")
        existing = bu.read_existing_keys(sb, args.date_from, args.date_to, with_units=False)
    to_write, same, units_without_row, rows_without_units = bu.plan_update(units, existing)
    qty = {}
    last = None
    while True:   # позиции таблицы по месяцам — для сравнения
        q = (sb.table("marketplace_buyouts").select("id,buyout_date,marketplace_sku,buyouts_qty").eq("marketplace_code", "ozon")
             .gte("buyout_date", args.date_from).lte("buyout_date", args.date_to).order("id").limit(1000))
        if last is not None:
            q = q.gt("id", last)
        page = q.execute().data or []
        for r in page:
            qty[(r["buyout_date"], str(r["marketplace_sku"]))] = int(float(r["buyouts_qty"] or 0))
        if len(page) < 1000:
            break
        last = page[-1]["id"]
    by = defaultdict(lambda: [0, 0, 0, 0])
    for k, v in units.items():
        m = by[k[0][:7]]
        m[0] += 1; m[1] += v; m[2] += positions[k]
    for k, v in qty.items():
        by[k[0][:7]][3] += v
    print(f"\n{'месяц':9}{'ключей':>8}{'штук':>8}{'позиций by-day':>16}{'штук − поз.':>13}{'qty в таблице':>15}")
    t = [0, 0, 0, 0]
    for m in sorted(by):
        a = by[m]
        t = [t[i] + a[i] for i in range(4)]
        print(f"{m:9}{a[0]:>8}{a[1]:>8}{a[2]:>16}{a[1] - a[2]:>13}{a[3]:>15}")
    print(f"{'итого':9}{t[0]:>8}{t[1]:>8}{t[2]:>16}{t[1] - t[2]:>13}{t[3]:>15}")
    print("  «qty в таблице» до 2026-08-17 писал старый API (там штуки), с 08-18 — позиции by-day: ряд смешанный.")
    print(f"\nПЛАН: строк выкупов в окне {len(existing)}; к записи {len(to_write)}, уже верно {same}; "
          f"штуки без строки выкупа {len(units_without_row)}, строки выкупа без штук {len(rows_without_units)}")
    for label, keys in (("штуки без строки", units_without_row), ("строки без штук", rows_without_units)):
        if keys:
            bym = defaultdict(int)
            for d, _s in keys:
                bym[d[:7]] += 1
            print(f"  {label} по месяцам: {dict(sorted(bym.items()))}; примеры: " + ", ".join(f"{d} sku {s}" for d, s in keys[:5]))
    if not args.apply:
        print("db_writes = 0")
        return
    written = bu.write_units(sb, to_write)
    after = bu.read_existing_keys(sb, args.date_from, args.date_to)
    wrong = [k for k, v in units.items() if k in after and after[k] != v]
    grew = len(after) - len(existing)
    print(f"записано {written}; в таблице со штуками {sum(1 for v in after.values() if v is not None)}, не совпало с планом {len(wrong)}, "
          f"новых строк появилось {grew} {'=' if not wrong and grew == 0 else '≠ ОШИБКА'}")
    print(f"db_writes = {written}")


if __name__ == "__main__":
    main()
