import os
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


def get_previous_month():
    today = date.today()
    month = today.month - 1
    year = today.year

    if month == 0:
        month = 12
        year -= 1

    return month, year


def get_ozon_realization(month, year):
    url = "https://api-seller.ozon.ru/v2/finance/realization"

    payload = {
        "month": month,
        "year": year
    }

    response = requests.post(url, headers=ozon_headers(), json=payload, timeout=120)

    print("Ozon realization HTTP status:", response.status_code)

    if response.status_code != 200:
        print("Ошибка Ozon realization:")
        print(response.text[:3000])
        return None

    return response.json()


def save_realization_to_buyouts(data):
    result = data.get("result", {})
    header = result.get("header", {})
    rows = result.get("rows", [])

    doc_date = header.get("doc_date")
    stop_date = header.get("stop_date")
    buyout_date = stop_date or doc_date or date.today().isoformat()
    buyout_date = buyout_date[:10]

    grouped = {}

    for row in rows:
        item = row.get("item", {}) or {}
        delivery = row.get("delivery_commission", {}) or {}

        sku = item.get("sku")
        offer_id = item.get("offer_id")
        name = item.get("name")

        if not sku:
            continue

        quantity = float(delivery.get("quantity") or 0)
        amount = float(delivery.get("amount") or 0)
        total = float(delivery.get("total") or 0)
        standard_fee = float(delivery.get("standard_fee") or 0)

        key = (
            buyout_date,
            "ozon",
            str(sku)
        )

        if key not in grouped:
            grouped[key] = {
                "buyout_date": buyout_date,
                "marketplace_code": "ozon",
                "marketplace_sku": str(sku),
                "article": str(offer_id or ""),
                "product_name": name,
                "buyouts_qty": 0,
                "buyouts_amount_buyer": 0,
                "buyouts_amount_seller": 0,
                "revenue_after_commission_vat": 0,
                "commission_amount": 0,
                "vat_amount": 0,
            }

        grouped[key]["buyouts_qty"] += quantity
        grouped[key]["buyouts_amount_buyer"] += amount
        grouped[key]["buyouts_amount_seller"] += amount
        grouped[key]["revenue_after_commission_vat"] += total
        grouped[key]["commission_amount"] += standard_fee

    rows_to_save = list(grouped.values())

    if not rows_to_save:
        print("Нет строк Ozon realization для записи")
        return

    for i in range(0, len(rows_to_save), 500):
        batch = rows_to_save[i:i + 500]
        supabase.table("marketplace_buyouts").upsert(
            batch,
            on_conflict="buyout_date,marketplace_code,marketplace_sku"
        ).execute()

    print(f"✅ Ozon realization записан в marketplace_buyouts: {len(rows_to_save)} строк")
    print(f"Дата реализации: {buyout_date}")


if __name__ == "__main__":
    month, year = get_previous_month()
    print(f"Загружаем Ozon realization за {month}.{year}")

    data = get_ozon_realization(month, year)

    if data:
        save_realization_to_buyouts(data)
