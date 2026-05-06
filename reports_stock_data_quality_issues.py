import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client


load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")
TABLE_NAME = "stock_data_quality_issues"

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def parse_args():
    parser = argparse.ArgumentParser(description="Build stock data quality issues for Ozon decision layer.")
    parser.add_argument("--date", help="single-day shortcut, sets both --date-from and --date-to")
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--days-back", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug-sample", action="store_true")
    return parser.parse_args()


def resolve_date_range(args):
    if args.date:
        return args.date, args.date

    date_to = args.date_to or (datetime.now(ZoneInfo(APP_TIMEZONE)).date() - timedelta(days=1)).isoformat()
    if args.date_from:
        return args.date_from, date_to

    date_from = (datetime.fromisoformat(date_to).date() - timedelta(days=args.days_back - 1)).isoformat()
    return date_from, date_to


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
        if order:
            query = query.order(order, desc=desc)
        result = query.range(start, start + page_size - 1).execute()
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size

    return rows


def num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def rows_by_key(rows, key):
    grouped = defaultdict(list)
    for row in rows:
        value = str(row.get(key) or "").strip()
        if value:
            grouped[value].append(row)
    return grouped


def latest_stock_snapshot_date():
    rows = fetch_all(
        "stock_daily",
        filters=[("marketplace_code", "eq", "ozon")],
        order="stock_date",
        desc=True,
    )
    if not rows:
        return None
    return str(rows[0].get("stock_date") or "").strip() or None


def build_stock_quality_rows(date_from, date_to):
    stock_date = latest_stock_snapshot_date()
    if not stock_date:
        return [], {"rows": 0, "issue_distribution": {}, "unknown_count": 0}

    decision_rows = fetch_all(
        "sku_decision_daily_input",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("kpi_date", "gte", date_from),
            ("kpi_date", "lte", date_to),
        ],
        order="marketplace_sku",
    )
    stock_rows = fetch_all(
        "stock_daily",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("stock_date", "eq", stock_date),
        ],
        order="marketplace_sku",
    )
    stock_rows_all = fetch_all(
        "stock_daily",
        filters=[("marketplace_code", "eq", "ozon")],
        order="stock_date",
    )
    sku_catalog_rows = fetch_all(
        "sku_catalog",
        filters=[("marketplace_code", "eq", "ozon")],
        order="article",
    )
    order_rows = fetch_all(
        "marketplace_orders",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("order_date", "gte", date_from),
            ("order_date", "lte", date_to),
        ],
        order="order_date",
    )
    total_rows = fetch_all(
        "ozon_daily_sku_total_orders",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("sale_date", "gte", date_from),
            ("sale_date", "lte", date_to),
        ],
        order="sale_date",
    )
    ad_attr_rows = fetch_all(
        "ozon_daily_sku_ad_attribution",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("sale_date", "gte", date_from),
            ("sale_date", "lte", date_to),
        ],
        order="sale_date",
    )
    organic_rows = fetch_all(
        "ozon_daily_sku_organic",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("sale_date", "gte", date_from),
            ("sale_date", "lte", date_to),
        ],
        order="sale_date",
    )

    stock_by_article = rows_by_key(stock_rows, "article")
    stock_by_product_id = rows_by_key(stock_rows, "product_id")
    stock_by_decision_sku = rows_by_key(stock_rows, "decision_marketplace_sku")
    stock_by_stock_sku = rows_by_key(stock_rows, "stock_marketplace_sku")
    stock_history_by_article = rows_by_key(stock_rows_all, "article")

    catalog_by_article = rows_by_key(sku_catalog_rows, "article")
    catalog_by_marketplace_sku = rows_by_key(sku_catalog_rows, "marketplace_sku")
    catalog_by_product_id = rows_by_key(sku_catalog_rows, "product_id")

    orders_by_sku = rows_by_key(order_rows, "marketplace_sku")
    total_by_sku = rows_by_key(total_rows, "marketplace_sku")
    ad_attr_by_sku = rows_by_key(ad_attr_rows, "marketplace_sku")
    organic_by_sku = rows_by_key(organic_rows, "marketplace_sku")

    output = []
    issue_distribution = Counter()

    for row in decision_rows:
        issue_date = row.get("kpi_date")
        sku = str(row.get("marketplace_sku") or "").strip()
        article = str(row.get("article") or "").strip()
        product_name = str(row.get("product_name") or "").strip()
        stock_qty = row.get("stock_qty")
        stock_issue_type = "clean_stock_matched"
        stock_status = "stock_ok"
        suggested_fix = None
        evidence = {
            "latest_stock_date": stock_date,
            "decision_has_article": bool(article),
            "in_sku_catalog_by_marketplace_sku": bool(catalog_by_marketplace_sku.get(sku)),
            "in_sku_catalog_by_article": bool(article and catalog_by_article.get(article)),
            "in_marketplace_orders": bool(orders_by_sku.get(sku)),
            "in_total_source": bool(total_by_sku.get(sku)),
            "in_ad_attribution": bool(ad_attr_by_sku.get(sku)),
            "in_organic_rows": bool(organic_by_sku.get(sku)),
            "in_stock_by_article": bool(article and stock_by_article.get(article)),
            "in_stock_by_product_id": False,
            "in_stock_by_decision_marketplace_sku": bool(stock_by_decision_sku.get(sku)),
            "in_stock_by_stock_marketplace_sku": bool(stock_by_stock_sku.get(sku)),
            "stock_history_for_article": bool(article and stock_history_by_article.get(article)),
        }

        product_ids = {
            str(item.get("product_id") or "").strip()
            for item in (catalog_by_marketplace_sku.get(sku) or []) + (catalog_by_article.get(article) or [])
            if str(item.get("product_id") or "").strip()
        }
        evidence["product_ids_from_catalog"] = sorted(product_ids)
        evidence["in_stock_by_product_id"] = any(stock_by_product_id.get(pid) for pid in product_ids)

        if stock_qty is None:
            stock_status = "missing_stock"
            if not article:
                stock_issue_type = "missing_article"
                suggested_fix = "product_identity_loader"
                if not evidence["in_sku_catalog_by_marketplace_sku"]:
                    stock_issue_type = "decision_sku_not_mapped"
                    suggested_fix = "product_identity_loader"
            elif not evidence["in_stock_by_article"]:
                if evidence["stock_history_for_article"]:
                    stock_issue_type = "stock_product_unresolved"
                    suggested_fix = "identity_mapping_review"
                else:
                    stock_issue_type = "article_not_in_stock_source"
                    suggested_fix = "stock_source_coverage_review"
            else:
                stock_issue_type = "unknown"
                suggested_fix = "manual_review"
        elif num(stock_qty) <= 0:
            stock_status = "stock_out"
            stock_issue_type = "stock_out"
            suggested_fix = "none"
        else:
            stock_status = "stock_ok"
            stock_issue_type = "clean_stock_matched"
            suggested_fix = "none"

        issue_distribution[stock_issue_type] += 1
        output.append(
            {
                "issue_date": issue_date,
                "marketplace_code": "ozon",
                "marketplace_sku": sku,
                "article": article or None,
                "product_name": product_name or None,
                "stock_status": stock_status,
                "issue_type": stock_issue_type,
                "issue_reason": stock_issue_type,
                "orders_revenue": num(row.get("orders_revenue")),
                "ad_spend": num(row.get("ad_spend")),
                "ad_orders_revenue": num(row.get("ad_attributed_revenue")),
                "organic_revenue": num(row.get("organic_revenue")),
                "stock_qty": None if stock_qty is None else num(stock_qty),
                "available_qty": None if stock_qty is None else num(stock_qty),
                "evidence": evidence,
                "suggested_fix": suggested_fix,
                "severity": "warning" if stock_issue_type in {"clean_stock_matched", "stock_out"} else "issue",
                "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            }
        )

    summary = {
        "rows": len(output),
        "issue_distribution": dict(issue_distribution),
        "unknown_count": issue_distribution.get("unknown", 0),
        "stock_ok": issue_distribution.get("clean_stock_matched", 0),
        "stock_out": issue_distribution.get("stock_out", 0),
        "missing_stock_explained": sum(
            count for key, count in issue_distribution.items() if key not in {"clean_stock_matched", "stock_out", "unknown"}
        ),
    }
    return output, summary


def save_rows(rows):
    if not rows:
        print("Нет stock data quality rows для записи")
        return
    for i in range(0, len(rows), 500):
        supabase.table(TABLE_NAME).upsert(
            rows[i:i + 500],
            on_conflict="issue_date,marketplace_code,marketplace_sku",
        ).execute()
    print(f"✅ {TABLE_NAME} обновлена: {len(rows)} строк")


def print_sample(rows, limit=20):
    for row in rows[:limit]:
        print(row)


def main():
    args = parse_args()
    date_from, date_to = resolve_date_range(args)
    rows, summary = build_stock_quality_rows(date_from, date_to)
    print("Stock data quality summary:")
    print(json.dumps({"date_from": date_from, "date_to": date_to, **summary}, ensure_ascii=False, indent=2))
    if args.debug_sample or args.dry_run:
        print_sample(rows)
    if args.dry_run:
        print("Dry run: stock_data_quality_issues не обновлялась")
        return
    save_rows(rows)


if __name__ == "__main__":
    main()
