import os
import requests
from datetime import date
from dotenv import load_dotenv

load_dotenv()

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")

headers = {
    "Client-Id": OZON_CLIENT_ID,
    "Api-Key": OZON_API_KEY,
    "Content-Type": "application/json",
}

url = "https://api-seller.ozon.ru/v2/finance/realization"

today = date.today()

month = today.month - 1
year = today.year

if month == 0:
    month = 12
    year -= 1

payload = {
    "month": month,
    "year": year
}

print("Пробуем отчет Ozon realization за:", month, year)

response = requests.post(url, headers=headers, json=payload, timeout=120)

print("HTTP status:", response.status_code)
print(response.text[:5000])
