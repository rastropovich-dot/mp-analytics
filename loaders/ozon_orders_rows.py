"""Одно правило для заказов Ozon обеих схем: подтверждённые и отменённые рядом.

До 2026-09-16 правила были разные — в этом и был корень: FBO выбрасывал
`cancelled` и оставлял в таблице ключи, у которых отправлений не осталось;
FBS статус не смотрел и складывал отменённые вместе с доставленными.

Правило одно на обе схемы. Каждое отправление из сбора попадает ровно в одну
пару колонок по своему статусу:

    orders_qty / orders_amount_*                      — подтверждённые (все статусы, кроме отмены)
    cancelled_orders_qty / cancelled_orders_amount_*  — отменённые (`cancelled`)

Сумма пар — «созданные» заказы (отчёт владельца), старые колонки сохраняют
смысл «подтверждённые»: девять читателей таблицы не меняются.

Ключ строки прежний, `(order_date, marketplace_code, marketplace_sku,
order_schema)`. Дата — когорта заказа, не дата отмены: FBO — `created_at`,
FBS — `in_process_at` (`created_at` в `/v4/posting/fbs/list` нет; заполнен у
всех 5 838 отправлений снимка 09-15). Дата отмены живёт в
`ozon_posting_status_log`.

Дозревание. Отмена приходит неделями после заказа. Ночной сбор строит окно
целиком заново, и upsert переписывает обе пары у каждого ключа окна: заказ,
вчера лежавший в `orders_*`, сегодня перекладывается в `cancelled_*` той же
строкой. Ключ не исчезает — отменённое отправление остаётся в выборке, так
что «устаревших ключей» от отмен больше не возникает. `observed_at` — момент
сбора, подтвердившего строку: по нему видно строки, которых свежий полный
сбор не вернул.

`cancelled_from_split_pending` (FBS v4) — родитель разделённого отправления;
его товары продолжают жить в дочерних отправлениях, поэтому родитель не
считается ни подтверждённым, ни отменённым, и его число печатается.
Незнакомый статус считается подтверждённым (не отменой) и называется
вслух — молчаливого else нет.
"""
from collections import Counter
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
import os

APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")
MARKETPLACE = "ozon"
CENT = Decimal("0.01")

# Спека: posting.v3.PostingFboListResponse.status и posting.v4.PostingFbsListResponse.Postings.status.
CANCELLED_STATUSES = frozenset({"cancelled"})
SPLIT_PARENT_STATUSES = frozenset({"cancelled_from_split_pending"})
CONFIRMED_STATUSES = frozenset({
    "awaiting_registration", "acceptance_in_progress", "awaiting_approve",
    "awaiting_packaging", "awaiting_deliver", "awaiting_verification",
    "arbitration", "client_arbitration", "delivering", "driver_pickup",
    "delivered", "not_accepted", "sent_by_seller",
})


def utc_now():
    """Текущий момент в UTC. Отдельной функцией, чтобы тесты окна могли зафиксировать «сейчас»."""
    return datetime.now(timezone.utc)


def utc_window(date_from, date_to, now_utc=None):
    """Границы сбора в UTC для ЛОКАЛЬНЫХ дней date_from … date_to (ISO-строки или date).

    order_date — локальная дата (APP_TIMEZONE), а метод фильтрует по UTC-времени
    заказа. Окно от 00:00 UTC теряло первые три часа локального дня date_from.
    Конец — не позже «сейчас»: будущее методу не передаём.
    """
    tz = ZoneInfo(APP_TIMEZONE)
    now_utc = now_utc or utc_now()
    d1 = date.fromisoformat(date_from) if isinstance(date_from, str) else date_from
    d2 = date.fromisoformat(date_to) if isinstance(date_to, str) else date_to
    start = datetime.combine(d1, dtime.min, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(d2 + timedelta(days=1), dtime.min, tzinfo=tz).astimezone(timezone.utc)
    return start, min(end, now_utc.replace(microsecond=0))


def nightly_window(days_back, now_utc=None):
    """Окно ночного сбора: от локальной полуночи даты (сегодня по местному − days_back) до «сейчас».

    До 2026-09-19 окно было «сейчас − days_back» в UTC, то есть начиналось в ~03:20 МСК
    даты D. Строки строятся из отправлений окна, и upsert ЗАМЕЩАЕТ ключ (D, sku): если
    товар в день D продавался и до 03:20, и после, ранние заказы исчезали из строки
    навсегда — следующей ночью D уже вне окна. Измерено: ~1,1 % по дате за ночь
    (FBO 5 079 975 за 05-16 … 08-18), предсказание на 08-20 сбылось до рубля.
    Первый день окна обязан быть целым, потому что он переписывается целиком.
    """
    now_utc = now_utc or utc_now()
    first_day = now_utc.astimezone(ZoneInfo(APP_TIMEZONE)).date() - timedelta(days=days_back)
    start, _end = utc_window(first_day, first_day, now_utc)
    return start, now_utc


def to_local_order_date(value):
    """UTC-время Ozon → локальная дата (Europe/Moscow), как и раньше у обоих загрузчиков."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(APP_TIMEZONE)).date().isoformat()


def order_date_of(posting, schema):
    """Когорта заказа. FBO — created_at; FBS — in_process_at (created_at в /v4 нет)."""
    if schema == "fbo":
        raw = posting.get("created_at") or posting.get("in_process_at")
    else:
        raw = posting.get("in_process_at") or posting.get("shipment_date")
    return to_local_order_date(raw) if raw else None


def product_price(product, schema):
    """Цена товара как Decimal. Строка (FBS /v3) или объект {amount, currency} (FBO /v3, FBS /v4).

    Чужая валюта и объект без amount — сломанный контракт, а не ноль: молчаливый
    ноль занизил бы выручку по всем заказам.
    """
    raw = product.get("price")
    if isinstance(raw, dict):
        currency = str(raw.get("currency") or "")
        if currency and currency != "RUB":
            raise RuntimeError(
                f"Ozon {schema.upper()}: цена не в рублях — currency={currency}, "
                f"sku={product.get('sku')}, price={raw}"
            )
        if raw.get("amount") in (None, ""):
            raise RuntimeError(
                f"Ozon {schema.upper()}: в цене нет amount — sku={product.get('sku')}, price={raw}"
            )
        raw = raw["amount"]
    if raw in (None, ""):
        return Decimal(0)
    return Decimal(str(raw))


def _empty_row(order_date, sku, product, schema, observed_at):
    return {
        "order_date": order_date,
        "marketplace_code": MARKETPLACE,
        "marketplace_sku": sku,
        "article": str(product.get("offer_id") or ""),
        "product_name": product.get("name"),
        "order_schema": schema,
        "orders_qty": Decimal(0),
        "orders_amount_buyer": Decimal(0),
        "orders_amount_seller": Decimal(0),
        "cancelled_orders_qty": Decimal(0),
        "cancelled_orders_amount_buyer": Decimal(0),
        "cancelled_orders_amount_seller": Decimal(0),
        "observed_at": observed_at,
    }


def build_order_rows(postings, schema, observed_at=None):
    """Строки marketplace_orders из отправлений одной схемы. Возвращает (rows, counters).

    observed_at — момент сбора (ISO, UTC); пишется в каждую строку.
    """
    if schema not in ("fbo", "fbs"):
        raise ValueError(f"схема {schema!r}: ожидается fbo или fbs")
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    grouped, counters, unknown = {}, Counter(), Counter()
    for posting in postings:
        status = posting.get("status")
        if status in SPLIT_PARENT_STATUSES:
            counters["split_parent_skipped"] += 1
            continue
        cancelled = status in CANCELLED_STATUSES
        if not cancelled and status not in CONFIRMED_STATUSES:
            unknown[str(status)] += 1
        order_date = order_date_of(posting, schema)
        if not order_date:
            counters["no_date"] += 1
            continue
        counters["cancelled_postings" if cancelled else "confirmed_postings"] += 1
        prefix = "cancelled_orders" if cancelled else "orders"
        for product in posting.get("products") or []:
            sku = str(product.get("sku") or "")
            if not sku:
                counters["no_sku"] += 1
                continue
            qty = Decimal(str(product.get("quantity") or 0))
            if qty <= 0:
                counters["zero_qty"] += 1
                continue
            amount = qty * product_price(product, schema)
            key = (order_date, MARKETPLACE, sku, schema)
            row = grouped.get(key)
            if row is None:
                row = grouped[key] = _empty_row(order_date, sku, product, schema, observed_at)
            row[f"{prefix}_qty"] += qty
            row[f"{prefix}_amount_buyer"] += amount
            row[f"{prefix}_amount_seller"] += amount
    rows = []
    for row in grouped.values():
        for field in ("orders_qty", "cancelled_orders_qty"):
            row[field] = float(row[field])
        for field in ("orders_amount_buyer", "orders_amount_seller",
                      "cancelled_orders_amount_buyer", "cancelled_orders_amount_seller"):
            row[field] = float(row[field].quantize(CENT))
        rows.append(row)
    counters["rows"] = len(rows)
    counters = dict(counters)
    counters["unknown_statuses"] = dict(unknown)
    return rows, counters


def print_counters(schema, counters):
    print(f"Ozon {schema.upper()}: отправлений подтверждённых {counters.get('confirmed_postings', 0)}, "
          f"отменённых {counters.get('cancelled_postings', 0)}, "
          f"родителей разделённых пропущено {counters.get('split_parent_skipped', 0)}, "
          f"без даты {counters.get('no_date', 0)}, без sku {counters.get('no_sku', 0)}, "
          f"нулевых количеств {counters.get('zero_qty', 0)}; строк к записи {counters.get('rows', 0)}")
    unknown = counters.get("unknown_statuses") or {}
    if unknown:
        print(f"ВНИМАНИЕ: незнакомые статусы, посчитаны как подтверждённые: {unknown}")


def sums(rows):
    """Итоги по строкам: (подтв. qty, подтв. сумма, отм. qty, отм. сумма) как Decimal."""
    out = [Decimal(0)] * 4
    for r in rows:
        out[0] += Decimal(str(r["orders_qty"]))
        out[1] += Decimal(str(r["orders_amount_seller"]))
        out[2] += Decimal(str(r["cancelled_orders_qty"]))
        out[3] += Decimal(str(r["cancelled_orders_amount_seller"]))
    return tuple(out)
