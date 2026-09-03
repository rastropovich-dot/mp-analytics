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

# Окно запроса и окно записи — одно и то же число, и это не совпадение.
#
# WB Statistics API с flag=0 отбирает по дате ПОСЛЕДНЕГО ИЗМЕНЕНИЯ, а не по дате
# заказа. Ответ на dateFrom = today - 30 содержит:
#   * все заказы с order_date внутри окна — целиком, потому что
#     lastChangeDate >= order_date, и раз order_date попал в окно, то и
#     lastChangeDate тоже (проверено на 4780 строках ночного ответа 2026-09-03:
#     ни одной строки с lastChangeDate < date);
#   * и ТОЛЬКО ИЗМЕНИВШУЮСЯ ЧАСТЬ заказов старше окна — 3 заказа из 286,
#     у которых недавно шевельнулся статус.
#
# save_wb_orders собирает агрегат по (order_date, sku) из пришедшего и делает
# upsert, то есть ЗАМЕЩАЕТ хранимое. Для дат внутри окна это правильно: пришёл
# полный день. Для дат старше окна это затирание полного дня тремя строками —
# механизм осыпания истории (docs/wb_data_integrity.md §1).
#
# Поэтому пишем только то, что пришло полным: order_date >= today - days_back.
# Строки старше окна не выбрасываются молча — они считаются и печатаются.
#
# Проверка полноты внутри окна (2026-09-03): 2026-08-10, возраст 24 дня, у самого
# края окна. flag=1 отдал 218 строк, ночной flag=0 — те же 218, множества srid
# совпали посимвольно, в базе лежит 218. Внутри окна flag=0 полон.
#
# Цена окна в 30 дней: изменения заказов старше 30 дней перестают записываться.
# По замеру за 8 суток (probe flag=0, 445 изменений с lastChangeDate > date)
# на возраст больше 30 дней приходится 5 изменений — 1,1 %, все отмены.
# Их подбирает ремонт flag=1: scripts/wb_orders_repair.py (вариант C).
DEFAULT_DAYS_BACK = 30

WB_API_KEY = os.getenv("WB_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def write_window_start(days_back=DEFAULT_DAYS_BACK, today=None):
    """Первая дата, которую ответ flag=0 отдаёт целиком, а значит можно писать."""
    return (today or date.today()) - timedelta(days=days_back)


def get_wb_orders(days_back=DEFAULT_DAYS_BACK, today=None):
    url = "https://statistics-api.wildberries.ru/api/v1/supplier/orders"

    headers = {
        "Authorization": WB_API_KEY
    }

    # today передаётся снаружи, чтобы окно запроса и окно записи считались от
    # одной даты. Иначе прогон, перешагнувший полночь UTC, взял бы их от разных.
    params = {
        "dateFrom": write_window_start(days_back, today).isoformat(),
        "flag": 0
    }

    response = http_retry.get(url, label="WB orders", retry_429=http_retry.RETRY_429_ANY,
                              headers=headers, params=params, timeout=120)

    print("WB orders HTTP status:", response.status_code)

    if response.status_code != 200:
        print("Ошибка WB orders API:")
        print(response.text[:3000])
        return []

    return response.json()


def aggregate_orders(items):
    """Агрегат по (order_date, sku). Логика не менялась — вынесена, чтобы её
    могли переиспользовать ремонт и восстановление истории."""
    grouped = {}

    for item in items:
        nm_id = item.get("nmId")
        if not nm_id:
            continue

        date_raw = item.get("date")
        if not date_raw:
            continue

        order_date = date_raw[:10]

        key = (
            order_date,
            "wb",
            str(nm_id)
        )

        price = item.get("totalPrice", 0) or 0
        discount_percent = item.get("discountPercent", 0) or 0
        finished_price = item.get("finishedPrice", 0) or 0
        price_with_disc = item.get("priceWithDisc", 0) or 0

        # Для WB в разных отчетах могут быть разные поля цен.
        # Сохраняем максимально близко:
        # buyer amount = finishedPrice, seller amount = priceWithDisc.
        buyer_amount = finished_price or price_with_disc or price
        seller_amount = price_with_disc or finished_price or price

        if key not in grouped:
            grouped[key] = {
                "order_date": order_date,
                "marketplace_code": "wb",
                "order_schema": "marketplace",
                "marketplace_sku": str(nm_id),
                "article": str(item.get("supplierArticle") or ""),
                "product_name": item.get("subject"),
                "orders_qty": 0,
                "orders_amount_buyer": 0,
                "orders_amount_seller": 0,
            }

        grouped[key]["orders_qty"] += 1
        grouped[key]["orders_amount_buyer"] += buyer_amount
        grouped[key]["orders_amount_seller"] += seller_amount

    return grouped


def split_by_write_window(rows, window_start):
    """(что пишем, что придержали). Придержанное — даты старше окна, по которым
    ответ flag=0 заведомо неполон."""
    boundary = window_start.isoformat()
    writable = [r for r in rows if str(r["order_date"]) >= boundary]
    held = [r for r in rows if str(r["order_date"]) < boundary]
    return writable, held


def describe_held(held):
    """Строка для лога: сколько дат и заказов не записано и почему."""
    if not held:
        return "Старше окна записи ничего не пришло."

    dates = sorted({str(r["order_date"]) for r in held})
    qty = sum(float(r["orders_qty"] or 0) for r in held)
    return (
        f"Не записано (старше окна записи, ответ по ним неполон): "
        f"{len(dates)} дат, {len(held)} строк агрегата, {qty:.0f} заказов. "
        f"Даты: {dates[0]}…{dates[-1]}. "
        f"Их чинит scripts/wb_orders_repair.py (flag=1)."
    )


def save_wb_orders(items, days_back=DEFAULT_DAYS_BACK, today=None):
    grouped = aggregate_orders(items)
    window_start = write_window_start(days_back, today)
    rows, held = split_by_write_window(list(grouped.values()), window_start)

    print(f"Окно записи: с {window_start.isoformat()} (последние {days_back} дней)")
    print(describe_held(held))

    if not rows:
        print("Нет WB заказов для записи")
        return {"rows_written": 0, "held_rows": len(held), "window_start": window_start.isoformat()}

    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        supabase.table("marketplace_orders").upsert(
            batch,
            on_conflict="order_date,marketplace_code,marketplace_sku,order_schema"
        ).execute()

    print(f"✅ WB заказы записаны в marketplace_orders: {len(rows)} строк")

    return {"rows_written": len(rows), "held_rows": len(held), "window_start": window_start.isoformat()}


if __name__ == "__main__":
    run_date = date.today()
    items = get_wb_orders(days_back=DEFAULT_DAYS_BACK, today=run_date)
    print(f"Получено строк WB orders: {len(items)}")
    save_wb_orders(items, days_back=DEFAULT_DAYS_BACK, today=run_date)
