#!/usr/bin/env python3
"""Восстановление истории заказов WB по flag=1. По дню на запрос или из файлов сырья.

ЧТО ЭТО ДЕЛАЕТ. Берёт даты старше окна записи загрузчика, берёт по каждой полный
день (flag=1 — отбор по дате заказа) и перезаписывает агрегат дня целиком, уже по
правилу Ozon: orders_* — без отмены, cancelled_orders_* — отменённые, observed_at
— момент сбора (loaders/wb_orders_rows.py, та же функция, что у ночного
загрузчика). flag=0 полного дня по старой дате не отдаёт в принципе
(docs/wb_data_integrity.md §1).

ГОРИЗОНТ. У flag=1 есть предел глубины: 2026-09-21 дата 2026-03-14 (191 день)
отдаёт пустой список, 2026-03-15 (190 дней) — 218 строк; 2026-09-03 дата
2026-03-01 (186 дней) ещё отдавала 362 строки. Горизонт едет вперёд, поэтому
сырьё снимается заранее (scripts/wb_flag1_raw_capture.py), а основной путь —
--from-files: план и запись строятся с диска, без единого обращения к WB.

ЧТО НЕ ПИШЕТСЯ. Даты раньше 2026-03-26: сбор WB начался 03-26, выкупов WB до
этой даты в базе 4 штуки, и заказы без выкупов дали бы витрине ложную
выкупаемость ноль (решение советника 2026-09-21). Сырьё по ним снято и лежит;
вернёмся вместе с историей продаж. В плане они идут отдельной строкой.

ОБЯЗАТЕЛЬНЫЙ ПОРЯДОК. Нарушать нельзя, иначе работа пропадёт:

    1. Загрузчик с окном записи и новым правилом — В ПРОДЕ (в origin/main: Render
       деплоит main). Иначе ближайшая ночь осыплет восстановленное заново и
       запишет его по старому правилу. Скрипт проверяет origin/main сам.
    2. Снимок marketplace_orders по затрагиваемым датам.
       Делает сам скрипт перед первой записью, без снимка записи не будет.
    3. Восстановление (этот скрипт).
    4. Пересчёт витрин: reports_daily_sku_kpi.py и reports_daily_marketplace_kpi.py.
       Скрипт их НЕ запускает — печатает команды в конце.

ОЖИДАНИЕ (измерено 2026-09-21 на четырёх датах: 1 208 созданных против 757 в
базе, 37,33 % по штукам и 26,84 % по рублям). Для плато 2026-03-26 … 2026-08-03:
+14 356 созданных заказов (коридор 10 454 … 19 715) и +218 млн ₽ (152 … 351).
Прежние «+360 млн ₽» были штучной долей, перенесённой на рубли. Под новым
правилом orders_* с базой напрямую не сравнить — сравниваются СОЗДАННЫЕ:
подтверждённые + отменённые против того, что лежит в базе. Из файлов прирост
известен до первой записи; вне коридора запись отказывает.

Запись выключена по умолчанию: без --approve-wb-orders-write скрипт только
читает, считает и печатает, db_writes = 0.

Запуск:
    python3 scripts/wb_orders_history_restore.py --from-files logs/wb_flag1_raw --plan
    python3 scripts/wb_orders_history_restore.py --from-files logs/wb_flag1_raw --approve-wb-orders-write
"""

import argparse
import csv
import gzip
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, ".")

import loaders.wb_orders_loader as orders_loader  # noqa: E402
from loaders import http_retry  # noqa: E402
from loaders.wb_orders_loader import (  # noqa: E402
    DEFAULT_DAYS_BACK,
    WB_API_KEY,
    supabase,
    write_window_start,
)
from loaders.wb_orders_rows import build_order_rows  # noqa: E402

STATISTICS_API = "https://statistics-api.wildberries.ru/api/v1/supplier/orders"
PROGRESS_PATH = "logs/wb_orders_history_restore_progress.json"
SNAPSHOT_DIR = "snapshots"
DEFAULT_SLEEP_SECONDS = 65
PAGE_SIZE = 1000  # PostgREST отдаёт не больше 1000 строк на ответ, просить больше бессмысленно

# Горизонт flag=1, измерен двоичным поиском 2026-09-21: 190 дней отдаёт, 191 — пусто.
KNOWN_GOOD_DEPTH_DAYS = 190

# Начало сбора WB: раньше этой даты в базе огрызки заказов и 4 выкупа. Не пишем.
WB_COLLECTION_START = "2026-03-26"

# Граница групп плана. До неё — «плато»: даты осыпались до упора, на них измерены
# доли недобора. После — даты, вышедшие из окна записи с 2026-09-03: осыпались
# не до конца, доля на них не мерилась, и в коридор они не входят.
PLATEAU_END = "2026-08-03"

# Коридор прироста СОЗДАННЫХ заказов на плато при базе 24 096 шт / 594 184 173,27 ₽
# (2026-09-21). Доли по четырём пробам: штуки 30,3 … 45,0 %, рубли 20,4 … 37,2 %.
# База продолжает осыпаться (−325 заказов за 18 дней), так что к записи прирост
# чуть вырастет; коридор шире этого дрейфа на порядок.
PLATEAU_QTY_CORRIDOR = (Decimal(10454), Decimal(19715))
PLATEAU_AMOUNT_CORRIDOR = (Decimal(152_000_000), Decimal(351_000_000))

LOADER_MARKERS = ("split_by_write_window", "build_order_rows")


def loader_is_fixed():
    """В ПРОДЕ ли загрузчик с окном записи и новым правилом.

    Render деплоит origin/main, поэтому смотрим туда, а не в свой checkout: в
    ветке wb-fixes загрузчик починен всегда, и проверка локального файла
    пропустила бы запись при непочиненном проде."""
    local = all(hasattr(orders_loader, name) for name in LOADER_MARKERS)
    if not local:
        return False
    try:
        subprocess.run(["git", "fetch", "--quiet", "origin", "main"], check=True, timeout=60,
                       env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        source = subprocess.run(["git", "show", "origin/main:loaders/wb_orders_loader.py"], check=True,
                                capture_output=True, text=True, timeout=60).stdout
    except (subprocess.SubprocessError, OSError) as error:
        print(f"Не удалось прочитать origin/main: {error}")
        return False
    return all(marker in source for marker in LOADER_MARKERS)


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


def _decimal(value):
    """Деньги и штуки — только Decimal. None не превращаем в ноль: у WB в
    marketplace_orders пустых qty и сумм нет (23 814 строк, 2026-09-21), и если
    появится — это повод упасть, а не тихо занизить базу."""
    if value is None:
        raise ValueError("Пустое количество или сумма в marketplace_orders (WB)")
    return Decimal(str(value))


def _read_pages(build_query, key_of):
    """Постраничное чтение с проверкой, что ключ не пришёл дважды. build_query
    обязан задать order() по полному ключу: range() у PostgREST без сортировки
    отдаёт страницы с повторами и пропусками (docs/how-we-work.md, 2026-09-15)."""
    seen = set()
    start = 0
    while True:
        batch = build_query().range(start, start + PAGE_SIZE - 1).execute().data or []
        for row in batch:
            key = key_of(row)
            if key in seen:
                raise RuntimeError(f"Ключ {key} пришёл дважды — постраничное чтение разошлось")
            seen.add(key)
            yield row
        if len(batch) < PAGE_SIZE:
            return
        start += PAGE_SIZE


def stored_dates(cutoff, date_from=None, date_to=None):
    """Даты WB старше окна записи: что лежит в базе, по датам и по SKU.

    qty / amount — СОЗДАННЫЕ: orders_* + cancelled_orders_*. По старому правилу
    отмены сидят внутри orders_*, по новому — рядом; сумма одна и та же, поэтому
    сравнение с файлами не ломается ни до восстановления, ни после него."""
    def build_query():
        query = (
            supabase
            .table("marketplace_orders")
            .select("order_date,marketplace_sku,order_schema,orders_qty,orders_amount_seller,"
                    "cancelled_orders_qty,cancelled_orders_amount_seller")
            .eq("marketplace_code", "wb")
            .lt("order_date", cutoff)
        )
        if date_from:
            query = query.gte("order_date", date_from)
        if date_to:
            query = query.lte("order_date", date_to)
        return query.order("order_date").order("marketplace_sku").order("order_schema")

    per_date = {}
    for row in _read_pages(build_query, lambda r: (str(r["order_date"]), str(r["marketplace_sku"]),
                                                   str(r["order_schema"]))):
        day = str(row["order_date"])
        cell = per_date.setdefault(day, {"qty": Decimal(0), "amount": Decimal(0), "rows": 0, "skus": set()})
        cell["qty"] += _decimal(row["orders_qty"]) + Decimal(str(row.get("cancelled_orders_qty") or 0))
        cell["amount"] += (_decimal(row["orders_amount_seller"])
                           + Decimal(str(row.get("cancelled_orders_amount_seller") or 0)))
        cell["rows"] += 1
        cell["skus"].add(str(row["marketplace_sku"]))

    return dict(sorted(per_date.items()))


def kpi_keys(days):
    """Ключи daily_sku_kpi по WB на этих датах: (дата, sku)."""
    if not days:
        return set()

    def build_query():
        return (
            supabase
            .table("daily_sku_kpi")
            .select("kpi_date,marketplace_sku")
            .eq("marketplace_code", "wb")
            .gte("kpi_date", min(days))
            .lte("kpi_date", max(days))
            .order("kpi_date")
            .order("marketplace_sku")
        )

    wanted = set(days)
    return {
        (str(row["kpi_date"]), str(row["marketplace_sku"]))
        for row in _read_pages(build_query, lambda r: (str(r["kpi_date"]), str(r["marketplace_sku"])))
        if str(row["kpi_date"]) in wanted
    }


def snapshot_dates(days):
    """Построчный снимок marketplace_orders по этим датам. Единственное, к чему
    можно вернуться: версионирования в базе нет."""
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SNAPSHOT_DIR, f"marketplace_orders_wb_before_restore_{stamp}.csv.gz")

    # cancelled_orders_* и observed_at появились в main 2026-09-17 (одно правило
    # записи для Ozon). У WB они пусты (2026-09-21: 0 строк с observed_at, 0 с
    # отменами), но снимок — это «к чему вернуться», он обязан быть полным.
    columns = [
        "order_date", "marketplace_code", "order_schema", "marketplace_sku",
        "article", "product_name", "orders_qty", "orders_amount_buyer", "orders_amount_seller",
        "cancelled_orders_qty", "cancelled_orders_amount_buyer", "cancelled_orders_amount_seller",
        "observed_at", "created_at",
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
                .order("marketplace_sku")
                .order("order_schema")
                .execute()
                .data
                or []
            )
            if len(rows) >= PAGE_SIZE:
                # День WB — 1–306 строк (2026-09-21); тысяча означает, что ответ обрезан, и
                # снимок с дырой хуже, чем отказ.
                raise RuntimeError(f"{day}: в снимок пришло {len(rows)} строк — страница PostgREST обрезана")
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


def _check_day(day, items):
    """Две защиты от «восстановления» мусором: чужие даты и не-список."""
    if not isinstance(items, list):
        return f"ответ не список, а {type(items).__name__}"
    foreign = sorted({str(x.get("date"))[:10] for x in items} - {day})
    if foreign:
        return f"flag=1 вернул посторонние даты: {foreign[:5]}"
    return None


def fetch_day(day, timeout=180):
    """(items, observed_at, ошибка) — один запрос flag=1 к WB."""
    observed_at = datetime.now(timezone.utc).isoformat()
    response = http_retry.get(
        STATISTICS_API,
        label=f"WB orders flag=1 {day}",
        retry_429=http_retry.RETRY_429_ANY,
        headers={"Authorization": WB_API_KEY},
        params={"dateFrom": day, "flag": 1},
        timeout=timeout,
    )

    if response.status_code != 200:
        return None, None, f"HTTP {response.status_code}: {response.text[:300]}"

    items = response.json()
    error = _check_day(day, items)
    if error:
        return None, None, error
    return items, observed_at, None


def captured_at(directory):
    """Дата → момент снятия сырья (UTC) по журналам обращений в каталоге."""
    moments = {}
    for name in ("capture_calls.json", "depth_calls.json", "calls.json"):
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            for entry in json.load(handle):
                prefix = "orders_flag1_"
                if entry.get("status") == 200 and str(entry.get("name", "")).startswith(prefix):
                    moments[entry["name"][len(prefix):]] = entry["at_utc"]
    return moments


def read_day(directory, day, moments=None):
    """(items, observed_at, ошибка) — день из файла сырья, ни одного обращения к WB.

    observed_at — когда сырьё сняли, а не когда его пишут: строка подтверждена
    ответом WB на тот момент. Нет записи в журнале — время изменения файла."""
    path = os.path.join(directory, f"orders_flag1_{day}.json")
    if not os.path.exists(path):
        return None, None, "нет файла сырья"
    with open(path, encoding="utf-8") as handle:
        items = json.load(handle, parse_float=Decimal)
    error = _check_day(day, items)
    if error:
        return None, None, error
    observed_at = (moments or {}).get(day) or datetime.fromtimestamp(
        os.path.getmtime(path), timezone.utc).isoformat()
    return items, observed_at, None


def build_day(day, items, observed_at, stored):
    """Что станет с днём: строки по новому правилу и сравнение СОЗДАННЫХ с базой."""
    stored = stored or {}
    stored_qty = _decimal(stored.get("qty", 0))
    stored_amount = _decimal(stored.get("amount", 0))

    if not items and stored_qty > 0:
        # Пустой ответ там, где в базе что-то лежит, — это не «истина ноль».
        # Так выглядит и выход за горизонт flag=1, и сбой на стороне WB.
        return None, "flag=1 вернул пусто, а в базе данные есть — день не трогаем"

    rows, _counters = build_order_rows(items, observed_at)

    def total(field):
        return sum((Decimal(str(r[field])) for r in rows), Decimal(0))

    confirmed_qty, cancelled_qty = total("orders_qty"), total("cancelled_orders_qty")
    confirmed_amount, cancelled_amount = total("orders_amount_seller"), total("cancelled_orders_amount_seller")
    truth_skus = {r["marketplace_sku"] for r in rows}
    stored_skus = set(stored.get("skus") or ())

    return {
        "date": day,
        "rows": len(rows),
        "row_data": rows,
        "qty_confirmed": confirmed_qty,
        "qty_cancelled": cancelled_qty,
        "amount_confirmed": confirmed_amount,
        "amount_cancelled": cancelled_amount,
        "qty_truth": confirmed_qty + cancelled_qty,
        "amount_truth": confirmed_amount + cancelled_amount,
        "qty_stored": stored_qty,
        "amount_stored": stored_amount,
        "qty_delta": confirmed_qty + cancelled_qty - stored_qty,
        "amount_delta": confirmed_amount + cancelled_amount - stored_amount,
        "skus_new": sorted(truth_skus - stored_skus),
        "skus_stale": sorted(stored_skus - truth_skus),
        "written": False,
    }, None


def restore_day(day, stored, write_allowed, source=None):
    """Возвращает (результат, ошибка). Запись — только если разрешена.

    source — функция день → (items, observed_at, ошибка); по умолчанию запрос к WB."""
    items, observed_at, error = (source or fetch_day)(day)
    if error:
        return None, error

    result, error = build_day(day, items, observed_at, stored)
    if error:
        return None, error

    rows = result.pop("row_data")
    if write_allowed and rows:
        for i in range(0, len(rows), 500):
            supabase.table("marketplace_orders").upsert(
                rows[i:i + 500],
                on_conflict="order_date,marketplace_code,marketplace_sku,order_schema",
            ).execute()
        result["written"] = True

    return result, None


def plan_from_files(directory, per_date, queue, whole_plateau=True):
    """План из файлов: что станет по группам, ключи витрины, коридор. Ни одного
    обращения к WB. Возвращает (итоги по группам, отказы, в коридоре ли плато).

    whole_plateau — вся ли очередь плато перед нами. Коридор измерен для плато
    целиком; на куске (--max-dates, границы дат, продолжение после обрыва) он
    ничего не значит и не проверяется."""
    moments = captured_at(directory)
    groups = {
        "plateau": {"title": f"плато {WB_COLLECTION_START} … {PLATEAU_END}"},
        "recent": {"title": f"вышедшие из окна, после {PLATEAU_END}"},
    }
    for group in groups.values():
        group.update({"dates": 0, "rows": 0, "rows_stored": 0, "new_keys": 0, "stale_keys": 0,
                      **{k: Decimal(0) for k in ("qty_confirmed", "qty_cancelled", "amount_confirmed",
                                                 "amount_cancelled", "qty_truth", "amount_truth",
                                                 "qty_stored", "amount_stored")}})
    refused = {}
    truth_keys = set()

    for day in queue:
        items, observed_at, error = read_day(directory, day, moments)
        result = None
        if not error:
            result, error = build_day(day, items, observed_at, per_date.get(day))
        if error:
            refused[day] = error
            continue

        group = groups["plateau" if day <= PLATEAU_END else "recent"]
        group["dates"] += 1
        group["rows"] += result["rows"]
        group["rows_stored"] += (per_date.get(day) or {}).get("rows", 0)
        group["new_keys"] += len(result["skus_new"])
        group["stale_keys"] += len(result["skus_stale"])
        for field in ("qty_confirmed", "qty_cancelled", "amount_confirmed", "amount_cancelled",
                      "qty_truth", "amount_truth", "qty_stored", "amount_stored"):
            group[field] += result[field]
        truth_keys.update((day, row["marketplace_sku"]) for row in result["row_data"])

    for group in groups.values():
        if not group["dates"]:
            continue
        print(f"\n{group['title']}: {group['dates']} дат")
        print(f"   строк станет {group['rows']:,} (в базе {group['rows_stored']:,}); "
              f"новых ключей (дата, SKU) {group['new_keys']:,}; в базе есть, в сырье нет — {group['stale_keys']:,}")
        print(f"   созданных       {group['qty_truth']:>10,.0f} шт / {group['amount_truth']:>16,.2f} ₽"
              f"   (в базе {group['qty_stored']:,.0f} / {group['amount_stored']:,.2f}; "
              f"прирост {group['qty_truth'] - group['qty_stored']:+,.0f} / "
              f"{group['amount_truth'] - group['amount_stored']:+,.2f})")
        print(f"   подтверждённых  {group['qty_confirmed']:>10,.0f} шт / {group['amount_confirmed']:>16,.2f} ₽   → orders_*")
        print(f"   отменённых      {group['qty_cancelled']:>10,.0f} шт / {group['amount_cancelled']:>16,.2f} ₽   → cancelled_orders_*")

    plateau = groups["plateau"]
    qty_gain = plateau["qty_truth"] - plateau["qty_stored"]
    amount_gain = plateau["amount_truth"] - plateau["amount_stored"]
    in_corridor = (PLATEAU_QTY_CORRIDOR[0] <= qty_gain <= PLATEAU_QTY_CORRIDOR[1]
                   and PLATEAU_AMOUNT_CORRIDOR[0] <= amount_gain <= PLATEAU_AMOUNT_CORRIDOR[1])
    if not whole_plateau:
        in_corridor = True
        print("\nКоридор не проверяется: в очереди не всё плато (границы дат, --max-dates или продолжение).")
    elif plateau["dates"]:
        print(f"\nКоридор плато (созданные): шт {PLATEAU_QTY_CORRIDOR[0]:,} … {PLATEAU_QTY_CORRIDOR[1]:,}, "
              f"₽ {PLATEAU_AMOUNT_CORRIDOR[0]:,} … {PLATEAU_AMOUNT_CORRIDOR[1]:,} → "
              f"{'В КОРИДОРЕ' if in_corridor else 'ВНЕ КОРИДОРА — разбираться до записи'}")

    done_days = [d for d in queue if d not in refused]
    existing = kpi_keys(done_days)
    print(f"\nВитрина daily_sku_kpi (WB) на {len(done_days)} датах: ключей {len(existing):,} — пересчёт перепишет все; "
          f"новых ключей от восстановления {len(truth_keys - existing):,}; "
          f"ключей без источника восстановление не создаёт (только upsert, удалений нет).")

    print(f"\nДат без файла, с чужими датами или пустых: {len(refused)}")
    for day, error in sorted(refused.items()):
        print(f"   {day}: {error}")

    return groups, refused, in_corridor


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Restore eroded WB order history via flag=1.")
    parser.add_argument("--plan", action="store_true", help="Только показать план и выйти. Ни одного запроса к WB.")
    parser.add_argument("--from-files", metavar="DIR",
                        help="Брать дни из файлов сырья orders_flag1_<дата>.json, а не из WB. Основной путь.")
    parser.add_argument("--date-from", help="Нижняя граница дат YYYY-MM-DD.")
    parser.add_argument("--date-to", help="Верхняя граница дат YYYY-MM-DD.")
    parser.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK,
                        help="Окно записи загрузчика: даты новее него восстанавливать не нужно.")
    parser.add_argument("--newest-first", action="store_true",
                        help="Идти от свежих дат к старым. При запросах к WB вредно: горизонт съедает старые.")
    parser.add_argument("--max-dates", type=int, help="Потолок дат за прогон. Остальное — следующим запуском.")
    parser.add_argument("--sleep-seconds", type=int, default=DEFAULT_SLEEP_SECONDS)
    parser.add_argument("--progress-path", default=PROGRESS_PATH)
    parser.add_argument("--restart", action="store_true", help="Забыть прогресс и начать сначала.")
    parser.add_argument("--retry-failed", action="store_true", help="Повторить даты, на которых был отказ.")
    parser.add_argument("--approve-wb-orders-write", action="store_true",
                        help="Разрешить запись. Без флага прогон только читает.")
    parser.add_argument("--accept-out-of-corridor", action="store_true",
                        help="Писать, даже если прирост плато вне коридора. Только после разбора причины.")
    parser.add_argument("--skip-snapshot", action="store_true",
                        help="Не делать снимок. Только если снимок уже снят вручную.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    write_allowed = bool(args.approve_wb_orders_write)

    cutoff = write_window_start(args.days_back).isoformat()
    print(f"Окно записи загрузчика: с {cutoff}. Восстанавливаем всё, что старше.")
    print("Режим: ЗАПИСЬ" if write_allowed else "Режим: dry-run, db_writes = 0")
    print(f"Источник дней: {'файлы ' + args.from_files if args.from_files else 'запросы flag=1 к WB'}")

    if write_allowed and not loader_is_fixed():
        print("❌ В origin/main нет загрузчика с окном записи и новым правилом "
              f"({', '.join(LOADER_MARKERS)}). Сначала мерж и деплой, иначе ближайшая ночь "
              "осыплет восстановленное и перепишет его по старому правилу.")
        return 2

    progress = {"done": {}, "failed": {}, "snapshot": None, "started_at": None} if args.restart \
        else load_progress(args.progress_path)

    per_date = stored_dates(cutoff, args.date_from, args.date_to)
    oldest_allowed = (date.today() - timedelta(days=KNOWN_GOOD_DEPTH_DAYS)).isoformat()
    before_start = [d for d in per_date if d < WB_COLLECTION_START]
    too_deep = [d for d in before_start if d < oldest_allowed]
    # Без файлов горизонт режет и то, что после начала сбора: с 2026-10-03 он дойдёт до 03-26.
    lost_to_horizon = [] if args.from_files else [d for d in per_date if WB_COLLECTION_START <= d < oldest_allowed]

    eligible = [d for d in per_date if d >= WB_COLLECTION_START and d not in lost_to_horizon]
    queue = [d for d in eligible if d not in progress["done"]]
    if not args.retry_failed:
        queue = [d for d in queue if d not in progress["failed"]]
    queue.sort(reverse=bool(args.newest_first))
    if args.max_dates:
        queue = queue[:args.max_dates]

    def totals(days):
        return (sum((_decimal(per_date[d]["qty"]) for d in days), Decimal(0)),
                sum((_decimal(per_date[d]["amount"]) for d in days), Decimal(0)))

    total_qty, total_amount = totals(per_date)
    print(f"Дат старше окна: {len(per_date)} ({min(per_date, default='—')} … {max(per_date, default='—')}), "
          f"в базе по ним {total_qty:,.0f} созданных заказов / {total_amount:,.0f} ₽")
    if before_start:
        qty, amount = totals(before_start)
        print(f"⚠️  Раньше начала сбора WB ({WB_COLLECTION_START}) — НЕ пишем: {len(before_start)} дат "
              f"({before_start[0]}…{before_start[-1]}), в базе {qty:,.0f} / {amount:,.0f} ₽; "
              f"из них за горизонтом flag=1 ({KNOWN_GOOD_DEPTH_DAYS} дн.) — {len(too_deep)} дат.")
    if lost_to_horizon:
        qty, amount = totals(lost_to_horizon)
        print(f"⚠️  За горизонтом flag=1 уже после начала сбора: {len(lost_to_horizon)} дат "
              f"({lost_to_horizon[0]}…{lost_to_horizon[-1]}), в базе {qty:,.0f} / {amount:,.0f} ₽. "
              f"Вернуть их можно только из файлов (--from-files).")
    qty, amount = totals(eligible)
    print(f"К восстановлению: {len(eligible)} дат, в базе {qty:,.0f} / {amount:,.0f} ₽. "
          f"Уже сделано ранее: {len(progress['done'])}, отказов: {len(progress['failed'])}")
    if args.from_files:
        print(f"В очередь этого прогона: {len(queue)} дат, из файлов — без обращений к WB и без пауз")
    else:
        print(f"В очередь этого прогона: {len(queue)} дат "
              f"≈ {len(queue) * args.sleep_seconds // 60} мин при паузе {args.sleep_seconds} с")

    in_corridor = True
    if args.from_files and queue:
        plateau_dates = {d for d in per_date if WB_COLLECTION_START <= d <= PLATEAU_END}
        whole_plateau = not (args.date_from or args.date_to) and plateau_dates <= set(queue)
        _groups, _refused, in_corridor = plan_from_files(args.from_files, per_date, queue, whole_plateau)

    if args.plan or not queue:
        if not queue:
            print("✅ Очередь пуста.")
        print("Ни одного запроса к WB не сделано.")
        return 0

    if write_allowed and not in_corridor and not args.accept_out_of_corridor:
        print("❌ Прирост плато вне коридора — запись отказана. Разобраться, затем "
              "--accept-out-of-corridor, если расхождение объяснено.")
        return 4

    if write_allowed and not args.skip_snapshot and not progress.get("snapshot"):
        progress["snapshot"] = snapshot_dates(sorted(per_date))
        progress["started_at"] = datetime.now(timezone.utc).isoformat()
        save_progress(args.progress_path, progress)
    elif write_allowed and args.skip_snapshot and not progress.get("snapshot"):
        print("⚠️  Снимок пропущен по --skip-snapshot. Откатиться будет нечем.")

    source = None
    if args.from_files:
        moments = captured_at(args.from_files)
        source = lambda day: read_day(args.from_files, day, moments)  # noqa: E731

    qty_gain = Decimal(0)
    amount_gain = Decimal(0)

    for index, day in enumerate(queue):
        if index and not args.from_files:
            time.sleep(args.sleep_seconds)

        result, error = restore_day(day, per_date.get(day, {}), write_allowed, source)

        if error:
            print(f"{day}: ❌ {error}")
            progress["failed"][day] = {"error": error, "at": datetime.now(timezone.utc).isoformat()}
            save_progress(args.progress_path, progress)
            continue

        qty_gain += result["qty_delta"]
        amount_gain += result["amount_delta"]

        share = (result["qty_delta"] / result["qty_truth"] * 100) if result["qty_truth"] else 0
        print(
            f"{day}: создано {result['qty_truth']:.0f} шт / {result['amount_truth']:,.0f} ₽ "
            f"(подтв. {result['qty_confirmed']:.0f}, отм. {result['qty_cancelled']:.0f}) | "
            f"в базе {result['qty_stored']:.0f} / {result['amount_stored']:,.0f} ₽ | "
            f"недобор {result['qty_delta']:+.0f} шт ({share:+.0f} %) / {result['amount_delta']:+,.0f} ₽"
            f"{' | записано' if result['written'] else ''}"
        )

        if result["written"]:
            progress["done"][day] = {"rows": result["rows"]}
            for field in ("qty_truth", "qty_confirmed", "qty_cancelled", "qty_delta", "amount_delta"):
                progress["done"][day][field] = str(result[field])  # Decimal в JSON — строкой, без потери копеек
            progress["done"][day]["at"] = datetime.now(timezone.utc).isoformat()
            progress["failed"].pop(day, None)
            save_progress(args.progress_path, progress)

    print("")
    print(f"Прогон: {len(queue)} дат, суммарный недобор созданных {qty_gain:+,.0f} заказов / {amount_gain:+,.0f} ₽")
    remaining = [d for d in eligible if d not in progress["done"]]
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
