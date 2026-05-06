import argparse
import json
import math
import os
from collections import defaultdict
from datetime import date

from dotenv import load_dotenv
from supabase import create_client


load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

DEFAULT_MARKETPLACE = "ozon"
DEFAULT_INCLUDE_API_SOURCES = "product-list,info-list,attributes,stocks"
PRODUCT_LIST_LIMIT = 1000
INFO_LIST_LIMIT = 1000
STOCKS_BATCH_LIMIT = 100


def parse_args():
    parser = argparse.ArgumentParser(description="Plan Ozon product identity enrichment without touching live APIs by default.")
    parser.add_argument("--plan-only", action="store_true", help="Build read-only identity closure plan.")
    parser.add_argument("--dry-run", action="store_true", help="Reserved for future DB-only preparation without writes.")
    parser.add_argument("--live", action="store_true", help="Reserved future mode for live Ozon Seller API enrichment.")
    parser.add_argument("--marketplace", default=DEFAULT_MARKETPLACE)
    parser.add_argument("--only-tail", action="store_true", help="Focus on current stock tail from stock_data_quality_issues.")
    parser.add_argument(
        "--include-api-sources",
        default=DEFAULT_INCLUDE_API_SOURCES,
        help="Comma-separated Seller API sources to plan: product-list,info-list,attributes,stocks",
    )
    parser.add_argument("--max-requests", type=int, default=200)
    parser.add_argument("--limit-tail", type=int)
    parser.add_argument("--issue-date", help="Explicit stock_data_quality_issues date. Defaults to latest issue_date.")
    return parser.parse_args()


def parse_include_api_sources(raw_value):
    values = []
    seen = set()
    for item in str(raw_value or "").split(","):
        value = item.strip().lower()
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    return values


def fetch_all(table, filters=None, order=None, desc=False, page_size=1000):
    rows = []
    offset = 0

    while True:
        query = supabase.table(table).select("*")
        for field, operator, value in filters or []:
            if operator == "eq":
                query = query.eq(field, value)
            elif operator == "gte":
                query = query.gte(field, value)
            elif operator == "lte":
                query = query.lte(field, value)
            elif operator == "in":
                query = query.in_(field, value)
        if order:
            query = query.order(order, desc=desc)
        batch = query.range(offset, offset + page_size - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    return rows


def fetch_in_chunks(table, field, values, extra_filters=None, chunk_size=100):
    values = [value for value in values if value]
    rows = []
    for start in range(0, len(values), chunk_size):
        rows.extend(
            fetch_all(
                table,
                filters=(extra_filters or []) + [(field, "in", values[start:start + chunk_size])],
            )
        )
    return rows


def rows_by_key(rows, key):
    grouped = defaultdict(list)
    for row in rows:
        value = str(row.get(key) or "").strip()
        if value:
            grouped[value].append(row)
    return grouped


def num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def normalize_name(value):
    return " ".join(str(value or "").lower().split())


def latest_value(table, field, marketplace_code):
    rows = (
        supabase.table(table)
        .select(field)
        .eq("marketplace_code", marketplace_code)
        .order(field, desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0].get(field) if rows else None


def latest_issue_date(marketplace_code):
    rows = (
        supabase.table("stock_data_quality_issues")
        .select("issue_date")
        .eq("marketplace_code", marketplace_code)
        .order("issue_date", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0].get("issue_date") if rows else None


def ceil_div(value, divisor):
    if not value:
        return 0
    return int(math.ceil(float(value) / float(divisor)))


def build_tail_rows(issue_date, marketplace_code, limit_tail=None):
    rows = fetch_all(
        "stock_data_quality_issues",
        filters=[
            ("issue_date", "eq", issue_date),
            ("marketplace_code", "eq", marketplace_code),
            ("issue_type", "in", ["decision_sku_not_mapped", "article_not_in_stock_source"]),
        ],
        order="marketplace_sku",
    )

    if limit_tail:
        rows = sorted(rows, key=lambda row: (num(row.get("orders_revenue")), num(row.get("ad_spend"))), reverse=True)[:limit_tail]
    return rows


def build_plan(issue_date, marketplace_code=DEFAULT_MARKETPLACE, only_tail=True, limit_tail=None, include_api_sources=None):
    include_api_sources = include_api_sources or parse_include_api_sources(DEFAULT_INCLUDE_API_SOURCES)
    tail_rows = build_tail_rows(issue_date, marketplace_code, limit_tail=limit_tail) if only_tail else []

    decision_tail = [row for row in tail_rows if row.get("issue_type") == "decision_sku_not_mapped"]
    stock_source_tail = [row for row in tail_rows if row.get("issue_type") == "article_not_in_stock_source"]

    latest_stock_date = latest_value("stock_daily", "stock_date", marketplace_code)
    stock_latest_rows = fetch_all(
        "stock_daily",
        filters=[("marketplace_code", "eq", marketplace_code), ("stock_date", "eq", latest_stock_date)],
    ) if latest_stock_date else []

    sku_catalog_rows = fetch_all("sku_catalog", filters=[("marketplace_code", "eq", marketplace_code)])
    orders_rows = fetch_all(
        "marketplace_orders",
        filters=[("marketplace_code", "eq", marketplace_code), ("order_date", "eq", issue_date)],
    )
    total_orders_rows = fetch_all(
        "ozon_daily_sku_total_orders",
        filters=[("marketplace_code", "eq", marketplace_code), ("sale_date", "eq", issue_date)],
    )
    ad_rows = fetch_all(
        "ozon_daily_sku_ad_attribution",
        filters=[("marketplace_code", "eq", marketplace_code), ("sale_date", "eq", issue_date)],
    )

    issue_skus = sorted({str(row.get("marketplace_sku") or "").strip() for row in tail_rows if str(row.get("marketplace_sku") or "").strip()})
    issue_articles = sorted({str(row.get("article") or "").strip() for row in tail_rows if str(row.get("article") or "").strip()})

    cat_by_sku = rows_by_key(fetch_in_chunks("sku_catalog", "marketplace_sku", issue_skus, [("marketplace_code", "eq", marketplace_code)]), "marketplace_sku")
    cat_by_article = rows_by_key(fetch_in_chunks("sku_catalog", "article", issue_articles, [("marketplace_code", "eq", marketplace_code)]), "article")
    orders_by_sku = rows_by_key(fetch_in_chunks("marketplace_orders", "marketplace_sku", issue_skus, [("marketplace_code", "eq", marketplace_code)]), "marketplace_sku")
    ad_by_order = rows_by_key(fetch_in_chunks("ozon_daily_sku_ad_attribution", "order_sku", issue_skus, [("marketplace_code", "eq", marketplace_code)]), "order_sku")
    ad_by_promoted = rows_by_key(fetch_in_chunks("ozon_daily_sku_ad_attribution", "promoted_sku", issue_skus, [("marketplace_code", "eq", marketplace_code)]), "promoted_sku")
    stock_by_article = rows_by_key(fetch_in_chunks("stock_daily", "article", issue_articles, [("marketplace_code", "eq", marketplace_code), ("stock_date", "eq", latest_stock_date)]), "article")

    stock_name_to_articles = defaultdict(set)
    for row in stock_latest_rows:
        article = str(row.get("article") or "").strip()
        normalized_name = normalize_name(row.get("product_name"))
        if normalized_name and article:
            stock_name_to_articles[normalized_name].add(article)

    recovered_articles = set()
    recovered_state = {}
    for row in decision_tail:
        marketplace_sku = str(row.get("marketplace_sku") or "").strip()
        recovered_via = None
        recovered_article = None

        sku_catalog_matches = cat_by_sku.get(marketplace_sku) or []
        catalog_articles = sorted(
            {
                str(item.get("article") or "").strip()
                for item in sku_catalog_matches
                if str(item.get("article") or "").strip()
            }
        )
        if catalog_articles:
            recovered_via = "sku_catalog"
            recovered_article = catalog_articles[0]

        if not recovered_article:
            order_articles = sorted(
                {
                    str(item.get("article") or "").strip()
                    for item in (orders_by_sku.get(marketplace_sku) or [])
                    if str(item.get("article") or "").strip()
                }
            )
            if order_articles:
                recovered_via = "marketplace_orders"
                recovered_article = order_articles[0]

        if not recovered_article:
            promoted_articles = sorted(
                {
                    str(item.get("article") or "").strip()
                    for item in (ad_by_promoted.get(marketplace_sku) or [])
                    if str(item.get("article") or "").strip()
                }
            )
            if promoted_articles:
                recovered_via = "promoted_sku"
                recovered_article = promoted_articles[0]

        if not recovered_article:
            name_articles = sorted(stock_name_to_articles.get(normalize_name(row.get("product_name"))) or [])
            if len(name_articles) == 1:
                recovered_via = "exact_product_name"
                recovered_article = name_articles[0]

        if recovered_article:
            recovered_articles.add(recovered_article)
        recovered_state[marketplace_sku] = {
            "recovered_via": recovered_via,
            "recovered_article": recovered_article,
        }

    if recovered_articles:
        recovered_catalog_rows = fetch_in_chunks("sku_catalog", "article", sorted(recovered_articles), [("marketplace_code", "eq", marketplace_code)])
        recovered_stock_rows = fetch_in_chunks(
            "stock_daily",
            "article",
            sorted(recovered_articles),
            [("marketplace_code", "eq", marketplace_code), ("stock_date", "eq", latest_stock_date)],
        )
        for article, rows in rows_by_key(recovered_catalog_rows, "article").items():
            cat_by_article[article].extend(rows)
        for article, rows in rows_by_key(recovered_stock_rows, "article").items():
            stock_by_article[article].extend(rows)

    tail_audit_rows = []
    truly_unrecoverable_from_current_db = []
    recoverable_via_promoted_sku = []
    recovered_article_but_no_stock_source = []

    stock_source_articles = set()
    product_ids_to_verify = set()
    offer_ids_to_verify = set()

    for row in decision_tail:
        marketplace_sku = str(row.get("marketplace_sku") or "").strip()
        recovered_via = (recovered_state.get(marketplace_sku) or {}).get("recovered_via")
        recovered_article = (recovered_state.get(marketplace_sku) or {}).get("recovered_article")

        audit = {
            "marketplace_sku": marketplace_sku,
            "article": recovered_article or None,
            "product_name": row.get("product_name") or None,
            "issue_type": "decision_sku_not_mapped",
            "orders_revenue": num(row.get("orders_revenue")),
            "ad_spend": num(row.get("ad_spend")),
            "found_in_sku_catalog": bool(sku_catalog_matches),
            "sku_catalog_product_id": next((str(item.get("marketplace_sku") or "").strip() for item in sku_catalog_matches if str(item.get("marketplace_sku") or "").strip()), None),
            "found_in_ad_attribution_order_sku": bool(ad_by_order.get(marketplace_sku)),
            "found_in_ad_attribution_promoted_sku": bool(ad_by_promoted.get(marketplace_sku)),
            "found_in_marketplace_orders": bool(orders_by_sku.get(marketplace_sku)),
            "found_in_total_orders": False,
            "found_in_stock_daily_by_article": bool(recovered_article and stock_by_article.get(recovered_article)),
            "found_in_stock_daily_by_product_id": False,
            "required_source": None,
            "expected_fix": None,
            "business_priority": "high" if num(row.get("orders_revenue")) >= 10000 or num(row.get("ad_spend")) >= 1000 else "medium",
        }

        if recovered_article:
            article_rows = cat_by_article.get(recovered_article) or []
            product_ids = {
                str(item.get("marketplace_sku") or "").strip()
                for item in article_rows
                if str(item.get("marketplace_sku") or "").strip()
            }
            product_ids_to_verify.update(product_ids)
            if not product_ids:
                offer_ids_to_verify.add(recovered_article)

            if recovered_via == "promoted_sku" and stock_by_article.get(recovered_article):
                recoverable_via_promoted_sku.append(marketplace_sku)
                audit["required_source"] = "recoverable_from_current_db"
                audit["expected_fix"] = "persist_recovered_article_from_promoted_sku"
            else:
                recovered_article_but_no_stock_source.append(marketplace_sku)
                stock_source_articles.add(recovered_article)
                audit["required_source"] = "requires_stock_api_verification"
                audit["expected_fix"] = "verify_stock_source_for_recovered_article"
        else:
            truly_unrecoverable_from_current_db.append(marketplace_sku)
            audit["required_source"] = "requires_product_list"
            audit["expected_fix"] = "product_identity_loader"

        tail_audit_rows.append(audit)

    article_not_in_stock_rows = []
    stock_source_gap_rows = []
    for row in stock_source_tail:
        article = str(row.get("article") or "").strip()
        catalog_rows = cat_by_article.get(article) or []
        product_ids = {
            str(item.get("marketplace_sku") or "").strip()
            for item in catalog_rows
            if str(item.get("marketplace_sku") or "").strip()
        }
        product_ids_to_verify.update(product_ids)
        if article and not product_ids:
            offer_ids_to_verify.add(article)
        stock_source_articles.add(article)
        stock_source_gap_rows.append(str(row.get("marketplace_sku") or "").strip())
        article_not_in_stock_rows.append(
            {
                "marketplace_sku": str(row.get("marketplace_sku") or "").strip(),
                "article": article or None,
                "product_name": row.get("product_name") or None,
                "issue_type": "article_not_in_stock_source",
                "orders_revenue": num(row.get("orders_revenue")),
                "ad_spend": num(row.get("ad_spend")),
                "found_in_sku_catalog": bool(catalog_rows),
                "sku_catalog_product_id": next((str(item.get("marketplace_sku") or "").strip() for item in catalog_rows if str(item.get("marketplace_sku") or "").strip()), None),
                "found_in_ad_attribution_order_sku": False,
                "found_in_ad_attribution_promoted_sku": False,
                "found_in_marketplace_orders": False,
                "found_in_total_orders": False,
                "found_in_stock_daily_by_article": False,
                "found_in_stock_daily_by_product_id": False,
                "required_source": "stock_source_gap",
                "expected_fix": "verify_product_with_stock_api",
                "business_priority": "high" if num(row.get("orders_revenue")) >= 10000 or num(row.get("ad_spend")) >= 1000 else "medium",
            }
        )

    gap_rows = sorted(
        tail_audit_rows + article_not_in_stock_rows,
        key=lambda row: (row["orders_revenue"], row["ad_spend"]),
        reverse=True,
    )

    sku_catalog_row_count = len(sku_catalog_rows)
    stock_daily_latest_rows = len(stock_latest_rows)
    ad_attribution_row_count = len(ad_rows)
    total_orders_row_count = len(total_orders_rows)
    orders_row_count = len(orders_rows)

    product_ids_to_verify = {value for value in product_ids_to_verify if value}
    offer_ids_to_verify = {value for value in offer_ids_to_verify if value}
    sku_ids_to_verify = {value for value in truly_unrecoverable_from_current_db if value}

    product_list_requests = ceil_div(sku_catalog_row_count, PRODUCT_LIST_LIMIT) if "product-list" in include_api_sources and truly_unrecoverable_from_current_db else 0
    info_list_identifiers = len(product_ids_to_verify) + len(offer_ids_to_verify) + len(sku_ids_to_verify)
    info_list_requests = ceil_div(info_list_identifiers, INFO_LIST_LIMIT) if "info-list" in include_api_sources and info_list_identifiers else 0
    attributes_requests = ceil_div(len(product_ids_to_verify) + len(offer_ids_to_verify), INFO_LIST_LIMIT) if "attributes" in include_api_sources and (product_ids_to_verify or offer_ids_to_verify) else 0
    stock_requests = ceil_div(len(product_ids_to_verify), STOCKS_BATCH_LIMIT) if "stocks" in include_api_sources and product_ids_to_verify else 0

    plan = {
        "stock_data_quality_date": issue_date,
        "total_tail": len(tail_rows),
        "decision_sku_not_mapped": len(decision_tail),
        "article_not_in_stock_source": len(stock_source_tail),
        "truly_unrecoverable_from_current_db": len(truly_unrecoverable_from_current_db),
        "recoverable_via_promoted_sku": len(recoverable_via_promoted_sku),
        "recovered_article_but_no_stock_source": len(recovered_article_but_no_stock_source),
        "can_close_from_current_db": len(recoverable_via_promoted_sku),
        "requires_product_identity_api": len(truly_unrecoverable_from_current_db),
        "requires_stock_source_verification": len(recovered_article_but_no_stock_source) + len(stock_source_tail),
        "expected_unknown_after_identity_plan": 0,
        "identity_sources_available_in_current_db": {
            "sku_catalog_rows": sku_catalog_row_count,
            "stock_daily_latest_rows": stock_daily_latest_rows,
            "ad_attribution_rows": ad_attribution_row_count,
            "total_orders_rows": total_orders_row_count,
            "orders_rows": orders_row_count,
        },
        "api_plan": {
            "/v3/product/list": {
                "planned": "product-list" in include_api_sources and bool(truly_unrecoverable_from_current_db),
                "reason": "refresh_full_catalog_and_visibility_for_unrecoverable_tail" if truly_unrecoverable_from_current_db else "not_needed_for_current_tail",
                "estimated_requests": product_list_requests,
            },
            "/v3/product/info/list": {
                "planned": "info-list" in include_api_sources and bool(info_list_identifiers),
                "identifiers_by_product_id": len(product_ids_to_verify),
                "identifiers_by_offer_id": len(offer_ids_to_verify),
                "identifiers_by_sku": len(sku_ids_to_verify),
                "batches": info_list_requests,
                "estimated_requests": info_list_requests,
            },
            "/v3/products/info/attributes": {
                "planned": "design_only" if "attributes" in include_api_sources else "no",
                "estimated_requests_if_enabled": attributes_requests,
            },
            "/v4/product/info/stocks": {
                "planned": "stocks" in include_api_sources and bool(product_ids_to_verify),
                "product_ids_to_verify": len(product_ids_to_verify),
                "batches": stock_requests,
                "estimated_requests": stock_requests,
            },
        },
        "limits": {
            "max_requests": None,
            "estimated_total_requests": product_list_requests + info_list_requests + stock_requests,
        },
        "tail_rows": gap_rows,
        "latest_stock_date": latest_stock_date,
        "include_api_sources": include_api_sources,
    }
    return plan


def print_plan(plan, max_requests=None):
    plan["limits"]["max_requests"] = max_requests

    print("=== Ozon Product Identity Loader — Plan Only ===")
    print()
    print("Current tail:")
    print(f"  stock_data_quality_date: {plan['stock_data_quality_date']}")
    print(f"  total_tail: {plan['total_tail']}")
    print(f"  decision_sku_not_mapped: {plan['decision_sku_not_mapped']}")
    print(f"  article_not_in_stock_source: {plan['article_not_in_stock_source']}")
    print()
    print("Tail split:")
    print(f"  truly_unrecoverable_from_current_db: {plan['truly_unrecoverable_from_current_db']}")
    print(f"  recoverable_via_promoted_sku: {plan['recoverable_via_promoted_sku']}")
    print(f"  recovered_article_but_no_stock_source: {plan['recovered_article_but_no_stock_source']}")
    print(f"  article_not_in_stock_source: {plan['article_not_in_stock_source']}")
    print()
    print("Identity sources available in current DB:")
    for key, value in plan["identity_sources_available_in_current_db"].items():
        print(f"  {key}: {value}")
    print()
    print("API plan:")
    for endpoint, endpoint_plan in plan["api_plan"].items():
        print(f"  {endpoint}:")
        for key, value in endpoint_plan.items():
            print(f"    {key}: {value}")
    print()
    print("Expected closure:")
    print(f"  can_close_from_current_db: {plan['can_close_from_current_db']}")
    print(f"  requires_product_identity_api: {plan['requires_product_identity_api']}")
    print(f"  requires_stock_source_verification: {plan['requires_stock_source_verification']}")
    print(f"  expected_unknown_after_identity_plan: {plan['expected_unknown_after_identity_plan']}")
    print()
    print("Live API:")
    print("  NOT called (--plan-only)")
    print()
    print("Plan JSON:")
    print(json.dumps(plan, ensure_ascii=False, indent=2))


def run():
    args = parse_args()
    include_api_sources = parse_include_api_sources(args.include_api_sources)
    issue_date = args.issue_date or latest_issue_date(args.marketplace)

    if not issue_date:
        raise RuntimeError("No stock_data_quality_issues rows found for requested marketplace.")

    plan = build_plan(
        issue_date=issue_date,
        marketplace_code=args.marketplace,
        only_tail=True,
        limit_tail=args.limit_tail,
        include_api_sources=include_api_sources,
    )

    if args.plan_only or not args.live:
        print_plan(plan, max_requests=args.max_requests)
        return

    raise RuntimeError("Live mode is intentionally disabled until separately approved.")


if __name__ == "__main__":
    run()
