import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def empty_kpi_row(kpi_date, marketplace_code, marketplace_sku, article="", product_name=None):
    return {
        "kpi_date": kpi_date,
        "marketplace_code": marketplace_code,
        "marketplace_sku": marketplace_sku,
        "article": article,
        "product_name": product_name,
        "orders_qty": 0,
        "orders_amount_seller": 0,
        "buyouts_qty": 0,
        "buyouts_amount_seller": 0,
        "buyout_rate": 0,
        "ad_spend": 0,
        "ad_orders_qty": 0,
        "ad_orders_revenue": 0,
        "organic_orders_qty": 0,
        "organic_orders_revenue": 0,
        "ad_share_of_orders": 0,
        "ad_share_orders": 0,
        "ad_share_revenue": 0,
        "roas": 0,
        "commission_amount": 0,
        "logistics_amount": 0,
        "other_expenses_amount": 0,
    }


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



def load_expenses():
    all_rows = []
    start = 0
    page_size = 1000

    while True:
        result = (
            supabase
            .table("marketplace_expenses")
            .select("*")
            .range(start, start + page_size - 1)
            .execute()
        )

        rows = result.data or []
        all_rows.extend(rows)

        if len(rows) < page_size:
            break

        start += page_size

    print(f"Загружено расходов: {len(all_rows)}")
    return all_rows


def load_ozon_organic():
    all_rows = []
    start = 0
    page_size = 1000

    while True:
        try:
            result = (
                supabase
                .table("ozon_daily_sku_organic")
                .select("*")
                .range(start, start + page_size - 1)
                .execute()
            )
        except Exception as e:
            print(
                "Не удалось загрузить ozon_daily_sku_organic. "
                "Проверьте миграцию sql/20260506_create_ozon_daily_sku_organic.sql. "
                f"Ошибка: {e}"
            )
            return []

        rows = result.data or []
        all_rows.extend(rows)

        if len(rows) < page_size:
            break

        start += page_size

    print(f"Загружено Ozon organic строк: {len(all_rows)}")
    return all_rows


def build_kpi():
    orders = load_orders()
    buyouts = load_buyouts()
    expenses = load_expenses()
    ozon_organic = load_ozon_organic()

    grouped = {}

    for row in orders:
        key = (
            row["order_date"],
            row["marketplace_code"],
            row["marketplace_sku"],
        )

        if key not in grouped:
            grouped[key] = empty_kpi_row(
                row["order_date"],
                row["marketplace_code"],
                row["marketplace_sku"],
                row.get("article"),
                row.get("product_name"),
            )

        grouped[key]["orders_qty"] += float(row.get("orders_qty") or 0)
        grouped[key]["orders_amount_seller"] += float(row.get("orders_amount_seller") or 0)

    for row in buyouts:
        key = (
            row["buyout_date"],
            row["marketplace_code"],
            row["marketplace_sku"],
        )

        if key not in grouped:
            grouped[key] = empty_kpi_row(
                row["buyout_date"],
                row["marketplace_code"],
                row["marketplace_sku"],
                row.get("article"),
                row.get("product_name"),
            )

        grouped[key]["buyouts_qty"] += float(row.get("buyouts_qty") or 0)
        grouped[key]["buyouts_amount_seller"] += float(row.get("buyouts_amount_seller") or 0)

    for row in expenses:
        key = (
            row["expense_date"],
            row["marketplace_code"],
            row["marketplace_sku"],
        )

        if key not in grouped:
            grouped[key] = empty_kpi_row(
                row["expense_date"],
                row["marketplace_code"],
                row["marketplace_sku"],
                row.get("article"),
                None,
            )

        expense_type = row.get("expense_type")
        amount = float(row.get("expense_amount") or 0)

        if expense_type == "commission":
            grouped[key]["commission_amount"] += amount
        elif expense_type == "logistics":
            grouped[key]["logistics_amount"] += amount
        elif str(expense_type or "").startswith("advertising"):
            grouped[key]["ad_spend"] += amount
        else:
            grouped[key]["other_expenses_amount"] += amount

    for row in ozon_organic:
        key = (
            row["sale_date"],
            row["marketplace_code"],
            row["marketplace_sku"],
        )

        if key not in grouped:
            grouped[key] = empty_kpi_row(
                row["sale_date"],
                row["marketplace_code"],
                row["marketplace_sku"],
                row.get("article"),
                row.get("product_name"),
            )

        grouped[key]["ad_orders_qty"] += float(row.get("ad_orders_qty") or 0)
        grouped[key]["ad_orders_revenue"] += float(row.get("ad_orders_revenue") or 0)
        grouped[key]["organic_orders_qty"] += float(row.get("organic_orders_qty") or 0)
        grouped[key]["organic_orders_revenue"] += float(row.get("organic_orders_revenue") or 0)

        if not grouped[key].get("article") and row.get("article"):
            grouped[key]["article"] = row.get("article")
        if not grouped[key].get("product_name") and row.get("product_name"):
            grouped[key]["product_name"] = row.get("product_name")

    rows = []

    for row in grouped.values():
        orders_qty = row["orders_qty"]
        buyouts_qty = row["buyouts_qty"]

        if orders_qty > 0:
            row["buyout_rate"] = round(buyouts_qty / orders_qty, 4)
        else:
            row["buyout_rate"] = 0

        orders_amount = row["orders_amount_seller"]
        ad_spend = row.get("ad_spend") or 0

        if orders_amount > 0:
            row["ad_share_of_orders"] = round(ad_spend / orders_amount, 4)
            row["ad_share_revenue"] = round((row.get("ad_orders_revenue") or 0) / orders_amount, 4)
        else:
            row["ad_share_of_orders"] = 0
            row["ad_share_revenue"] = 0

        if orders_qty > 0:
            row["ad_share_orders"] = round((row.get("ad_orders_qty") or 0) / orders_qty, 4)
        else:
            row["ad_share_orders"] = 0

        if ad_spend > 0:
            row["roas"] = round(orders_amount / ad_spend, 4)
        else:
            row["roas"] = 0

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
