#!/usr/bin/env python3
"""CPO «Все» (all_sku_promo/orders) за ОДИН день — без повторного сбора CPC (тридцать седьмая §1 п. 6, добор 09-14).

    venv/bin/python3 scripts/ozon_cpo_one_day.py --date 2026-09-14                          план: что в таблице и в леджере, обращений 0
    venv/bin/python3 scripts/ozon_cpo_one_day.py --date 2026-09-14 --fetch                  снять отчёт и разобрать, в БД не писать
    venv/bin/python3 scripts/ozon_cpo_one_day.py --date 2026-09-14 --fetch --write --approve-cpo-write     записать по слову владельца
    venv/bin/python3 scripts/ozon_cpo_one_day.py --date-from 2026-08-23 --date-to 2026-10-22 --from-file <csv>   окно из снятого файла: план, API 0
    … --from-file <csv> --write --approve-cpo-write                                                                 записать окно по слову

Окно из файла (RBH 27.10, долг CPO РБХ-2): отчёт CPO «Все» за окно ≤ 62 дней, снятый раньше одним запросом, разбирается теми же
build_cpo_rows / build_cpo_attribution_rows; пишутся только даты окна. Если у даты окна в таблице уже есть CPO-строки, которых нет
в файле, — стоп: upsert их не удалит (застрявшие ключи), а сумма дня станет неверной.

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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
import cabinet  # noqa: E402
_PROFILE = cabinet.profile()   # каталоги данных и логов кабинета (MP_CABINET); guard — у загрузчика / точки входа, здесь только профиль
from loaders import ozon_performance_ads_loader as ads  # noqa: E402
from loaders.pipeline_window import in_morning_alert_window, in_nightly_run_window  # noqa: E402

RAW_DIR = cabinet.data_path("ozon_cpo_raw", prof=_PROFILE)
D = lambda v: Decimal(str(v or 0))  # noqa: E731
CENT = Decimal("0.01")


def select_day_rows(rows, attribution_rows, day):
    """Только строки дня — тем же правилом, что ночной target-date filter. Возвращает (строки, атрибуция, отброшено, отброшено атрибуции)."""
    rows, dropped = ads.enforce_target_dates(rows, {day}, "expense_date", "marketplace_expenses")
    attribution_rows, dropped_attr = ads.enforce_target_dates(attribution_rows, {day}, "sale_date", "ozon_daily_sku_ad_attribution")
    return rows, attribution_rows, dropped, dropped_attr


def select_window_rows(rows, attribution_rows, days):
    """Только строки дат окна — тем же правилом, что ночной target-date filter."""
    rows, dropped = ads.enforce_target_dates(rows, set(days), "expense_date", "marketplace_expenses")
    attribution_rows, dropped_attr = ads.enforce_target_dates(attribution_rows, set(days), "sale_date", "ozon_daily_sku_ad_attribution")
    return rows, attribution_rows, dropped, dropped_attr


def window_days(date_from, date_to):
    d0, d1 = date.fromisoformat(date_from), date.fromisoformat(date_to)
    if d1 < d0 or (d1 - d0).days > 61:
        raise SystemExit(f"окно {date_from} … {date_to}: нужно 1 … 62 дня (лимит одной выгрузки Performance)")
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def by_day_type(rows, date_field="expense_date"):
    out = defaultdict(Decimal)
    for r in rows:
        out[(r[date_field], r["expense_type"])] += D(r["expense_amount"]).quantize(CENT)
    return dict(out)


def stale_cpo_keys(existing_keys, built_rows):
    """Ключи CPO в таблице на датах окна, которых нет среди построенных строк: upsert их не удалит."""
    built = {(r["expense_date"], str(r["marketplace_sku"]), r["expense_type"]) for r in built_rows}
    return sorted(k for k in existing_keys if k not in built)


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


def table_window(sb, days):
    """CPO-строки таблицы по датам окна (по дню — PostgREST отдаёт ≤ 1 000 строк) и тип 54 леджера по дням."""
    keys, sums, attr = set(), defaultdict(Decimal), defaultdict(int)
    for d in days:
        res = sb.table("marketplace_expenses").select("marketplace_sku,expense_type,expense_amount").eq("marketplace_code", "ozon").eq("expense_date", d).like("expense_type", "advertising_order%").limit(5000).execute()
        for r in res.data or []:
            keys.add((d, str(r["marketplace_sku"]), r["expense_type"]))
            sums[(d, r["expense_type"])] += D(r["expense_amount"]).quantize(CENT)
        a = sb.table("ozon_daily_sku_ad_attribution").select("marketplace_sku", count="exact").eq("marketplace_code", "ozon").eq("sale_date", d).eq("ad_source", "cpo").limit(1).execute()
        attr[d] = int(a.count or 0)
    ledger = sb.table("ozon_accrual_daily_types").select("accrual_date,amount").eq("type_id", 54).gte("accrual_date", days[0]).lte("accrual_date", days[-1]).limit(5000).execute()
    t54 = {r["accrual_date"]: -D(r["amount"]) for r in ledger.data or []}
    return keys, dict(sums), dict(attr), t54


def run_window(args):
    days = window_days(args.date_from, args.date_to)
    if not args.from_file:
        raise SystemExit("окно — только из снятого файла (--from-file); снять отчёт за окно — отдельно, одна выгрузка")
    now = datetime.now(timezone.utc)
    if args.write and (in_nightly_run_window(now) or in_morning_alert_window(now)):
        raise SystemExit("окно ночного прогона или утреннего алерта — не пишу")
    text = open(args.from_file, encoding="utf-8").read()
    rows, _counters, summary = ads.build_cpo_rows(text)
    attribution_rows, _ = ads.build_cpo_attribution_rows(text)
    rows, attribution_rows, dropped, dropped_attr = select_window_rows(rows, attribution_rows, days)
    print(f"файл {args.from_file}: сверка отчёта {summary}; строк расходов окна {len(rows)} (чужих дат {dropped}), атрибуции {len(attribution_rows)} (чужих {dropped_attr}); API 0")
    built = by_day_type(rows)
    sb = ads.supabase
    keys, before, attr_before, t54 = table_window(sb, days)
    print(f"{'дата':10} {'построено':>12} {'тип 54':>12} {'Δ':>9} {'в таблице до':>13} {'атриб. до':>9}")
    bad = []
    for d in days:
        b = sum((v for (dd, _t), v in built.items() if dd == d), Decimal(0))
        t = t54.get(d, Decimal(0))
        e = sum((v for (dd, _t), v in before.items() if dd == d), Decimal(0))
        if abs(b - t) > CENT:
            bad.append((d, b - t))
        print(f"{d:10} {b:>12,.2f} {t:>12,.2f} {b - t:>9,.2f} {e:>13,.2f} {attr_before.get(d, 0):>9}")
    total_b, total_t = sum(built.values(), Decimal(0)), sum(t54.values(), Decimal(0))
    print(f"Σ построено {total_b:,.2f}, Σ тип 54 {total_t:,.2f}, Δ {total_b - total_t:,.2f}; дней с Δ > 0,01: {len(bad)} {[(d, str(v)) for d, v in bad]}")
    stale = stale_cpo_keys(keys, rows)
    if stale:
        print(f"⚠️ в таблице {len(stale)} CPO-ключей окна, которых нет в файле (upsert их не удалит): {stale[:10]}")
    if abs(float(summary.get("difference") or 0)) > ads.RECON_ERROR_THRESHOLD:
        raise SystemExit("сверка отчёта с самим собой расходится — не пишу")
    if not args.write:
        print(f"план: marketplace_expenses {len(ads.aggregate_rows(rows))} строк, атрибуция {len(attribution_rows)}; db_writes = 0 (запись — --write --approve-cpo-write по слову)")
        return 0
    if stale:
        raise SystemExit("застрявшие CPO-ключи в окне — не пишу")
    written = ads.save_rows(rows)
    ads.save_ad_attribution_rows(attribution_rows)
    _keys, after, attr_after, _ = table_window(sb, days)
    diff_types = {k: (v, after.get(k, Decimal(0))) for k, v in built.items() if after.get(k, Decimal(0)) != v}
    attr_built = defaultdict(int)
    for r in ads.aggregate_ad_attribution_rows(attribution_rows):
        attr_built[r["sale_date"]] += 1
    diff_attr = {d: (attr_built.get(d, 0), attr_after.get(d, 0)) for d in days if attr_after.get(d, 0) != attr_built.get(d, 0)}
    print(f"контроль чтением: (дата, тип) с разницей {len(diff_types)} {list(diff_types.items())[:5]}; дат с другим числом строк атрибуции {len(diff_attr)} {list(diff_attr.items())[:5]}")
    print(f"db_writes = {len(written) + len(attribution_rows)}")
    return 0 if not diff_types and not diff_attr else 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--date-from"); ap.add_argument("--date-to")
    ap.add_argument("--from-file", help="CSV отчёта CPO «Все», снятый раньше (в API не идёт)")
    ap.add_argument("--fetch", action="store_true", help="снять отчёт CPO за день (в API); без флага — только план по базе")
    ap.add_argument("--write", action="store_true"); ap.add_argument("--approve-cpo-write", action="store_true")
    args = ap.parse_args(argv)
    if args.date_from or args.date_to:
        if args.date or args.fetch or not (args.date_from and args.date_to):
            raise SystemExit("окно: --date-from и --date-to вместе, без --date и --fetch")
        if args.write and not args.approve_cpo_write:
            raise SystemExit("--write требует --approve-cpo-write (слово владельца)")
        return run_window(args)
    if not args.date:
        raise SystemExit("нужен --date или окно --date-from/--date-to")
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
