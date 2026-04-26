import os
import time
import requests
from datetime import date
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def ozon_headers():
    return {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def get_ozon_products_from_db():
    result = (
        supabase
        .table("sku_catalog")
        .select("marketplace_sku, article, product_name")
        .eq("marketplace_code", "ozon")
        .execute()
    )

    products = []
    for row in result.data:
        if str(row.get("marketplace_sku", "")).isdigit():
            products.append({
                "product_id": int(row["marketplace_sku"]),
                "offer_id": row.get("article"),
                "product_name": row.get("product_name"),
            })

    return products


def get_ozon_stocks(products):
    url = "https://api-seller.ozon.ru/v4/product/info/stocks"
    all_items = []

    for batch in chunks(products, 100):
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
        print(f"Получено остатков: {len(all_items)} / товаров проверено: {len(product_ids)}")

        time.sleep(0.25)

    return all_items


def save_stocks(items):
    today = date.today().isoformat()

    grouped = {}

    for item in items:
        product_id = item.get("product_id")
        offer_id = item.get("offer_id")
        stocks = item.get("stocks", [])

        for stock in stocks:
            stock_type = stock.get("type", "unknown")

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
                    "marketplace_sku": str(product_id),
                    "article": str(offer_id or ""),
                    "product_name": None,
                    "warehouse_name": stock_type,
                    "stock_qty": 0,
                    "reserved_qty": 0,
                    "available_qty": 0,
                }

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
    products = get_ozon_products_from_db()
    print(f"Товаров Ozon в базе: {len(products)}")

    stocks = get_ozon_stocks(products)
    print(f"Итого товаров с остатками из Ozon: {len(stocks)}")

    save_stocks(stocks)
