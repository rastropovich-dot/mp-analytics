#!/usr/bin/env python3
"""Selected CPO («Оплата за заказ», отчёт statistic/orders/generate) за ОДИН день — тем же путём, что ночной шаг (тридцать седьмая, добор 09-14).

    venv/bin/python3 scripts/ozon_selected_cpo_one_day.py --date 2026-09-14                                   план: таблицы и леджер, обращений 0
    venv/bin/python3 scripts/ozon_selected_cpo_one_day.py --date 2026-09-14 --fetch                           снять отчёт (в Ozon), в БД не писать
    venv/bin/python3 scripts/ozon_selected_cpo_one_day.py --date 2026-09-14 --fetch --write --approve-selected-cpo-write   записать по слову владельца

Зачем отдельный скрипт. Ночной шаг (run_selected_cpo_step) берёт только D−1; cpc-recovery / cpc-backfill / CPO-однодневка Selected CPO не трогают.
Здесь — те же функции загрузчика, что ночью: fetch_search_promo_orders_csv (submit + опросы + скачивание, write=True пишет source-таблицу
ozon_search_promo_selected_cpo_orders) → selected_cpo_downstream_dry_run(write=True) (marketplace_expenses типа advertising_order_selected_cpo и
ozon_daily_sku_ad_attribution с ad_source cpo_selected_products). Отчёт запрашивается за одну дату (from … to одного дня), строки несут sale_date =
дата отчёта — чужих дат в нём нет; на всякий случай строки другой даты отбрасываются и называются. Ключи upsert те же, что ночью: CPC- и CPO-строки
дня не затрагиваются (другой expense_type / ad_source).

Цена: 1 POST /api/client/statistic/orders/generate + опросы + 1 скачивание — по практике +1 выгрузка Performance (CLAUDE.md §4); в леджер
ozon_performance_statistics_json_usage не ложится (он считает только statistics/json). Отказ в окнах ночного прогона и утреннего алерта.
Состояние клиента (кеш отчётов jobs: в pipeline_runtime_state) не сохраняется — save_state отключён, как в selected_cpo_source_fetch_dry_run.
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

D = lambda v: Decimal(str(v or 0))  # noqa: E731
Q = Decimal("0.01")


def by_type(rows, key="expense_type", amount="expense_amount"):
    out = defaultdict(lambda: [0, Decimal(0)])
    for r in rows:
        out[r[key]][0] += 1
        out[r[key]][1] += D(r[amount])
    return dict(out)


def split_by_day(rows, day, date_key):
    """Строки целевой даты и число чужих (отчёт за один день чужих дат нести не должен — но проверяем, не верим)."""
    kept = [r for r in rows if str(r.get(date_key) or "")[:10] == day]
    return kept, len(rows) - len(kept)


def table_state(sb, day):
    """Реклама дня по типам в расходах, атрибуция по источникам, source-строки, леджер 41 / 54 (знак расхода)."""
    exp = sb.table("marketplace_expenses").select("expense_type,expense_amount").eq("marketplace_code", "ozon").eq("expense_date", day).like("expense_type", "advertising%").order("expense_type").limit(5000).execute()
    attr = sb.table("ozon_daily_sku_ad_attribution").select("ad_source,ad_spend").eq("marketplace_code", "ozon").eq("sale_date", day).order("ad_source").limit(5000).execute()
    src = sb.table(ads.SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE).select("spend").eq("sale_date", day).limit(5000).execute()
    ledger = sb.table("ozon_accrual_daily_types").select("type_id,amount").eq("accrual_date", day).in_("type_id", [41, 54]).execute()
    return {"expenses": by_type(exp.data or []), "attribution": by_type(attr.data or [], "ad_source", "ad_spend"),
            "source": (len(src.data or []), sum((D(r["spend"]) for r in src.data or []), Decimal(0))),
            "ledger": {int(r["type_id"]): -D(r["amount"]) for r in ledger.data or []}}


def print_state(title, st):
    print(title)
    for t, (n, amt) in sorted(st["expenses"].items()):
        print(f"   расходы   {t:32} строк {n:>5}  {amt:>16,.2f}")
    for t, (n, amt) in sorted(st["attribution"].items()):
        print(f"   атрибуция {t:32} строк {n:>5}  {amt:>16,.2f}")
    n, s = st["source"]
    print(f"   source    {ads.SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE:32} строк {n:>5}  {s:>16,.2f}")
    total = sum((v[1] for v in st["expenses"].values()), Decimal(0))
    led = st["ledger"].get(41, Decimal(0)) + st["ledger"].get(54, Decimal(0))
    print(f"   Σ рекламы в расходах {total:,.2f} против леджера 41 + 54 = {st['ledger'].get(41, Decimal(0)):,.2f} + {st['ledger'].get(54, Decimal(0)):,.2f} = {led:,.2f}: "
          f"разница {(total - led).quantize(Q):,.2f}")
    return total, led


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--fetch", action="store_true", help="снять отчёт Selected CPO за день (в API); без флага — только план по базе")
    ap.add_argument("--write", action="store_true"); ap.add_argument("--approve-selected-cpo-write", action="store_true")
    args = ap.parse_args(argv)
    if args.write and not (args.fetch and args.approve_selected_cpo_write):
        raise SystemExit("--write требует --fetch и --approve-selected-cpo-write (слово владельца)")
    now = datetime.now(timezone.utc)
    if args.fetch and (in_nightly_run_window(now) or in_morning_alert_window(now)):
        raise SystemExit("окно ночного прогона или утреннего алерта — не стартую")
    sb = ads.supabase
    before = table_state(sb, args.date)
    total_before, ledger_sum = print_state(f"{args.date} до: реклама в таблицах", before)
    if not args.fetch:
        print(f"план: 1 POST {ads.SEARCH_PROMO_ORGANISATION_ORDERS_SUBMIT_ENDPOINT} + опросы + 1 скачивание (≈ +1 выгрузка Performance, леджер её не считает); "
              f"запись — source-таблица, затем расходы типа {ads.SELECTED_CPO_MARKETPLACE_EXPENSE_TYPE} и атрибуция {ads.SELECTED_CPO_AD_SOURCE} только за {args.date}; "
              f"ожидание по леджеру: {(ledger_sum - total_before).quantize(Q):,.2f}. db_writes = 0")
        return 0
    if not ads.ENABLE_OZON_SELECTED_CPO_DAILY:
        raise SystemExit("ENABLE_OZON_SELECTED_CPO_DAILY выключен — Selected CPO в этой среде не собирается")
    client = ads.OzonPerformanceClient()
    client.save_state = lambda *a, **k: None
    http_before = len(client.state.get("request_history", []) or [])
    schema_ok = ads.selected_cpo_source_schema_applied(sb) if args.write else False
    if args.write and not schema_ok:
        raise SystemExit(f"{ads.SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE} недоступна — не пишу")
    src = client.fetch_search_promo_orders_csv(date=args.date, write=args.write, schema_applied=schema_ok, db_client=sb if args.write else None)
    http_after = len(client.state.get("request_history", []) or [])
    rows = src.get("source_table_rows") or []
    rows_day, foreign = split_by_day(rows, args.date, "sale_date")
    agg = src.get("aggregation") or {}
    print(f"отчёт: uuid {src.get('uuid')}, status {(src.get('status') or {}).get('state')}; обращений {http_after - http_before}; строк source {len(rows)} "
          f"(чужих дат {foreign}), Σ spend строк {D(agg.get('total_spend_data_rows')):,.2f}, итоговая строка отчёта {D(agg.get('total_spend_total_rows')):,.2f}; "
          f"source db_writes {src.get('db_writes')}")
    exp_rows = ads.build_selected_cpo_marketplace_expenses_rows(rows_day)
    attr_rows = ads.build_selected_cpo_ad_attribution_rows(rows_day)
    exp_sum = sum((D(r["expense_amount"]) for r in exp_rows), Decimal(0))
    attr_sum = sum((D(r["ad_spend"]) for r in attr_rows), Decimal(0))
    print(f"построено за {args.date}: расходов {len(exp_rows)} / {exp_sum:,.2f}, атрибуции {len(attr_rows)} / {attr_sum:,.2f}; "
          f"с кликами и CPO дня: {(total_before + exp_sum):,.2f} против леджера {ledger_sum:,.2f} — разница {(total_before + exp_sum - ledger_sum).quantize(Q):,.2f}")
    if foreign:
        print(f"ВНИМАНИЕ: {foreign} строк чужих дат в отчёте за один день — отброшены")
    if not args.write:
        print("db_writes = 0 (запись — --write --approve-selected-cpo-write по слову)")
        return 0
    down = client.selected_cpo_downstream_dry_run(date=args.date, write=True, approve_downstream_write=True, db_client=sb, source_rows=rows_day)
    after = table_state(sb, args.date)
    total_after, _ = print_state(f"{args.date} после", after)
    got = after["expenses"].get(ads.SELECTED_CPO_MARKETPLACE_EXPENSE_TYPE, (0, Decimal(0)))
    got_attr = after["attribution"].get(ads.SELECTED_CPO_AD_SOURCE, (0, Decimal(0)))
    ok = got[1] == exp_sum.quantize(Q) and got_attr[1] == attr_sum.quantize(Q) and after["source"][0] >= len(rows_day)
    print(f"контроль чтением: расходы selected_cpo {got[1]:,.2f} = построенным {exp_sum:,.2f}, атрибуция {got_attr[1]:,.2f} = {attr_sum:,.2f}, "
          f"source строк {after['source'][0]} ≥ {len(rows_day)} — {'да' if ok else 'НЕТ'}; "
          f"db_writes = {int(src.get('db_writes') or 0) + int(down.get('db_writes') or 0)} (source {src.get('db_writes')}, расходы {down.get('marketplace_expenses_writes')}, "
          f"атрибуция {down.get('ozon_daily_sku_ad_attribution_writes')})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
