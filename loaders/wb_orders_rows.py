"""Одно правило для заказов WB — то же, что у Ozon: подтверждённые и отменённые рядом.

До 2026-09-21 `orders_*` у WB означали «созданные»: каждая строка ответа
`supplier/orders`, включая отменённые. У Ozon с 2026-09-17 те же колонки —
«подтверждённые», а отмены лежат в `cancelled_orders_*`
(loaders/ozon_orders_rows.py). Одно имя значило разное, и любая строка «заказы
обеих площадок» складывала разные величины. На пробах 2026-09-21 отменённых у
WB 617 из 1 208 — 51 %.

Правило. Каждая строка ответа (1 строка = 1 заказ = 1 единица товара — так в
документации WB) попадает ровно в одну пару колонок:

    orders_qty / orders_amount_*                      — без отмены (`isCancel = false`)
    cancelled_orders_qty / cancelled_orders_amount_*  — отменённые (`isCancel = true`)

Сумма пар — «созданные», то, что лежало в `orders_*` раньше. Ничего не
выбрасывается. Ключ строки прежний: `(order_date, marketplace_code,
marketplace_sku, order_schema)`, дата — когорта заказа (`date`), не дата отмены.
`observed_at` — момент сбора, подтвердившего строку.

Этой функцией строят строки и ночной загрузчик, и ремонт, и восстановление
истории: правило одно, разойтись ему негде.

ЧТО WB НАЗЫВАЕТ `isCancel`. Документация (dev.wildberries.ru, Отчёты →
`/api/v1/supplier/orders`, прочитано 2026-09-21) говорит только: «Отмена заказа:
true — заказ отменен», `cancelDate` — «Дата и время отмены заказа. Если заказ не
был отменен, то "0001-01-01T00:00:00"». Отмену до отгрузки и отказ при получении
она не различает. По данным (29 дат, 3 538 отмен) 47 % отмен приходят на 3-й
день и позже — см. docs/wb_orders_write_window.md. Правило от ответа не зависит.

ДЕНЬГИ — Decimal. Раньше суммы копились во float, и в базе лежит
`5258633.509999999995`. Цены: продавец — `priceWithDisc`, покупатель —
`finishedPrice`; нулевое значение по-прежнему замещается следующим по цепочке
(на 9 658 строках сырья 2026-09-21 цепочка не сработала ни разу: оба поля
всегда ненулевые). Отсутствующее поле — не ноль, а сломанный контракт: падаем.
"""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

MARKETPLACE = "wb"
ORDER_SCHEMA = "marketplace"
CENT = Decimal("0.01")

PRICE_FIELDS = ("totalPrice", "priceWithDisc", "finishedPrice")
AMOUNT_FIELDS = (
    "orders_amount_buyer", "orders_amount_seller",
    "cancelled_orders_amount_buyer", "cancelled_orders_amount_seller",
)
QTY_FIELDS = ("orders_qty", "cancelled_orders_qty")


def _price(item, field):
    """Цена как Decimal. Нет ключа, None или не число — падаем: молчаливый ноль
    занизил бы выручку и остался бы незамеченным."""
    if field not in item or item[field] is None:
        raise RuntimeError(f"WB orders: в строке нет {field} — srid={item.get('srid')}, nmId={item.get('nmId')}")
    try:
        return Decimal(str(item[field]))
    except InvalidOperation:
        raise RuntimeError(
            f"WB orders: {field}={item[field]!r} не число — srid={item.get('srid')}, nmId={item.get('nmId')}"
        )


def order_amounts(item):
    """(сумма покупателя, сумма продавца) одной строки ответа."""
    total, with_disc, finished = (_price(item, field) for field in PRICE_FIELDS)
    buyer = finished or with_disc or total
    seller = with_disc or finished or total
    if not buyer or not seller:
        raise RuntimeError(
            f"WB orders: все три цены нулевые — srid={item.get('srid')}, nmId={item.get('nmId')}"
        )
    return buyer, seller


def is_cancelled(item):
    """Отменён ли заказ. Не bool — не «подтверждённый по умолчанию», а отказ."""
    value = item.get("isCancel")
    if not isinstance(value, bool):
        raise RuntimeError(f"WB orders: isCancel={value!r} — ожидался true/false, srid={item.get('srid')}")
    return value


def _empty_row(order_date, sku, item, observed_at):
    return {
        "order_date": order_date,
        "marketplace_code": MARKETPLACE,
        "order_schema": ORDER_SCHEMA,
        "marketplace_sku": sku,
        "article": str(item.get("supplierArticle") or ""),
        "product_name": item.get("subject"),
        "orders_qty": Decimal(0),
        "orders_amount_buyer": Decimal(0),
        "orders_amount_seller": Decimal(0),
        "cancelled_orders_qty": Decimal(0),
        "cancelled_orders_amount_buyer": Decimal(0),
        "cancelled_orders_amount_seller": Decimal(0),
        "observed_at": observed_at,
    }


def build_order_rows(items, observed_at=None):
    """Строки marketplace_orders из ответа supplier/orders. Возвращает (rows, counters).

    observed_at — момент сбора (ISO, UTC); пишется в каждую строку. При
    восстановлении из файлов это момент, когда сырьё сняли, а не когда его пишут.
    """
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    grouped, counters = {}, Counter()

    for item in items:
        nm_id = item.get("nmId")
        if not nm_id:
            counters["no_nm_id"] += 1
            continue
        date_raw = item.get("date")
        if not date_raw:
            counters["no_date"] += 1
            continue

        order_date = str(date_raw)[:10]
        sku = str(nm_id)
        cancelled = is_cancelled(item)
        buyer, seller = order_amounts(item)

        key = (order_date, MARKETPLACE, sku)
        row = grouped.get(key)
        if row is None:
            row = grouped[key] = _empty_row(order_date, sku, item, observed_at)

        prefix = "cancelled_orders" if cancelled else "orders"
        row[f"{prefix}_qty"] += 1
        row[f"{prefix}_amount_buyer"] += buyer
        row[f"{prefix}_amount_seller"] += seller
        counters["cancelled" if cancelled else "confirmed"] += 1

    rows = []
    for row in grouped.values():
        # Копим в Decimal, в строку кладём float один раз и уже округлённым до
        # копейки — так же, как loaders/ozon_orders_rows.py: клиент Supabase
        # сериализует JSON, а 5258633.51 в нём остаётся 5258633.51.
        for field in QTY_FIELDS:
            row[field] = float(row[field])
        for field in AMOUNT_FIELDS:
            row[field] = float(row[field].quantize(CENT))
        rows.append(row)

    counters["rows"] = len(rows)
    return rows, dict(counters)


def describe_counters(counters):
    text = (f"WB orders: заказов без отмены {counters.get('confirmed', 0)}, "
            f"отменённых {counters.get('cancelled', 0)}; строк агрегата {counters.get('rows', 0)}")
    skipped = [(name, counters.get(key, 0)) for name, key in (("без nmId", "no_nm_id"), ("без даты", "no_date"))]
    skipped = [f"{name} {count}" for name, count in skipped if count]
    if skipped:
        text += f". ВНИМАНИЕ, пропущено: {', '.join(skipped)}"
    return text


def sums(rows):
    """Итоги по строкам как Decimal: подтв. шт, подтв. ₽, отм. шт, отм. ₽ (продавец)."""
    out = [Decimal(0)] * 4
    for row in rows:
        out[0] += Decimal(str(row["orders_qty"]))
        out[1] += Decimal(str(row["orders_amount_seller"]))
        out[2] += Decimal(str(row["cancelled_orders_qty"]))
        out[3] += Decimal(str(row["cancelled_orders_amount_seller"]))
    return tuple(out)
