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


def get_wb_sales(days_back=30):
    url = "https://statistics-api.wildberries.ru/api/v1/supplier/sales"

    headers = {
        "Authorization": WB_API_KEY
    }

    params = {
        "dateFrom": (date.today() - timedelta(days=days_back)).isoformat(),
        "flag": 0
    }

    response = requests.get(url, headers=headers, params=params, timeout=120)

    print("WB sales HTTP status:", response.status_code)

    if response.status_code != 200:
        print("Ошибка WB sales API:")
        print(response.text[:3000])
        return []

    return response.json()


def save_wb_sales(items):
    grouped = {}

    for item in items:
        nm_id = item.get("nmId")
        if not nm_id:
            continue

        date_raw = item.get("date")
        if not date_raw:
            continue

        sale_date = date_raw[:10]

        key = (
            sale_date,
            "wb",
            str(nm_id)
        )

        price = item.get("totalPrice", 0) or 0
        finished_price = item.get("finishedPrice", 0) or 0
        price_with_disc = item.get("priceWithDisc", 0) or 0

        buyer_amount = finished_price or price_with_disc or price
        seller_amount = price_with_disc or finished_price or price

        # В WB sales могут попадать возвраты с отрицательными значениями.
        # Для первого слоя считаем положительные продажи как выкупы.
        qty = 1
        if buyer_amount < 0 or seller_amount < 0:
            qty = -1

        if key not in grouped:
            grouped[key] = {
                "buyout_date": sale_date,
                "marketplace_code": "wb",
                "marketplace_sku": str(nm_id),
                "article": str(item.get("supplierArticle") or ""),
                "product_name": item.get("subject"),
                "buyouts_qty": 0,
                "buyouts_amount_buyer": 0,
                "buyouts_amount_seller": 0,
                "revenue_after_commission_vat": 0,
                "commission_amount": 0,
                "vat_amount": 0,
            }

        grouped[key]["buyouts_qty"] += qty
        grouped[key]["buyouts_amount_buyer"] += buyer_amount
        grouped[key]["buyouts_amount_seller"] += seller_amount

    rows = list(grouped.values())

    if not rows:
        print("Нет WB продаж/выкупов для записи")
        return

    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        supabase.table("marketplace_buyouts").upsert(
            batch,
            on_conflict="buyout_date,marketplace_code,marketplace_sku"
        ).execute()

    print(f"✅ WB выкупы/продажи записаны в marketplace_buyouts: {len(rows)} строк")


if __name__ == "__main__":
    items = get_wb_sales(days_back=30)
    print(f"Получено строк WB sales: {len(items)}")
    save_wb_sales(items)
