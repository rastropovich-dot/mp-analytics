import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def load_daily_sku_kpi():
    all_rows = []
    start = 0
    page_size = 1000

    while True:
        result = (
            supabase
            .table("daily_sku_kpi")
            .select("*")
            .range(start, start + page_size - 1)
            .execute()
        )

        rows = result.data or []
        all_rows.extend(rows)

        if len(rows) < page_size:
            break

        start += page_size

    return all_rows


def build_marketplace_kpi():
    sku_rows = load_daily_sku_kpi()
    print(f"Строк daily_sku_kpi загружено: {len(sku_rows)}")

    grouped = {}

    for row in sku_rows:
        kpi_date = row.get("kpi_date")
        marketplace_code = row.get("marketplace_code")

        if not kpi_date or not marketplace_code:
            continue

        key = (kpi_date, marketplace_code)

        if key not in grouped:
            grouped[key] = {
                "kpi_date": kpi_date,
                "marketplace_code": marketplace_code,

                "orders_qty": 0,
                "orders_amount_seller": 0,

                "buyouts_qty": 0,
                "buyouts_amount_seller": 0,

                "buyout_rate": 0,

                "ad_spend": 0,
                "ad_share_of_orders": 0,
                "roas": 0,

                "commission_amount": 0,
                "logistics_amount": 0,
                "other_expenses_amount": 0,

                "gross_margin_amount": 0,
                "gross_margin_percent": 0,

                "ebitda_amount": 0,
                "ebitda_percent": 0,
            }

        grouped[key]["orders_qty"] += float(row.get("orders_qty") or 0)
        grouped[key]["orders_amount_seller"] += float(row.get("orders_amount_seller") or 0)

        grouped[key]["buyouts_qty"] += float(row.get("buyouts_qty") or 0)
        grouped[key]["buyouts_amount_seller"] += float(row.get("buyouts_amount_seller") or 0)

        grouped[key]["ad_spend"] += float(row.get("ad_spend") or 0)
        grouped[key]["commission_amount"] += float(row.get("commission_amount") or 0)
        grouped[key]["logistics_amount"] += float(row.get("logistics_amount") or 0)
        grouped[key]["other_expenses_amount"] += float(row.get("other_expenses_amount") or 0)
        grouped[key]["gross_margin_amount"] += float(row.get("gross_margin_amount") or 0)
        grouped[key]["ebitda_amount"] += float(row.get("ebitda_amount") or 0)

    rows = []

    for row in grouped.values():
        if row["orders_qty"] > 0:
            row["buyout_rate"] = round(row["buyouts_qty"] / row["orders_qty"], 4)
        else:
            row["buyout_rate"] = 0

        if row["buyouts_amount_seller"] > 0:
            row["gross_margin_percent"] = round(row["gross_margin_amount"] / row["buyouts_amount_seller"], 4)
            row["ebitda_percent"] = round(row["ebitda_amount"] / row["buyouts_amount_seller"], 4)

        if row["orders_amount_seller"] > 0:
            row["ad_share_of_orders"] = round(row["ad_spend"] / row["orders_amount_seller"], 4)

        if row["ad_spend"] > 0:
            row["roas"] = round(row["orders_amount_seller"] / row["ad_spend"], 4)

        rows.append(row)

    print(f"Строк daily_marketplace_kpi к записи: {len(rows)}")
    return rows


def save_marketplace_kpi(rows):
    if not rows:
        print("Нет данных для daily_marketplace_kpi")
        return

    for batch in chunks(rows, 500):
        supabase.table("daily_marketplace_kpi").upsert(
            batch,
            on_conflict="kpi_date,marketplace_code"
        ).execute()

    print(f"✅ daily_marketplace_kpi обновлена: {len(rows)} строк")


if __name__ == "__main__":
    rows = build_marketplace_kpi()
    save_marketplace_kpi(rows)
