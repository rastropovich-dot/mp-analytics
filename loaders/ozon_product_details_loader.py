import os
import time
import requests
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


def get_ozon_product_ids_from_db():
    result = (
        supabase
        .table("sku_catalog")
        .select("marketplace_sku")
        .eq("marketplace_code", "ozon")
        .execute()
    )

    return [int(row["marketplace_sku"]) for row in result.data if str(row["marketplace_sku"]).isdigit()]


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def get_product_details(product_ids):
    url = "https://api-seller.ozon.ru/v3/product/info/list"

    all_details = []

    for batch in chunks(product_ids, 100):
        payload = {
            "product_id": batch
        }

        response = requests.post(url, headers=ozon_headers(), json=payload, timeout=60)

        if response.status_code != 200:
            print("Ошибка Ozon product info:")
            print(response.status_code)
            print(response.text[:2000])
            continue

        data = response.json()
        items = data.get("items", []) or data.get("result", {}).get("items", [])

        all_details.extend(items)

        print(f"Получено деталей: {len(all_details)} / {len(product_ids)}")
        time.sleep(0.3)

    return all_details


def save_details(details):
    rows = []

    for item in details:
        product_id = item.get("id") or item.get("product_id")
        if not product_id:
            continue

        barcodes = item.get("barcodes") or []
        barcode = barcodes[0] if barcodes else None

        row = {
            "marketplace_code": "ozon",
            "marketplace_sku": str(product_id),
            "article": str(item.get("offer_id") or ""),
            "barcode": barcode,
            "product_name": item.get("name"),
            "brand": item.get("brand"),
            "category": item.get("category_name") or item.get("type_name"),
        }

        rows.append(row)

    if not rows:
        print("Нет деталей для записи")
        return

    for batch in chunks(rows, 500):
        supabase.table("sku_catalog").upsert(
            batch,
            on_conflict="marketplace_code,marketplace_sku"
        ).execute()

    print(f"✅ Обновлено товаров Ozon: {len(rows)}")


if __name__ == "__main__":
    product_ids = get_ozon_product_ids_from_db()
    print(f"Товаров Ozon в базе: {len(product_ids)}")

    details = get_product_details(product_ids)
    save_details(details)
