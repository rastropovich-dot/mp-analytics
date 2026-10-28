import argparse
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client


load_dotenv()
import cabinet  # noqa: E402
CABINET = cabinet.assert_env()  # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

from loaders import ozon_cpo_order_lines as cpo_lines  # noqa: E402
from loaders import stale_keys  # noqa: E402

# 59-я (слово владельца 28.10): рекламный заказ = клики по своему SKU (statistics/json) + ОЗЗ «Все» по order_sku, датированный
# днём СОЗДАНИЯ заказа по номеру (ozon_cpo_order_lines) + Selected CPO. Органика = всего − реклама как есть, без обрезки в ноль:
# отрицательная — флаг ad_exceeds_total и строка в алерте. «Всего» — созданные (подтверждённые + отменённые): аналитика Ozon, где
# ключа нет — marketplace_orders той же базы. Пустые строки (ни заказов, ни рекламы) не пишутся: аналитика с 24.10 отдаёт ~8 900
# строк в день, из них ~8 700 нулевых.
CPO_SOURCE = "cpo"
ORGANIC_KEY = ("sale_date", "marketplace_code", "marketplace_sku")


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_args():
    parser = argparse.ArgumentParser(description="Calculate Ozon organic sales by SKU/day.")
    parser.add_argument(
        "--mode",
        choices=("daily-yesterday", "full"),
        default="full",
        help="daily-yesterday = production D-1 calc; full = explicit date/date-range",
    )
    parser.add_argument("--date", help="single-day shortcut, sets both --date-from and --date-to")
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--days-back", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--from-db-only",
        action="store_true",
        help="Use only existing DB data. This is the default behavior in the current implementation.",
    )
    parser.add_argument(
        "--no-api",
        action="store_true",
        help="Alias for --from-db-only. Seller API fallback is not used in the current implementation.",
    )
    parser.add_argument("--debug-sample", action="store_true")
    return parser.parse_args()


def resolve_date_range(args):
    if args.date:
        if args.date_from or args.date_to:
            raise RuntimeError("--date нельзя комбинировать с --date-from/--date-to")
        return args.date, args.date

    if args.mode == "daily-yesterday":
        if args.date_from or args.date_to:
            raise RuntimeError("daily-yesterday mode нельзя комбинировать с --date-from/--date-to")
        target_date = datetime.now(ZoneInfo(APP_TIMEZONE)).date() - timedelta(days=1)
        return target_date.isoformat(), target_date.isoformat()

    date_to = args.date_to or date.today().isoformat()
    if args.date_from:
        return args.date_from, date_to

    date_from = (datetime.fromisoformat(date_to).date() - timedelta(days=args.days_back)).isoformat()
    return date_from, date_to


def fetch_all(table, filters=None, order=None, select="*"):
    """Постраничное чтение с сортировкой по ПОЛНОМУ ключу (order — кортеж колонок): по одной колонке PostgREST отдаёт повторы и
    пропуски на границе страниц. filters — (поле, оператор, значение) или ("or_", выражение)."""
    prepared = [("or_", f[1]) if f[0] == "or_" else (f[1], f[0], f[2]) for f in filters or []]
    order = (order,) if isinstance(order, str) else tuple(order or ())
    return stale_keys.read_window_rows(supabase, table, select, prepared, order)


def load_total_orders_from_marketplace_orders(date_from, date_to):
    rows = fetch_all(
        "marketplace_orders",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("order_date", "gte", date_from),
            ("order_date", "lte", date_to),
        ],
        order=("order_date", "id"),
        select="id,order_date,marketplace_sku,article,product_name,orders_qty,orders_amount_seller,cancelled_orders_qty,"
               "cancelled_orders_amount_seller",
    )

    grouped = {}

    for row in rows:
        sale_date = row.get("order_date")
        sku = str(row.get("marketplace_sku") or "")
        if not sale_date or not sku:
            continue

        key = (sale_date, sku)
        if key not in grouped:
            grouped[key] = {
                "sale_date": sale_date,
                "marketplace_code": "ozon",
                "marketplace_sku": sku,
                "article": row.get("article") or "",
                "product_name": row.get("product_name") or "",
                "total_orders_qty": 0.0,
                "total_orders_revenue": 0.0,
            }

        # созданные = подтверждённые + отменённые (с 2026-09-17 orders_* — только подтверждённые); база та же, что у аналитики
        grouped[key]["total_orders_qty"] += float(row.get("orders_qty") or 0) + float(row.get("cancelled_orders_qty") or 0)
        grouped[key]["total_orders_revenue"] += (float(row.get("orders_amount_seller") or 0)
                                                 + float(row.get("cancelled_orders_amount_seller") or 0))
        if not grouped[key].get("article") and row.get("article"):
            grouped[key]["article"] = row.get("article")
        if not grouped[key].get("product_name") and row.get("product_name"):
            grouped[key]["product_name"] = row.get("product_name")

    return grouped


def load_total_orders_from_seller_analytics(date_from, date_to):
    try:
        rows = fetch_all(
            "ozon_daily_sku_total_orders",
            filters=[
                ("marketplace_code", "eq", "ozon"),
                ("total_revenue_source", "eq", "seller_analytics"),
                ("sale_date", "gte", date_from),
                ("sale_date", "lte", date_to),
                ("or_", "total_orders_qty.gt.0,total_orders_revenue.gt.0"),   # нулевые строки аналитики — не заказы
            ],
            order=ORGANIC_KEY,
        )
    except Exception as e:
        print(
            "Не удалось загрузить ozon_daily_sku_total_orders. "
            "Проверьте миграцию sql/20260506_create_ozon_daily_sku_total_orders.sql. "
            f"Ошибка: {e}"
        )
        return {}

    grouped = {}

    for row in rows:
        sale_date = row.get("sale_date")
        sku = str(row.get("marketplace_sku") or "")
        if not sale_date or not sku:
            continue

        key = (sale_date, sku)
        if key not in grouped:
            grouped[key] = {
                "sale_date": sale_date,
                "marketplace_code": "ozon",
                "marketplace_sku": sku,
                "article": row.get("article") or "",
                "product_name": row.get("product_name") or "",
                "total_orders_qty": 0.0,
                "total_orders_revenue": 0.0,
                "total_revenue_source": row.get("total_revenue_source") or "seller_analytics",
            }

        grouped[key]["total_orders_qty"] += float(row.get("total_orders_qty") or 0)
        grouped[key]["total_orders_revenue"] += float(row.get("total_orders_revenue") or 0)
        if not grouped[key].get("article") and row.get("article"):
            grouped[key]["article"] = row.get("article")
        if not grouped[key].get("product_name") and row.get("product_name"):
            grouped[key]["product_name"] = row.get("product_name")

    return grouped


def load_total_orders(date_from, date_to):
    analytics_by_key = load_total_orders_from_seller_analytics(date_from, date_to)
    marketplace_by_key = load_total_orders_from_marketplace_orders(date_from, date_to)

    grouped = dict(analytics_by_key)
    for key, row in marketplace_by_key.items():
        if key in grouped:
            continue
        row_copy = dict(row)
        row_copy["total_revenue_source"] = "marketplace_orders"
        grouped[key] = row_copy

    return grouped


def load_ad_attribution(date_from, date_to, cpo_by_lines_dates=frozenset()):
    """Атрибуция direct по (день, SKU). ОЗЗ ("cpo") за дни, где есть строки ОЗЗ по номеру заказа (cpo_by_lines_dates), отсюда НЕ
    берётся — её ставит load_cpo_by_created по дню создания; за остальные дни — как раньше, по дню списания."""
    try:
        rows = fetch_all(
            "ozon_daily_sku_ad_attribution",
            filters=[
                ("marketplace_code", "eq", "ozon"),
                ("attribution_type", "eq", "direct"),
                ("sale_date", "gte", date_from),
                ("sale_date", "lte", date_to),
            ],
            order=("sale_date", "marketplace_code", "marketplace_sku", "ad_source", "attribution_type", "campaign_id"),
        )
    except Exception as e:
        print(
            "Не удалось загрузить ozon_daily_sku_ad_attribution. "
            "Проверьте миграцию sql/20260506_create_ozon_daily_sku_organic.sql. "
            f"Ошибка: {e}"
        )
        return {}, set(), set()

    grouped = {}
    attribution_dates = set()
    cpo_by_charge_days = set()   # дни, где ОЗЗ взята отсюда — по дню списания

    for row in rows:
        sale_date = row.get("sale_date")
        sku = str(row.get("marketplace_sku") or "")
        if not sale_date or not sku:
            continue

        attribution_dates.add(sale_date)
        if row.get("ad_source") == CPO_SOURCE:
            if sale_date in cpo_by_lines_dates:
                continue
            cpo_by_charge_days.add(sale_date)
        key = (sale_date, sku)
        if key not in grouped:
            grouped[key] = {
                "sale_date": sale_date,
                "marketplace_code": "ozon",
                "marketplace_sku": sku,
                "article": row.get("article") or "",
                "product_name": row.get("product_name") or "",
                "ad_orders_qty": 0.0,
                "ad_orders_revenue": 0.0,
            }

        grouped[key]["ad_orders_qty"] += float(row.get("ad_orders_qty") or 0)
        grouped[key]["ad_orders_revenue"] += float(row.get("ad_orders_revenue") or 0)
        if not grouped[key].get("article") and row.get("article"):
            grouped[key]["article"] = row.get("article")
        if not grouped[key].get("product_name") and row.get("product_name"):
            grouped[key]["product_name"] = row.get("product_name")

    return grouped, attribution_dates, cpo_by_charge_days


def load_ad_expense_dates(date_from, date_to):
    rows = fetch_all(
        "marketplace_expenses",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("expense_date", "gte", date_from),
            ("expense_date", "lte", date_to),
        ],
        order=("expense_date", "id"),
        select="id,expense_date,expense_type",
    )

    dates = set()
    for row in rows:
        if str(row.get("expense_type") or "").startswith("advertising"):
            dates.add(row.get("expense_date"))

    return dates


def load_cpo_by_created(date_from, date_to, lines=None):
    """ОЗЗ по дню СОЗДАНИЯ заказа из ozon_cpo_order_lines → ({(день, SKU): {qty, revenue}}, дни списания со строками в окне,
    счётчики). Строка без даты создания — на день списания. Таблицы нет / не прочиталась — ({}, пусто): органика возьмёт ОЗЗ из
    атрибуции по дню списания, как до 59-й, и скажет об этом."""
    counters = {"lines": 0, "same_day": 0, "earlier": 0, "without_created_date": 0, "outside_window": 0, "read_failed": False}
    if lines is None:
        try:
            lines = cpo_lines.read_for_created_window(supabase, date_from, date_to)
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: {cpo_lines.TABLE} не прочитана ({type(e).__name__}: {str(e)[:160]}) — ОЗЗ по дню списания из атрибуции")
            counters["read_failed"] = True
            return {}, set(), counters
    grouped = {}
    charge_dates = set()
    for line in lines:
        charge = str(line["charge_date"])[:10]
        if date_from <= charge <= date_to:
            charge_dates.add(charge)
        created = str(line.get("order_created_date") or "")[:10] or None
        day = created or charge
        if not (date_from <= day <= date_to):
            counters["outside_window"] += 1
            continue
        counters["lines"] += 1
        counters["without_created_date" if not created else "same_day" if created == charge else "earlier"] += 1
        sku = str(line["marketplace_sku"])
        g = grouped.setdefault((day, sku), {"sale_date": day, "marketplace_code": "ozon", "marketplace_sku": sku,
                                            "article": line.get("article") or "", "product_name": "",
                                            "ad_orders_qty": 0.0, "ad_orders_revenue": 0.0})
        g["ad_orders_qty"] += float(line.get("qty") or 0)
        g["ad_orders_revenue"] += float(line.get("sale_amount") or 0)
    return grouped, charge_dates, counters


def merge_ad(ad_by_key, cpo_by_key):
    """Сумма атрибуции (клики, Selected, ОЗЗ непокрытых дней) и ОЗЗ по дню создания по ключу (день, SKU)."""
    merged = {k: dict(v) for k, v in ad_by_key.items()}
    for key, row in cpo_by_key.items():
        if key not in merged:
            merged[key] = dict(row)
            continue
        merged[key]["ad_orders_qty"] += row["ad_orders_qty"]
        merged[key]["ad_orders_revenue"] += row["ad_orders_revenue"]
        if not merged[key].get("article") and row.get("article"):
            merged[key]["article"] = row["article"]
    return merged


def safe_ratio(numerator, denominator):
    numerator = float(numerator or 0)
    denominator = float(denominator or 0)
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def calculate_row(total_row, ad_row, ad_coverage_exists):
    total_qty = float((total_row or {}).get("total_orders_qty") or 0)
    total_revenue = float((total_row or {}).get("total_orders_revenue") or 0)
    ad_qty = float((ad_row or {}).get("ad_orders_qty") or 0)
    ad_revenue = float((ad_row or {}).get("ad_orders_revenue") or 0)

    warnings = []
    calculation_status = "ok"

    if total_row is None:
        calculation_status = "missing_total"
        if ad_row is not None and (ad_qty > 0 or ad_revenue > 0):
            warnings.append("ad_attribution_without_total")
    elif (
        total_row is not None
        and total_qty == 0
        and total_revenue == 0
        and ad_row is not None
        and (ad_qty > 0 or ad_revenue > 0)
    ):
        calculation_status = "missing_total"
        warnings.append("zero_total_with_ad_attribution")
    elif ad_row is None and ad_coverage_exists:
        ad_qty = 0.0
        ad_revenue = 0.0
    elif ad_row is None and not ad_coverage_exists:
        calculation_status = "missing_ad_attribution"

    # 59-я: без обрезки в ноль — до неё max(…, 0) молча прибавлял органике 91 … 352 шт в день на кабинет (разведка 28.10)
    organic_qty = total_qty - ad_qty
    organic_revenue = total_revenue - ad_revenue
    ad_exceeds_total = ad_qty > total_qty

    if ad_exceeds_total and total_qty > 0:
        warnings.append("ad_orders_exceed_total")
    if ad_revenue > total_revenue and total_revenue > 0:
        warnings.append("ad_revenue_exceed_total")

    return {
        "total_orders_qty": total_qty,
        "total_orders_revenue": total_revenue,
        "ad_orders_qty": ad_qty,
        "ad_orders_revenue": ad_revenue,
        "organic_orders_qty": organic_qty,
        "organic_orders_revenue": organic_revenue,
        "ad_share_orders": safe_ratio(ad_qty, total_qty),
        "ad_share_revenue": safe_ratio(ad_revenue, total_revenue),
        "ad_exceeds_total": ad_exceeds_total,
        "calculation_status": calculation_status,
        "warning": ",".join(warnings) if warnings else None,
    }


def is_empty(row):
    return not any(float(row.get(k) or 0) for k in ("total_orders_qty", "total_orders_revenue", "ad_orders_qty", "ad_orders_revenue"))


def build_reconciliation_breakdown(rows):
    raw_total_orders_qty = round(sum(float(row.get("total_orders_qty") or 0) for row in rows), 2)
    raw_total_orders_revenue = round(sum(float(row.get("total_orders_revenue") or 0) for row in rows), 2)
    raw_ad_orders_qty = round(sum(float(row.get("ad_orders_qty") or 0) for row in rows), 2)
    raw_ad_orders_revenue = round(sum(float(row.get("ad_orders_revenue") or 0) for row in rows), 2)
    raw_organic_orders_qty = round(sum(float(row.get("organic_orders_qty") or 0) for row in rows), 2)
    raw_organic_orders_revenue = round(sum(float(row.get("organic_orders_revenue") or 0) for row in rows), 2)

    missing_total_rows = [row for row in rows if row.get("calculation_status") == "missing_total"]
    missing_total_ad_orders_qty = round(
        sum(float(row.get("ad_orders_qty") or 0) for row in missing_total_rows),
        2,
    )
    missing_total_ad_orders_revenue = round(
        sum(float(row.get("ad_orders_revenue") or 0) for row in missing_total_rows),
        2,
    )

    ad_exceeds_total_orders_qty_excess = 0.0
    ad_exceeds_total_revenue_excess = 0.0
    ad_exceeds_total_rows_count = 0
    reconciled_rows_count = 0

    for row in rows:
        total_qty = float(row.get("total_orders_qty") or 0)
        total_revenue = float(row.get("total_orders_revenue") or 0)
        ad_qty = float(row.get("ad_orders_qty") or 0)
        ad_revenue = float(row.get("ad_orders_revenue") or 0)
        qty_excess = max(ad_qty - total_qty, 0) if total_qty > 0 else 0.0
        revenue_excess = max(ad_revenue - total_revenue, 0) if total_revenue > 0 else 0.0

        if qty_excess > 0 or revenue_excess > 0:
            ad_exceeds_total_rows_count += 1

        ad_exceeds_total_orders_qty_excess += qty_excess
        ad_exceeds_total_revenue_excess += revenue_excess

        if row.get("calculation_status") == "ok" and not row.get("warning"):
            reconciled_rows_count += 1

    ad_exceeds_total_orders_qty_excess = round(ad_exceeds_total_orders_qty_excess, 2)
    ad_exceeds_total_revenue_excess = round(ad_exceeds_total_revenue_excess, 2)

    raw_gap_orders_qty = round(raw_ad_orders_qty + raw_organic_orders_qty - raw_total_orders_qty, 2)
    raw_gap_orders_revenue = round(
        raw_ad_orders_revenue + raw_organic_orders_revenue - raw_total_orders_revenue,
        2,
    )

    explained_gap_orders_qty = round(
        missing_total_ad_orders_qty + ad_exceeds_total_orders_qty_excess,
        2,
    )
    explained_gap_orders_revenue = round(
        missing_total_ad_orders_revenue + ad_exceeds_total_revenue_excess,
        2,
    )

    unexplained_gap_orders_qty = round(raw_gap_orders_qty - explained_gap_orders_qty, 2)
    unexplained_gap_orders_revenue = round(raw_gap_orders_revenue - explained_gap_orders_revenue, 2)

    return {
        "raw_gap_orders_qty": raw_gap_orders_qty,
        "raw_gap_orders_revenue": raw_gap_orders_revenue,
        "missing_total_rows_count": len(missing_total_rows),
        "missing_total_ad_orders_qty": missing_total_ad_orders_qty,
        "missing_total_ad_orders_revenue": missing_total_ad_orders_revenue,
        "ad_exceeds_total_rows_count": ad_exceeds_total_rows_count,
        "ad_exceeds_total_orders_qty_excess": ad_exceeds_total_orders_qty_excess,
        "ad_exceeds_total_revenue_excess": ad_exceeds_total_revenue_excess,
        "explained_gap_orders_qty": explained_gap_orders_qty,
        "explained_gap_orders_revenue": explained_gap_orders_revenue,
        "unexplained_gap_orders_qty": unexplained_gap_orders_qty,
        "unexplained_gap_orders_revenue": unexplained_gap_orders_revenue,
        "reconciled_rows_count": reconciled_rows_count,
    }


def build_organic_rows(date_from, date_to, cpo_lines_rows=None):
    total_by_key = load_total_orders(date_from, date_to)
    cpo_by_key, cpo_charge_dates, cpo_counters = load_cpo_by_created(date_from, date_to, cpo_lines_rows)
    attribution_by_key, attribution_dates, cpo_by_charge = load_ad_attribution(date_from, date_to, cpo_charge_dates)
    ad_by_key = merge_ad(attribution_by_key, cpo_by_key)
    ad_expense_dates = load_ad_expense_dates(date_from, date_to)
    cpo_by_charge_days = sorted(cpo_by_charge)

    all_keys = sorted(set(total_by_key.keys()) | set(ad_by_key.keys()))
    rows = []
    warning_count = 0
    status_counts = defaultdict(int)

    for sale_date, sku in all_keys:
        total_row = total_by_key.get((sale_date, sku))
        ad_row = ad_by_key.get((sale_date, sku))
        sample = total_row or ad_row or {}

        ad_coverage_exists = sale_date in attribution_dates or sale_date not in ad_expense_dates
        calculated = calculate_row(total_row, ad_row, ad_coverage_exists)
        if is_empty(calculated):
            status_counts["empty_not_written"] += 1
            continue

        row = {
            "sale_date": sale_date,
            "marketplace_code": "ozon",
            "marketplace_sku": sku,
            "article": sample.get("article") or "",
            "product_name": sample.get("product_name") or "",
            "total_revenue_source": (total_row or {}).get("total_revenue_source"),
            **calculated,
        }

        rows.append(row)
        status_counts[row["calculation_status"]] += 1
        if row.get("warning"):
            warning_count += 1

    summary = {
        "date_from": date_from,
        "date_to": date_to,
        "rows": len(rows),
        "warning_count": warning_count,
        "status_counts": dict(status_counts),
        "totals": {
            "total_orders_qty": round(sum(float(row.get("total_orders_qty") or 0) for row in rows), 2),
            "total_orders_revenue": round(sum(float(row.get("total_orders_revenue") or 0) for row in rows), 2),
            "ad_orders_qty": round(sum(float(row.get("ad_orders_qty") or 0) for row in rows), 2),
            "ad_orders_revenue": round(sum(float(row.get("ad_orders_revenue") or 0) for row in rows), 2),
            "organic_orders_qty": round(sum(float(row.get("organic_orders_qty") or 0) for row in rows), 2),
            "organic_orders_revenue": round(sum(float(row.get("organic_orders_revenue") or 0) for row in rows), 2),
        },
        "total_source_counts": {
            "seller_analytics": sum(1 for row in rows if row.get("total_revenue_source") == "seller_analytics"),
            "marketplace_orders": sum(1 for row in rows if row.get("total_revenue_source") == "marketplace_orders"),
            "missing_total": sum(1 for row in rows if row.get("calculation_status") == "missing_total"),
        },
        "reconciliation_breakdown": build_reconciliation_breakdown(rows),
        "ad_exceeds_total": {
            "rows": sum(1 for row in rows if row.get("ad_exceeds_total")),
            "excess_qty": round(sum(row["ad_orders_qty"] - row["total_orders_qty"] for row in rows if row.get("ad_exceeds_total")), 2),
        },
        "cpo_by_created": cpo_counters,
        # дни окна, где ОЗЗ в атрибуции есть, а строк по номеру заказа нет: ОЗЗ по дню списания (история до 59-й, отказ чтения)
        "cpo_by_charge_days": cpo_by_charge_days,
    }

    return rows, summary


def save_rows(rows, date_from, date_to):
    """Upsert строк окна, затем удаление ключей окна, которых нет среди построенных (пустые строки прежних прогонов, ключи без
    заказов и рекламы после передатировки ОЗЗ). День без единой построенной строки не трогается (stale_keys.plan)."""
    if not rows:
        print("Нет Ozon organic rows для записи — окно не трогаю")
        return 0, 0

    prepared_rows = []
    for row in rows:
        prepared = dict(row)
        prepared.pop("total_revenue_source", None)
        prepared_rows.append(prepared)

    for batch in chunks(prepared_rows, 500):
        supabase.table("ozon_daily_sku_organic").upsert(batch, on_conflict=",".join(ORGANIC_KEY)).execute()

    def key_of(r):
        return (str(r["sale_date"])[:10], r["marketplace_code"], str(r["marketplace_sku"]))

    existing = fetch_all("ozon_daily_sku_organic", [("marketplace_code", "eq", "ozon"), ("sale_date", "gte", date_from),
                                                    ("sale_date", "lte", date_to)], ORGANIC_KEY, select=",".join(ORGANIC_KEY))
    stale, skipped_days = stale_keys.plan(existing, {key_of(r) for r in prepared_rows}, {str(r["sale_date"])[:10] for r in prepared_rows},
                                          key_of, lambda r: str(r["sale_date"])[:10])
    if skipped_days:
        print(f"Органика: дни со строками в таблице без единой построенной — не трогаю: {', '.join(skipped_days)}")
    for r in stale:
        supabase.table("ozon_daily_sku_organic").delete().eq("sale_date", r["sale_date"]).eq("marketplace_code", r["marketplace_code"]) \
            .eq("marketplace_sku", r["marketplace_sku"]).execute()
    print(f"✅ ozon_daily_sku_organic: записано {len(prepared_rows)} строк, удалено ключей окна без заказов и рекламы {len(stale)}")
    return len(prepared_rows), len(stale)


def extend_for_late_cpo(date_from, date_to, max_lag_days=45):
    """daily-yesterday: ОЗЗ, списанная вчера за заказ, созданный раньше, меняет органику дня создания — окно расширяется до
    самого раннего такого дня (не глубже max_lag_days). Таблицы нет — окно как было."""
    try:
        lines = fetch_all(cpo_lines.TABLE, [("charge_date", "gte", date_from), ("charge_date", "lte", date_to)], cpo_lines.KEY,
                          select="charge_date,order_number,marketplace_sku,promoted_sku,order_created_date")
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: {cpo_lines.TABLE} не прочитана ({type(e).__name__}) — окно органики не расширяю")
        return date_from
    floor = (date.fromisoformat(date_from) - timedelta(days=max_lag_days)).isoformat()
    created = [str(r["order_created_date"])[:10] for r in lines if r.get("order_created_date")]
    earliest = max(min(created + [date_from]), floor)
    if earliest < date_from:
        print(f"Органика: ОЗЗ {date_from} … {date_to} списана и за заказы, созданные раньше — пересчитываю с {earliest}")
    return earliest


def format_summary_line(summary):
    t, ex, cpo = summary["totals"], summary["ad_exceeds_total"], summary["cpo_by_created"]
    line = (f"Органика {summary['date_from']} … {summary['date_to']}: всего {t['total_orders_qty']:.0f} шт, реклама {t['ad_orders_qty']:.0f}, "
            f"органика {t['organic_orders_qty']:.0f} (без обрезки); реклама больше всех заказов у {ex['rows']} SKU, лишних {ex['excess_qty']:.0f} шт; "
            f"ОЗЗ по дню создания: строк {cpo['lines']}, в день списания {cpo['same_day']}, раньше {cpo['earlier']}, "
            f"без даты создания {cpo['without_created_date']}; пустых ключей не записано {summary['status_counts'].get('empty_not_written', 0)}")
    if summary["cpo_by_charge_days"]:
        days = summary["cpo_by_charge_days"]
        line += f"\n⚠️ ОЗЗ по дню списания (строк по номеру заказа нет) за {len(days)} дн.: {days[0]} … {days[-1]}"
    return line


def print_sample(rows, limit=10):
    sample = rows[:limit]
    for row in sample:
        print(row)


def main():
    args = parse_args()
    date_from, date_to = resolve_date_range(args)

    if not (args.from_db_only or args.no_api):
        print(
            "reports_ozon_sku_organic.py использует только DB sources. "
            "Приоритет total source: ozon_daily_sku_total_orders(seller_analytics) -> marketplace_orders."
        )

    if args.mode == "daily-yesterday":
        date_from = extend_for_late_cpo(date_from, date_to)

    rows, summary = build_organic_rows(date_from, date_to)

    print("Ozon organic summary:")
    print(summary)

    if args.debug_sample or args.dry_run:
        print("Ozon organic sample rows:")
        print_sample(rows)

    print(format_summary_line(summary))

    if args.dry_run:
        print("Dry run: ozon_daily_sku_organic не обновлялась, db_writes = 0")
        return

    save_rows(rows, date_from, date_to)


if __name__ == "__main__":
    main()
