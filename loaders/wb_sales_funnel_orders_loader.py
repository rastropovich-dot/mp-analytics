import os
import time
import requests
from datetime import date, timedelta
from dotenv import load_dotenv
from supabase import create_client

load_dotenv("/Users/mihaileliseev/mp-analytics/.env")

WB_API_KEY = os.getenv("WB_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not WB_API_KEY:
    raise RuntimeError("Не найден WB_API_KEY в .env")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Не найдены SUPABASE_URL / SUPABASE_SERVICE_KEY / SUPABASE_KEY / SUPABASE_SERVICE_ROLE_KEY в .env")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"

HEADERS = {
    "Authorization": WB_API_KEY,
    "Content-Type": "application/json",
}

DEFAULT_DAYS_BACK = 4


def get_days_back():
    raw_days_back = os.getenv("WB_SALES_FUNNEL_DAYS_BACK")

    if not raw_days_back:
        return DEFAULT_DAYS_BACK

    try:
        days_back = int(raw_days_back)
    except ValueError:
        raise RuntimeError("WB_SALES_FUNNEL_DAYS_BACK должен быть целым числом")

    if days_back < 1:
        raise RuntimeError("WB_SALES_FUNNEL_DAYS_BACK должен быть больше 0")

    return days_back


def fetch_wb_sales_funnel_day(day: str):
    """
    Загружает WB Sales Funnel products за 1 день.
    Суммирует statistic.selected.orderCount и statistic.selected.orderSum по всем страницам.
    """
    limit = 1000
    offset = 0

    total_order_count = 0
    total_order_sum = 0
    total_products = 0

    while True:
        payload = {
            "selectedPeriod": {
                "start": day,
                "end": day
            },
            "brandNames": [],
            "subjectIds": [],
            "tagIds": [],
            "nmIds": [],
            "timezone": "Europe/Moscow",
            "limit": limit,
            "offset": offset
        }

        resp = requests.post(URL, headers=HEADERS, json=payload, timeout=60)

        print(f"WB Sales Funnel {day} offset {offset} HTTP: {resp.status_code}")

        if resp.status_code == 429:
            print("⏳ WB rate limit. Жду 60 секунд...")
            time.sleep(60)
            continue

        if resp.status_code != 200:
            print(resp.text[:3000])
            raise RuntimeError(f"WB Sales Funnel API error: {resp.status_code}")

        data = resp.json()
        products = data.get("data", {}).get("products", [])

        print(f"Получено товаров: {len(products)}")

        for product in products:
            stat = product.get("statistic", {}).get("selected", {})
            total_order_count += float(stat.get("orderCount") or 0)
            total_order_sum += float(stat.get("orderSum") or 0)

        total_products += len(products)

        if len(products) < limit:
            break

        offset += limit
        time.sleep(3)

    return {
        "order_date": day,
        "marketplace_code": "wb",
        "orders_qty": total_order_count,
        "orders_amount": total_order_sum,
        "source": "wb_sales_funnel",
        "products_count": total_products,
    }


def save_day(row):
    payload = {
        "order_date": row["order_date"],
        "marketplace_code": row["marketplace_code"],
        "orders_qty": row["orders_qty"],
        "orders_amount": row["orders_amount"],
        "source": row["source"],
    }

    supabase.table("marketplace_orders_analytics").upsert(
        payload,
        on_conflict="order_date,marketplace_code,source"
    ).execute()

    print(
        f"✅ WB Sales Funnel orders записаны: "
        f"{row['order_date']} | "
        f"{row['orders_qty']:.0f} шт | "
        f"{row['orders_amount']:.0f} руб | "
        f"товаров: {row['products_count']}"
    )


def main(days_back=DEFAULT_DAYS_BACK):
    today = date.today()

    # Берем вчера и предыдущие дни. Сегодня не берем как полный день.
    print(f"WB Sales Funnel: обновляю последние {days_back} полных дней")

    for i in range(days_back, 0, -1):
        day = (today - timedelta(days=i)).isoformat()
        row = fetch_wb_sales_funnel_day(day)
        save_day(row)
        time.sleep(5)


if __name__ == "__main__":
    main(days_back=get_days_back())
