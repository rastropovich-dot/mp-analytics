import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def load_orders():
    all_rows = []
    start = 0
    page_size = 1000

    while True:
        result = (
            supabase
            .table("marketplace_orders")
            .select("*")
            .range(start, start + page_size - 1)
            .execute()
        )

        rows = result.data or []
        all_rows.extend(rows)

        if len(rows) < page_size:
            break

        start += page_size

    print(f"Загружено заказов: {len(all_rows)}")
    return all_rows


def load_buyouts():
    all_rows = []
    start = 0
    page_size = 1000

    while True:
        result = (
            supabase
            .table("marketplace_buyouts")
            .select("*")
            .range(start, start + page_size - 1)
            .execute()
        )

        rows = result.data or []
        all_rows.extend(rows)

        if len(rows) < page_size:
            break

        start += page_size

    print(f"Загружено выкупов: {len(all_rows)}")
    return all_rows


def build_kpi():
    orders = load_orders()
    buyouts = load_buyouts()

    grouped = {}

    for row in orders:
        key = (
            row["order_date"],
            row["marketplace_code"],
            row["marketplace_sku"],
        )

        if key not in grouped:
            grouped[key] = {
                "kpi_date": row["order_date"],
                "marketplace_code": row["marketplace_code"],
                "marketplace_sku": row["marketplace_sku"],
                "article": row.get("article"),
                "product_name": row.get("product_name"),
                "orders_qty": 0,
                "orders_amount_seller": 0,
                "buyouts_qty": 0,
                "buyouts_amount_seller": 0,
                "buyout_rate": 0,
            }

        grouped[key]["orders_qty"] += float(row.get("orders_qty") or 0)
        grouped[key]["orders_amount_seller"] += float(row.get("orders_amount_seller") or 0)

    for row in buyouts:
        key = (
            row["buyout_date"],
            row["marketplace_code"],
            row["marketplace_sku"],
        )

        if key not in grouped:
            grouped[key] = {
                "kpi_date": row["buyout_date"],
                "marketplace_code": row["marketplace_code"],
                "marketplace_sku": row["marketplace_sku"],
                "article": row.get("article"),
                "product_name": row.get("product_name"),
                "orders_qty": 0,
                "orders_amount_seller": 0,
                "buyouts_qty": 0,
                "buyouts_amount_seller": 0,
                "buyout_rate": 0,
            }

        grouped[key]["buyouts_qty"] += float(row.get("buyouts_qty") or 0)
        grouped[key]["buyouts_amount_seller"] += float(row.get("buyouts_amount_seller") or 0)

    rows = []

    for row in grouped.values():
        orders_qty = row["orders_qty"]
        buyouts_qty = row["buyouts_qty"]

        if orders_qty > 0:
            row["buyout_rate"] = round(buyouts_qty / orders_qty, 4)
        else:
            row["buyout_rate"] = 0

        rows.append(row)

    return rows


def save_kpi(rows):
    if not rows:
        print("Нет KPI для записи")
        return

    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        supabase.table("daily_sku_kpi").upsert(
            batch,
            on_conflict="kpi_date,marketplace_code,marketplace_sku"
        ).execute()

    print(f"✅ daily_sku_kpi обновлена: {len(rows)} строк")


if __name__ == "__main__":
    rows = build_kpi()
    save_kpi(rows)
