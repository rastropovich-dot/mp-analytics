"""Остатки WB. Старый эндпоинт отключён, разбор переписан под новый.

ЧТО СЛУЧИЛОСЬ. `GET statistics-api/api/v1/supplier/stocks` снят с эксплуатации
и отдаёт машиночитаемое уведомление:

    {"status":404,"detail":"This method is deprecated.
      Link: https://dev.wildberries.ru/release-notes?id=494","origin":"ag-statistics"}

Загрузчик печатал это в лог каждую ночь, писал ноль строк и рапортовал «Готово».
По WB `stock_daily` стоит с 2026-07-14 (docs/wb_data_integrity.md §3).

ЗАМЕНА. `POST seller-analytics-api/api/analytics/v1/stocks-report/wb-warehouses`,
работает текущим ключом, менять токен не нужно. Проверено 2026-09-03:
2821 строка, 1474 nmId, 989 nmId с ненулевым остатком, всего 5569 единиц.

ЧТО В ОТВЕТЕ ИЗМЕНИЛОСЬ — и это не косметика:

    было (supplier/stocks)          стало (stocks-report/wb-warehouses)
    ------------------------------  ----------------------------------------
    плоский список                  data.items[]
    строка на (склад, размер)       строка на (warehouseId, nmId, chrtId)
    warehouseName = реальный склад  warehouseId = -999999, «Склад WB»  ← см. ниже
    supplierArticle, subject,       ЭТИХ ПОЛЕЙ НЕТ ВООБЩЕ
      brand, category, barcode
    quantity                        quantity, inWayToClient, inWayFromClient

РАЗБИВКИ ПО СКЛАДАМ В ОТВЕТЕ НЕТ. Все 2821 строка приходят с одним и тем же
`warehouseId = -999999` и именем «Склад WB» — это агрегат по всем складам WB.
Пробовали `groupByWarehouse`, `warehouseIds`, период — эндпоинт молча
игнорирует незнакомые поля и отдаёт байт в байт тот же ответ (md5 совпал).
Поэтому разбор пишет то имя склада, которое пришло: появится настоящая
разбивка — строки разъедутся по складам сами, менять код не придётся.
До 2026-07-14 в `stock_daily` лежало 55 складов; сравнивать разрез «по складам»
через границу 07-14 нельзя.

АРТИКУЛ И НАЗВАНИЕ БЕРУТСЯ ИЗ sku_catalog. В ответе их нет, а `stock_daily.article`
читают витрины (reports_sku_decision_daily_input агрегирует остатки и по article).
Покрытие на 2026-09-03: 1360 из 1474 nmId. Оставшиеся 114 получат пустой артикул —
это видно в логе отдельной строкой.

СЛЕДСТВИЕ, КОТОРОЕ НАДО РЕШИТЬ ОТДЕЛЬНО: раньше этот же загрузчик пополнял
`sku_catalog` (артикул, баркод, бренд, категория) из ответа по остаткам. Новых
полей нет — пополнять нечем, и новые SKU в каталоге сами не появятся. Источник
для этого — Content API (`/content/v2/get/cards/list`), это отдельная задача.

Лимит нового эндпоинта — 1 запрос в 20 секунд, до 250 000 строк, offset-пагинация
(проверено: offset=5 отдаёт ровно строки 6–10 полного ответа).

Запуск:
    python3 loaders/wb_stocks_loader.py
    python3 loaders/wb_stocks_loader.py --dry-run    # ничего не пишет
"""

import argparse
import os
import time

import requests

try:
    from loaders import http_retry
except ImportError:  # пайплайн зовёт как скрипт: python3 loaders/<файл>.py
    import http_retry
from datetime import date
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

WB_API_KEY = os.getenv("WB_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

STOCKS_REPORT_URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses"

PAGE_LIMIT = 1000

# Лимит эндпоинта — 1 запрос в 20 секунд. 21, чтобы не спорить с округлением.
# http_retry сам по себе ждёт не больше 10 с (CAP_SLEEP_SECONDS), для этого
# эндпоинта этого мало, поэтому паузу повтора поднимаем до лимита.
PAGE_SLEEP_SECONDS = 21

# Имя, под которым эндпоинт отдаёт агрегат по всем складам WB (warehouseId -999999).
AGGREGATE_WAREHOUSE = "Склад WB"


def _slow_sleep(seconds):
    time.sleep(max(seconds, PAGE_SLEEP_SECONDS))


def get_wb_stocks(page_limit=PAGE_LIMIT, sleep_seconds=PAGE_SLEEP_SECONDS, max_pages=300):
    """Все строки отчёта. Пустой список — либо отказ, либо остатков нет."""
    items = []
    offset = 0

    for _ in range(max_pages):
        response = http_retry.post(
            STOCKS_REPORT_URL,
            label=f"WB stocks-report offset={offset}",
            retry_429=http_retry.RETRY_429_ANY,
            sleep_fn=_slow_sleep,
            headers={"Authorization": WB_API_KEY, "Content-Type": "application/json"},
            json={"limit": page_limit, "offset": offset},
            timeout=120,
        )

        print(f"WB stocks-report HTTP status: {response.status_code} (offset {offset})")

        if response.status_code != 200:
            print("Ошибка WB stocks-report API:")
            print(response.text[:3000])
            # Возвращаем то, что успели: неполнота видна по числу строк в логе.
            return items

        page_items = ((response.json() or {}).get("data") or {}).get("items") or []
        items.extend(page_items)
        print(f"Получено строк на странице: {len(page_items)} (всего {len(items)})")

        if len(page_items) < page_limit:
            return items

        offset += page_limit
        time.sleep(sleep_seconds)

    print(f"⚠️ Достигнут потолок страниц {max_pages}, данные могут быть неполными")
    return items


def load_sku_catalog():
    """{marketplace_sku: {article, product_name}} по WB. В ответе по остаткам
    этих полей больше нет, а витринам они нужны."""
    catalog = {}
    start = 0

    while True:
        batch = (
            supabase
            .table("sku_catalog")
            .select("marketplace_sku,article,product_name")
            .eq("marketplace_code", "wb")
            .range(start, start + 999)
            .execute()
            .data
            or []
        )

        for row in batch:
            catalog[str(row["marketplace_sku"])] = {
                "article": str(row.get("article") or ""),
                "product_name": row.get("product_name"),
            }

        if len(batch) < 1000:
            return catalog

        start += 1000


def aggregate_stocks(items, catalog=None, today=None):
    """Строки stock_daily из ответа отчёта.

    Схлопывает размеры (chrtId) внутри одного nmId и склада: ключ stock_daily —
    (stock_date, marketplace_code, marketplace_sku, warehouse_name), размера в
    нём нет. Строки с нулевым остатком сохраняются: «остаток кончился» — тоже
    факт, и старый загрузчик их писал.
    """
    catalog = catalog or {}
    stock_date = (today or date.today()).isoformat()
    grouped = {}

    for item in items:
        nm_id = item.get("nmId")
        if not nm_id:
            continue

        warehouse_name = item.get("warehouseName") or AGGREGATE_WAREHOUSE
        sku = str(nm_id)
        key = (stock_date, "wb", sku, warehouse_name)

        if key not in grouped:
            known = catalog.get(sku, {})
            grouped[key] = {
                "stock_date": stock_date,
                "marketplace_code": "wb",
                "marketplace_sku": sku,
                "article": known.get("article", ""),
                "product_name": known.get("product_name"),
                "warehouse_name": warehouse_name,
                "stock_qty": 0,
                "reserved_qty": 0,
                "available_qty": 0,
            }

        quantity = item.get("quantity", 0) or 0
        grouped[key]["stock_qty"] += quantity
        # Как и раньше: остаток считается доступным целиком. inWayToClient и
        # inWayFromClient положить в stock_daily некуда — колонок нет. Заводить
        # их — отдельная миграция; выдумывать им место здесь нельзя.
        grouped[key]["available_qty"] += quantity

    return grouped


def describe_rows(rows):
    """Что именно получилось — чтобы неполнота была видна в логе, а не в витрине."""
    if not rows:
        return "Строк нет."

    skus = {r["marketplace_sku"] for r in rows}
    warehouses = sorted({r["warehouse_name"] for r in rows})
    without_article = sorted({r["marketplace_sku"] for r in rows if not r["article"]})
    with_stock = {r["marketplace_sku"] for r in rows if r["stock_qty"] > 0}

    lines = [
        f"Строк: {len(rows)}, SKU: {len(skus)}, единиц на остатке: {sum(r['stock_qty'] for r in rows)}",
        f"SKU с ненулевым остатком: {len(with_stock)}",
        f"Складов в ответе: {len(warehouses)} ({', '.join(warehouses[:5])})",
    ]

    if warehouses == [AGGREGATE_WAREHOUSE]:
        lines.append("⚠️ Разбивки по складам нет: эндпоинт отдаёт агрегат «Склад WB».")

    if without_article:
        lines.append(
            f"⚠️ Без артикула (нет в sku_catalog): {len(without_article)} SKU из {len(skus)}, "
            f"например {without_article[:3]}"
        )

    return "\n".join(lines)


def save_wb_stocks(rows):
    if not rows:
        print("Нет WB остатков для записи")
        return 0

    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        supabase.table("stock_daily").upsert(
            batch,
            on_conflict="stock_date,marketplace_code,marketplace_sku,warehouse_name"
        ).execute()

    print(f"✅ WB остатки записаны в stock_daily: {len(rows)} строк")
    return len(rows)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Load WB stocks from the stocks-report endpoint.")
    parser.add_argument("--dry-run", action="store_true", help="Ничего не писать: только разобрать и показать.")
    parser.add_argument("--limit", type=int, default=PAGE_LIMIT, help="Размер страницы.")
    parser.add_argument("--max-pages", type=int, default=300)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    items = get_wb_stocks(page_limit=args.limit, max_pages=args.max_pages)
    print(f"Получено строк WB stocks-report: {len(items)}")

    if not items:
        print("❌ Пусто. Остатки за сегодня не обновлены.")
        return 1

    catalog = load_sku_catalog()
    print(f"sku_catalog по WB: {len(catalog)} записей")

    rows = list(aggregate_stocks(items, catalog).values())
    print(describe_rows(rows))

    if args.dry_run:
        print("Режим dry-run: db_writes = 0")
        for row in rows[:5]:
            print("   ", row)
        return 0

    save_wb_stocks(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
