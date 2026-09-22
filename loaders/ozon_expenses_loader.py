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
    """ОТКЛЮЧЁН 2026-09-08: «obsolete method cannot be used». Оставлен как
    свидетельство прежнего поведения; вызывать нельзя."""
    raise RuntimeError(
        "/v3/finance/transaction/list отключён Ozon 2026-09-08. "
        "Источник расходов — loaders/ozon_finance_accrual.py"
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

        response = http_retry.post(url, label="Ozon expenses",
                                   headers=ozon_headers(), json=payload, timeout=120)
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
    """Расход как ПОЛОЖИТЕЛЬНАЯ величина, но со знаком движения денег.

    Ozon отдаёт списание отрицательным, возврат положительным. Модуль убивает
    это различие, и возврат прибавляется к расходу вместо вычитания: так
    27 721,60 рекламных возвратов за период считались как расход дважды —
    один раз списанием, второй возвратом.

    Поэтому знак инвертируем, а не отбрасываем: списание (amount < 0) даёт
    положительный расход, возврат (amount > 0) — отрицательный.
    """
    if expense_type == "commission":
        # sale_commission Ozon отдаёт положительной у продаж и положительной же
        # у возвратов, где она нам ВОЗВРАЩАЕТСЯ. Знак берём по типу операции.
        commission = float(op.get("sale_commission") or 0)
        if op.get("operation_type") in RETURN_COMMISSION_OPERATION_TYPES:
            return -abs(commission)
        return abs(commission)

    return -float(op.get("amount") or 0)


# Операции, где комиссия не удерживается, а возвращается продавцу.
RETURN_COMMISSION_OPERATION_TYPES = {"ClientReturnAgentOperation"}


def save_expense_rows(rows):
    """Запись готовых строк. Разбор живёт в loaders/ozon_finance_accrual.py."""
    if not rows:
        print("Нет расходов Ozon для записи")
        return
    for i in range(0, len(rows), 500):
        supabase.table("marketplace_expenses").upsert(
            rows[i:i + 500],
            on_conflict="expense_date,marketplace_code,marketplace_sku,expense_type",
        ).execute()
    print(f"✅ Расходы Ozon записаны в marketplace_expenses: {len(rows)} строк")


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


def save_type_ledger_rows(rows):
    """Леджер начислений по типам (ozon_accrual_daily_types): upsert по (дата, тип)."""
    if not rows:
        print("Леджер типов: строк нет")
        return
    for i in range(0, len(rows), 500):
        supabase.table("ozon_accrual_daily_types").upsert(
            rows[i:i + 500],
            on_conflict="accrual_date,type_id",
        ).execute()
    print(f"✅ Леджер начислений по типам записан в ozon_accrual_daily_types: {len(rows)} строк")


# Статьи, которые пишет ЭТОТ загрузчик. Реклама Performance (advertising*) и Selected CPO живут в той же таблице,
# их строит другой шаг — чистка застрявших ключей их не видит и не трогает никогда.
OWN_EXPENSE_TYPES = {"commission"} | set(accrual.TYPE_TO_EXPENSE.values())


def is_own_expense_type(expense_type):
    t = str(expense_type or "")
    return t in OWN_EXPENSE_TYPES or t.startswith("unknown_")


def cleanup_stale_expenses(window, rows, apply):
    """Ключи окна в marketplace_expenses (свои статьи), которых полный сбор не построил, — удалить (stale_keys)."""
    existing = stale_keys.read_window_rows(
        supabase, "marketplace_expenses", "id,expense_date,marketplace_sku,expense_type,expense_amount",
        [("eq", "marketplace_code", "ozon"), ("gte", "expense_date", window["day_from"]), ("lte", "expense_date", window["day_to"])],
        ["expense_date", "marketplace_code", "marketplace_sku", "expense_type"])
    existing = [r for r in existing if is_own_expense_type(r["expense_type"])]
    key = lambda r: (r["expense_date"], str(r.get("marketplace_sku") or ""), r["expense_type"])  # noqa: E731
    built_keys, built_days = {key(r) for r in rows}, {r["expense_date"] for r in rows}
    return stale_keys.cleanup(
        supabase, "marketplace_expenses", window, existing, built_keys, built_days, key, lambda r: r["expense_date"],
        lambda r: f"{r['expense_date']} sku {r.get('marketplace_sku') or '—'} {r['expense_type']} {float(r['expense_amount'] or 0):,.2f} (id {r['id']})",
        lambda sb, stale: stale_keys.delete_by_id(sb, "marketplace_expenses", stale), apply)


def cleanup_stale_ledger(window, ledger_rows, apply):
    """Ключи (дата, тип) окна в ozon_accrual_daily_types, которых полный сбор не построил, — удалить."""
    existing = stale_keys.read_window_rows(
        supabase, "ozon_accrual_daily_types", "accrual_date,type_id,type_name,amount",
        [("gte", "accrual_date", window["day_from"]), ("lte", "accrual_date", window["day_to"])], ["accrual_date", "type_id"])
    key = lambda r: (r["accrual_date"], int(r["type_id"]))  # noqa: E731
    built_keys, built_days = {key(r) for r in ledger_rows}, {r["accrual_date"] for r in ledger_rows}
    return stale_keys.cleanup(
        supabase, "ozon_accrual_daily_types", window, existing, built_keys, built_days, key, lambda r: r["accrual_date"],
        lambda r: f"{r['accrual_date']} тип {r['type_id']} {r.get('type_name') or ''} {float(r['amount'] or 0):,.2f}",
        lambda sb, stale: stale_keys.delete_by_date_and_column(sb, "ozon_accrual_daily_types", stale, "accrual_date", "type_id"), apply)


def run(days_back=30, apply=True):
    # Источник расходов — /v1/finance/accrual/by-day. Разбор построчный по
    # услугам: старое operation_type из новых данных не восстанавливается,
    # доказано перебором (ни одно подмножество type_id не воспроизводит
    # прежние суммы). Поэтому 2026-09-08 — ДАТА РАЗРЫВА РЯДА расходов:
    # до неё логика одна, после другая, и до пересчёта истории периоды
    # несопоставимы. См. docs/ozon_finance_migration.md.
    accruals, window = accrual.fetch_window_checked(days_back=days_back)
    type_names = accrual.load_accrual_types()
    rows, counters, unknown = accrual.build_expense_rows(accruals, type_names)

    print("Расходы Ozon из accrual/by-day:")
    print(f"  окно {window['day_from']} … {window['day_to']}: страниц {window['pages']}, обращений {window['requests']}, "
          f"повторов {window['retries']}, отказов {window['failures']} — сбор {'полон' if window['complete'] else 'НЕ полон'}")
    print(f"  начислений получено: {len(accruals)}")
    print(f"  строк к записи: {len(rows)}")
    print(f"  счётчики: {counters}")
    if unknown:
        print("  НЕРАЗОБРАННЫЕ ТИПЫ (в витрины не идут, ждут классификации):")
        for type_id, amount in unknown.items():
            print(f"    unknown_{type_id:<5} {type_names.get(type_id, '?'):<34} {amount:>14,.2f}")

    if apply:
        save_expense_rows(rows)
    else:
        print(f"--dry-run: {len(rows)} строк расходов не записаны")
    # Чистка — ПОСЛЕ записи: ключи окна, которых нет среди построенных строк. Защиты — loaders/stale_keys.py.
    try:
        cleanup_stale_expenses(window, rows, apply)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  чистка застрявших ключей marketplace_expenses не выполнена{', расходы записаны' if apply else ''}: {type(exc).__name__}: {exc}", flush=True)

    # Леджер по типам — из ТЕХ ЖЕ начислений, без нового обращения к API. Он вспомогательный:
    # расходы уже записаны, и отказ леджера (нет таблицы, сеть) не должен ронять шаг и всё,
    # что идёт после него. Но молчать нельзя — отказ печатается с причиной.
    try:
        ledger = accrual.build_type_ledger_rows(accruals, type_names)
        days = sorted({r["accrual_date"] for r in ledger})
        print(f"Леджер типов: строк {len(ledger)}, дат {len(days)}" + (f" ({days[0]} … {days[-1]})" if days else "")
              + f", типов {len({r['type_id'] for r in ledger})}")
        if apply:
            save_type_ledger_rows(ledger)
        else:
            print(f"--dry-run: {len(ledger)} строк леджера не записаны")
    except Exception as exc:
        print(f"⚠️  леджер начислений по типам НЕ записан, расходы записаны: {type(exc).__name__}: {exc}", flush=True)
        return
    try:
        cleanup_stale_ledger(window, ledger, apply)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  чистка застрявших ключей ozon_accrual_daily_types не выполнена{', леджер записан' if apply else ''}: {type(exc).__name__}: {exc}", flush=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Расходы Ozon из accrual/by-day + леджер типов; чистка застрявших ключей окна.")
    ap.add_argument("--dry-run", action="store_true", help="собрать окно и показать план (в т.ч. какие ключи удалились бы); в БД не писать")
    args = ap.parse_args()
    run(apply=not args.dry_run)
    if args.dry_run:
        print("db_writes = 0")
