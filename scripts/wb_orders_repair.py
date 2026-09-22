#!/usr/bin/env python3
"""Ремонт дней WB, которые загрузчик больше не пишет: вариант C.

Разделение труда с загрузчиком:

    wb_orders_loader (вариант D)  — пишет только даты внутри окна flag=0,
                                    где ответ полон. Старые даты не портит,
                                    но и не обновляет.
    этот скрипт      (вариант C)  — берёт старые даты, которых коснулись,
                                    и перезаливает их ЦЕЛИКОМ через flag=1.

Почему flag=1 — ремонт, а flag=0 — только детектор. flag=1 отбирает по дате
заказа и отдаёт весь день: проверено на 2026-08-10 (218 строк, множество srid
совпало с ночным flag=0 посимвольно) и на четырёх исторических датах в
docs/wb_data_integrity.md §1. flag=0 по старой дате отдаёт только изменившуюся
часть, и пересборка дня из неё — это и есть осыпание.

Сколько это стоит за ночь. Детектор — один запрос. Ремонт — по запросу на дату.
Замер 2026-09-03 по 8 суткам изменений (445 изменений с lastChangeDate > date):
за сутки трогается 14–28 дат, но старше 30 дней из них 0–3 (медиана 0,
среднее 0,6). То есть в установившемся режиме ночь стоит 1–4 запроса, около
1–4 минут при лимите statistics-api ~1 запрос в минуту.

ПЕРВЫЙ запуск дороже: за окном детектора накопился хвост. На ночном ответе
2026-09-03 (dateFrom = today-30) старше окна лежало 28 дат — это верхняя оценка
для разового прогона с --since-hours 720, 28 запросов ≈ полчаса. Поэтому по
умолчанию детектор смотрит на 26 часов, а не на 30 дней.

Историю за 158 дат чинит не этот скрипт, а scripts/wb_orders_history_restore.py.

Запись выключена по умолчанию. Без --approve-wb-orders-write скрипт только
читает и печатает, db_writes = 0.

Запуск:
    python3 scripts/wb_orders_repair.py                       # dry-run, отчёт
    python3 scripts/wb_orders_repair.py --dates 2026-07-21    # конкретные даты
    python3 scripts/wb_orders_repair.py --approve-wb-orders-write
"""

import argparse
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, ".")

from loaders import http_retry  # noqa: E402
from loaders.wb_orders_loader import (  # noqa: E402
    DEFAULT_DAYS_BACK,
    aggregate_orders,
    supabase,
    write_window_start,
)
from loaders.wb_sales_loader import aggregate_sales  # noqa: E402
from loaders.wb_orders_loader import WB_API_KEY  # noqa: E402

STATISTICS_API = "https://statistics-api.wildberries.ru/api/v1/supplier"

# Лимит statistics-api — примерно 1 запрос в минуту. 65 секунд, а не 60,
# чтобы не спорить с округлением на стороне WB.
DEFAULT_SLEEP_SECONDS = 65

# Детектор смотрит чуть дальше суток: ночь может сдвинуться, а пропущенное
# изменение вернётся только если дату тронут ещё раз.
DEFAULT_DETECTOR_HOURS = 26

SOURCES = {
    "orders": {
        "path": "orders",
        "table": "marketplace_orders",
        "date_field": "order_date",
        "qty_field": "orders_qty",
        "amount_field": "orders_amount_seller",
        # Правило Ozon (2026-09-21): строка пишется целиком, обе пары колонок.
        # Поздняя отмена под ним — не «−1 заказ», а «−1 подтверждённый, +1 отменённый»;
        # без второй пары диагностика показывала бы только половину.
        "cancelled_qty_field": "cancelled_orders_qty",
        "cancelled_amount_field": "cancelled_orders_amount_seller",
        "conflict": "order_date,marketplace_code,marketplace_sku,order_schema",
        "aggregate": aggregate_orders,
        "label": "WB orders",
    },
    "sales": {
        "path": "sales",
        "table": "marketplace_buyouts",
        "date_field": "buyout_date",
        "qty_field": "buyouts_qty",
        "amount_field": "buyouts_amount_seller",
        "conflict": "buyout_date,marketplace_code,marketplace_sku",
        "aggregate": aggregate_sales,
        "label": "WB sales",
    },
}


def fetch(path, params, label):
    """Один запрос к statistics-api. Возвращает список или None при отказе."""
    response = http_retry.get(
        f"{STATISTICS_API}/{path}",
        label=label,
        retry_429=http_retry.RETRY_429_ANY,
        headers={"Authorization": WB_API_KEY},
        params=params,
        timeout=180,
    )

    if response.status_code != 200:
        print(f"❌ {label} {params}: HTTP {response.status_code}")
        print(response.text[:1000])
        return None

    return response.json()


def detect_touched_dates(items, window_start, source):
    """Даты старше окна записи, которых коснулись. Это и есть список к ремонту.

    Внутри окна ремонтировать нечего: там загрузчик пишет полный день сам.
    """
    boundary = window_start.isoformat()
    touched = defaultdict(int)

    for item in items or []:
        raw = item.get("date")
        if not raw:
            continue
        day = raw[:10]
        if day < boundary:
            touched[day] += 1

    return dict(sorted(touched.items()))


def _measure_fields(source):
    """Колонки, по которым сравниваем: (qty, amount) и, если у источника есть
    отмены, ещё (cancelled_qty, cancelled_amount)."""
    fields = [source["qty_field"], source["amount_field"]]
    if source.get("cancelled_qty_field"):
        fields += [source["cancelled_qty_field"], source["cancelled_amount_field"]]
    return fields


def _measures(row, source):
    return tuple(float(row.get(field) or 0) for field in _measure_fields(source))


def stored_day(day, source):
    """Что лежит в базе по этой дате: {sku: (qty, amount[, cancelled_qty, cancelled_amount])}."""
    rows = (
        supabase
        .table(source["table"])
        .select("marketplace_sku," + ",".join(_measure_fields(source)))
        .eq("marketplace_code", "wb")
        .eq(source["date_field"], day)
        .order("marketplace_sku")
        .execute()
        .data
        or []
    )

    return {str(r["marketplace_sku"]): _measures(r, source) for r in rows}


def truth_day(day, source):
    """Полный день из flag=1. None — если запрос не удался."""
    items = fetch(source["path"], {"dateFrom": day, "flag": 1}, f"{source['label']} flag=1 {day}")
    if items is None:
        return None

    # flag=1 обязан вернуть ровно одну дату. Если вернул больше — молчать нельзя:
    # это значит, что смысл параметра изменился, и весь ремонт построен на песке.
    foreign = sorted({str(x.get("date"))[:10] for x in items} - {day})
    if foreign:
        print(f"⚠️  {day}: flag=1 вернул посторонние даты {foreign[:5]} — день не трогаем")
        return None

    return list(source["aggregate"](items).values())


def compare(day, rows, stored, source):
    """Разница между истиной и базой по дню. У заказов — обе пары колонок:
    qty/amount — подтверждённые, cancelled_* — отменённые; строка пишется целиком."""
    truth = {str(r["marketplace_sku"]): _measures(r, source) for r in rows}

    def total(cells, index):
        return sum((v[index] if index < len(v) else 0.0) for v in cells.values())

    truth_qty, stored_qty = total(truth, 0), total(stored, 0)
    truth_amount, stored_amount = total(truth, 1), total(stored, 1)

    # SKU, которые есть в базе, но которых нет в истине. upsert их не тронет,
    # они останутся лишними. Удаляем только по явному --delete-stale.
    stale = sorted(set(stored) - set(truth))

    result = {
        "date": day,
        "skus_truth": len(truth),
        "skus_stored": len(stored),
        "qty_truth": truth_qty,
        "qty_stored": stored_qty,
        "qty_delta": truth_qty - stored_qty,
        "amount_truth": truth_amount,
        "amount_stored": stored_amount,
        "amount_delta": truth_amount - stored_amount,
        "stale_skus": stale,
    }
    if source.get("cancelled_qty_field"):
        truth_c_qty, stored_c_qty = total(truth, 2), total(stored, 2)
        truth_c_amount, stored_c_amount = total(truth, 3), total(stored, 3)
        result.update({
            "cancelled_qty_truth": truth_c_qty,
            "cancelled_qty_stored": stored_c_qty,
            "cancelled_qty_delta": truth_c_qty - stored_c_qty,
            "cancelled_amount_truth": truth_c_amount,
            "cancelled_amount_stored": stored_c_amount,
            "cancelled_amount_delta": truth_c_amount - stored_c_amount,
        })
    return result


def write_day(rows, source, delete_stale, stale_skus, day):
    for i in range(0, len(rows), 500):
        supabase.table(source["table"]).upsert(
            rows[i:i + 500],
            on_conflict=source["conflict"],
        ).execute()

    if delete_stale and stale_skus:
        (
            supabase
            .table(source["table"])
            .delete()
            .eq("marketplace_code", "wb")
            .eq(source["date_field"], day)
            .in_("marketplace_sku", stale_skus)
            .execute()
        )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Repair WB days outside the loader write window via flag=1.")
    parser.add_argument("--source", choices=sorted(SOURCES), default="orders")
    parser.add_argument("--dates", nargs="*", help="Конкретные даты YYYY-MM-DD. Без них работает детектор.")
    parser.add_argument("--since-hours", type=int, default=DEFAULT_DETECTOR_HOURS,
                        help=f"Окно детектора в часах (по умолчанию {DEFAULT_DETECTOR_HOURS}).")
    parser.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK,
                        help="Окно записи загрузчика: даты новее него ремонтировать не нужно.")
    parser.add_argument("--max-dates", type=int, default=10,
                        help="Потолок дат за прогон. Защита от получасовой очереди запросов.")
    parser.add_argument("--sleep-seconds", type=int, default=DEFAULT_SLEEP_SECONDS)
    parser.add_argument("--approve-wb-orders-write", action="store_true",
                        help="Разрешить запись. Без флага прогон только читает.")
    parser.add_argument("--delete-stale", action="store_true",
                        help="Удалять SKU, которых нет в flag=1. По умолчанию только сообщать.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    source = SOURCES[args.source]
    write_allowed = bool(args.approve_wb_orders_write)

    window_start = write_window_start(args.days_back)
    print(f"Источник: {args.source} → {source['table']}")
    print(f"Окно записи загрузчика: с {window_start.isoformat()}. Чиним то, что старше.")
    print("Режим: ЗАПИСЬ" if write_allowed else "Режим: dry-run, db_writes = 0")

    if args.dates:
        targets = {d: 0 for d in sorted(args.dates)}
        print(f"Даты заданы явно: {list(targets)}")
    else:
        since = (datetime.now(timezone.utc) - timedelta(hours=args.since_hours)).strftime("%Y-%m-%dT%H:%M:%S")
        print(f"Детектор: {source['label']} flag=0 с {since} (1 запрос)")
        items = fetch(source["path"], {"dateFrom": since, "flag": 0}, f"{source['label']} детектор")
        if items is None:
            print("Детектор не отработал, ремонт не начинаем.")
            return 1
        targets = detect_touched_dates(items, window_start, source)
        print(f"Тронуто дат старше окна: {len(targets)}")
        for day, count in targets.items():
            print(f"   {day}: {count} изменившихся строк")

    if not targets:
        print("✅ Ремонтировать нечего.")
        return 0

    queue = list(targets)[:args.max_dates]
    if len(queue) < len(targets):
        print(f"⚠️  Ограничение --max-dates {args.max_dates}: в этот прогон войдут {len(queue)} из {len(targets)}.")

    print(f"План: {len(queue)} дат × 1 запрос flag=1, пауза {args.sleep_seconds} с "
          f"≈ {len(queue) * args.sleep_seconds // 60} мин")

    repaired = 0
    failed = []

    for index, day in enumerate(queue):
        if index:
            time.sleep(args.sleep_seconds)

        rows = truth_day(day, source)
        if rows is None:
            failed.append(day)
            continue

        stored = stored_day(day, source)
        diff = compare(day, rows, stored, source)

        print(
            f"{day}: истина {diff['qty_truth']:.0f} шт / {diff['amount_truth']:,.0f} ₽ | "
            f"в базе {diff['qty_stored']:.0f} / {diff['amount_stored']:,.0f} ₽ | "
            f"разница {diff['qty_delta']:+.0f} шт / {diff['amount_delta']:+,.0f} ₽"
        )
        if "cancelled_qty_truth" in diff:
            print(
                f"   отменённых: истина {diff['cancelled_qty_truth']:.0f} шт / {diff['cancelled_amount_truth']:,.0f} ₽ | "
                f"в базе {diff['cancelled_qty_stored']:.0f} / {diff['cancelled_amount_stored']:,.0f} ₽ | "
                f"разница {diff['cancelled_qty_delta']:+.0f} шт / {diff['cancelled_amount_delta']:+,.0f} ₽"
            )
        if diff["stale_skus"]:
            print(f"   лишних SKU в базе: {len(diff['stale_skus'])} "
                  f"({'удаляем' if args.delete_stale else 'оставляем, нужен --delete-stale'})")

        if write_allowed:
            write_day(rows, source, args.delete_stale, diff["stale_skus"], day)
            repaired += 1
            print(f"   ✅ записано {len(rows)} строк")

    print("")
    print(f"Итог: обработано {len(queue) - len(failed)} дат, записано {repaired}, отказов {len(failed)}")
    if failed:
        print(f"Не удались: {failed}")
    if not write_allowed:
        print("Записи не было. Для записи: --approve-wb-orders-write")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
