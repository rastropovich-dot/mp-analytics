import os
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
WB_API_KEY = os.getenv("WB_API_KEY")


def check_env():
    missing = []

    if not OZON_CLIENT_ID:
        missing.append("OZON_CLIENT_ID")

    if not OZON_API_KEY:
        missing.append("OZON_API_KEY")

    if not WB_API_KEY:
        missing.append("WB_API_KEY")

    if missing:
        raise ValueError("Не заполнены переменные в .env: " + ", ".join(missing))


def test_ozon():
    print("\n=== Проверка Ozon API ===")

    url = "https://api-seller.ozon.ru/v3/product/list"

    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }

    payload = {
        "filter": {
            "visibility": "ALL"
        },
        "last_id": "",
        "limit": 10
    }

    response = requests.post(url, headers=headers, json=payload, timeout=30)

    print("HTTP status:", response.status_code)

    if response.status_code != 200:
        print("Ответ Ozon:")
        print(response.text[:2000])
        return False

    data = response.json()
    items = data.get("result", {}).get("items", [])

    print(f"✅ Ozon API работает. Получено товаров: {len(items)}")

    if items:
        print("Первый товар:")
        print(items[0])

    return True


def test_wb():
    print("\n=== Проверка WB API ===")

    yesterday = (date.today() - timedelta(days=1)).isoformat()

    url = "https://statistics-api.wildberries.ru/api/v1/supplier/stocks"

    headers = {
        "Authorization": WB_API_KEY
    }

    params = {
        "dateFrom": yesterday
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)

    print("HTTP status:", response.status_code)

    if response.status_code != 200:
        print("Ответ WB:")
        print(response.text[:2000])
        return False

    data = response.json()

    print(f"✅ WB API работает. Получено строк остатков: {len(data)}")

    if data:
        print("Первая строка:")
        print(data[0])

    return True


if __name__ == "__main__":
    check_env()

    ozon_ok = test_ozon()
    wb_ok = test_wb()

    print("\n=== Итог ===")
    print("Ozon:", "OK" if ozon_ok else "ERROR")
    print("WB:", "OK" if wb_ok else "ERROR")
