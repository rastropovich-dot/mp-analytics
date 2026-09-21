#!/usr/bin/env python3
"""Ночной шаг: штуки выкупов в marketplace_buyouts.buyouts_units. Нефатальный, идёт после шага выкупов.

Окно — те же 30 дней, что у загрузчика выкупов. by-day снимается заново (шаги — отдельные
процессы, общего ответа у них нет): ~2 обращения на день ≈ 60–65 за ночь, затем accrual/postings
по 200 номеров за вызов ≈ 25–30. Итого ~90–95 обращений к Seller API, 2–3 минуты; оценка
печатается ДО первого обращения к accrual/postings, факт — в конце.

Пишет ТОЛЬКО колонку buyouts_units и только у существующих строк выкупов. Ключ, у которого хоть
одна строка не свелась построчно (seller_price × quantity = sale_amount и accrued =
sale_commission), не пишется вовсе — остаётся null, «не измерено». Строки выкупов без штук и штуки
без строки считаются и печатаются.

    venv/bin/python3 scripts/ozon_buyout_units_step.py --dry-run        ничего не пишет, db_writes = 0
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from loaders import ozon_buyout_units as bu  # noqa: E402
from loaders import ozon_finance_accrual as accrual  # noqa: E402


def run(supabase, days_back=30, dry_run=False, accrual_module=accrual, units_module=bu):
    t0 = time.monotonic()
    accruals = accrual_module.fetch_window(days_back=days_back)
    days = sorted({str(a.get("date") or "")[:10] for a in accruals if a.get("date")})
    numbers = units_module.sold_postings(accruals)
    expected_calls = -(-len(numbers) // units_module.CHUNK)
    print(f"Штуки выкупов: начислений {len(accruals)}, дат {len(days)}" + (f" ({days[0]} … {days[-1]})" if days else "")
          + f", отправлений с продажей {len(numbers)} → оценка {expected_calls} обращений к accrual/postings", flush=True)
    counters = {"requests": 0, "429": 0}
    postings = units_module.fetch_postings(numbers, counters)
    got = {p.get("posting_number") for p in postings}
    units, c, unmatched, _positions = units_module.units_by_key(accruals, postings)
    print(f"  accrual/postings: обращений {counters['requests']} (оценка {expected_calls}), пауз 429 — {counters['429']}; "
          f"спросили {len(numbers)}, в ответе {len(got)}, нет в ответе {len([n for n in numbers if n not in got])}")
    print(f"  строк by-day {c.get('rows', 0)}, сведено {c.get('matched_rows', 0)}, не сведено {c.get('unmatched_rows', 0)}; "
          f"ключей (дата, sku) {c.get('keys', 0)}, измерено {c.get('keys_measured', 0)}, НЕ измерено {c.get('keys_unmeasured', 0)}; "
          f"штук {c.get('units', 0)} на {c.get('positions_of_measured', 0)} позиций")
    if unmatched:
        print("  НЕ СВЕДЕНЫ (штуки по ключу не пишутся, остаётся null): " + ", ".join(f"{d} sku {s} × {n}" for (d, s), n in sorted(unmatched.items())[:10])
              + (" …" if len(unmatched) > 10 else ""))
    if not days:
        print("  начислений нет — писать нечего; db_writes = 0")
        return 0
    existing = units_module.read_existing_keys(supabase, days[0], days[-1])
    to_write, same, units_without_row, rows_without_units = units_module.plan_update(units, existing)
    print(f"  строк выкупов в окне {len(existing)}; к записи {len(to_write)}, уже стоят верные штуки у {same}; "
          f"штуки без строки выкупа {len(units_without_row)}, строки выкупа без штук {len(rows_without_units)}")
    for label, keys in (("штуки без строки", units_without_row), ("строки без штук", rows_without_units)):
        if keys:
            print(f"    {label}: " + ", ".join(f"{d} sku {s}" for d, s in keys[:8]) + (" …" if len(keys) > 8 else ""))
    if dry_run:
        print(f"  сухой прогон; db_writes = 0; {time.monotonic() - t0:.0f} с")
        return 0
    written = units_module.write_units(supabase, to_write)
    print(f"✅ Штуки выкупов записаны в marketplace_buyouts.buyouts_units: {written} строк; {time.monotonic() - t0:.0f} с")
    return written


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days-back", type=int, default=30)
    args = ap.parse_args()
    import loaders.ozon_fbo_orders_loader as fbo
    run(fbo.supabase, days_back=args.days_back, dry_run=args.dry_run)
