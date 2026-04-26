import os
import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

headers = {
    "Client-Id": OZON_CLIENT_ID,
    "Api-Key": OZON_API_KEY,
    "Content-Type": "application/json",
}

# Берем первые 20 товаров Ozon из базы
products = (
    supabase
    .table("sku_catalog")
    .select("marketplace_sku, article")
    .eq("marketplace_code", "ozon")
    .limit(20)
    .execute()
)

product_ids = []
offer_ids = []

for row in products.data:
    if str(row.get("marketplace_sku", "")).isdigit():
        product_ids.append(int(row["marketplace_sku"]))
    if row.get("article"):
        offer_ids.append(row["article"])

print("product_ids:", product_ids[:5])
print("offer_ids:", offer_ids[:5])

url = "https://api-seller.ozon.ru/v4/product/info/stocks"

payload = {
    "filter": {
        "product_id": product_ids,
        "offer_id": offer_ids,
        "visibility": "ALL"
    },
    "last_id": "",
    "limit": 100
}

response = requests.post(url, headers=headers, json=payload, timeout=60)

print("HTTP status:", response.status_code)
print(response.text[:5000])
