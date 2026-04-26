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


SALE_OPERATION_TYPES = {
    "OperationAgentDeliveredToCustomer",
}

RETURN_OPERATION_TYPES = {
    "ClientReturnAgentOperation",
}


def ozon_headers():
    return {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def get_ozon_finance_transactions(days_back=30):
    url = "https://api-seller.ozon.ru/v3/finance/transaction/list"

    date_to = datetime.now(timezone.utc)
    date_from = date_to - timedelta(days=days_back)

    operations = []
    page = 1
    page_size = 1000

    while True:
        payload = {
            "filter": {
                "date": {
                    "from": date_from.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "to": date_to.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                },
                "operation_type": [],
                "posting_number": "",
                "transaction_type": "all",
            },
            "page": page,
            "page_size": page_size,
        }

        response = requests.post(url, headers=ozon_headers(), json=payload, timeout=120)
        print(f"Ozon finance page {page} HTTP status: {response.status_code}")

        if response.status_code != 200:
            print("Ошибка Ozon finance:")
            print(response.text[:3000])
            break

        data = response.json()
        result = data.get("result", {}) or {}
        batch = result.get("operations", []) or []

        operations.extend(batch)

        page_count = int(result.get("page_count") or 1)

        if page >= page_count or not batch:
            break

        page += 1

    print(f"Получено операций Ozon finance: {len(operations)}")
    return operations


def save_ozon_sales_to_buyouts(operations):
    grouped = {}

    sale_ops = 0
    return_ops = 0
    skipped_ops = 0

    for op in operations:
        operation_type = op.get("operation_type")
        operation_date_raw = op.get("operation_date")

        if not operation_date_raw:
            skipped_ops += 1
            continue

        if operation_type in SALE_OPERATION_TYPES:
            sign = 1
            sale_ops += 1
        elif operation_type in RETURN_OPERATION_TYPES:
            sign = -1
            return_ops += 1
        else:
            skipped_ops += 1
            continue

        buyout_date = operation_date_raw[:10]

        accruals_for_sale = abs(float(op.get("accruals_for_sale") or 0))
        sale_commission = abs(float(op.get("sale_commission") or 0))
        amount = float(op.get("amount") or 0)

        items = op.get("items", []) or []
        if not items:
            skipped_ops += 1
            continue

        item_count = len(items)

        accrual_per_item = accruals_for_sale / item_count
        commission_per_item = sale_commission / item_count
        amount_per_item = amount / item_count

        for item in items:
            sku = item.get("sku")
            name = item.get("name")

            if not sku:
                continue

            key = (buyout_date, "ozon", str(sku))

            if key not in grouped:
                grouped[key] = {
                    "buyout_date": buyout_date,
                    "marketplace_code": "ozon",
                    "marketplace_sku": str(sku),
                    "article": "",
                    "product_name": name,
                    "buyouts_qty": 0,
                    "buyouts_amount_buyer": 0,
                    "buyouts_amount_seller": 0,
                    "revenue_after_commission_vat": 0,
                    "commission_amount": 0,
                    "vat_amount": 0,
                }

            grouped[key]["buyouts_qty"] += sign * 1
            grouped[key]["buyouts_amount_buyer"] += sign * accrual_per_item
            grouped[key]["buyouts_amount_seller"] += sign * accrual_per_item
            grouped[key]["revenue_after_commission_vat"] += amount_per_item
            grouped[key]["commission_amount"] += sign * commission_per_item

    rows = list(grouped.values())

    print(f"Продажных операций: {sale_ops}")
    print(f"Возвратных операций: {return_ops}")
    print(f"Пропущено прочих операций: {skipped_ops}")
    print(f"Строк к записи в marketplace_buyouts: {len(rows)}")

    if not rows:
        print("Нет строк Ozon finance для записи")
        return

    for batch in chunks(rows, 500):
        supabase.table("marketplace_buyouts").upsert(
            batch,
            on_conflict="buyout_date,marketplace_code,marketplace_sku"
        ).execute()

    print(f"✅ Ozon daily finance записан в marketplace_buyouts: {len(rows)} строк")


if __name__ == "__main__":
    operations = get_ozon_finance_transactions(days_back=30)
    save_ozon_sales_to_buyouts(operations)
