#!/usr/bin/env python3
"""Восстановление истории заказов WB по flag=1. По дню на запрос.

ЧТО ЭТО ДЕЛАЕТ. Берёт даты старше окна записи загрузчика, запрашивает каждую
через flag=1 (отбор по дате заказа, отдаёт весь день) и перезаписывает агрегат
дня целиком. Это единственный способ вернуть осыпавшуюся историю: flag=0 полного
дня по старой дате не отдаёт в принципе (docs/wb_data_integrity.md §1).

ОБЯЗАТЕЛЬНЫЙ ПОРЯДОК. Нарушать нельзя, иначе работа пропадёт:

    1. Починка загрузчика (окно записи, вариант D) уже выкачена.
       Иначе ближайшая ночь осыплет восстановленное заново.
       Скрипт проверяет это сам и без починки писать отказывается.
    2. Снимок marketplace_orders по затрагиваемым датам.
       Делает сам скрипт перед первой записью, без снимка записи не будет.
    3. Восстановление (этот скрипт).
    4. Пересчёт витрин: reports_daily_sku_kpi.py и reports_daily_marketplace_kpi.py.
       Скрипт их НЕ запускает — печатает команды в конце.

ЦЕНА. 158 дат старше 30 дней (2026-02-04 … 2026-08-03, в базе 24 919 заказов /
617 416 438 ₽). Лимит statistics-api ≈ 1 запрос в минуту, пауза 65 с →
около 2 часов 50 минут. Ночное окно занимать нельзя: пайплайн идёт 00:22–03:00
UTC и ходит в тот же statistics-api. Прогон прерываемый и продолжаемый:
состояние лежит в файле прогресса, повторный запуск берёт с места обрыва.

ОЖИДАЕМЫЙ РЕЗУЛЬТАТ. По четырём пробам недобор 37 % (1 208 против 763), то есть
ожидается прирост порядка 14 600 заказов и 360 млн ₽. Разброс по пробам 30–43 %.
Если по итогам прогона прирост окажется вне 10–19 тыс заказов — расходится с
измерением, и это повод остановиться и разобраться, а не принимать на веру.

Запись выключена по умолчанию: без --approve-wb-orders-write скрипт только
читает, считает и печатает, db_writes = 0.

Запуск:
    python3 scripts/wb_orders_history_restore.py --plan
    python3 scripts/wb_orders_history_restore.py --max-dates 5
    python3 scripts/wb_orders_history_restore.py --approve-wb-orders-write
"""

import argparse
import csv
import gzip
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, ".")

import loaders.wb_orders_loader as orders_loader  # noqa: E402
from loaders import http_retry  # noqa: E402
from loaders.wb_orders_loader import (  # noqa: E402
    DEFAULT_DAYS_BACK,
    WB_API_KEY,
    aggregate_orders,
    supabase,
    write_window_start,
)

STATISTICS_API = "https://statistics-api.wildberries.ru/api/v1/supplier/orders"
PROGRESS_PATH = "logs/wb_orders_history_restore_progress.json"
SNAPSHOT_DIR = "snapshots"
DEFAULT_SLEEP_SECONDS = 65

# Глубина, на которой flag=1 ещё отдаёт данные. Проверено 2026-03-01 → 362 строки
# (186 дней). Заявленные вторичными источниками «90 дней хранения» практикой не
# подтверждаются. 2026-02-04 — первый день истории аккаунта.
KNOWN_GOOD_DEPTH_DAYS = 186


def loader_is_fixed():
    """Есть ли в выкаченном загрузчике окно записи. Без него восстанавливать
    бессмысленно: ближайшая ночь перезапишет всё обратно."""
    return hasattr(orders_loader, "split_by_write_window") and hasattr(orders_loader, "write_window_start")


def load_progress(path):
    if not os.path.exists(path):
        return {"done": {}, "failed": {}, "snapshot": None, "started_at": None}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save_progress(path, progress):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(progress, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def stored_dates(cutoff, date_from=None, date_to=None):
    """Даты WB старше окна записи, по которым в базе что-то лежит."""
    per_date = {}
    start = 0
    while True:
        query = (
            supabase
            .table("marketplace_orders")
            .select("order_date,orders_qty,orders_amount_seller")
            .eq("marketplace_code", "wb")
            .lt("order_date", cutoff)
        )
        if date_from:
            query = query.gte("order_date", date_from)
        if date_to:
            query = query.lte("order_date", date_to)

        batch = query.range(start, start + 999).execute().data or []
        for row in batch:
            day = str(row["order_date"])
            cell = per_date.setdefault(day, {"qty": 0.0, "amount": 0.0})
            cell["qty"] += float(row["orders_qty"] or 0)
            cell["amount"] += float(row["orders_amount_seller"] or 0)

        if len(batch) < 1000:
            break
        start += 1000

    return dict(sorted(per_date.items()))


def snapshot_dates(days):
    """Построчный снимок marketplace_orders по этим датам. Единственное, к чему
    можно вернуться: версионирования в базе нет."""
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SNAPSHOT_DIR, f"marketplace_orders_wb_before_restore_{stamp}.csv.gz")

    columns = [
        "order_date", "marketplace_code", "order_schema", "marketplace_sku",
        "article", "product_name", "orders_qty", "orders_amount_buyer", "orders_amount_seller",
    ]

    rows_written = 0
    with gzip.open(path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for day in days:
            rows = (
                supabase
                .table("marketplace_orders")
                .select(",".join(columns))
                .eq("marketplace_code", "wb")
                .eq("order_date", day)
                .execute()
                .data
                or []
            )
            for row in rows:
                writer.writerow([row.get(column) for column in columns])
                rows_written += 1

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)

    meta = {
        "taken_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "состояние marketplace_orders (WB) до восстановления истории по flag=1",
        "file": path,
        "rows": rows_written,
        "dates": len(days),
        "sha256": digest.hexdigest(),
    }
    meta_path = path.replace(".csv.gz", "_meta.json")
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2)

    print(f"📸 Снимок: {path} ({rows_written} строк, {len(days)} дат)")
    return meta


def fetch_day(day, timeout=180):
    response = http_retry.get(
        STATISTICS_API,
        label=f"WB orders flag=1 {day}",
        retry_429=http_retry.RETRY_429_ANY,
        headers={"Authorization": WB_API_KEY},
        params={"dateFrom": day, "flag": 1},
        timeout=timeout,
    )

    if response.status_code != 200:
        return None, f"HTTP {response.status_code}: {response.text[:300]}"

    items = response.json()

    foreign = sorted({str(x.get("date"))[:10] for x in items} - {day})
    if foreign:
        return None, f"flag=1 вернул посторонние даты: {foreign[:5]}"

    return items, None


def restore_day(day, stored, write_allowed):
    """Возвращает (результат, ошибка). Запись — только если разрешена."""
    items, error = fetch_day(day)
    if error:
        return None, error

    rows = list(aggregate_orders(items).values())
    truth_qty = sum(float(r["orders_qty"]) for r in rows)
    truth_amount = sum(float(r["orders_amount_seller"]) for r in rows)

    stored_qty = stored.get("qty", 0.0)
    stored_amount = stored.get("amount", 0.0)

    if not items and stored_qty > 0:
        # Пустой ответ там, где в базе что-то лежит, — это не «истина ноль».
        # Так выглядит и выход за глубину хранения, и сбой на стороне WB.
        return None, "flag=1 вернул пусто, а в базе данные есть — день не трогаем"

    result = {
        "date": day,
        "rows": len(rows),
        "qty_truth": truth_qty,
        "qty_stored": stored_qty,
        "qty_delta": truth_qty - stored_qty,
        "amount_truth": truth_amount,
        "amount_stored": stored_amount,
        "amount_delta": truth_amount - stored_amount,
        "written": False,
    }

    if write_allowed and rows:
        for i in range(0, len(rows), 500):
            supabase.table("marketplace_orders").upsert(
                rows[i:i + 500],
                on_conflict="order_date,marketplace_code,marketplace_sku,order_schema",
            ).execute()
        result["written"] = True

    return result, None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Restore eroded WB order history via flag=1, one request per day.")
    parser.add_argument("--plan", action="store_true", help="Только показать план и выйти. Ни одного запроса к WB.")
    parser.add_argument("--date-from", help="Нижняя граница дат YYYY-MM-DD.")
    parser.add_argument("--date-to", help="Верхняя граница дат YYYY-MM-DD.")
    parser.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK,
                        help="Окно записи загрузчика: даты новее него восстанавливать не нужно.")
    parser.add_argument("--newest-first", action="store_true",
                        help="Идти от свежих дат к старым. По умолчанию от старых.")
    parser.add_argument("--max-dates", type=int, help="Потолок дат за прогон. Остальное — следующим запуском.")
    parser.add_argument("--sleep-seconds", type=int, default=DEFAULT_SLEEP_SECONDS)
    parser.add_argument("--progress-path", default=PROGRESS_PATH)
    parser.add_argument("--restart", action="store_true", help="Забыть прогресс и начать сначала.")
    parser.add_argument("--retry-failed", action="store_true", help="Повторить даты, на которых был отказ.")
    parser.add_argument("--approve-wb-orders-write", action="store_true",
                        help="Разрешить запись. Без флага прогон только читает.")
    parser.add_argument("--skip-snapshot", action="store_true",
                        help="Не делать снимок. Только если снимок уже снят вручную.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    write_allowed = bool(args.approve_wb_orders_write)

    cutoff = write_window_start(args.days_back).isoformat()
    print(f"Окно записи загрузчика: с {cutoff}. Восстанавливаем всё, что старше.")
    print("Режим: ЗАПИСЬ" if write_allowed else "Режим: dry-run, db_writes = 0")

    if write_allowed and not loader_is_fixed():
        print("❌ В загрузчике нет окна записи (split_by_write_window). Сначала выкатывается")
        print("   починка loaders/wb_orders_loader.py, иначе ближайшая ночь осыплет восстановленное.")
        return 2

    progress = {"done": {}, "failed": {}, "snapshot": None, "started_at": None} if args.restart \
        else load_progress(args.progress_path)

    per_date = stored_dates(cutoff, args.date_from, args.date_to)
    oldest_allowed = (date.today() - timedelta(days=KNOWN_GOOD_DEPTH_DAYS)).isoformat()
    too_deep = [d for d in per_date if d < oldest_allowed]

    queue = [d for d in per_date if d not in progress["done"]]
    if not args.retry_failed:
        queue = [d for d in queue if d not in progress["failed"]]
    queue.sort(reverse=bool(args.newest_first))
    if args.max_dates:
        queue = queue[:args.max_dates]

    total_qty = sum(v["qty"] for v in per_date.values())
    total_amount = sum(v["amount"] for v in per_date.values())

    print(f"Дат старше окна: {len(per_date)} ({min(per_date, default='—')} … {max(per_date, default='—')})")
    print(f"В базе по ним: {total_qty:,.0f} заказов / {total_amount:,.0f} ₽")
    print(f"Уже сделано ранее: {len(progress['done'])}, отказов: {len(progress['failed'])}")
    print(f"В очередь этого прогона: {len(queue)} дат "
          f"≈ {len(queue) * args.sleep_seconds // 60} мин при паузе {args.sleep_seconds} с")
    if too_deep:
        print(f"⚠️  Глубже проверенных {KNOWN_GOOD_DEPTH_DAYS} дней: {len(too_deep)} дат "
              f"({too_deep[0]}…{too_deep[-1]}). Пустой ответ по ним — вероятно, предел хранения, а не потеря.")

    if args.plan or not queue:
        if not queue:
            print("✅ Очередь пуста.")
        print("Ни одного запроса к WB не сделано.")
        return 0

    if write_allowed and not args.skip_snapshot and not progress.get("snapshot"):
        progress["snapshot"] = snapshot_dates(sorted(per_date))
        progress["started_at"] = datetime.now(timezone.utc).isoformat()
        save_progress(args.progress_path, progress)
    elif write_allowed and args.skip_snapshot and not progress.get("snapshot"):
        print("⚠️  Снимок пропущен по --skip-snapshot. Откатиться будет нечем.")

    qty_gain = 0.0
    amount_gain = 0.0

    for index, day in enumerate(queue):
        if index:
            time.sleep(args.sleep_seconds)

        result, error = restore_day(day, per_date.get(day, {}), write_allowed)

        if error:
            print(f"{day}: ❌ {error}")
            progress["failed"][day] = {"error": error, "at": datetime.now(timezone.utc).isoformat()}
            save_progress(args.progress_path, progress)
            continue

        qty_gain += result["qty_delta"]
        amount_gain += result["amount_delta"]

        share = (result["qty_delta"] / result["qty_truth"] * 100) if result["qty_truth"] else 0
        print(
            f"{day}: истина {result['qty_truth']:.0f} шт / {result['amount_truth']:,.0f} ₽ | "
            f"в базе {result['qty_stored']:.0f} / {result['amount_stored']:,.0f} ₽ | "
            f"недобор {result['qty_delta']:+.0f} шт ({share:+.0f} %) / {result['amount_delta']:+,.0f} ₽"
            f"{' | записано' if result['written'] else ''}"
        )

        if result["written"]:
            progress["done"][day] = {k: result[k] for k in ("rows", "qty_truth", "qty_delta", "amount_delta")}
            progress["done"][day]["at"] = datetime.now(timezone.utc).isoformat()
            progress["failed"].pop(day, None)
            save_progress(args.progress_path, progress)

    print("")
    print(f"Прогон: {len(queue)} дат, суммарный недобор {qty_gain:+,.0f} заказов / {amount_gain:+,.0f} ₽")
    remaining = [d for d in per_date if d not in progress["done"]]
    print(f"Осталось дат: {len(remaining)}")

    if write_allowed:
        print("")
        print("Дальше — пересчёт витрин (скрипт их не запускает):")
        print("   python3 reports_daily_sku_kpi.py")
        print("   python3 reports_daily_marketplace_kpi.py")
        print("Снимок KPI до пересчёта берётся отдельно, как 2026-09-03.")
    else:
        print("Записи не было. Для записи: --approve-wb-orders-write")

    return 0


if __name__ == "__main__":
    sys.exit(main())
