import os
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")


def ozon_headers():
    return {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def to_local_order_date(value):
    if not value:
        return None

    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(ZoneInfo(APP_TIMEZONE)).date().isoformat()


def get_fbo_postings(days_back=30):
    url = "https://api-seller.ozon.ru/v2/posting/fbo/list"

    date_to = datetime.now(timezone.utc)
    date_from = date_to - timedelta(days=days_back)

    postings = []
    limit = 1000
    offset = 0

    while True:
        payload = {
            "dir": "ASC",
            "filter": {
                "since": date_from.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "to": date_to.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "status": "",
            },
            "limit": limit,
            "offset": offset,
            "translit": True,
            "with": {
                "analytics_data": True,
                "financial_data": True,
            },
        }

        response = requests.post(url, headers=ozon_headers(), json=payload, timeout=120)
        print(f"Ozon FBO postings offset {offset} HTTP status: {response.status_code}")

        if response.status_code != 200:
            print(response.text[:3000])
            break

        data = response.json()
        batch = data.get("result", []) or []

        postings.extend(batch)

        if len(batch) < limit:
            break

        offset += limit

    print(f"Получено FBO postings: {len(postings)}")
    return postings


def parse_date(value):
    if not value:
        return None
    return to_local_order_date(value)


def build_order_rows(postings):
    grouped = {}

    skipped_cancelled = 0
    skipped_no_date = 0

    for posting in postings:
        status = posting.get("status")

        # Отмененные заказы не считаем как заказы к продаже
        if status == "cancelled":
            skipped_cancelled += 1
            continue

        order_date = parse_date(posting.get("created_at") or posting.get("in_process_at"))

        if not order_date:
            skipped_no_date += 1
            continue

        products = posting.get("products", []) or []

        for product in products:
            sku = str(product.get("sku") or "")
            offer_id = str(product.get("offer_id") or "")
            name = product.get("name")
            qty = float(product.get("quantity") or 0)
            price = float(product.get("price") or 0)

            if qty <= 0:
                continue

            key = (order_date, "ozon", sku, "fbo")

            if key not in grouped:
                grouped[key] = {
                    "order_date": order_date,
                    "marketplace_code": "ozon",
                    "marketplace_sku": sku,
                    "article": offer_id,
                    "product_name": name,
                    "orders_qty": 0,
                    "orders_amount_buyer": 0,
                    "orders_amount_seller": 0,
                    "order_schema": "fbo",
                }

            grouped[key]["orders_qty"] += qty
            grouped[key]["orders_amount_buyer"] += qty * price
            grouped[key]["orders_amount_seller"] += qty * price

    rows = list(grouped.values())

    print(f"Пропущено cancelled: {skipped_cancelled}")
    print(f"Пропущено без даты: {skipped_no_date}")
    print(f"Строк к записи в marketplace_orders FBO: {len(rows)}")

    return rows


def save_orders(rows):
    if not rows:
        print("Нет FBO заказов для записи")
        return

    for batch in chunks(rows, 500):
        supabase.table("marketplace_orders").upsert(
            batch,
            on_conflict="order_date,marketplace_code,marketplace_sku,order_schema"
        ).execute()

    print(f"✅ Ozon FBO orders записаны в marketplace_orders: {len(rows)} строк")


if __name__ == "__main__":
    postings = get_fbo_postings(days_back=30)
    rows = build_order_rows(postings)
    save_orders(rows)
