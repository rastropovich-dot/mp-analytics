import os
import requests

try:
    from loaders import http_retry
    from loaders.wb_orders_rows import build_order_rows, describe_counters
except ImportError:  # пайплайн зовёт как скрипт: python3 loaders/<файл>.py
    import http_retry
    from wb_orders_rows import build_order_rows, describe_counters
from datetime import date, datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# ЧТО ПИШЕМ. Правило одно с Ozon и живёт в loaders/wb_orders_rows.py: orders_* —
# заказы без отмены, cancelled_orders_* — с isCancel, observed_at — момент сбора.
# До 2026-09-21 orders_* у WB были «созданные» вместе с отменёнными. На ответе
# 2026-09-21 внутри окна: создано 1 005 шт / 30 960 564 ₽, из них отменено 426 /
# 15 387 708 (42,4 % шт, 49,7 % ₽) — на столько orders_* по WB и упадут после
# выкатки. Это смена базы, как у Ozon 2026-08-19, а не потеря заказов.
#
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


def aggregate_orders(items, observed_at=None):
    """Агрегат по (order_date, sku) — словарём, как его ждёт scripts/wb_orders_repair.py.

    Само правило живёт в loaders/wb_orders_rows.py и одно на загрузчик, ремонт и
    восстановление: orders_* — без отмены, cancelled_orders_* — с isCancel."""
    rows, _counters = build_order_rows(items, observed_at)
    return {(row["order_date"], row["marketplace_code"], row["marketplace_sku"]): row for row in rows}


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
    # Созданные = без отмены + отменённые: придержанное считаем целиком.
    qty = sum(float(r["orders_qty"] or 0) + float(r.get("cancelled_orders_qty") or 0) for r in held)
    return (
        f"Не записано (старше окна записи, ответ по ним неполон): "
        f"{len(dates)} дат, {len(held)} строк агрегата, {qty:.0f} заказов. "
        f"Даты: {dates[0]}…{dates[-1]}. "
        f"Их чинит scripts/wb_orders_repair.py (flag=1)."
    )


def save_wb_orders(items, days_back=DEFAULT_DAYS_BACK, today=None, observed_at=None):
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    all_rows, counters = build_order_rows(items, observed_at)
    print(describe_counters(counters))
    window_start = write_window_start(days_back, today)
    rows, held = split_by_write_window(all_rows, window_start)

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
