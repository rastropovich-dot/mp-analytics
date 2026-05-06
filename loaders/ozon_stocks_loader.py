import argparse
import json
import os
import time
from collections import Counter, defaultdict
from datetime import date

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
DEFAULT_SKU_CATALOG_PAGE_SIZE = int(os.getenv("OZON_STOCKS_SKU_CATALOG_PAGE_SIZE", "1000"))
DEFAULT_STOCK_API_BATCH_SIZE = int(os.getenv("OZON_STOCKS_API_BATCH_SIZE", "100"))
DEFAULT_IDENTITY_LOOKBACK_DAYS = int(os.getenv("OZON_STOCKS_IDENTITY_LOOKBACK_DAYS", "90"))


def ozon_headers():
    return {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_args():
    parser = argparse.ArgumentParser(description="Load Ozon stocks into stock_daily")
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Read and summarize sku_catalog pagination plan without calling Ozon API or writing to DB.",
    )
    parser.add_argument(
        "--sku-catalog-page-size",
        type=int,
        default=DEFAULT_SKU_CATALOG_PAGE_SIZE,
        help=f"Pagination size for reading sku_catalog from Supabase (default: {DEFAULT_SKU_CATALOG_PAGE_SIZE}).",
    )
    parser.add_argument(
        "--stock-api-batch-size",
        type=int,
        default=DEFAULT_STOCK_API_BATCH_SIZE,
        help=f"Batch size for POST /v4/product/info/stocks product_ids (default: {DEFAULT_STOCK_API_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--identity-lookback-days",
        type=int,
        default=DEFAULT_IDENTITY_LOOKBACK_DAYS,
        help=f"Lookback window for article -> decision SKU mapping (default: {DEFAULT_IDENTITY_LOOKBACK_DAYS}).",
    )
    return parser.parse_args()


def load_ozon_products_from_db(page_size):
    rows = []
    offset = 0
    page_count = 0

    while True:
        page = (
            supabase
            .table("sku_catalog")
            .select("marketplace_sku, article, product_name")
            .eq("marketplace_code", "ozon")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
            or []
        )
        rows.extend(page)
        page_count += 1

        if len(page) < page_size:
            break

        offset += page_size

    return rows, page_count


def build_ozon_products(rows):
    products = []
    duplicate_articles = 0
    duplicate_product_ids = 0
    seen_articles = set()
    seen_product_ids = set()

    for row in rows:
        article = row.get("article")
        if article:
            if article in seen_articles:
                duplicate_articles += 1
            else:
                seen_articles.add(article)

        if str(row.get("marketplace_sku", "")).isdigit():
            product_id = int(row["marketplace_sku"])
            if product_id in seen_product_ids:
                duplicate_product_ids += 1
            else:
                seen_product_ids.add(product_id)

            products.append({
                "product_id": product_id,
                "offer_id": article,
                "product_name": row.get("product_name"),
            })

    return {
        "products": products,
        "distinct_articles": len(seen_articles),
        "distinct_product_ids": len(seen_product_ids),
        "duplicate_articles": duplicate_articles,
        "duplicate_product_ids": duplicate_product_ids,
    }


def get_ozon_products_from_db(page_size):
    rows, page_count = load_ozon_products_from_db(page_size)
    summary = build_ozon_products(rows)
    summary["sku_catalog_rows_loaded"] = len(rows)
    summary["page_count"] = page_count
    return summary


def fetch_all(table, filters=None, order=None):
    rows = []
    offset = 0
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

        page = query.range(offset, offset + page_size - 1).execute().data or []
        rows.extend(page)

        if len(page) < page_size:
            break

        offset += page_size

    return rows


def stock_identity_columns_supported():
    try:
        supabase.table("stock_daily").select(
            "product_id,stock_marketplace_sku,decision_marketplace_sku,stock_identity_status"
        ).limit(1).execute()
        return True
    except Exception:
        return False


def build_decision_sku_by_article_map(lookback_days):
    date_to = date.today().isoformat()
    date_from = date.fromordinal(date.today().toordinal() - lookback_days).isoformat()
    counters = defaultdict(Counter)

    orders = fetch_all(
        "marketplace_orders",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("order_date", "gte", date_from),
            ("order_date", "lte", date_to),
        ],
        order="order_date",
    )
    for row in orders:
        article = str(row.get("article") or "").strip()
        sku = str(row.get("marketplace_sku") or "").strip()
        if article and sku:
            counters[article][sku] += 1

    attr_rows = fetch_all(
        "ozon_daily_sku_ad_attribution",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("sale_date", "gte", date_from),
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


def get_ozon_stocks(products, stock_api_batch_size):
    url = "https://api-seller.ozon.ru/v4/product/info/stocks"
    all_items = []
    batch_count = 0

    for batch in chunks(products, stock_api_batch_size):
        batch_count += 1
        product_ids = [p["product_id"] for p in batch]
        offer_ids = [p["offer_id"] for p in batch if p.get("offer_id")]

        payload = {
            "filter": {
                "product_id": product_ids,
                "offer_id": offer_ids,
                "visibility": "ALL"
            },
            "last_id": "",
            "limit": 1000
        }

        response = requests.post(url, headers=ozon_headers(), json=payload, timeout=60)

        if response.status_code != 200:
            print("Ошибка Ozon stocks API:")
            print(response.status_code)
            print(response.text[:2000])
            continue

        data = response.json()
        items = data.get("items", []) or data.get("result", {}).get("items", [])

        all_items.extend(items)
        print(
            json.dumps(
                {
                    "event": "ozon_stock_api_batch",
                    "batch_index": batch_count,
                    "product_ids_count": len(product_ids),
                    "offer_ids_count": len(offer_ids),
                    "items_returned": len(items),
                    "items_accumulated": len(all_items),
                },
                ensure_ascii=False,
            )
        )

        time.sleep(0.25)

    return all_items, batch_count


def save_stocks(items, identity_columns_enabled, decision_sku_by_article):
    today = date.today().isoformat()

    grouped = {}

    for item in items:
        product_id = item.get("product_id")
        offer_id = item.get("offer_id")
        stocks = item.get("stocks", [])

        for stock in stocks:
            stock_type = stock.get("type", "unknown")
            decision_sku = decision_sku_by_article.get(str(offer_id or "").strip())

            present = stock.get("present", 0) or 0
            reserved = stock.get("reserved", 0) or 0

            key = (
                today,
                "ozon",
                str(product_id),
                stock_type
            )

            if key not in grouped:
                grouped[key] = {
                    "stock_date": today,
                    "marketplace_code": "ozon",
                    # TODO: stock_daily.marketplace_sku currently stores Ozon product_id.
                    # Keep this column backward-compatible even after identity normalization.
                    "marketplace_sku": str(product_id),
                    "article": str(offer_id or ""),
                    "product_name": None,
                    "warehouse_name": stock_type,
                    "stock_qty": 0,
                    "reserved_qty": 0,
                    "available_qty": 0,
                }
                if identity_columns_enabled:
                    grouped[key]["product_id"] = str(product_id)
                    grouped[key]["stock_marketplace_sku"] = str(product_id)
                    grouped[key]["decision_marketplace_sku"] = decision_sku or None
                    grouped[key]["stock_identity_status"] = (
                        "mapped_by_article" if decision_sku else "unresolved"
                    )

            grouped[key]["stock_qty"] += present
            grouped[key]["reserved_qty"] += reserved
            grouped[key]["available_qty"] = grouped[key]["stock_qty"] - grouped[key]["reserved_qty"]

    rows = list(grouped.values())

    if not rows:
        print("Нет остатков для записи")
        return

    for batch in chunks(rows, 500):
        supabase.table("stock_daily").upsert(
            batch,
            on_conflict="stock_date,marketplace_code,marketplace_sku,warehouse_name"
        ).execute()

    print(f"✅ Остатки Ozon записаны в stock_daily: {len(rows)} строк")


if __name__ == "__main__":
    args = parse_args()
    catalog = get_ozon_products_from_db(args.sku_catalog_page_size)
    products = catalog["products"]
    planned_stock_api_batches = (
        (len(products) + args.stock_api_batch_size - 1) // args.stock_api_batch_size
        if products
        else 0
    )

    plan_summary = {
        "sku_catalog_rows_loaded": catalog["sku_catalog_rows_loaded"],
        "page_count": catalog["page_count"],
        "distinct_articles": catalog["distinct_articles"],
        "distinct_product_ids": catalog["distinct_product_ids"],
        "duplicate_articles": catalog["duplicate_articles"],
        "duplicate_product_ids": catalog["duplicate_product_ids"],
        "planned_stock_api_batches": planned_stock_api_batches,
        "stock_api_batch_size": args.stock_api_batch_size,
        "expected_coverage_improvement_vs_1000_rows": max(catalog["sku_catalog_rows_loaded"] - 1000, 0),
    }

    identity_columns_enabled = stock_identity_columns_supported()
    decision_sku_by_article = build_decision_sku_by_article_map(args.identity_lookback_days) if identity_columns_enabled else {}
    plan_summary["stock_identity_columns_enabled"] = identity_columns_enabled
    plan_summary["decision_sku_by_article_count"] = len(decision_sku_by_article)

    print(json.dumps(plan_summary, ensure_ascii=False, indent=2))

    if args.plan_only:
        raise SystemExit(0)

    stocks, batch_count = get_ozon_stocks(products, args.stock_api_batch_size)
    print(
        json.dumps(
            {
                "event": "ozon_stock_api_summary",
                "product_ids_count": len(products),
                "stock_api_batches_count": batch_count,
                "items_returned": len(stocks),
                "identity_columns_enabled": identity_columns_enabled,
                "decision_sku_by_article_count": len(decision_sku_by_article),
            },
            ensure_ascii=False,
        )
    )
    save_stocks(stocks, identity_columns_enabled, decision_sku_by_article)
