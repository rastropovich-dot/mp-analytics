import argparse
import json
import os
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from supabase import create_client


load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")
TABLE_NAME = "stock_data_quality_issues"

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

READ_RETRY_SLEEP_SECONDS = (2, 5)
TRANSIENT_READ_ERRORS = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    TimeoutError,
)


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


def execute_read_with_retry(execute_fn, label, max_attempts=3, sleep_seconds=READ_RETRY_SLEEP_SECONDS):
    attempt = 0

    while True:
        attempt += 1
        try:
            return execute_fn()
        except TRANSIENT_READ_ERRORS as exc:
            if attempt >= max_attempts:
                raise

            sleep_for = sleep_seconds[min(attempt - 1, len(sleep_seconds) - 1)] if sleep_seconds else 0
            print(
                f"[read-retry] label={label} attempt={attempt} error={exc.__class__.__name__} sleep={sleep_for}s"
            )
            if sleep_for > 0:
                time.sleep(sleep_for)


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
        result = execute_read_with_retry(
            lambda: query.range(start, start + page_size - 1).execute(),
            label=f"stock:{table}:{start}",
        )
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size

    return rows


def build_filters(filters=None, query=None):
    query = query or supabase
    for field, operator, value in filters or []:
        if operator == "eq":
            query = query.eq(field, value)
        elif operator == "gte":
            query = query.gte(field, value)
        elif operator == "lte":
            query = query.lte(field, value)
    return query


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


def latest_stock_snapshot_date(marketplace_code="ozon"):
    query = supabase.table("stock_daily").select("stock_date")
    query = build_filters([("marketplace_code", "eq", marketplace_code)], query=query)
    query = query.order("stock_date", desc=True)
    result = execute_read_with_retry(
        lambda: query.range(0, 0).execute(),
        label=f"stock:latest_snapshot_date:{marketplace_code}",
    )
    rows = result.data or []
    if not rows:
        return None
    return str(rows[0].get("stock_date") or "").strip() or None


def build_stock_quality_rows(date_from, date_to, sku_filter=None, article_filter=None):
    stock_date = latest_stock_snapshot_date()
    if not stock_date:
        return [], {"rows": 0, "issue_distribution": {}, "unknown_count": 0}

    decision_filters = [
        ("marketplace_code", "eq", "ozon"),
        ("kpi_date", "gte", date_from),
        ("kpi_date", "lte", date_to),
    ]
    if sku_filter:
        decision_filters.append(("marketplace_sku", "eq", str(sku_filter)))

    decision_rows = fetch_all(
        "sku_decision_daily_input",
        filters=decision_filters,
        order="marketplace_sku",
    )
    target_article = article_filter
    if not target_article and decision_rows:
        target_article = str(decision_rows[0].get("article") or "").strip() or None

    stock_filters = [
        ("marketplace_code", "eq", "ozon"),
        ("stock_date", "eq", stock_date),
    ]
    stock_history_filters = [("marketplace_code", "eq", "ozon")]
    if target_article:
        stock_filters.append(("article", "eq", target_article))
        stock_history_filters.append(("article", "eq", target_article))

    stock_rows = fetch_all(
        "stock_daily",
        filters=stock_filters,
        order="marketplace_sku",
    )
    stock_rows_all = fetch_all(
        "stock_daily",
        filters=stock_history_filters,
        order="stock_date",
    )
    catalog_filters = [("marketplace_code", "eq", "ozon")]
    if sku_filter:
        catalog_filters.append(("marketplace_sku", "eq", str(sku_filter)))
    elif target_article:
        catalog_filters.append(("article", "eq", target_article))

    sku_catalog_rows = fetch_all("sku_catalog", filters=catalog_filters, order="article")

    fact_filters = [
        "marketplace_orders",
        [
            ("marketplace_code", "eq", "ozon"),
            ("order_date", "gte", date_from),
            ("order_date", "lte", date_to),
        ],
        "order_date",
    ]
    if sku_filter:
        fact_filters[1].append(("marketplace_sku", "eq", str(sku_filter)))

    order_rows = fetch_all(fact_filters[0], filters=fact_filters[1], order=fact_filters[2])

    total_filters = [
        ("marketplace_code", "eq", "ozon"),
        ("sale_date", "gte", date_from),
        ("sale_date", "lte", date_to),
    ]
    if sku_filter:
        total_filters.append(("marketplace_sku", "eq", str(sku_filter)))
    total_rows = fetch_all(
        "ozon_daily_sku_total_orders",
        filters=total_filters,
        order="sale_date",
    )

    ad_attr_filters = [
        ("marketplace_code", "eq", "ozon"),
        ("sale_date", "gte", date_from),
        ("sale_date", "lte", date_to),
    ]
    if sku_filter:
        ad_attr_filters.append(("marketplace_sku", "eq", str(sku_filter)))
    ad_attr_rows = fetch_all(
        "ozon_daily_sku_ad_attribution",
        filters=ad_attr_filters,
        order="sale_date",
    )

    organic_filters = [
        ("marketplace_code", "eq", "ozon"),
        ("sale_date", "gte", date_from),
        ("sale_date", "lte", date_to),
    ]
    if sku_filter:
        organic_filters.append(("marketplace_sku", "eq", str(sku_filter)))
    organic_rows = fetch_all(
        "ozon_daily_sku_organic",
        filters=organic_filters,
        order="sale_date",
    )

    issue_filters = [
        ("marketplace_code", "eq", "ozon"),
        ("issue_date", "gte", date_from),
        ("issue_date", "lte", date_to),
    ]
    if sku_filter:
        issue_filters.append(("marketplace_sku", "eq", str(sku_filter)))
    existing_issue_rows = fetch_all(
        TABLE_NAME,
        filters=issue_filters,
        order="issue_date",
    )
    identity_filters = [("marketplace_code", "eq", "ozon")]
    if sku_filter:
        identity_filters.append(("decision_marketplace_sku", "eq", str(sku_filter)))
    elif target_article:
        identity_filters.append(("article", "eq", target_article))
    identity_rows = fetch_all("ozon_product_identity", filters=identity_filters, order="identity_key")

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
    existing_issue_by_sku = rows_by_key(existing_issue_rows, "marketplace_sku")
    identity_by_product_id = rows_by_key(identity_rows, "product_id")
    identity_by_article = rows_by_key(identity_rows, "article")
    identity_by_decision_sku = rows_by_key(identity_rows, "decision_marketplace_sku")

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

        identity_candidates = []
        for pid in sorted(product_ids):
            identity_candidates.extend(identity_by_product_id.get(pid) or [])
        if article:
            identity_candidates.extend(identity_by_article.get(article) or [])
        identity_candidates.extend(identity_by_decision_sku.get(sku) or [])

        stock_api_blocks = []
        for candidate in identity_candidates:
            candidate_evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {}
            stock_api = candidate_evidence.get("stock_api")
            if isinstance(stock_api, dict):
                stock_api_blocks.append(stock_api)

        evidence["stock_api_verified"] = any(block.get("verified") is True for block in stock_api_blocks)
        evidence["stock_api_returned_stock_rows"] = any(block.get("returned_stock_rows") is True for block in stock_api_blocks)
        evidence["stock_api_verified_no_stock_rows"] = any(
            block.get("verified") is True and block.get("returned_stock_rows") is False
            for block in stock_api_blocks
        )

        if stock_qty is None:
            stock_status = "missing_stock"
            if not article:
                stock_issue_type = "missing_article"
                suggested_fix = "product_identity_loader"
                if not evidence["in_sku_catalog_by_marketplace_sku"]:
                    stock_issue_type = "decision_sku_not_mapped"
                    suggested_fix = "product_identity_loader"
            elif not evidence["in_stock_by_article"]:
                if evidence["stock_api_verified"] and evidence["stock_api_returned_stock_rows"]:
                    stock_issue_type = "stock_api_returned_but_not_in_stock_daily"
                    suggested_fix = "stock_daily_reload_or_identity_alignment"
                elif evidence["stock_api_verified_no_stock_rows"]:
                    stock_issue_type = "stock_api_verified_no_stock_rows"
                    suggested_fix = "none"
                elif evidence["stock_history_for_article"]:
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

        existing_issue = next(
            (
                item
                for item in (existing_issue_by_sku.get(sku) or [])
                if str(item.get("issue_date") or "") == str(issue_date or "")
            ),
            None,
        )
        existing_evidence = existing_issue.get("evidence") if existing_issue else {}
        if not isinstance(existing_evidence, dict):
            existing_evidence = {}
        if (
            stock_issue_type == "decision_sku_not_mapped"
            and existing_issue
            and existing_issue.get("issue_type") == "product_identity_not_returned_by_ozon_api"
            and existing_evidence.get("result") == "not_returned"
        ):
            stock_issue_type = "product_identity_not_returned_by_ozon_api"
            suggested_fix = "manual_identity_review_or_product_list_refresh"
            evidence["product_status"] = existing_evidence.get("product_status") or "not_returned_by_current_ozon_api"
            evidence["searched_by"] = existing_evidence.get("searched_by") or ["sku"]
            evidence["endpoints_tried"] = existing_evidence.get("endpoints_tried") or ["/v3/product/list", "/v3/product/info/list"]
            evidence["result"] = "not_returned"
            evidence["possible_reason"] = existing_evidence.get("possible_reason") or "inactive_or_not_visible_possible"

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
