"""Лог переходов статусов отправлений Ozon: чистые функции и запись.

Схема: sql/20260916_create_ozon_posting_status_log.sql. Разбор: docs/reports_model.md §3, §5.

Источник — те же списки отправлений, что ночной сбор уже получает
(/v3/posting/fbo/list, /v3/posting/fbs/list). Ни одного дополнительного
обращения к API: шаги заказов кладут сырой ответ в data/postings_raw/, этот
модуль читает файл.

Пишутся только изменения: первое наблюдение отправления и каждая смена
статуса. observed_at — момент сбора (точность сутки), не момент события у Ozon.
"""
import json
import os
from datetime import datetime, timezone
from decimal import Decimal

try:
    from loaders.ozon_fbo_orders_loader import to_local_order_date, posting_price
except ImportError:  # вызов как скрипт из корня
    from ozon_fbo_orders_loader import to_local_order_date, posting_price

RAW_DIR = os.path.join("data", "postings_raw")
TABLE = "ozon_posting_status_log"
BATCH = 500


def posting_amount(posting):
    total = Decimal(0)
    for product in posting.get("products") or []:
        qty = Decimal(str(product.get("quantity") or 0))
        total += qty * Decimal(str(posting_price(product)))
    return total.quantize(Decimal("0.01"))


def order_date_of(posting, schema):
    """FBO — created_at; FBS — in_process_at (created_at в /v4 нет). Как у загрузчиков заказов."""
    if schema == "fbo":
        raw = posting.get("created_at") or posting.get("in_process_at")
    else:
        raw = posting.get("in_process_at") or posting.get("shipment_date")
    return to_local_order_date(raw) if raw else None, raw


def observation_rows(postings, schema, observed_at, source, last_status):
    """Строки лога из одного сбора.

    last_status: {posting_number: status} по прошлому наблюдению (из таблицы).
    Возвращает (rows, counters). Строка — только если статус изменился или
    отправление видим впервые.
    """
    rows, counters = [], {"seen": 0, "new": 0, "changed": 0, "unchanged": 0, "no_date": 0, "duplicates": 0}
    seen = set()
    for posting in postings:
        number = str(posting.get("posting_number") or "")
        if not number or number in seen:
            counters["duplicates"] += 1
            continue
        seen.add(number)
        counters["seen"] += 1
        status = str(posting.get("status") or "")
        order_date, ordered_at = order_date_of(posting, schema)
        if not order_date:
            counters["no_date"] += 1
            continue
        previous = last_status.get(number)
        if previous == status:
            counters["unchanged"] += 1
            continue
        counters["new" if previous is None else "changed"] += 1
        cancellation = posting.get("cancellation") or {}
        rows.append({
            "posting_number": number,
            "observed_at": observed_at,
            "schema": schema,
            "order_date": order_date,
            "ordered_at": ordered_at,
            "status": status,
            "substatus": posting.get("substatus"),
            "previous_status": previous,
            "cancel_reason_id": posting.get("cancel_reason_id") or cancellation.get("cancel_reason_id"),
            "cancellation_type": cancellation.get("cancellation_type") or None,
            "cancelled_after_ship": cancellation.get("cancelled_after_ship") if schema == "fbs" else None,
            "amount": str(posting_amount(posting)),
            "source": source,
        })
    return rows, counters


def load_last_status(sb, posting_numbers):
    """Последний известный статус по каждому отправлению из таблицы, батчами по 500."""
    last = {}
    numbers = sorted(set(posting_numbers))
    for i in range(0, len(numbers), BATCH):
        chunk = numbers[i:i + BATCH]
        res = (sb.table(TABLE).select("posting_number,observed_at,status")
               .in_("posting_number", chunk).order("posting_number").order("observed_at", desc=True).execute())
        for r in res.data:
            last.setdefault(r["posting_number"], r["status"])   # первый в порядке observed_at desc — последний по времени
    return last


def write_rows(sb, rows):
    written = 0
    for i in range(0, len(rows), BATCH):
        sb.table(TABLE).upsert(rows[i:i + BATCH], on_conflict="posting_number,observed_at").execute()
        written += len(rows[i:i + BATCH])
    return written


def dump_raw(postings, schema, fetched_at=None):
    """Сырой ответ сбора — в data/postings_raw/<schema>_<UTC>.json. Зовут шаги заказов."""
    fetched_at = fetched_at or datetime.now(timezone.utc)
    os.makedirs(RAW_DIR, exist_ok=True)
    path = os.path.join(RAW_DIR, f"{schema}_{fetched_at.strftime('%Y%m%dT%H%M%SZ')}.json")
    json.dump({"schema": schema, "fetched_at": fetched_at.isoformat(), "postings": postings}, open(path, "w"), ensure_ascii=False)
    return path


def latest_raw(schema):
    if not os.path.isdir(RAW_DIR):
        return None
    files = sorted(f for f in os.listdir(RAW_DIR) if f.startswith(f"{schema}_") and f.endswith(".json"))
    return os.path.join(RAW_DIR, files[-1]) if files else None
