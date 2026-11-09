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
import cabinet  # noqa: E402
CABINET = cabinet.assert_env()  # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента
from loaders import ozon_buyout_units as bu  # noqa: E402
from loaders import ozon_finance_accrual as accrual  # noqa: E402

# Стоп по бюджету (вариант D) — не «успешно»: пайплайн видит невыполненный нефатальный шаг только по ненулевому коду процесса
# (run_daily_pipeline.run_step → Telegram «⚠️ Шаг не выполнен, прогон продолжен» и failed_steps → строка утреннего алерта).
# Код 0 при стопе был тихим нулём (замечание советника 08.11 к 8c8b7fb, п. 37). В базу при стопе ничего не пишется.
BUDGET_STOP = "budget_stop"
BUDGET_STOP_EXIT_CODE = 3


def exit_code(result):
    """Код процесса по итогу run(): стоп по бюджету → BUDGET_STOP_EXIT_CODE, иначе 0 (число записанных строк — не код)."""
    return BUDGET_STOP_EXIT_CODE if result == BUDGET_STOP else 0


def run(supabase, days_back=30, dry_run=False, accrual_module=accrual, units_module=bu):
    t0 = time.monotonic()
    accruals = accrual_module.fetch_window(days_back=days_back)
    days = sorted({str(a.get("date") or "")[:10] for a in accruals if a.get("date")})
    if not days:
        print(f"Штуки выкупов: начислений {len(accruals)}, дат 0 — писать нечего; db_writes = 0")
        return 0
    # Вариант C: сначала строки выкупов окна (только чтение), затем спрашиваем только отправления неизмеренных и свежих ключей.
    existing = units_module.read_existing_keys(supabase, days[0], days[-1])
    numbers, need, sel = units_module.select_postings(accruals, existing, days[-1])
    expected_calls = -(-len(numbers) // units_module.CHUNK)
    print(f"Штуки выкупов: начислений {len(accruals)}, дат {len(days)} ({days[0]} … {days[-1]}), отправлений с продажей {sel['postings_total']}, "
          f"к запросу {sel['postings_need']} (ключей к измерению {sel['keys_need']} из {sel['keys_total']}: без штук или с {sel['recent_from']}; "
          f"измеренных ранее и не запрашиваемых {sel['keys_measured_skipped']}) → оценка {expected_calls} обращений к accrual/postings", flush=True)
    counters = {"requests": 0, "http": 0, "retries": 0, "antispam_429": 0, "429": 0, "reasons": {}}
    budget = units_module.Budget(started=t0)
    try:
        postings = units_module.fetch_postings(numbers, counters, budget=budget)
    except units_module.BudgetExhausted as exc:
        # Вариант D: стоп с причиной, не RuntimeError; ничего не пишется — штуки ключей остаются как были (null = «не измерено»)
        print(f"⚠️ Штуки выкупов: стоп по бюджету шага — {exc}; обращений {counters['requests']}, 429 — {counters['429']}; "
              f"ничего не записано, db_writes = 0; код шага {BUDGET_STOP_EXIT_CODE}; {time.monotonic() - t0:.0f} с", flush=True)
        return BUDGET_STOP
    got = {p.get("posting_number") for p in postings}
    asked = set(numbers)
    units, c, unmatched, positions = units_module.units_by_key([a for a in accruals if a.get("unit_number") in asked], postings)
    # пишем только ключи, отобранные к измерению: измеренные ранее ключи старше свежих дней не перезаписываются
    units = {k: v for k, v in units.items() if k in need}
    unmatched = {k: v for k, v in unmatched.items() if k in need}
    reasons = ", ".join(f"{k} {v}" for k, v in sorted(counters["reasons"].items()))
    print(f"  accrual/postings: обращений {counters['requests']} (оценка {expected_calls}), HTTP-попыток {counters['http']}; "
          f"429 — {counters['429']} (повторов http_retry {counters['retries']}{': ' + reasons if reasons else ''}; "
          f"пауз {bu.ANTISPAM_PAUSE_SECONDS} с — {counters['antispam_429']}); "
          f"спросили {len(numbers)}, в ответе {len(got)}, нет в ответе {len([n for n in numbers if n not in got])}")
    print(f"  строк by-day {c.get('rows', 0)}, сведено {c.get('matched_rows', 0)}, не сведено {c.get('unmatched_rows', 0)}; "
          f"ключей (дата, sku) {c.get('keys', 0)}, измерено {c.get('keys_measured', 0)}, НЕ измерено {c.get('keys_unmeasured', 0)}; "
          f"штук {c.get('units', 0)} на {c.get('positions_of_measured', 0)} позиций")
    if unmatched:
        print("  НЕ СВЕДЕНЫ (штуки по ключу не пишутся, остаётся null): " + ", ".join(f"{d} sku {s} × {n}" for (d, s), n in sorted(unmatched.items())[:10])
              + (" …" if len(unmatched) > 10 else ""))
    # строки выкупа, которые этой ночью должны были измеряться: без штук или в свежих днях (старые измеренные — вне плана)
    scope = {k: v for k, v in existing.items() if v is None or k in need}
    to_write, same, units_without_row, rows_without_units = units_module.plan_update(units, scope)
    # «Штуки без строки» бывают двух родов. Нулевые (продажа и возврат одного SKU в один день) — норма: загрузчик
    # выкупов такую строку не пишет. Ненулевые — строка выкупа не записана или ещё не доехала: это и есть сигнал.
    real_without_row = [k for k in units_without_row if units[k] or positions.get(k)]
    print(f"  строк выкупов в окне {len(existing)}; к записи {len(to_write)}, уже стоят верные штуки у {same}; "
          f"штуки без строки выкупа {len(units_without_row)} (из них НЕНУЛЕВЫХ {len(real_without_row)}), строки выкупа без штук {len(rows_without_units)}")
    for label, keys in (("штуки без строки, ненулевые", real_without_row), ("строки без штук (в ответе Ozon их нет — застрявшие)", rows_without_units)):
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
    sys.exit(exit_code(run(fbo.supabase, days_back=args.days_back, dry_run=args.dry_run)))
