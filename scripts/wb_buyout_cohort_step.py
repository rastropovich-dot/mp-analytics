#!/usr/bin/env python3
"""Ночной шаг «WB: выкуп по когорте» и план для отчёта (WB-10 §2). Без --apply ничего не пишет.

    venv/bin/python3 scripts/wb_buyout_cohort_step.py --plan --date-from 2026-04-01            # план: месяц × (было в витрине, стало)
    venv/bin/python3 scripts/wb_buyout_cohort_step.py --days-back 60 --dry-run                 # как ночью, без записи
    venv/bin/python3 scripts/wb_buyout_cohort_step.py --days-back 60 --apply --approve-wb-cohort-write   # по слову

Окно ночи: последние --days-back дней заказов (60: 25 зрелых на прогноз + 30 незрелых + запас); отчёт читается с
первого дня окна по сегодня. Пишет wb_buyout_cohort_sku_daily и wb_buyout_cohort_daily (upsert по ключу); таблиц нет —
именованный отказ. Строка в run_daily_pipeline.py — по слову (§5 задач: строки шагов не трогать без слова).
"""
import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from loaders import wb_buyout_cohort as cohort  # noqa: E402
import loaders.wb_sales_report_loader as report_loader  # noqa: E402

Z = Decimal(0)


def payloads(sku_rows, daily_rows, observed_at):
    """Строки для upsert: Decimal → str, observed_at = время ЭТОЙ записи (иначе у перезаписанных ключей остаётся время первой
    вставки — первая ночь 09-26 переписала 4 042 ключа, а observed_at ночи получили только 10 новых; WB-10 §1)."""
    sku = [{**r, "created_sum": str(r["created_sum"]), "sold_sum": str(r["sold_sum"]), "rate_qty": None if r["rate_qty"] is None else str(r["rate_qty"]),
            "rate_sum": None if r["rate_sum"] is None else str(r["rate_sum"]), "observed_at": observed_at} for r in sku_rows]
    daily = [{**r, "created_sum": str(r["created_sum"]), "sold_sum": str(r["sold_sum"]), "observed_at": observed_at,
              **{k: (None if r[k] is None else str(r[k])) for k in ("rate_qty", "rate_sum", "forecast_rate_qty", "forecast_rate_sum")}} for r in daily_rows]
    return sku, daily


def run(sb, day_from, day_to, today, apply=False, plan_from=None):
    report_rows = cohort.read_report_rows(sb, day_from, today)
    funnel_rows = cohort.read_funnel_rows(sb, day_from, day_to)
    print(f"WB выкуп по когорте: окно заказов {day_from} … {day_to}, сегодня {today}; строк отчёта (продажи/возвраты, rr_date {day_from} … {today}) {len(report_rows)}, "
          f"карточек воронки с заказами {len(funnel_rows)}", flush=True)
    sku_rows, stats = cohort.build_sku_rows(funnel_rows, report_rows, today, day_from=day_from, day_to=day_to)
    daily_rows, forecast = cohort.build_daily_rows(sku_rows, today)
    print(f"  ключей (день, nmId) {stats['keys']}, зрелых {stats['mature_keys']}; продаж без строки воронки в день заказа {stats['sales_without_funnel_row']} "
          f"на {stats['sales_without_funnel_sum']:,.2f}; строк отчёта без orderDt {stats['skipped']['no_order_dt']}, без nmId {stats['skipped']['no_nm']}", flush=True)
    print(f"  прогноз для незрелых дней: ₽ {forecast['rate_sum']}, шт {forecast['rate_qty']} по зрелым дням {forecast['window']}", flush=True)
    months = cohort.month_table(daily_rows)
    showcase = cohort.read_showcase_by_month(sb, plan_from or day_from, day_to)
    print(f"  {'месяц':8}{'дней/зрелых':>12}{'создано ₽':>16}{'продано ₽':>16}{'стало ₽ (зрелые)':>18}{'стало шт':>10}{'все дни ₽':>11}  {'было (витрина)':>22}")
    for m, v in months.items():
        was = showcase.get(m)
        was_text = f"{was['rate']} ({was['buyouts_qty']:.0f}/{was['created_qty']:.0f})" if was else "—"
        days_text = f"{v['days']}/{v['mature_days']}"
        print(f"  {m:8}{days_text:>12}{v['created_sum']:>16,.0f}{v['sold_sum']:>16,.2f}{str(v['rate_sum']):>18}{str(v['rate_qty']):>10}{str(v['all_rate_sum']):>11}  {was_text:>22}", flush=True)
    if not apply:
        print(f"  --dry-run: не пишу (строк по (день, nmId) {len(sku_rows)}, по дням {len(daily_rows)}); db_writes = 0", flush=True)
        return sku_rows, daily_rows
    sku_payload, daily_payload = payloads(sku_rows, daily_rows, datetime.now(timezone.utc).isoformat())
    try:
        for i in range(0, len(sku_payload), 500):
            sb.table(cohort.SKU_TABLE).upsert(sku_payload[i:i + 500], on_conflict="day,nm_id").execute()
        for i in range(0, len(daily_payload), 500):
            sb.table(cohort.DAILY_TABLE).upsert(daily_payload[i:i + 500], on_conflict="day").execute()
    except Exception as error:
        text = str(error)
        if "PGRST205" in text or "42P01" in text or "does not exist" in text:
            print(f"  ❌ таблиц когорты нет (миграция sql/20260926_create_wb_buyout_cohort.sql — по слову): {text[:160]}", flush=True)
            return None
        raise
    print(f"  ✅ {cohort.SKU_TABLE}: upsert {len(sku_payload)} строк; {cohort.DAILY_TABLE}: upsert {len(daily_payload)} строк", flush=True)
    return sku_rows, daily_rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=60, help="окно дней заказов до вчера (по умолчанию 60)")
    ap.add_argument("--date-from", help="начало окна явно (для плана)")
    ap.add_argument("--date-to", help="конец окна (по умолчанию вчера)")
    ap.add_argument("--plan", action="store_true", help="только план по месяцам (не пишет)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--approve-wb-cohort-write", action="store_true")
    args = ap.parse_args(argv)
    today = date.today()
    day_to = args.date_to or (today - timedelta(days=1)).isoformat()
    day_from = args.date_from or (date.fromisoformat(day_to) - timedelta(days=args.days_back - 1)).isoformat()
    apply = bool(args.apply and args.approve_wb_cohort_write and not args.plan and not args.dry_run)
    if args.apply and not apply:
        print("--apply без --approve-wb-cohort-write (или вместе с --plan / --dry-run) — не пишу")
    sb = report_loader._client()
    result = run(sb, day_from, day_to, today.isoformat(), apply=apply)
    print("db_writes = 0" if not apply else "db_writes: см. выше")
    return 0 if result is not None else 1


if __name__ == "__main__":
    sys.exit(main())
