import argparse
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client


load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


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


def fetch_all(table, filters=None, order=None):
    all_rows = []
    start = 0
    page_size = 1000

    while True:
        query = supabase.table(table).select("*")

        for field, operator, value in filters or []:
            if operator == "eq":
                query = query.eq(field, value)
            elif operator == "gte":
                query = query.gte(field, value)
            elif operator == "lte":
                query = query.lte(field, value)

        if order:
            query = query.order(order)

        result = query.range(start, start + page_size - 1).execute()
        rows = result.data or []
        all_rows.extend(rows)

        if len(rows) < page_size:
            break

        start += page_size

    return all_rows


def load_total_orders_from_marketplace_orders(date_from, date_to):
    rows = fetch_all(
        "marketplace_orders",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("order_date", "gte", date_from),
            ("order_date", "lte", date_to),
        ],
        order="order_date",
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

        grouped[key]["total_orders_qty"] += float(row.get("orders_qty") or 0)
        grouped[key]["total_orders_revenue"] += float(row.get("orders_amount_seller") or 0)
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
            ],
            order="sale_date",
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


def load_ad_attribution(date_from, date_to):
    try:
        rows = fetch_all(
            "ozon_daily_sku_ad_attribution",
            filters=[
                ("marketplace_code", "eq", "ozon"),
                ("attribution_type", "eq", "direct"),
                ("sale_date", "gte", date_from),
                ("sale_date", "lte", date_to),
            ],
            order="sale_date",
        )
    except Exception as e:
        print(
            "Не удалось загрузить ozon_daily_sku_ad_attribution. "
            "Проверьте миграцию sql/20260506_create_ozon_daily_sku_organic.sql. "
            f"Ошибка: {e}"
        )
        return {}, set()

    grouped = {}
    attribution_dates = set()

    for row in rows:
        sale_date = row.get("sale_date")
        sku = str(row.get("marketplace_sku") or "")
        if not sale_date or not sku:
            continue

        attribution_dates.add(sale_date)
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

    return grouped, attribution_dates


def load_ad_expense_dates(date_from, date_to):
    rows = fetch_all(
        "marketplace_expenses",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("expense_date", "gte", date_from),
            ("expense_date", "lte", date_to),
        ],
        order="expense_date",
    )

    dates = set()
    for row in rows:
        if str(row.get("expense_type") or "").startswith("advertising"):
            dates.add(row.get("expense_date"))

    return dates


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

    organic_qty = max(total_qty - ad_qty, 0)
    organic_revenue = max(total_revenue - ad_revenue, 0)

    if ad_qty > total_qty and total_qty > 0:
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
        "calculation_status": calculation_status,
        "warning": ",".join(warnings) if warnings else None,
    }


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


def build_organic_rows(date_from, date_to):
    total_by_key = load_total_orders(date_from, date_to)
    ad_by_key, attribution_dates = load_ad_attribution(date_from, date_to)
    ad_expense_dates = load_ad_expense_dates(date_from, date_to)

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
    }

    return rows, summary


def save_rows(rows):
    if not rows:
        print("Нет Ozon organic rows для записи")
        return

    prepared_rows = []
    for row in rows:
        prepared = dict(row)
        prepared.pop("total_revenue_source", None)
        prepared_rows.append(prepared)

    for batch in chunks(prepared_rows, 500):
        try:
            supabase.table("ozon_daily_sku_organic").upsert(
                batch,
                on_conflict="sale_date,marketplace_code,marketplace_sku",
            ).execute()
        except Exception as e:
            print(
                "Не удалось записать ozon_daily_sku_organic. "
                "Проверьте миграцию sql/20260506_create_ozon_daily_sku_organic.sql. "
                f"Ошибка: {e}"
            )
            return

    print(f"✅ ozon_daily_sku_organic обновлена: {len(prepared_rows)} строк")


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

    rows, summary = build_organic_rows(date_from, date_to)

    print("Ozon organic summary:")
    print(summary)

    if args.debug_sample or args.dry_run:
        print("Ozon organic sample rows:")
        print_sample(rows)

    if args.dry_run:
        print("Dry run: ozon_daily_sku_organic не обновлялась")
        return

    save_rows(rows)


if __name__ == "__main__":
    main()
