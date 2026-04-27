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


def get_wb_orders(days_back=30):
    url = "https://statistics-api.wildberries.ru/api/v1/supplier/orders"

    headers = {
        "Authorization": WB_API_KEY
    }

    params = {
        "dateFrom": (date.today() - timedelta(days=days_back)).isoformat(),
        "flag": 0
    }

    response = requests.get(url, headers=headers, params=params, timeout=120)

    print("WB orders HTTP status:", response.status_code)

    if response.status_code != 200:
        print("Ошибка WB orders API:")
        print(response.text[:3000])
        return []

    return response.json()


def save_wb_orders(items):
    grouped = {}

    for item in items:
        nm_id = item.get("nmId")
        if not nm_id:
            continue

        date_raw = item.get("date")
        if not date_raw:
            continue

        order_date = date_raw[:10]

        key = (
            order_date,
            "wb",
            str(nm_id)
        )

        price = item.get("totalPrice", 0) or 0
        discount_percent = item.get("discountPercent", 0) or 0
        finished_price = item.get("finishedPrice", 0) or 0
        price_with_disc = item.get("priceWithDisc", 0) or 0

        # Для WB в разных отчетах могут быть разные поля цен.
        # Сохраняем максимально близко:
        # buyer amount = finishedPrice, seller amount = priceWithDisc.
        buyer_amount = finished_price or price_with_disc or price
        seller_amount = price_with_disc or finished_price or price

        if key not in grouped:
            grouped[key] = {
                "order_date": order_date,
                "marketplace_code": "wb",
                "order_schema": "marketplace",
                "marketplace_sku": str(nm_id),
                "article": str(item.get("supplierArticle") or ""),
                "product_name": item.get("subject"),
                "orders_qty": 0,
                "orders_amount_buyer": 0,
                "orders_amount_seller": 0,
            }

        grouped[key]["orders_qty"] += 1
        grouped[key]["orders_amount_buyer"] += buyer_amount
        grouped[key]["orders_amount_seller"] += seller_amount

    rows = list(grouped.values())

    if not rows:
        print("Нет WB заказов для записи")
        return

    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        supabase.table("marketplace_orders").upsert(
            batch,
            on_conflict="order_date,marketplace_code,marketplace_sku,order_schema"
        ).execute()

    print(f"✅ WB заказы записаны в marketplace_orders: {len(rows)} строк")


if __name__ == "__main__":
    items = get_wb_orders(days_back=30)
    print(f"Получено строк WB orders: {len(items)}")
    save_wb_orders(items)
