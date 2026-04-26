import os
import requests
from datetime import date, timedelta
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

WB_API_KEY = os.getenv("WB_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def get_wb_stocks():
    url = "https://statistics-api.wildberries.ru/api/v1/supplier/stocks"

    headers = {
        "Authorization": WB_API_KEY
    }

    params = {
        "dateFrom": (date.today() - timedelta(days=1)).isoformat()
    }

    response = requests.get(url, headers=headers, params=params, timeout=90)

    print("WB HTTP status:", response.status_code)

    if response.status_code != 200:
        print("Ошибка WB stocks API:")
        print(response.text[:3000])
        return []

    return response.json()


def save_wb_to_sku_catalog(items):
    grouped = {}

    for item in items:
        nm_id = item.get("nmId")
        if not nm_id:
            continue

        key = str(nm_id)

        if key not in grouped:
            grouped[key] = {
                "marketplace_code": "wb",
                "marketplace_sku": key,
                "article": str(item.get("supplierArticle") or ""),
                "barcode": str(item.get("barcode") or ""),
                "product_name": item.get("subject"),
                "category": item.get("category"),
                "brand": item.get("brand"),
            }

    rows = list(grouped.values())

    if not rows:
        print("Нет WB товаров для sku_catalog")
        return

    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        supabase.table("sku_catalog").upsert(
            batch,
            on_conflict="marketplace_code,marketplace_sku"
        ).execute()

    print(f"✅ WB товары записаны в sku_catalog: {len(rows)}")


def save_wb_stocks(items):
    today = date.today().isoformat()
    grouped = {}

    for item in items:
        nm_id = item.get("nmId")
        if not nm_id:
            continue

        warehouse_name = item.get("warehouseName") or "unknown"
        qty = item.get("quantity", 0) or 0

        key = (
            today,
            "wb",
            str(nm_id),
            warehouse_name
        )

        if key not in grouped:
            grouped[key] = {
                "stock_date": today,
                "marketplace_code": "wb",
                "marketplace_sku": str(nm_id),
                "article": str(item.get("supplierArticle") or ""),
                "product_name": item.get("subject"),
                "warehouse_name": warehouse_name,
                "stock_qty": 0,
                "reserved_qty": 0,
                "available_qty": 0,
            }

        grouped[key]["stock_qty"] += qty
        grouped[key]["available_qty"] += qty

    rows = list(grouped.values())

    if not rows:
        print("Нет WB остатков для записи")
        return

    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        supabase.table("stock_daily").upsert(
            batch,
            on_conflict="stock_date,marketplace_code,marketplace_sku,warehouse_name"
        ).execute()

    print(f"✅ WB остатки записаны в stock_daily: {len(rows)} строк")


if __name__ == "__main__":
    items = get_wb_stocks()
    print(f"Получено строк WB stocks: {len(items)}")

    save_wb_to_sku_catalog(items)
    save_wb_stocks(items)
