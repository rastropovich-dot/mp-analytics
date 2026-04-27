import os
import requests
from datetime import datetime, timedelta, timezone
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


def get_ozon_fbs_postings(days_back=14):
    url = "https://api-seller.ozon.ru/v3/posting/fbs/list"

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days_back)

    payload = {
        "dir": "ASC",
        "filter": {
            "since": since.isoformat(),
            "to": now.isoformat()
        },
        "limit": 1000,
        "offset": 0,
        "with": {
            "analytics_data": True,
            "financial_data": True
        }
    }

    all_postings = []

    while True:
        response = requests.post(url, headers=ozon_headers(), json=payload, timeout=120)

        print("Ozon FBS orders HTTP status:", response.status_code)

        if response.status_code != 200:
            print("Ошибка Ozon FBS orders API:")
            print(response.text[:3000])
            return all_postings

        data = response.json()
        result = data.get("result", {})
        postings = result.get("postings", [])

        all_postings.extend(postings)

        print(f"Получено отправлений Ozon FBS: {len(all_postings)}")

        if len(postings) < payload["limit"]:
            break

        payload["offset"] += payload["limit"]

    return all_postings


def save_ozon_orders(postings):
    grouped = {}

    for posting in postings:
        shipment_date = posting.get("shipment_date") or posting.get("in_process_at")
        if not shipment_date:
            continue

        order_date = shipment_date[:10]

        products = posting.get("products", [])

        for product in products:
            product_id = product.get("sku") or product.get("product_id") or product.get("offer_id")
            offer_id = product.get("offer_id")
            name = product.get("name")

            if not product_id:
                continue

            qty = product.get("quantity", 1) or 1
            price = float(product.get("price", 0) or 0)

            key = (
                order_date,
                "ozon",
                str(product_id)
            )

            if key not in grouped:
                grouped[key] = {
                    "order_date": order_date,
                    "marketplace_code": "ozon",
                "order_schema": "fbs",
                    "marketplace_sku": str(product_id),
                    "article": str(offer_id or ""),
                    "product_name": name,
                    "orders_qty": 0,
                    "orders_amount_buyer": 0,
                    "orders_amount_seller": 0,
                }

            grouped[key]["orders_qty"] += qty
            grouped[key]["orders_amount_buyer"] += price * qty
            grouped[key]["orders_amount_seller"] += price * qty

    rows = list(grouped.values())

    if not rows:
        print("Нет Ozon FBS заказов для записи")
        return

    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        supabase.table("marketplace_orders").upsert(
            batch,
            on_conflict="order_date,marketplace_code,marketplace_sku,order_schema"
        ).execute()

    print(f"✅ Ozon FBS заказы записаны в marketplace_orders: {len(rows)} строк")


if __name__ == "__main__":
    postings = get_ozon_fbs_postings(days_back=14)
    print(f"Итого отправлений Ozon FBS: {len(postings)}")
    save_ozon_orders(postings)
