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

SALE_OPERATION_TYPES = {"OperationAgentDeliveredToCustomer"}

LOGISTICS_OPERATION_TYPES = {
    "OperationItemReturn",
    "OperationReturnGoodsFBSofRMS",
    "MarketplaceSellerReexposureDeliveryReturnOperation",
    "MarketplaceServiceRedistributionOfDeliveryServicesRFBS",
}

OTHER_OPERATION_TYPES = {
    "MarketplaceRedistributionOfAcquiringOperation",
    "OperationMarketplacePackageMaterialsProvision",
    "OperationMarketplacePackageRedistribution",
    "OperationMarketplaceServiceStorage",
    "OperationMarketplaceItemTemporaryStorageRedistribution",
    "DefectFineShipmentDelayRated",
    "DefectFineShipmentDelayRatedCancelled",
    "MarketplaceSellerCorrectionOperation",
    "MarketplaceCorrectionPointOperation",
    "OperationMarketPlaceItemPinReview",
}

AD_OPERATION_TYPES = {
    "OperationMarketplaceCostPerClick",
    "OperationPromotionWithCostPerOrder",
    "MarketplaceMarketingActionCostOperation",
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
        print(f"Ozon expenses finance page {page} HTTP status: {response.status_code}")

        if response.status_code != 200:
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

    print(f"Получено операций Ozon для расходов: {len(operations)}")
    return operations


def detect_expense_type(op):
    operation_type = op.get("operation_type")

    if operation_type in SALE_OPERATION_TYPES:
        return "commission"

    if operation_type in LOGISTICS_OPERATION_TYPES:
        return "logistics"

    if operation_type in OTHER_OPERATION_TYPES:
        return "other"

    if operation_type in AD_OPERATION_TYPES:
        return "advertising"

    return None


def get_expense_amount(op, expense_type):
    if expense_type == "commission":
        return abs(float(op.get("sale_commission") or 0))

    return abs(float(op.get("amount") or 0))


def save_ozon_expenses(operations):
    grouped = {}

    counts = {
        "commission": 0,
        "logistics": 0,
        "other": 0,
        "advertising": 0,
        "skipped": 0,
    }

    for op in operations:
        operation_date_raw = op.get("operation_date")
        if not operation_date_raw:
            counts["skipped"] += 1
            continue

        expense_type = detect_expense_type(op)

        if not expense_type:
            counts["skipped"] += 1
            continue

        amount = get_expense_amount(op, expense_type)

        if amount == 0:
            counts["skipped"] += 1
            continue

        if expense_type == "advertising":
            counts["advertising"] += 1
            continue

        expense_date = operation_date_raw[:10]
        items = op.get("items", []) or []

        if not items:
            key = (expense_date, "ozon", "", expense_type)
            if key not in grouped:
                grouped[key] = {
                    "expense_date": expense_date,
                    "marketplace_code": "ozon",
                    "marketplace_sku": "",
                    "article": "",
                    "expense_type": expense_type,
                    "expense_amount": 0,
                }

            grouped[key]["expense_amount"] += amount
            counts[expense_type] += 1
            continue

        amount_per_item = amount / len(items)

        for item in items:
            sku = str(item.get("sku") or "")
            key = (expense_date, "ozon", sku, expense_type)

            if key not in grouped:
                grouped[key] = {
                    "expense_date": expense_date,
                    "marketplace_code": "ozon",
                    "marketplace_sku": sku,
                    "article": "",
                    "expense_type": expense_type,
                    "expense_amount": 0,
                }

            grouped[key]["expense_amount"] += amount_per_item
            counts[expense_type] += 1

    rows = list(grouped.values())

    print("Операций расходов:")
    print(counts)
    print(f"Строк к записи в marketplace_expenses: {len(rows)}")

    if not rows:
        print("Нет расходов Ozon для записи")
        return

    for batch in chunks(rows, 500):
        supabase.table("marketplace_expenses").upsert(
            batch,
            on_conflict="expense_date,marketplace_code,marketplace_sku,expense_type"
        ).execute()

    print(f"✅ Ozon expenses записаны в marketplace_expenses: {len(rows)} строк")


if __name__ == "__main__":
    operations = get_ozon_finance_transactions(days_back=30)
    save_ozon_expenses(operations)
