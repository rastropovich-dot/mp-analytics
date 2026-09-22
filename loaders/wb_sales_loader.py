import os
import requests

try:
    from loaders import http_retry
except ImportError:  # пайплайн зовёт как скрипт: python3 loaders/<файл>.py
    import http_retry
from datetime import date, timedelta
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# Тот же заряженный дефект, что и в wb_orders_loader: flag=0 отбирает по дате
# последнего изменения, а upsert замещает агрегат дня. Разница — в частоте
# выстрела: продажа терминальна и меняется редко.
#
# Замер 2026-09-03 (flag=1 по трём датам разного возраста, 331 строка):
#   2026-08-01 (33 дн): API 159 / 1 887 755 ₽ = база, расхождение 0;
#   2026-06-20 (75 дн): API  75 /   812 733 ₽ = база, расхождение 0;
#   2026-05-10 (116 дн): API 95 / 1 198 360 ₽, в базе 91 / 1 169 859 ₽.
#
# На 2026-05-10 дефект ВЫСТРЕЛИЛ: у sku 17641480 продажа S23234165356 от 10 мая
# изменилась 22 мая, всплыла в flag=0 одна, и агрегат (дата, sku) переписался
# с 5 продаж на 1. Записей с lastChangeDate позже даты продажи — 4 из 331 (1,2 %),
# ущерб наносит та, у которой в этот день у sku была не одна продажа.
#
# Поэтому окно записи ставится и здесь: цена нулевая, а выстрел редкий, но реальный.
DEFAULT_DAYS_BACK = 30

WB_API_KEY = os.getenv("WB_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def write_window_start(days_back=DEFAULT_DAYS_BACK, today=None):
    """Первая дата, которую ответ flag=0 отдаёт целиком, а значит можно писать."""
    return (today or date.today()) - timedelta(days=days_back)


def get_wb_sales(days_back=DEFAULT_DAYS_BACK, today=None):
    url = "https://statistics-api.wildberries.ru/api/v1/supplier/sales"

    headers = {
        "Authorization": WB_API_KEY
    }

    params = {
        "dateFrom": write_window_start(days_back, today).isoformat(),
        "flag": 0
    }

    response = http_retry.get(url, label="WB sales", retry_429=http_retry.RETRY_429_ANY,
                              headers=headers, params=params, timeout=120)

    print("WB sales HTTP status:", response.status_code)

    if response.status_code != 200:
        print("Ошибка WB sales API:")
        print(response.text[:3000])
        return []

    return response.json()


def aggregate_sales(items):
    """Агрегат по (buyout_date, sku). Логика не менялась — вынесена, чтобы её
    могли переиспользовать ремонт и восстановление истории."""
    grouped = {}

    for item in items:
        nm_id = item.get("nmId")
        if not nm_id:
            continue

        date_raw = item.get("date")
        if not date_raw:
            continue

        sale_date = date_raw[:10]

        key = (
            sale_date,
            "wb",
            str(nm_id)
        )

        price = item.get("totalPrice", 0) or 0
        finished_price = item.get("finishedPrice", 0) or 0
        price_with_disc = item.get("priceWithDisc", 0) or 0

        buyer_amount = finished_price or price_with_disc or price
        seller_amount = price_with_disc or finished_price or price

        # В WB sales могут попадать возвраты с отрицательными значениями.
        # Для первого слоя считаем положительные продажи как выкупы.
        qty = 1
        if buyer_amount < 0 or seller_amount < 0:
            qty = -1

        if key not in grouped:
            grouped[key] = {
                "buyout_date": sale_date,
                "marketplace_code": "wb",
                "marketplace_sku": str(nm_id),
                "article": str(item.get("supplierArticle") or ""),
                "product_name": item.get("subject"),
                "buyouts_qty": 0,
                "buyouts_amount_buyer": 0,
                "buyouts_amount_seller": 0,
                "revenue_after_commission_vat": 0,
                "commission_amount": 0,
                "vat_amount": 0,
            }

        grouped[key]["buyouts_qty"] += qty
        grouped[key]["buyouts_amount_buyer"] += buyer_amount
        grouped[key]["buyouts_amount_seller"] += seller_amount

    return grouped


def split_by_write_window(rows, window_start):
    """(что пишем, что придержали) по дате выкупа."""
    boundary = window_start.isoformat()
    writable = [r for r in rows if str(r["buyout_date"]) >= boundary]
    held = [r for r in rows if str(r["buyout_date"]) < boundary]
    return writable, held


def describe_held(held):
    if not held:
        return "Старше окна записи ничего не пришло."

    dates = sorted({str(r["buyout_date"]) for r in held})
    qty = sum(float(r["buyouts_qty"] or 0) for r in held)
    return (
        f"Не записано (старше окна записи, ответ по ним неполон): "
        f"{len(dates)} дат, {len(held)} строк агрегата, {qty:.0f} выкупов. "
        f"Даты: {dates[0]}…{dates[-1]}. "
        f"Их чинит scripts/wb_orders_repair.py --source sales (flag=1)."
    )


def save_wb_sales(items, days_back=DEFAULT_DAYS_BACK, today=None):
    grouped = aggregate_sales(items)
    window_start = write_window_start(days_back, today)
    rows, held = split_by_write_window(list(grouped.values()), window_start)

    print(f"Окно записи: с {window_start.isoformat()} (последние {days_back} дней)")
    print(describe_held(held))

    if not rows:
        print("Нет WB продаж/выкупов для записи")
        return {"rows_written": 0, "held_rows": len(held), "window_start": window_start.isoformat()}

    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        supabase.table("marketplace_buyouts").upsert(
            batch,
            on_conflict="buyout_date,marketplace_code,marketplace_sku"
        ).execute()

    print(f"✅ WB выкупы/продажи записаны в marketplace_buyouts: {len(rows)} строк")

    return {"rows_written": len(rows), "held_rows": len(held), "window_start": window_start.isoformat()}


if __name__ == "__main__":
    run_date = date.today()
    items = get_wb_sales(days_back=DEFAULT_DAYS_BACK, today=run_date)
    print(f"Получено строк WB sales: {len(items)}")
    save_wb_sales(items, days_back=DEFAULT_DAYS_BACK, today=run_date)
