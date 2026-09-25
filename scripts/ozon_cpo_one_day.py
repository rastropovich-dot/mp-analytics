#!/usr/bin/env python3
"""CPO «Все» (all_sku_promo/orders) за ОДИН день — без повторного сбора CPC (тридцать седьмая §1 п. 6, добор 09-14).

    venv/bin/python3 scripts/ozon_cpo_one_day.py --date 2026-09-14                          план: что в таблице и в леджере, обращений 0
    venv/bin/python3 scripts/ozon_cpo_one_day.py --date 2026-09-14 --fetch                  снять отчёт и разобрать, в БД не писать
    venv/bin/python3 scripts/ozon_cpo_one_day.py --date 2026-09-14 --fetch --write --approve-cpo-write     записать по слову владельца

Зачем отдельный скрипт. Ночной режим грузит CPO вместе с CPC (run() → fetch_all_sku_promo_csv → build_cpo_rows → save_rows), `cpc-backfill` и
`cpc-recovery` CPO пропускают, `cpo-report-check` только читает и не пишет, а `--mode full --date D` собрал бы заново все CPC-кампании дня.
Здесь — те же функции загрузчика, что ночью, только CPO и только одна дата: `enforce_target_dates` оставляет строки одной даты (иначе upsert
по ключу (дата, sku, тип) затёр бы соседние даты неполным набором), `save_rows` пишет upsert по тому же ключу — CPC-строки (advertising_clicks)
не затрагиваются, потому что у них другой expense_type. Сырой CSV — data/ozon_cpo_raw/<день>.csv (вне git).

Цена: 1 generate + опросы + 1 скачивание; по практике это +1 выгрузка Performance (CLAUDE.md §4). Сверка отчёта с самим собой
(cpo_summary["difference"]) — как ночью: больше RECON_ERROR_THRESHOLD — не пишем. Отказ в окнах ночного прогона и утреннего алерта.
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from loaders import ozon_performance_ads_loader as ads  # noqa: E402
from loaders.pipeline_window import in_morning_alert_window, in_nightly_run_window  # noqa: E402

RAW_DIR = os.path.join(ROOT, "data", "ozon_cpo_raw")
D = lambda v: Decimal(str(v or 0))  # noqa: E731


def select_day_rows(rows, attribution_rows, day):
    """Только строки дня — тем же правилом, что ночной target-date filter. Возвращает (строки, атрибуция, отброшено, отброшено атрибуции)."""
    rows, dropped = ads.enforce_target_dates(rows, {day}, "expense_date", "marketplace_expenses")
    attribution_rows, dropped_attr = ads.enforce_target_dates(attribution_rows, {day}, "sale_date", "ozon_daily_sku_ad_attribution")
    return rows, attribution_rows, dropped, dropped_attr


def by_type(rows):
    out = defaultdict(lambda: [0, Decimal(0)])
    for r in rows:
        out[r["expense_type"]][0] += 1
        out[r["expense_type"]][1] += D(r["expense_amount"])
    return dict(out)


def table_state(sb, day):
    res = sb.table("marketplace_expenses").select("expense_type,expense_amount").eq("marketplace_code", "ozon").eq("expense_date", day).like("expense_type", "advertising%").order("expense_type").limit(5000).execute()
    ledger = sb.table("ozon_accrual_daily_types").select("type_id,amount").eq("accrual_date", day).in_("type_id", [41, 54]).execute()
    return by_type(res.data or []), {int(r["type_id"]): -D(r["amount"]) for r in ledger.data or []}


def print_state(title, types, ledger):
    print(title)
    for t, (n, amt) in sorted(types.items()):
        print(f"   {t:32} строк {n:>5}  {amt:>16,.2f}")
    print(f"   леджер начислений: тип 41 (клики) {ledger.get(41, Decimal(0)):,.2f}, тип 54 (CPO) {ledger.get(54, Decimal(0)):,.2f}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--fetch", action="store_true", help="снять отчёт CPO за день (в API); без флага — только план по базе")
    ap.add_argument("--write", action="store_true"); ap.add_argument("--approve-cpo-write", action="store_true")
    args = ap.parse_args(argv)
    if args.write and not (args.fetch and args.approve_cpo_write):
        raise SystemExit("--write требует --fetch и --approve-cpo-write (слово владельца)")
    now = datetime.now(timezone.utc)
    if args.fetch and (in_nightly_run_window(now) or in_morning_alert_window(now)):
        raise SystemExit("окно ночного прогона или утреннего алерта — не стартую")
    sb = ads.supabase
    types_before, ledger = table_state(sb, args.date)
    print_state(f"marketplace_expenses за {args.date} до: реклама по типам", types_before, ledger)
    if not args.fetch:
        print("план: 1 generate all_sku_promo/orders + опросы + 1 скачивание (≈ +1 выгрузка Performance); запись — только строк даты, типы CPO "
              "(advertising_order_*); CPC-строки не затрагиваются. db_writes = 0")
        return 0
    client = ads.OzonPerformanceClient()
    uuid, status, csv_text = client.fetch_all_sku_promo_csv("orders", args.date, args.date)
    os.makedirs(RAW_DIR, exist_ok=True)
    with open(os.path.join(RAW_DIR, f"{args.date}.csv"), "w", encoding="utf-8") as fh:
        fh.write(csv_text)
    rows, counters, summary = ads.build_cpo_rows(csv_text)
    attribution_rows, attr_counters = ads.build_cpo_attribution_rows(csv_text)
    rows, attribution_rows, dropped, dropped_attr = select_day_rows(rows, attribution_rows, args.date)
    types_new = by_type(rows)
    print(f"отчёт CPO: uuid {uuid}, status {status}; строк расходов за день {len(rows)} (отброшено чужих дат {dropped}), атрибуции {len(attribution_rows)} (отброшено {dropped_attr})")
    print_state("построено за день по типам", types_new, ledger)
    print(f"сверка отчёта (cpo_summary): {summary}")
    total_new = sum((v[1] for v in types_new.values()), Decimal(0))
    print(f"Σ CPO построено {total_new:,.2f} против леджера тип 54 {ledger.get(54, Decimal(0)):,.2f}: разница {total_new - ledger.get(54, Decimal(0)):,.2f}")
    diff = abs(float(summary.get("difference") or 0))
    if not args.write:
        print("db_writes = 0 (запись — --write --approve-cpo-write по слову)")
        return 0
    if diff > ads.RECON_ERROR_THRESHOLD:
        raise SystemExit(f"сверка отчёта с самим собой расходится на {diff} > {ads.RECON_ERROR_THRESHOLD} — не пишу")
    written = ads.save_rows(rows)
    ads.save_ad_attribution_rows(attribution_rows)
    types_after, _ = table_state(sb, args.date)
    print_state(f"marketplace_expenses за {args.date} после", types_after, ledger)
    ok = all(types_after.get(t, (0, Decimal(0)))[1] == amt for t, (_n, amt) in types_new.items())
    print(f"контроль чтением: суммы типов CPO в таблице = построенным — {'да' if ok else 'НЕТ'}; db_writes = {len(written) + len(attribution_rows)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
