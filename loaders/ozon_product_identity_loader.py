import argparse
import json
import math
import os
from collections import Counter, defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from supabase import create_client


load_dotenv()

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

DEFAULT_MARKETPLACE = "ozon"
DEFAULT_INCLUDE_API_SOURCES = "product-list,info-list,attributes,stocks"
PRODUCT_LIST_LIMIT = 1000
INFO_LIST_LIMIT = 1000
STOCKS_BATCH_LIMIT = 100
REQUEST_TIMEOUT = 60


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


def ozon_headers():
    return {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }


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


def chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def merge_json_dict(base, extra):
    merged = dict(base or {})
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_json_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def build_stock_api_evidence(stock_item, verified_at):
    stocks = stock_item.get("stocks", []) or []
    fbo_present = None
    fbo_reserved = None
    fbs_present = None
    fbs_reserved = None
    total_present = 0
    total_reserved = 0
    saw_present = False
    saw_reserved = False
    fbo_sku = None
    fbs_sku = None

    for stock in stocks:
        stock_type = str(stock.get("type") or "").lower()
        present = stock.get("present")
        reserved = stock.get("reserved")
        sku_value = str(stock.get("sku") or "").strip() or None

        if present is not None:
            total_present += int(present)
            saw_present = True
        if reserved is not None:
            total_reserved += int(reserved)
            saw_reserved = True

        if stock_type == "fbo":
            if present is not None:
                fbo_present = int(present)
            if reserved is not None:
                fbo_reserved = int(reserved)
            if sku_value and not fbo_sku:
                fbo_sku = sku_value
        if stock_type in {"fbs", "rfbs"}:
            if present is not None:
                fbs_present = int(present) if fbs_present is None else fbs_present + int(present)
            if reserved is not None:
                fbs_reserved = int(reserved) if fbs_reserved is None else fbs_reserved + int(reserved)
            if sku_value and not fbs_sku:
                fbs_sku = sku_value

    total_available = None
    if saw_present or saw_reserved:
        total_available = (total_present if saw_present else 0) - (total_reserved if saw_reserved else 0)

    return {
        "verified": True,
        "source": "/v4/product/info/stocks",
        "verified_at": verified_at,
        "returned_stock_rows": True,
        "fbo_present": fbo_present,
        "fbo_reserved": fbo_reserved,
        "fbs_present": fbs_present,
        "fbs_reserved": fbs_reserved,
        "total_present": total_present if saw_present else None,
        "total_reserved": total_reserved if saw_reserved else None,
        "total_available": total_available,
        "fbo_sku": fbo_sku,
        "fbs_sku": fbs_sku,
    }


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
        sku_catalog_matches = cat_by_sku.get(marketplace_sku) or []

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
    product_id_batches = ceil_div(len(product_ids_to_verify), INFO_LIST_LIMIT) if "info-list" in include_api_sources and product_ids_to_verify else 0
    offer_id_batches = ceil_div(len(offer_ids_to_verify), INFO_LIST_LIMIT) if "info-list" in include_api_sources and offer_ids_to_verify else 0
    sku_batches = ceil_div(len(sku_ids_to_verify), INFO_LIST_LIMIT) if "info-list" in include_api_sources and sku_ids_to_verify else 0
    info_list_requests = product_id_batches + offer_id_batches + sku_batches
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
                "planned": "info-list" in include_api_sources and bool(info_list_requests),
                "identifiers_by_product_id": len(product_ids_to_verify),
                "identifiers_by_offer_id": len(offer_ids_to_verify),
                "identifiers_by_sku": len(sku_ids_to_verify),
                "product_id_batches": product_id_batches,
                "offer_id_batches": offer_id_batches,
                "sku_batches": sku_batches,
                "product_id_requests": product_id_batches,
                "offer_id_requests": offer_id_batches,
                "sku_requests": sku_batches,
                "total_info_list_requests": info_list_requests,
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


def post_ozon(url, payload, request_stats, endpoint_key):
    response = requests.post(url, headers=ozon_headers(), json=payload, timeout=REQUEST_TIMEOUT)
    request_stats[endpoint_key] += 1

    if response.status_code != 200:
        return {
            "ok": False,
            "status_code": response.status_code,
            "text": response.text[:2000],
            "payload": payload,
        }

    try:
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status_code": response.status_code,
            "text": f"invalid_json: {exc}",
            "payload": payload,
        }

    return {
        "ok": True,
        "status_code": response.status_code,
        "data": data,
        "payload": payload,
    }


def fetch_product_list_snapshot(request_stats, max_requests):
    if max_requests is not None and request_stats["/v3/product/list"] >= max_requests:
        return [], [{"endpoint": "/v3/product/list", "error": "max_requests_reached"}]

    url = "https://api-seller.ozon.ru/v3/product/list"
    last_id = ""
    items = []
    errors = []

    while True:
        if max_requests is not None and request_stats["/v3/product/list"] >= max_requests:
            errors.append({"endpoint": "/v3/product/list", "error": "max_requests_reached"})
            break

        payload = {
            "filter": {"visibility": "ALL"},
            "last_id": last_id,
            "limit": PRODUCT_LIST_LIMIT,
        }
        result = post_ozon(url, payload, request_stats, "/v3/product/list")
        if not result["ok"]:
            errors.append(
                {
                    "endpoint": "/v3/product/list",
                    "status_code": result.get("status_code"),
                    "error": result.get("text"),
                }
            )
            break

        data = result["data"]
        batch = data.get("result", {}).get("items", []) or data.get("items", []) or []
        items.extend(batch)
        last_id = data.get("result", {}).get("last_id")
        if not last_id or not batch:
            break

    return items, errors


def fetch_product_info_snapshot(product_ids, offer_ids, sku_ids, request_stats, max_requests):
    url = "https://api-seller.ozon.ru/v3/product/info/list"
    items = []
    errors = []
    stats = Counter()

    identifier_groups = [
        ("product_id", sorted({str(value).strip() for value in (product_ids or []) if str(value).strip()})),
        ("offer_id", sorted({str(value).strip() for value in (offer_ids or []) if str(value).strip()})),
        ("sku", sorted({str(value).strip() for value in (sku_ids or []) if str(value).strip()})),
    ]

    for identifier_type, identifiers in identifier_groups:
        if not identifiers:
            continue

        for batch in chunks(identifiers, INFO_LIST_LIMIT):
            if max_requests is not None and request_stats["/v3/product/info/list"] >= max_requests:
                errors.append(
                    {
                        "endpoint": "/v3/product/info/list",
                        "identifier_type": identifier_type,
                        "error": "max_requests_reached",
                    }
                )
                break

            payload = {identifier_type: batch}
            result = post_ozon(url, payload, request_stats, "/v3/product/info/list")
            stats[f"{identifier_type}_requests"] += 1
            if not result["ok"]:
                errors.append(
                    {
                        "endpoint": "/v3/product/info/list",
                        "identifier_type": identifier_type,
                        "status_code": result.get("status_code"),
                        "error": result.get("text"),
                        "payload": payload,
                    }
                )
                continue
            data = result["data"]
            items.extend(data.get("items", []) or data.get("result", {}).get("items", []) or [])

    return items, errors, dict(stats)


def fetch_product_stocks_snapshot(product_ids, offer_ids, request_stats, max_requests):
    url = "https://api-seller.ozon.ru/v4/product/info/stocks"
    items = []
    errors = []

    product_ids = list(product_ids or [])
    offer_ids = list(offer_ids or [])
    offer_id_by_product_id = {}
    for product_id, offer_id in zip(product_ids, offer_ids):
        if product_id:
            offer_id_by_product_id[str(product_id)] = offer_id

    batches = list(chunks(product_ids, STOCKS_BATCH_LIMIT))
    for batch in batches:
        if max_requests is not None and request_stats["/v4/product/info/stocks"] >= max_requests:
            errors.append({"endpoint": "/v4/product/info/stocks", "error": "max_requests_reached"})
            break

        batch_offer_ids = [offer_id_by_product_id.get(str(product_id)) for product_id in batch]
        payload = {
            "filter": {
                "product_id": batch,
                "offer_id": [value for value in batch_offer_ids if value],
                "visibility": "ALL",
            },
            "last_id": "",
            "limit": 1000,
        }
        result = post_ozon(url, payload, request_stats, "/v4/product/info/stocks")
        if not result["ok"]:
            errors.append(
                {
                    "endpoint": "/v4/product/info/stocks",
                    "status_code": result.get("status_code"),
                    "error": result.get("text"),
                    "payload": payload,
                }
            )
            continue
        data = result["data"]
        items.extend(data.get("items", []) or data.get("result", {}).get("items", []) or [])

    return items, errors


def build_identity_rows_from_api(plan, product_list_items, info_items, stock_items, verified_at=None):
    identity_by_key = {}
    verified_at = verified_at or datetime.now(ZoneInfo("UTC")).isoformat()

    for item in product_list_items:
        product_id = str(item.get("product_id") or item.get("id") or "").strip()
        offer_id = str(item.get("offer_id") or "").strip()
        if not product_id and not offer_id:
            continue
        identity_key = f"ozon:{product_id or offer_id}"
        row = identity_by_key.setdefault(
            identity_key,
            {
                "identity_key": identity_key,
                "marketplace_code": "ozon",
                "article": offer_id or None,
                "offer_id": offer_id or None,
                "product_id": product_id or None,
                "ozon_sku": None,
                "fbo_sku": None,
                "fbs_sku": None,
                "decision_marketplace_sku": None,
                "product_name": item.get("name") or None,
                "visibility": item.get("visibility") or None,
                "product_status": item.get("status") or None,
                "archived": item.get("archived") if item.get("archived") is not None else item.get("is_archived"),
                "has_fbo_stocks": item.get("has_fbo_stocks"),
                "has_fbs_stocks": item.get("has_fbs_stocks"),
                "source": "product_list",
                "evidence": {"product_list": item},
            },
        )
        row["evidence"] = merge_json_dict(row.get("evidence"), {"product_list": item})
        row["article"] = row["article"] or offer_id or None
        row["offer_id"] = row["offer_id"] or offer_id or None
        row["product_id"] = row["product_id"] or product_id or None
        row["product_name"] = row["product_name"] or item.get("name") or None
        row["visibility"] = row["visibility"] or item.get("visibility") or None
        row["product_status"] = row["product_status"] or item.get("status") or None
        if row["archived"] is None:
            row["archived"] = item.get("archived") if item.get("archived") is not None else item.get("is_archived")
        if row["has_fbo_stocks"] is None:
            row["has_fbo_stocks"] = item.get("has_fbo_stocks")
        if row["has_fbs_stocks"] is None:
            row["has_fbs_stocks"] = item.get("has_fbs_stocks")

    for item in info_items:
        product_id = str(item.get("id") or item.get("product_id") or "").strip()
        offer_id = str(item.get("offer_id") or "").strip()
        sku_values = item.get("sources") or item.get("sku") or item.get("skus") or []
        if isinstance(sku_values, (int, str)):
            sku_values = [sku_values]
        sku_values = [str(value).strip() for value in sku_values if str(value).strip()]
        identity_key = f"ozon:{product_id or offer_id or (sku_values[0] if sku_values else '')}"
        if identity_key == "ozon:":
            continue
        row = identity_by_key.setdefault(
            identity_key,
            {
                "identity_key": identity_key,
                "marketplace_code": "ozon",
                "article": offer_id or None,
                "offer_id": offer_id or None,
                "product_id": product_id or None,
                "ozon_sku": sku_values[0] if sku_values else None,
                "fbo_sku": None,
                "fbs_sku": None,
                "decision_marketplace_sku": None,
                "product_name": item.get("name") or None,
                "visibility": item.get("visibility") or None,
                "product_status": item.get("status") or None,
                "archived": item.get("archived") if item.get("archived") is not None else item.get("is_archived"),
                "has_fbo_stocks": None,
                "has_fbs_stocks": None,
                "source": "product_info_list",
                "evidence": {"product_info_list": item},
            },
        )
        row["evidence"] = merge_json_dict(row.get("evidence"), {"product_info_list": item})
        row["article"] = row["article"] or offer_id or None
        row["offer_id"] = row["offer_id"] or offer_id or None
        row["product_id"] = row["product_id"] or product_id or None
        row["ozon_sku"] = row["ozon_sku"] or (sku_values[0] if sku_values else None)
        row["product_name"] = row["product_name"] or item.get("name") or None
        row["visibility"] = row["visibility"] or item.get("visibility") or None
        row["product_status"] = row["product_status"] or item.get("status") or None

    for item in stock_items:
        product_id = str(item.get("product_id") or "").strip()
        offer_id = str(item.get("offer_id") or "").strip()
        identity_key = f"ozon:{product_id or offer_id}"
        if identity_key == "ozon:":
            continue
        row = identity_by_key.setdefault(
            identity_key,
            {
                "identity_key": identity_key,
                "marketplace_code": "ozon",
                "article": offer_id or None,
                "offer_id": offer_id or None,
                "product_id": product_id or None,
                "ozon_sku": None,
                "fbo_sku": None,
                "fbs_sku": None,
                "decision_marketplace_sku": None,
                "product_name": None,
                "visibility": None,
                "product_status": None,
                "archived": None,
                "has_fbo_stocks": None,
                "has_fbs_stocks": None,
                "source": "stock_api",
                "evidence": {},
            },
        )
        row["evidence"] = merge_json_dict(
            row.get("evidence"),
            {"stock_api": build_stock_api_evidence(item, verified_at)},
        )
        row["article"] = row["article"] or offer_id or None
        row["offer_id"] = row["offer_id"] or offer_id or None
        row["product_id"] = row["product_id"] or product_id or None

        for stock in item.get("stocks", []) or []:
            stock_type = str(stock.get("type") or "").lower()
            sku_value = str(stock.get("sku") or "").strip() or None
            if stock_type == "fbo" and sku_value and not row["fbo_sku"]:
                row["fbo_sku"] = sku_value
            if stock_type in {"fbs", "rfbs"} and sku_value and not row["fbs_sku"]:
                row["fbs_sku"] = sku_value

    tail_rows = plan["tail_rows"]
    by_marketplace_sku = {str(row.get("marketplace_sku") or "").strip(): row for row in tail_rows}
    by_article = defaultdict(list)
    for row in tail_rows:
        article = str(row.get("article") or "").strip()
        if article:
            by_article[article].append(row)

    for identity in identity_by_key.values():
        article = str(identity.get("article") or "").strip()
        if article and len(by_article.get(article) or []) == 1:
            identity["decision_marketplace_sku"] = by_article[article][0].get("marketplace_sku")

    return list(identity_by_key.values())


def run_dry_run(plan, max_requests):
    request_stats = defaultdict(int)
    errors = []

    product_list_items, product_list_errors = fetch_product_list_snapshot(request_stats, max_requests)
    errors.extend(product_list_errors)

    product_ids = set()
    offer_ids = set()
    sku_ids = set()
    for row in plan["tail_rows"]:
        if row.get("sku_catalog_product_id"):
            product_ids.add(str(row["sku_catalog_product_id"]))
        if row.get("article") and row.get("required_source") in {"requires_stock_api_verification", "stock_source_gap"}:
            offer_ids.add(str(row["article"]))
        if row.get("required_source") == "requires_product_list":
            sku_ids.add(str(row.get("marketplace_sku") or "").strip())

    info_items, info_errors, info_stats = fetch_product_info_snapshot(
        sorted(product_ids),
        sorted(offer_ids),
        sorted(sku_ids),
        request_stats,
        max_requests,
    )
    errors.extend(info_errors)

    stock_items, stock_errors = fetch_product_stocks_snapshot(
        sorted(product_ids),
        [next((row.get("article") for row in plan["tail_rows"] if str(row.get("sku_catalog_product_id") or "") == product_id and row.get("article")), None) for product_id in sorted(product_ids)],
        request_stats,
        max_requests,
    )
    errors.extend(stock_errors)

    identity_rows = build_identity_rows_from_api(plan, product_list_items, info_items, stock_items)
    identity_by_product_id = rows_by_key(identity_rows, "product_id")
    identity_by_offer_id = rows_by_key(identity_rows, "offer_id")
    identity_by_ozon_sku = rows_by_key(identity_rows, "ozon_sku")
    stock_by_product_id = rows_by_key(stock_items, "product_id")

    identity_gap_found = 0
    stock_source_verified = 0
    stock_source_with_stock_rows = 0
    identity_gap_details = []
    stock_source_details = []

    for row in plan["tail_rows"]:
        marketplace_sku = str(row.get("marketplace_sku") or "").strip()
        article = str(row.get("article") or "").strip()
        product_id = str(row.get("sku_catalog_product_id") or "").strip()
        required_source = row.get("required_source")

        if required_source == "requires_product_list":
            found_by_product_id = bool(product_id and identity_by_product_id.get(product_id))
            found_by_offer_id = bool(article and identity_by_offer_id.get(article))
            found_by_sku = bool(identity_by_ozon_sku.get(marketplace_sku))
            found = found_by_product_id or found_by_offer_id or found_by_sku or any(
                marketplace_sku == str(item.get("ozon_sku") or "").strip()
                or marketplace_sku == str(item.get("product_id") or "").strip()
                for item in identity_rows
            )
            if found:
                identity_gap_found += 1
            identity_gap_details.append(
                {
                    "marketplace_sku": marketplace_sku,
                    "article": article or None,
                    "product_name": row.get("product_name"),
                    "found": found,
                    "searched_by_product_id": bool(product_id),
                    "searched_by_offer_id": bool(article),
                    "searched_by_sku": True,
                    "found_by_product_id": found_by_product_id,
                    "found_by_offer_id": found_by_offer_id,
                    "found_by_sku": found_by_sku,
                    "reason_if_missing": None if found else "not_returned_by_product_list_or_info_list",
                }
            )
        elif required_source in {"requires_stock_api_verification", "stock_source_gap"}:
            found_identity = bool(product_id and identity_by_product_id.get(product_id)) or bool(article and identity_by_offer_id.get(article))
            found_stock = bool(product_id and stock_by_product_id.get(product_id))
            if found_identity:
                stock_source_verified += 1
            if found_stock:
                stock_source_with_stock_rows += 1
            stock_source_details.append(
                {
                    "marketplace_sku": marketplace_sku,
                    "article": article or None,
                    "product_name": row.get("product_name"),
                    "product_id": product_id or None,
                    "found_identity": found_identity,
                    "found_stock": found_stock,
                    "reason_if_missing": None if found_identity else "identity_not_returned_by_info_list",
                }
            )

    errors_by_identifier_type = Counter()
    for error in errors:
        identifier_type = str(error.get("identifier_type") or "unknown")
        errors_by_identifier_type[identifier_type] += 1

    return {
        "status": "PASSED" if not errors else ("PARTIAL" if identity_rows else "FAILED"),
        "requests": {
            "/v3/product/list": request_stats["/v3/product/list"],
            "/v3/product/info/list": request_stats["/v3/product/info/list"],
            "/v4/product/info/stocks": request_stats["/v4/product/info/stocks"],
            "/v3/products/info/attributes": 0,
        },
        "info_list_request_breakdown": {
            "product_id_requests": info_stats.get("product_id_requests", 0),
            "offer_id_requests": info_stats.get("offer_id_requests", 0),
            "sku_requests": info_stats.get("sku_requests", 0),
        },
        "identity_rows_received": len(identity_rows),
        "identity_gap_found": identity_gap_found,
        "identity_gap_missing": max(plan["requires_product_identity_api"] - identity_gap_found, 0),
        "stock_source_verified": stock_source_verified,
        "stock_source_with_stock_rows": stock_source_with_stock_rows,
        "errors_by_identifier_type": dict(errors_by_identifier_type),
        "errors": errors,
        "empty_responses": {
            "product_list_items": len(product_list_items) == 0,
            "product_info_items": len(info_items) == 0,
            "stock_items": len(stock_items) == 0,
        },
        "identity_gap_details": identity_gap_details,
        "stock_source_details": stock_source_details,
    }


def upsert_identity_rows(rows):
    if not rows:
        return 0, 0, 0, 0

    existing_by_key = {}
    identity_keys = [row["identity_key"] for row in rows if row.get("identity_key")]
    for batch_keys in chunks(identity_keys, 500):
        for existing in fetch_all(
            "ozon_product_identity",
            filters=[("identity_key", "in", batch_keys)],
        ):
            existing_by_key[existing["identity_key"]] = existing

    stock_api_evidence_merged_count = 0
    stock_api_verified_true_count = 0
    stock_api_returned_stock_rows_count = 0
    stock_api_no_stock_rows_count = 0
    merged_rows = []
    for row in rows:
        existing = existing_by_key.get(row["identity_key"]) or {}
        merged_evidence = merge_json_dict(existing.get("evidence"), row.get("evidence"))
        row["evidence"] = merged_evidence
        stock_api = merged_evidence.get("stock_api") if isinstance(merged_evidence, dict) else None
        if isinstance(stock_api, dict) and stock_api.get("verified") is True:
            stock_api_evidence_merged_count += 1
            stock_api_verified_true_count += 1
            if stock_api.get("returned_stock_rows") is True:
                stock_api_returned_stock_rows_count += 1
            elif stock_api.get("returned_stock_rows") is False:
                stock_api_no_stock_rows_count += 1
        merged_rows.append(row)

    for batch in chunks(merged_rows, 500):
        supabase.table("ozon_product_identity").upsert(
            batch,
            on_conflict="identity_key",
        ).execute()
    return (
        len(merged_rows),
        stock_api_evidence_merged_count,
        stock_api_verified_true_count,
        stock_api_returned_stock_rows_count,
        stock_api_no_stock_rows_count,
    )


def update_stock_issue_rows(issue_date, rows):
    if not rows:
        return 0

    payload = []
    for row in rows:
        payload.append(
            {
                "issue_date": issue_date,
                "marketplace_code": "ozon",
                "marketplace_sku": row["marketplace_sku"],
                "issue_type": "product_identity_not_returned_by_ozon_api",
                "issue_reason": "not_returned_by_current_ozon_api",
                "evidence": {
                    "searched_by": ["sku"],
                    "endpoints_tried": ["/v3/product/list", "/v3/product/info/list"],
                    "result": "not_returned",
                    "product_status": "not_returned_by_current_ozon_api",
                    "possible_reason": "inactive_or_not_visible_possible",
                },
                "suggested_fix": "manual_identity_review_or_product_list_refresh",
                "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            }
        )

    for batch in chunks(payload, 500):
        supabase.table("stock_data_quality_issues").upsert(
            batch,
            on_conflict="issue_date,marketplace_code,marketplace_sku",
        ).execute()
    return len(payload)


def run_live(plan, max_requests):
    request_stats = defaultdict(int)
    errors = []

    product_list_items, product_list_errors = fetch_product_list_snapshot(request_stats, max_requests)
    errors.extend(product_list_errors)

    product_ids = set()
    offer_ids = set()
    sku_ids = set()
    for row in plan["tail_rows"]:
        if row.get("sku_catalog_product_id"):
            product_ids.add(str(row["sku_catalog_product_id"]))
        if row.get("article") and row.get("required_source") in {"requires_stock_api_verification", "stock_source_gap"}:
            offer_ids.add(str(row["article"]))
        if row.get("required_source") == "requires_product_list":
            sku_ids.add(str(row.get("marketplace_sku") or "").strip())

    info_items, info_errors, info_stats = fetch_product_info_snapshot(
        sorted(product_ids),
        sorted(offer_ids),
        sorted(sku_ids),
        request_stats,
        max_requests,
    )
    errors.extend(info_errors)

    stock_items, stock_errors = fetch_product_stocks_snapshot(
        sorted(product_ids),
        [next((row.get("article") for row in plan["tail_rows"] if str(row.get("sku_catalog_product_id") or "") == product_id and row.get("article")), None) for product_id in sorted(product_ids)],
        request_stats,
        max_requests,
    )
    errors.extend(stock_errors)

    verified_at = datetime.now(ZoneInfo("UTC")).isoformat()
    identity_rows = build_identity_rows_from_api(plan, product_list_items, info_items, stock_items, verified_at=verified_at)
    identity_rows = [row for row in identity_rows if str(row.get("product_id") or "").strip()]
    stock_items_by_product_id = rows_by_key(stock_items, "product_id")
    verified_product_ids = sorted(product_ids)
    for row in identity_rows:
        product_id = str(row.get("product_id") or "").strip()
        if product_id and product_id in verified_product_ids and not stock_items_by_product_id.get(product_id):
            row["evidence"] = merge_json_dict(
                row.get("evidence"),
                {
                    "stock_api": {
                        "verified": True,
                        "source": "/v4/product/info/stocks",
                        "verified_at": verified_at,
                        "returned_stock_rows": False,
                    }
                },
            )

    (
        upserted_rows,
        stock_api_evidence_merged_count,
        stock_api_verified_true_count,
        stock_api_returned_stock_rows_count,
        stock_api_no_stock_rows_count,
    ) = upsert_identity_rows(identity_rows)

    identity_by_product_id = rows_by_key(identity_rows, "product_id")
    identity_by_offer_id = rows_by_key(identity_rows, "offer_id")
    identity_by_ozon_sku = rows_by_key(identity_rows, "ozon_sku")
    stock_by_product_id = rows_by_key(stock_items, "product_id")

    identity_gap_found = 0
    identity_gap_missing_rows = []
    stock_source_verified = 0
    stock_source_with_stock_rows = 0

    for row in plan["tail_rows"]:
        marketplace_sku = str(row.get("marketplace_sku") or "").strip()
        article = str(row.get("article") or "").strip()
        product_id = str(row.get("sku_catalog_product_id") or "").strip()
        required_source = row.get("required_source")

        if required_source == "requires_product_list":
            found_by_product_id = bool(product_id and identity_by_product_id.get(product_id))
            found_by_offer_id = bool(article and identity_by_offer_id.get(article))
            found_by_sku = bool(identity_by_ozon_sku.get(marketplace_sku))
            found = found_by_product_id or found_by_offer_id or found_by_sku or any(
                marketplace_sku == str(item.get("ozon_sku") or "").strip()
                or marketplace_sku == str(item.get("product_id") or "").strip()
                for item in identity_rows
            )
            if found:
                identity_gap_found += 1
            else:
                identity_gap_missing_rows.append({"marketplace_sku": marketplace_sku})
        elif required_source in {"requires_stock_api_verification", "stock_source_gap"}:
            found_identity = bool(product_id and identity_by_product_id.get(product_id)) or bool(article and identity_by_offer_id.get(article))
            found_stock = bool(product_id and stock_by_product_id.get(product_id))
            if found_identity:
                stock_source_verified += 1
            if found_stock:
                stock_source_with_stock_rows += 1

    updated_issue_rows = update_stock_issue_rows(plan["stock_data_quality_date"], identity_gap_missing_rows)

    return {
        "status": "PASSED" if not errors else ("PARTIAL" if upserted_rows or updated_issue_rows else "FAILED"),
        "requests": {
            "/v3/product/list": request_stats["/v3/product/list"],
            "/v3/product/info/list": request_stats["/v3/product/info/list"],
            "/v4/product/info/stocks": request_stats["/v4/product/info/stocks"],
            "/v3/products/info/attributes": 0,
        },
        "info_list_request_breakdown": {
            "product_id_requests": info_stats.get("product_id_requests", 0),
            "offer_id_requests": info_stats.get("offer_id_requests", 0),
            "sku_requests": info_stats.get("sku_requests", 0),
        },
        "identity_rows_received": len(identity_rows),
        "identity_rows_upserted": upserted_rows,
        "identity_gap_found": identity_gap_found,
        "identity_gap_missing": max(plan["requires_product_identity_api"] - identity_gap_found, 0),
        "stock_source_verified": stock_source_verified,
        "stock_source_with_stock_rows": stock_source_with_stock_rows,
        "stock_issue_rows_updated": updated_issue_rows,
        "stock_api_evidence_merged_count": stock_api_evidence_merged_count,
        "stock_api_verified_true_count": stock_api_verified_true_count,
        "stock_api_returned_stock_rows_count": stock_api_returned_stock_rows_count,
        "stock_api_no_stock_rows_count": stock_api_no_stock_rows_count,
        "filled_counts": {
            "product_id": sum(1 for row in identity_rows if str(row.get("product_id") or "").strip()),
            "offer_id": sum(1 for row in identity_rows if str(row.get("offer_id") or "").strip()),
            "article": sum(1 for row in identity_rows if str(row.get("article") or "").strip()),
            "fbo_sku": sum(1 for row in identity_rows if str(row.get("fbo_sku") or "").strip()),
            "fbs_sku": sum(1 for row in identity_rows if str(row.get("fbs_sku") or "").strip()),
        },
        "errors": errors,
    }


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

    if args.plan_only or args.dry_run:
        if args.dry_run:
            summary = run_dry_run(plan, max_requests=args.max_requests)
            print("=== Ozon Product Identity Loader — Dry Run ===")
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return
        print_plan(plan, max_requests=args.max_requests)
        return

    summary = run_live(plan, max_requests=args.max_requests)
    print("=== Ozon Product Identity Loader — Live Run Summary ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
