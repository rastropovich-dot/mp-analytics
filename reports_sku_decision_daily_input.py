import argparse
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client
from reports_ozon_organic_reconciliation_issues import build_reconciliation_rows
from reports_stock_data_quality_issues import build_stock_quality_rows


load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def parse_args():
    parser = argparse.ArgumentParser(description="Build daily SKU decision input table for Ozon.")
    parser.add_argument(
        "--mode",
        choices=("daily-yesterday", "full"),
        default="full",
        help="daily-yesterday = production D-1 build; full = explicit date/date-range",
    )
    parser.add_argument("--date", help="single-day shortcut, sets both --date-from and --date-to")
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--days-back", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug-sample", action="store_true")
    return parser.parse_args()


def resolve_date_range(args):
    if args.date:
        if args.date_from or args.date_to:
            raise RuntimeError("--date нельзя комбинировать с --date-from/--date-to")
        return args.date, args.date

    if args.mode == "daily-yesterday":
        target_date = datetime.now(ZoneInfo(APP_TIMEZONE)).date() - timedelta(days=1)
        return target_date.isoformat(), target_date.isoformat()

    date_to = args.date_to or date.today().isoformat()
    if args.date_from:
        return args.date_from, date_to

    date_from = (datetime.fromisoformat(date_to).date() - timedelta(days=args.days_back)).isoformat()
    return date_from, date_to


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def safe_div(numerator, denominator):
    denominator = num(denominator)
    if denominator <= 0:
        return None
    return num(numerator) / denominator


def fetch_all(table, filters=None, order=None, desc=False):
    rows = []
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
            elif operator == "lt":
                query = query.lt(field, value)
            elif operator == "gt":
                query = query.gt(field, value)

        if order:
            query = query.order(order, desc=desc)

        result = query.range(start, start + page_size - 1).execute()
        batch = result.data or []
        rows.extend(batch)

        if len(batch) < page_size:
            break

        start += page_size

    return rows


def load_daily_kpi(history_from, date_to):
    return fetch_all(
        "daily_sku_kpi",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("kpi_date", "gte", history_from),
            ("kpi_date", "lte", date_to),
        ],
        order="kpi_date",
    )


def load_organic_rows(date_from, date_to):
    try:
        return fetch_all(
            "ozon_daily_sku_organic",
            filters=[
                ("marketplace_code", "eq", "ozon"),
                ("sale_date", "gte", date_from),
                ("sale_date", "lte", date_to),
            ],
            order="sale_date",
        )
    except Exception as exc:
        print(f"WARNING: Не удалось загрузить ozon_daily_sku_organic: {exc}")
        return []


def build_decision_sku_by_article_map(history_from, date_to):
    counters = defaultdict(Counter)

    order_rows = fetch_all(
        "marketplace_orders",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("order_date", "gte", history_from),
            ("order_date", "lte", date_to),
        ],
        order="order_date",
    )
    for row in order_rows:
        article = str(row.get("article") or "").strip()
        sku = str(row.get("marketplace_sku") or "").strip()
        if article and sku:
            counters[article][sku] += 1

    attr_rows = fetch_all(
        "ozon_daily_sku_ad_attribution",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("sale_date", "gte", history_from),
            ("sale_date", "lte", date_to),
        ],
        order="sale_date",
    )
    for row in attr_rows:
        article = str(row.get("article") or "").strip()
        sku = str(row.get("marketplace_sku") or "").strip()
        if article and sku:
            counters[article][sku] += 1

    return {
        article: sku_counts.most_common(1)[0][0]
        for article, sku_counts in counters.items()
        if len(sku_counts) == 1
    }


def aggregate_stock_rows(rows, key_field):
    latest_date_by_key = {}

    for row in rows:
        key = str(row.get(key_field) or "").strip()
        row_date = str(row.get("stock_date") or "").strip()
        if not key or not row_date:
            continue
        existing_date = latest_date_by_key.get(key)
        if existing_date is None or existing_date <= row_date:
            latest_date_by_key[key] = row_date

    aggregated = {}

    for row in rows:
        key = str(row.get(key_field) or "").strip()
        article = str(row.get("article") or "").strip()
        row_date = str(row.get("stock_date") or "").strip()
        if not key or not row_date:
            continue
        if latest_date_by_key.get(key) != row_date:
            continue

        if key not in aggregated:
            aggregated[key] = {
                "stock_date": row_date,
                key_field: key,
                "article": article,
                "stock_qty": 0.0,
                "reserved_qty": 0.0,
                "available_qty": 0.0,
                "stock_identity_status": str(row.get("stock_identity_status") or "").strip() or None,
            }

        aggregated[key]["stock_qty"] += num(row.get("stock_qty"))
        aggregated[key]["reserved_qty"] += num(row.get("reserved_qty"))
        aggregated[key]["available_qty"] += num(row.get("available_qty"))
        if not aggregated[key].get("article") and article:
            aggregated[key]["article"] = article
        if not aggregated[key].get("stock_identity_status"):
            status = str(row.get("stock_identity_status") or "").strip()
            if status:
                aggregated[key]["stock_identity_status"] = status

    return aggregated


def load_recent_stock(history_from, date_to):
    stock_from = (datetime.now(ZoneInfo(APP_TIMEZONE)).date() - timedelta(days=30)).isoformat()
    decision_sku_by_article = build_decision_sku_by_article_map(history_from, date_to)
    rows = fetch_all(
        "stock_daily",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("stock_date", "gte", stock_from),
        ],
        order="stock_date",
    )

    latest_by_stock_sku = aggregate_stock_rows(rows, "marketplace_sku")
    latest_by_article = aggregate_stock_rows(rows, "article")

    decision_rows = []
    for stock in latest_by_stock_sku.values():
        article = str(stock.get("article") or "").strip()
        decision_sku = str(stock.get("decision_marketplace_sku") or "").strip()
        if not decision_sku and article:
            decision_sku = decision_sku_by_article.get(article, "")
        if not decision_sku:
            continue

        decision_rows.append(
            {
                "stock_date": stock.get("stock_date"),
                "decision_marketplace_sku": decision_sku,
                "article": article,
                "stock_qty": stock.get("stock_qty"),
                "reserved_qty": stock.get("reserved_qty"),
                "available_qty": stock.get("available_qty"),
                "stock_identity_status": stock.get("stock_identity_status") or (
                    "mapped_by_article" if article else "unresolved"
                ),
            }
        )

    latest_by_decision_sku = aggregate_stock_rows(decision_rows, "decision_marketplace_sku")

    return {
        "by_stock_sku": latest_by_stock_sku,
        "by_decision_sku": latest_by_decision_sku,
        "by_article": latest_by_article,
        "decision_sku_by_article_count": len(decision_sku_by_article),
    }


def load_identity_stock_evidence():
    rows = fetch_all(
        "ozon_product_identity",
        filters=[("marketplace_code", "eq", "ozon")],
        order="identity_key",
    )

    by_decision_sku = {}
    by_article = {}

    for row in rows:
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        stock_api = evidence.get("stock_api")
        if not isinstance(stock_api, dict) or stock_api.get("verified") is not True:
            continue

        total_available = stock_api.get("total_available")
        total_present = stock_api.get("total_present")
        total_reserved = stock_api.get("total_reserved")
        stock_qty = total_available
        if stock_qty is None and total_present is not None and total_reserved is not None:
            stock_qty = num(total_present) - num(total_reserved)
        if stock_qty is None and total_present is not None:
            stock_qty = num(total_present)

        record = {
            "stock_qty": None if stock_qty is None else num(stock_qty),
            "available_qty": None if total_available is None else num(total_available),
            "stock_as_of_date": str(stock_api.get("verified_at") or "")[:10] or None,
            "stock_verified_at": stock_api.get("verified_at"),
            "returned_stock_rows": stock_api.get("returned_stock_rows"),
            "note": "not sale_date snapshot, identity loader snapshot",
        }

        decision_sku = str(row.get("decision_marketplace_sku") or "").strip()
        article = str(row.get("article") or "").strip()
        if decision_sku and decision_sku not in by_decision_sku:
            by_decision_sku[decision_sku] = record
        if article and article not in by_article:
            by_article[article] = record

    return {
        "by_decision_sku": by_decision_sku,
        "by_article": by_article,
    }


def load_recent_price_points(history_from, date_to):
    rows = fetch_all(
        "marketplace_orders",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("order_date", "gte", history_from),
            ("order_date", "lte", date_to),
        ],
        order="order_date",
    )

    latest = {}
    for row in rows:
        sku = str(row.get("marketplace_sku") or "")
        order_date = row.get("order_date")
        qty = num(row.get("orders_qty"))
        if not sku or not order_date or qty <= 0:
            continue
        unit_price = num(row.get("orders_amount_seller")) / qty
        existing = latest.get(sku)
        if not existing or str(existing["order_date"]) <= str(order_date):
            latest[sku] = {
                "current_price": round(unit_price, 2),
                "current_price_source": "latest_order_unit_price",
                "order_date": order_date,
            }
    return latest


def load_latest_ozon_run_status(date_from, date_to):
    try:
        rows = fetch_all(
            "ozon_performance_daily_load_status",
            filters=[
                ("marketplace_code", "eq", "ozon"),
                ("target_date", "gte", date_from),
                ("target_date", "lte", date_to),
            ],
            order="updated_at",
            desc=True,
        )
    except Exception as exc:
        print(f"WARNING: Не удалось загрузить ozon_performance_daily_load_status: {exc}")
        return {}

    latest = {}
    mode_rank = {
        "daily-yesterday": 3,
        "full": 2,
        "cpc-backfill": 1,
    }
    for row in rows:
        target_date = row.get("target_date")
        if not target_date:
            continue
        existing = latest.get(target_date)
        if not existing:
            latest[target_date] = row
            continue
        existing_rank = mode_rank.get(str(existing.get("mode") or ""), 0)
        current_rank = mode_rank.get(str(row.get("mode") or ""), 0)
        if current_rank > existing_rank:
            latest[target_date] = row
    return latest


def build_history_indexes(kpi_rows):
    by_sku = defaultdict(list)
    current_rows = {}

    for row in kpi_rows:
        sku = str(row.get("marketplace_sku") or "")
        kpi_date = row.get("kpi_date")
        if not sku or not kpi_date:
            continue
        by_sku[sku].append(row)
        current_rows[(kpi_date, sku)] = row

    for rows in by_sku.values():
        rows.sort(key=lambda row: str(row.get("kpi_date") or ""))

    return by_sku, current_rows


def tokens(value):
    if not value:
        return set()
    return {item.strip() for item in str(value).split(",") if item and item.strip()}


def build_rows(date_from, date_to):
    history_from = (datetime.fromisoformat(date_from).date() - timedelta(days=29)).isoformat()
    kpi_rows = load_daily_kpi(history_from, date_to)
    by_sku, current_rows = build_history_indexes(kpi_rows)
    organic_rows = {
        (row.get("sale_date"), str(row.get("marketplace_sku") or "")): row
        for row in load_organic_rows(date_from, date_to)
        if row.get("sale_date") and row.get("marketplace_sku")
    }
    latest_stock = load_recent_stock(history_from, date_to)
    latest_stock_by_stock_sku = latest_stock.get("by_stock_sku", {})
    latest_stock_by_decision_sku = latest_stock.get("by_decision_sku", {})
    latest_stock_by_article = latest_stock.get("by_article", {})
    identity_stock = load_identity_stock_evidence()
    identity_stock_by_decision_sku = identity_stock.get("by_decision_sku", {})
    identity_stock_by_article = identity_stock.get("by_article", {})
    latest_price = load_recent_price_points(history_from, date_to)
    latest_run_status = load_latest_ozon_run_status(date_from, date_to)
    stock_quality_rows, _ = build_stock_quality_rows(date_from, date_to)
    stock_quality_by_key = {
        (row.get("issue_date"), str(row.get("marketplace_sku") or "")): row
        for row in stock_quality_rows
        if row.get("issue_date") and row.get("marketplace_sku")
    }
    reconciliation_rows, _ = build_reconciliation_rows(date_from, date_to)
    reconciliation_by_key = {
        (row.get("sale_date"), str(row.get("marketplace_sku") or "")): row
        for row in reconciliation_rows
        if row.get("sale_date") and row.get("marketplace_sku")
    }

    rows = []
    summary = defaultdict(int)

    for (kpi_date, sku), row in sorted(current_rows.items()):
        if not (date_from <= kpi_date <= date_to):
            continue

        history_rows = by_sku.get(sku, [])
        window_14_from = (datetime.fromisoformat(kpi_date).date() - timedelta(days=13)).isoformat()
        window_30_from = (datetime.fromisoformat(kpi_date).date() - timedelta(days=29)).isoformat()

        window_14 = [item for item in history_rows if window_14_from <= str(item.get("kpi_date") or "") <= kpi_date]
        window_30 = [item for item in history_rows if window_30_from <= str(item.get("kpi_date") or "") <= kpi_date]

        orders_14 = sum(num(item.get("orders_qty")) for item in window_14)
        buyouts_14 = sum(num(item.get("buyouts_qty")) for item in window_14)
        orders_30 = sum(num(item.get("orders_qty")) for item in window_30)
        buyouts_30 = sum(num(item.get("buyouts_qty")) for item in window_30)
        buyouts_rev_30 = sum(num(item.get("buyouts_amount_seller")) for item in window_30)

        buyout_rate_14d = safe_div(buyouts_14, orders_14)
        buyout_rate_30d = safe_div(buyouts_30, orders_30)
        buyout_rate_for_model = buyout_rate_14d if buyout_rate_14d is not None else buyout_rate_30d

        commission_rate_30d = safe_div(sum(num(item.get("commission_amount")) for item in window_30), buyouts_rev_30) or 0
        logistics_rate_30d = safe_div(sum(num(item.get("logistics_amount")) for item in window_30), buyouts_rev_30) or 0
        other_rate_30d = safe_div(sum(num(item.get("other_expenses_amount")) for item in window_30), buyouts_rev_30) or 0

        orders_revenue = num(row.get("orders_amount_seller"))
        ad_attributed_revenue = num(row.get("ad_orders_revenue"))
        organic_revenue = num(row.get("organic_orders_revenue"))
        ad_spend = num(row.get("ad_spend"))

        expected_revenue_after_buyout = (
            round(orders_revenue * buyout_rate_for_model, 2)
            if buyout_rate_for_model is not None
            else None
        )
        if expected_revenue_after_buyout is not None:
            expected_margin_after_ads = round(
                expected_revenue_after_buyout
                - (expected_revenue_after_buyout * commission_rate_30d)
                - (expected_revenue_after_buyout * logistics_rate_30d)
                - (expected_revenue_after_buyout * other_rate_30d)
                - ad_spend,
                2,
            )
        else:
            expected_margin_after_ads = None

        organic_row = organic_rows.get((kpi_date, sku), {})
        stock_quality_row = stock_quality_by_key.get((kpi_date, sku), {})
        reconciliation_row = reconciliation_by_key.get((kpi_date, sku), {})
        price_info = latest_price.get(sku, {})
        stock_info = latest_stock_by_decision_sku.get(sku, {})
        if not stock_info:
            article = str(row.get("article") or organic_row.get("article") or "").strip()
            if article:
                stock_info = latest_stock_by_article.get(article, {})
        if not stock_info:
            stock_info = latest_stock_by_stock_sku.get(sku, {})

        stock_source_kind = "stock_daily"
        used_identity_stock_fallback = False
        if not stock_info:
            stock_info = identity_stock_by_decision_sku.get(sku, {})
            if not stock_info:
                article = str(row.get("article") or organic_row.get("article") or "").strip()
                if article:
                    stock_info = identity_stock_by_article.get(article, {})
            if stock_info:
                stock_source_kind = "product_identity_stock_api_evidence"
                used_identity_stock_fallback = True

        run_status_row = latest_run_status.get(kpi_date, {})

        quality_flags = []
        run_status = str(run_status_row.get("run_status") or "")
        organic_status = str(organic_row.get("calculation_status") or "")
        organic_warning = str(organic_row.get("warning") or "")
        stock_status = str(stock_quality_row.get("stock_status") or "")
        stock_issue_type = str(stock_quality_row.get("issue_type") or "")
        organic_reconciliation_status = str(reconciliation_row.get("reconciliation_status") or "")
        unreconciled_revenue = num(reconciliation_row.get("unreconciled_revenue"))

        if run_status in {"partial_ads", "partial_quota", "failed"}:
            quality_flags.append(run_status)
        if not organic_row and ad_spend > 0:
            quality_flags.append("missing_organic_attribution")
        if organic_status and organic_status != "ok":
            quality_flags.append(organic_status)
        if organic_warning:
            quality_flags.extend(sorted(tokens(organic_warning)))
        if buyout_rate_for_model is None:
            quality_flags.append("missing_buyout_rate")
        if orders_30 <= 0:
            quality_flags.append("low_history")
        stock_qty_value = stock_info.get("available_qty")
        if stock_qty_value is None:
            stock_qty_value = stock_info.get("stock_qty")

        if used_identity_stock_fallback:
            quality_flags.append("stock_from_identity_evidence")

        if stock_status == "missing_stock":
            quality_flags.append(stock_issue_type or "missing_stock")
        elif stock_status == "stock_out":
            quality_flags.append("stock_out")
        elif stock_qty_value is None:
            quality_flags.append("missing_stock")
        elif num(stock_qty_value) <= 0:
            quality_flags.append("stock_out")

        if organic_reconciliation_status and organic_reconciliation_status != "clean":
            quality_flags.append(organic_reconciliation_status)
        if num(row.get("orders_qty")) <= 0:
            quality_flags.append("low_data_volume")

        final_stock_status = stock_status or (
            "stock_out" if stock_qty_value is not None and num(stock_qty_value) <= 0 else "stock_ok" if stock_qty_value is not None else "missing_stock"
        )
        final_stock_issue_type = stock_issue_type or (
            "stock_out" if stock_qty_value is not None and num(stock_qty_value) <= 0 else "clean_stock_matched" if stock_qty_value is not None else "missing_stock"
        )

        if used_identity_stock_fallback:
            if stock_qty_value is None:
                final_stock_status = "missing_stock"
                final_stock_issue_type = "stock_from_identity_evidence"
            elif num(stock_qty_value) <= 0:
                final_stock_status = "stock_out"
                final_stock_issue_type = "stock_from_identity_evidence"
            else:
                final_stock_status = "stock_from_identity_evidence"
                final_stock_issue_type = "stock_from_identity_evidence"

        quality_flags = sorted(set(flag for flag in quality_flags if flag))
        data_quality_status = "ok" if not quality_flags else ",".join(quality_flags)
        decision_status = "ready" if data_quality_status == "ok" else "hold"

        decision_row = {
            "kpi_date": kpi_date,
            "marketplace_code": "ozon",
            "marketplace_sku": sku,
            "article": row.get("article") or organic_row.get("article") or "",
            "product_name": row.get("product_name") or organic_row.get("product_name") or "",
            "current_price": price_info.get("current_price"),
            "current_price_source": price_info.get("current_price_source"),
            "orders_qty": num(row.get("orders_qty")),
            "orders_revenue": orders_revenue,
            "buyouts_qty": num(row.get("buyouts_qty")),
            "buyouts_revenue": num(row.get("buyouts_amount_seller")),
            "buyout_rate_rolling_14d": round(buyout_rate_14d, 4) if buyout_rate_14d is not None else None,
            "buyout_rate_rolling_30d": round(buyout_rate_30d, 4) if buyout_rate_30d is not None else None,
            "ad_spend": ad_spend,
            "ad_attributed_revenue": ad_attributed_revenue,
            "organic_revenue": organic_revenue,
            "ad_share_revenue": round(ad_attributed_revenue / orders_revenue, 4) if orders_revenue > 0 else None,
            "organic_share_revenue": round(organic_revenue / orders_revenue, 4) if orders_revenue > 0 else None,
            "commission": num(row.get("commission_amount")),
            "logistics": num(row.get("logistics_amount")),
            "other_expenses": num(row.get("other_expenses_amount")),
            "expected_revenue_after_buyout": expected_revenue_after_buyout,
            "expected_margin_after_ads": expected_margin_after_ads,
            "stock_qty": (
                None
                if stock_qty_value is None
                else num(stock_qty_value)
            ),
            "stock_status": final_stock_status,
            "stock_issue_type": final_stock_issue_type,
            "stock_as_of_date": stock_info.get("stock_date") or stock_info.get("stock_as_of_date"),
            "organic_reconciliation_status": organic_reconciliation_status or "clean",
            "unreconciled_revenue": unreconciled_revenue,
            "source_run_status": run_status or None,
            "decision_status": decision_status,
            "data_quality_status": data_quality_status,
            "warning": (
                organic_warning
                if not used_identity_stock_fallback
                else ", ".join(
                    [part for part in [
                        organic_warning or None,
                        "stock_source:product_identity_stock_api_evidence",
                        stock_info.get("note"),
                        f"stock_verified_at:{stock_info.get('stock_verified_at')}" if stock_info.get("stock_verified_at") else None,
                    ] if part]
                ) or None
            ),
            "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        }
        rows.append(decision_row)
        summary["rows"] += 1
        summary[f"decision_status:{decision_status}"] += 1
        summary[f"data_quality:{data_quality_status}"] += 1

    return rows, summary


def save_rows(rows):
    if not rows:
        print("Нет decision input строк для записи")
        return

    for batch in chunks(rows, 500):
        supabase.table("sku_decision_daily_input").upsert(
            batch,
            on_conflict="kpi_date,marketplace_code,marketplace_sku",
        ).execute()

    print(f"✅ sku_decision_daily_input обновлена: {len(rows)} строк")


def print_sample(rows, limit=10):
    for row in rows[:limit]:
        print(row)


def main():
    args = parse_args()
    date_from, date_to = resolve_date_range(args)
    rows, summary = build_rows(date_from, date_to)

    print("SKU decision input summary:")
    print(
        {
            "date_from": date_from,
            "date_to": date_to,
            "rows": len(rows),
            "summary": dict(summary),
        }
    )

    if args.debug_sample or args.dry_run:
        print_sample(rows)

    if args.dry_run:
        print("Dry run: sku_decision_daily_input не обновлялась")
        return

    save_rows(rows)


if __name__ == "__main__":
    main()
