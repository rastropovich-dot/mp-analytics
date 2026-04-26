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


def get_ozon_products():
    url = "https://api-seller.ozon.ru/v3/product/list"

    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }

    all_items = []
    last_id = ""

    while True:
        payload = {
            "filter": {
                "visibility": "ALL"
            },
            "last_id": last_id,
            "limit": 1000
        }

        response = requests.post(url, headers=headers, json=payload, timeout=60)

        if response.status_code != 200:
            print("Ошибка Ozon API:")
            print(response.status_code)
            print(response.text[:2000])
            break

        data = response.json()
        result = data.get("result", {})
        items = result.get("items", [])

        all_items.extend(items)

        print(f"Получено товаров: {len(all_items)}")

        last_id = result.get("last_id")

        if not last_id or len(items) == 0:
            break

        time.sleep(0.2)

    return all_items


def save_products_to_supabase(items):
    rows = []

    for item in items:
        marketplace_sku = str(item.get("product_id") or item.get("offer_id") or "")

        if not marketplace_sku:
            continue

        row = {
            "marketplace_code": "ozon",
            "marketplace_sku": marketplace_sku,
            "article": str(item.get("offer_id") or ""),
            "product_name": None,
            "brand": None,
            "category": None,
            "metal": None,
            "stones": None,
        }

        rows.append(row)

    if not rows:
        print("Нет данных для записи")
        return

    batch_size = 500

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]

        result = (
            supabase
            .table("sku_catalog")
            .upsert(
                batch,
                on_conflict="marketplace_code,marketplace_sku"
            )
            .execute()
        )

        print(f"Записано строк: {i + len(batch)}")

    print("✅ Товары Ozon записаны в sku_catalog")


if __name__ == "__main__":
    products = get_ozon_products()
    print(f"Итого товаров Ozon: {len(products)}")
    save_products_to_supabase(products)
