import os
import requests

try:
    from loaders import ozon_finance_accrual as accrual
except ImportError:  # пайплайн зовёт как скрипт
    import ozon_finance_accrual as accrual

try:
    from loaders import http_retry
except ImportError:  # пайплайн зовёт как скрипт: python3 loaders/<файл>.py
    import http_retry
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client

try:
    from loaders import stale_keys
except ImportError:  # пайплайн зовёт как скрипт: python3 loaders/<файл>.py
    import stale_keys

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
    """ОТКЛЮЧЁН Ozon 2026-09-08. Источник выкупов — accrual/by-day."""
    raise RuntimeError(
        "/v3/finance/transaction/list отключён Ozon 2026-09-08. "
        "Источник выкупов — loaders/ozon_finance_accrual.py"
    )


def _dead_transaction_list(days_back=30):
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

        response = http_retry.post(url, label="Ozon finance transactions",
                                   headers=ozon_headers(), json=payload, timeout=120)
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


COINVEST_COLUMNS = ("bonus_amount", "coinvestment_amount")   # sql/20260924_add_buyouts_bonus_coinvestment.sql; нет колонок — пишем без них и говорим


def save_buyout_rows(rows):
    """Запись готовых строк выкупов. Разбор — в loaders/ozon_finance_accrual.py.

    Колонки соинвеста (bonus_amount, coinvestment_amount) появляются миграцией по слову владельца; до неё upsert со
    строками, где они есть, падает на первой пачке — тогда строки пишутся без этих ключей, а отсутствие колонок
    называется вслух (null в таблице = не измерено, как у buyouts_units)."""
    if not rows:
        print("Нет выкупов Ozon для записи")
        return
    batches = [rows[i:i + 500] for i in range(0, len(rows), 500)]
    try:
        supabase.table("marketplace_buyouts").upsert(batches[0], on_conflict="buyout_date,marketplace_code,marketplace_sku").execute()
        rest = batches[1:]
    except Exception as exc:
        if not any(col in str(exc) for col in COINVEST_COLUMNS):
            raise
        print(f"⚠️  в marketplace_buyouts нет колонок {COINVEST_COLUMNS} (миграция не применена) — пишу выкупы без них: {str(exc)[:120]}")
        rows = [{k: v for k, v in r.items() if k not in COINVEST_COLUMNS} for r in rows]
        batches = [rows[i:i + 500] for i in range(0, len(rows), 500)]
        supabase.table("marketplace_buyouts").upsert(batches[0], on_conflict="buyout_date,marketplace_code,marketplace_sku").execute()
        rest = batches[1:]
    for batch in rest:
        supabase.table("marketplace_buyouts").upsert(batch, on_conflict="buyout_date,marketplace_code,marketplace_sku").execute()
    print(f"✅ Выкупы Ozon записаны в marketplace_buyouts: {len(rows)} строк")


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

        # Модуль здесь НЕ отбрасывает знак, а нормализует величину, знак
        # приходит отдельно из sign по типу операции (продажа +1, возврат −1).
        # Проверяем, что собственный знак Ozon с ним не спорит: если поспорит,
        # это перемена в API, и молча ошибиться на возвратах мы не хотим.
        raw_accruals = float(op.get("accruals_for_sale") or 0)
        if raw_accruals and (raw_accruals < 0) != (sign < 0):
            print(
                "WARNING: знак accruals_for_sale не совпадает с типом операции: "
                f"operation_type={operation_type}, accruals_for_sale={raw_accruals}, sign={sign}"
            )
        accruals_for_sale = abs(raw_accruals)
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


def cleanup_stale_buyouts(window, rows, apply):
    """Ключи (дата, sku) окна в marketplace_buyouts, которых полный сбор не построил, — удалить (loaders/stale_keys.py)."""
    existing = stale_keys.read_window_rows(
        supabase, "marketplace_buyouts", "id,buyout_date,marketplace_sku,buyouts_amount_seller,buyouts_units",
        [("eq", "marketplace_code", "ozon"), ("gte", "buyout_date", window["day_from"]), ("lte", "buyout_date", window["day_to"])],
        ["buyout_date", "marketplace_code", "marketplace_sku"])
    key = lambda r: (r["buyout_date"], str(r.get("marketplace_sku") or ""))  # noqa: E731
    built_keys, built_days = {key(r) for r in rows}, {r["buyout_date"] for r in rows}
    return stale_keys.cleanup(
        supabase, "marketplace_buyouts", window, existing, built_keys, built_days, key, lambda r: r["buyout_date"],
        lambda r: f"{r['buyout_date']} sku {r.get('marketplace_sku')} {float(r['buyouts_amount_seller'] or 0):,.2f} штук {r.get('buyouts_units')} (id {r['id']})",
        lambda sb, stale: stale_keys.delete_by_id(sb, "marketplace_buyouts", stale), apply)


def run(days_back=30, apply=True):
    # Продажа и возврат в новой модели — одно начисление с обратными знаками.
    # Отдельного типа операции нет, знак несёт смысл сам, поэтому прежняя
    # конструкция «abs() плюс sign по типу» здесь не нужна и была бы вредна.
    # Приёмка (2026-09-11): сумма, комиссия и выручка после комиссии совпали
    # с прежними значениями до копейки на 09-05, 09-06 и 09-07.
    accruals, window = accrual.fetch_window_checked(days_back=days_back)
    rows, counters = accrual.build_buyout_rows(accruals)
    print("Выкупы Ozon из accrual/by-day:")
    print(f"  окно {window['day_from']} … {window['day_to']}: страниц {window['pages']}, обращений {window['requests']}, "
          f"повторов {window['retries']}, отказов {window['failures']} — сбор {'полон' if window['complete'] else 'НЕ полон'}")
    print(f"  начислений получено: {len(accruals)}")
    print(f"  строк к записи: {len(rows)}")
    print(f"  счётчики: {counters}")
    if apply:
        save_buyout_rows(rows)
    else:
        print(f"--dry-run: {len(rows)} строк выкупов не записаны")
    # Чистка — ПОСЛЕ записи; отказ чистки выкупы не трогает и шаг не роняет, но называется
    try:
        cleanup_stale_buyouts(window, rows, apply)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  чистка застрявших ключей marketplace_buyouts не выполнена, выкупы записаны: {type(exc).__name__}: {exc}", flush=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Выкупы Ozon из accrual/by-day; чистка застрявших ключей окна.")
    ap.add_argument("--dry-run", action="store_true", help="собрать окно и показать план (в т.ч. какие ключи удалились бы); в БД не писать")
    args = ap.parse_args()
    run(apply=not args.dry_run)
    if args.dry_run:
        print("db_writes = 0")
