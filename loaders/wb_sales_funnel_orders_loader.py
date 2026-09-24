#!/usr/bin/env python3
"""Воронка продаж WB по товарам и дням: sales-funnel/products → wb_funnel_products_daily + сводка дня.

    python3 loaders/wb_sales_funnel_orders_loader.py               окно DEFAULT_DAYS_BACK дней до вчера, запись
    python3 loaders/wb_sales_funnel_orders_loader.py --dry-run     то же без записи, db_writes = 0
    python3 loaders/wb_sales_funnel_orders_loader.py --days-back 4

МЕТОД: POST seller-analytics-api /api/analytics/v3/sales-funnel/products (spec/wb/11-analytics.yaml):
selectedPeriod день…день, timezone Europe/Moscow, пустые фильтры — все карточки продавца, страницы по
limit ≤ 1000 / offset. Лимит — 3 запроса в минуту, интервал 20 с (пауза PAGE_SLEEP_SECONDS = 21 с между
любыми двумя запросами: раньше страницы шли через 3 с и 429 ловился каждую ночь — 09-22, 09-23).
Данные обновляются раз в час; выкупы, отмены и возвраты отчёт относит к ДНЮ ЗАКАЗА (спека).

ЗАЧЕМ ОКНО И РАЗРЕЗ ПО ТОВАРУ (WB-7 §3). Воронка пересматривает orderSum вниз задним числом
(09-14: 1 006 834 → 841 205 за 9 дней, −16 %), а прежний загрузчик перечитывал 4 последних дня и
хранил только сводку дня — лист «Заказы WB» владельца нужен по товарам (буква артикула = площадка:
`t` — Дискаунтер, остальное — Standard) и по окну. Теперь: окно DEFAULT_DAYS_BACK дней до вчера, каждый
день — все страницы; из тех же ответов одним проходом пишутся строки по товарам (ключ (день, nmId))
и сводка дня в marketplace_orders_analytics (как раньше: Σ orderCount, Σ orderSum, source wb_sales_funnel).

ЗАСТРЯВШИЕ КЛЮЧИ: карточка, которой в свежем ответе за день нет, а в таблице есть, — снимается по
правилу loaders/stale_keys.py (только при полном сборе дня — все страницы 200, без повторов nmId между
страницами; порог; список до удаления; удаление после записи).

ЦЕНА НОЧИ: карточек ~1 000–1 100 → 2 страницы на день; 14 дней × 2 = 28 обращений × 21 с ≈ 10 мин.
Отказ (429 / сеть не изжиты за WB_RATE_LIMIT_MAX_ATTEMPTS попыток) — RuntimeError с именем дня; шаг
нефатален по правилу FATAL_STEPS. Таблицы wb_funnel_products_daily нет — сводки дней всё равно пишутся,
а в конце шаг падает с перечнем дней, чьи строки по товарам не записаны (утренний алерт это покажет).
"""
import argparse
import os
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests
from dotenv import load_dotenv
from supabase import create_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, ".env"))   # путь от __file__, не от машины владельца (WB-7 §5)

try:
    from loaders import stale_keys
except ImportError:  # пайплайн зовёт как скрипт: python3 loaders/<файл>.py
    import stale_keys

WB_API_KEY = os.getenv("WB_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not WB_API_KEY:
    raise RuntimeError("Не найден WB_API_KEY в .env")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Не найдены SUPABASE_URL / SUPABASE_SERVICE_KEY / SUPABASE_KEY / SUPABASE_SERVICE_ROLE_KEY в .env")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"
TABLE = "wb_funnel_products_daily"
SUMMARY_TABLE = "marketplace_orders_analytics"
SOURCE = "wb_sales_funnel"
PAGE_LIMIT = 1000

# Предел повторов по 429 и сетевым отказам. Без него цикл ниже крутился бесконечно.
WB_RATE_LIMIT_MAX_ATTEMPTS = 5
WB_RATE_LIMIT_SLEEP_SECONDS = 60

# Лимит sales-funnel/products — 3 запроса в минуту, интервал 20 с (spec/wb/11-analytics.yaml).
# Раньше страницы шли через 3 с, дни — через 5 с: 4 запроса за 30 с, и 429 на
# третьем дне ловился каждую ночь (09-22, 09-23). 21 с между запросами — под лимит.
PAGE_SLEEP_SECONDS = 21

# Ночь 09-23: после 429 и паузы повторный запрос не дождался ответа
# (ReadTimeout 60 с), исключение никто не ловил, шаг упал с кодом 1 и, будучи
# фатальным, унёс весь прогон до KPI обеих площадок. Сетевой отказ — такой же
# транзиентный, как 429: те же попытки, та же пауза, по исчерпании — RuntimeError.
TRANSIENT_ERRORS = (requests.exceptions.Timeout, requests.exceptions.ConnectionError)

HEADERS = {
    "Authorization": WB_API_KEY,
    "Content-Type": "application/json",
}

# Окно 14 дней: воронка пересматривает день задним числом ещё неделю-полторы (WB-6 §2), 4 дней не хватало.
DEFAULT_DAYS_BACK = 14

# Поле ответа → колонка таблицы: (откуда, имя в ответе, колонка, тип: i — целое, n — numeric, s — строка).
PRODUCT_FIELDS = (
    ("product", "vendorCode", "vendor_code", "s"), ("product", "title", "title", "s"), ("product", "brandName", "brand", "s"),
    ("product", "subjectId", "subject_id", "i"), ("product", "subjectName", "subject_name", "s"),
    ("product", "productRating", "product_rating", "n"), ("product", "feedbackRating", "feedback_rating", "n"),
    ("stocks", "wb", "stocks_wb", "i"), ("stocks", "mp", "stocks_mp", "i"), ("stocks", "balanceSum", "balance_sum", "n"),
    ("selected", "openCount", "open_count", "i"), ("selected", "cartCount", "cart_count", "i"),
    ("selected", "orderCount", "order_count", "i"), ("selected", "orderSum", "order_sum", "n"),
    ("selected", "buyoutCount", "buyout_count", "i"), ("selected", "buyoutSum", "buyout_sum", "n"),
    ("selected", "cancelCount", "cancel_count", "i"), ("selected", "cancelSum", "cancel_sum", "n"),
    ("selected", "avgPrice", "avg_price", "n"), ("selected", "addToWishlist", "add_to_wishlist", "i"),
)


def get_days_back():
    raw_days_back = os.getenv("WB_SALES_FUNNEL_DAYS_BACK")

    if not raw_days_back:
        return DEFAULT_DAYS_BACK

    try:
        days_back = int(raw_days_back)
    except ValueError:
        raise RuntimeError("WB_SALES_FUNNEL_DAYS_BACK должен быть целым числом")

    if days_back < 1:
        raise RuntimeError("WB_SALES_FUNNEL_DAYS_BACK должен быть больше 0")

    return days_back


def _value(item, where, name):
    if where == "product":
        return (item.get("product") or {}).get(name)
    if where == "stocks":
        return ((item.get("product") or {}).get("stocks") or {}).get(name)
    return ((item.get("statistic") or {}).get("selected") or {}).get(name)


def build_product_row(day, item, observed_at):
    """Строка wb_funnel_products_daily из карточки ответа. Без nmId — None (карточка не ложится в ключ, считается вслух)."""
    nm_id = (item.get("product") or {}).get("nmId")
    if nm_id in (None, ""):
        return None
    row = {"day": day, "nm_id": int(nm_id), "observed_at": observed_at}
    for where, name, dst, kind in PRODUCT_FIELDS:
        value = _value(item, where, name)
        if value in (None, ""):
            row[dst] = None
        elif kind == "i":
            row[dst] = int(value)
        elif kind == "n":
            try:
                row[dst] = str(Decimal(str(value)))
            except InvalidOperation:
                raise RuntimeError(f"WB Sales Funnel {day}: поле {name} не число у nmId {nm_id}: {value!r}")
        else:
            row[dst] = str(value)
    return row


def request_page(day, offset, counters, sleep_fn=None, limit=PAGE_LIMIT):
    """Одна страница воронки за день. 429 / таймаут / сеть — пауза и повтор, WB_RATE_LIMIT_MAX_ATTEMPTS раз."""
    sleep_fn = sleep_fn or time.sleep   # разрешается при вызове: тесты подменяют time.sleep
    payload = {
        "selectedPeriod": {"start": day, "end": day},
        "brandNames": [], "subjectIds": [], "tagIds": [], "nmIds": [],
        "timezone": "Europe/Moscow",
        "limit": limit, "offset": offset,
    }
    rate_limit_attempts = 0
    while True:
        try:
            resp = requests.post(URL, headers=HEADERS, json=payload, timeout=60)
        except TRANSIENT_ERRORS as error:
            counters["transient"] += 1
            rate_limit_attempts += 1
            print(f"WB Sales Funnel {day} offset {offset}: сеть — {type(error).__name__}: {str(error)[:200]}")
            if rate_limit_attempts > WB_RATE_LIMIT_MAX_ATTEMPTS:
                raise RuntimeError(
                    f"WB Sales Funnel: сетевой отказ не изжит за {WB_RATE_LIMIT_MAX_ATTEMPTS} попыток "
                    f"({day}, offset {offset}): {type(error).__name__}"
                ) from error
            print(f"⏳ Жду {WB_RATE_LIMIT_SLEEP_SECONDS} секунд (попытка {rate_limit_attempts}/{WB_RATE_LIMIT_MAX_ATTEMPTS})...")
            sleep_fn(WB_RATE_LIMIT_SLEEP_SECONDS)
            continue

        counters["requests"] += 1
        print(f"WB Sales Funnel {day} offset {offset} HTTP: {resp.status_code}")

        if resp.status_code == 429:
            # WB-429 транзиентный (частота запросов), повтор осмыслен — в отличие от 429 Performance API
            # Ozon, где это исчерпанная суточная квота. Попытки ограничены, по исчерпании — RuntimeError.
            counters["429"] += 1
            rate_limit_attempts += 1
            if rate_limit_attempts > WB_RATE_LIMIT_MAX_ATTEMPTS:
                raise RuntimeError(
                    f"WB Sales Funnel rate limit не изжит за {WB_RATE_LIMIT_MAX_ATTEMPTS} попыток "
                    f"({day}, offset {offset})"
                )
            print(
                f"⏳ WB rate limit. Жду {WB_RATE_LIMIT_SLEEP_SECONDS} секунд "
                f"(попытка {rate_limit_attempts}/{WB_RATE_LIMIT_MAX_ATTEMPTS})..."
            )
            sleep_fn(WB_RATE_LIMIT_SLEEP_SECONDS)
            continue

        if resp.status_code != 200:
            print(resp.text[:3000])
            raise RuntimeError(f"WB Sales Funnel API error: {resp.status_code}")

        data = resp.json()
        return (data.get("data") or {}).get("products") or []


def fetch_wb_sales_funnel_day(day: str, counters=None, sleep_fn=None):
    """Все страницы воронки за день: сводка (Σ orderCount, Σ orderSum) и сами карточки.

    complete — все страницы получены и nmId между страницами не повторялся (иначе чистка застрявших
    за день не делается: сдвиг сортировки между страницами мог спрятать карточку).
    """
    counters = counters if counters is not None else Counter()
    sleep_fn = sleep_fn or time.sleep
    offset = 0
    products, pages = [], 0
    total_order_count = 0
    total_order_sum = 0

    while True:
        page = request_page(day, offset, counters, sleep_fn)
        pages += 1
        print(f"Получено товаров: {len(page)}")
        for product in page:
            stat = product.get("statistic", {}).get("selected", {})
            total_order_count += float(stat.get("orderCount") or 0)
            total_order_sum += float(stat.get("orderSum") or 0)
        products.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        sleep_fn(PAGE_SLEEP_SECONDS)

    ids = [(p.get("product") or {}).get("nmId") for p in products]
    ids = [i for i in ids if i not in (None, "")]
    dup_nm_ids = len(ids) - len(set(ids))
    without_nm = len(products) - len(ids)
    if dup_nm_ids:
        print(f"ВНИМАНИЕ: {day}: nmId повторяется между страницами — {dup_nm_ids}; день считается неполным, застрявшие не чищу")
    if without_nm:
        print(f"ВНИМАНИЕ: {day}: карточек без nmId — {without_nm}, в таблицу по товарам они не ложатся")

    return {
        "order_date": day,
        "marketplace_code": "wb",
        "orders_qty": total_order_count,
        "orders_amount": total_order_sum,
        "source": SOURCE,
        "products_count": len(products),
        "products": products,
        "pages": pages,
        "complete": dup_nm_ids == 0,
        "dup_nm_ids": dup_nm_ids,
    }


def save_day(row, sb=None):
    """Сводка дня в marketplace_orders_analytics — как и прежде (читают алерт и report_management)."""
    sb = sb or supabase
    payload = {
        "order_date": row["order_date"],
        "marketplace_code": row["marketplace_code"],
        "orders_qty": row["orders_qty"],
        "orders_amount": row["orders_amount"],
        "source": row["source"],
    }

    sb.table(SUMMARY_TABLE).upsert(
        payload,
        on_conflict="order_date,marketplace_code,source"
    ).execute()

    print(
        f"✅ WB Sales Funnel orders записаны: "
        f"{row['order_date']} | "
        f"{row['orders_qty']:.0f} шт | "
        f"{row['orders_amount']:.0f} руб | "
        f"товаров: {row['products_count']}"
    )


def upsert_products(sb, rows, batch=500):
    written = 0
    for i in range(0, len(rows), batch):
        sb.table(TABLE).upsert(rows[i:i + batch], on_conflict="day,nm_id").execute()
        written += len(rows[i:i + batch])
    return written


def _delete_products(sb, rows):
    return stale_keys.delete_by_date_and_column(sb, TABLE, rows, "day", "nm_id")


def cleanup_stale_products(sb, day, rows, complete, apply):
    """Карточки дня, которых в свежем ответе нет, — снять (stale_keys, со всеми защитами)."""
    existing = stale_keys.read_window_rows(sb, TABLE, "day,nm_id,vendor_code,order_count,order_sum", [("eq", "day", day)], ["nm_id"])
    built_keys = {r["nm_id"] for r in rows}
    window = {"day_from": day, "day_to": day, "complete": complete, "retries": 0, "failures": 0 if complete else 1}
    return stale_keys.cleanup(
        sb, TABLE, window, existing, built_keys, {day} if rows else set(),
        lambda r: r["nm_id"], lambda r: str(r["day"]),
        lambda r: f"{r['day']} nmId {r['nm_id']} {r.get('vendor_code')} заказов {r.get('order_count')} на {r.get('order_sum')}",
        _delete_products, apply)


def save_products(sb, day, rows, complete, apply=True):
    """Строки по товарам за день: upsert, затем чистка застрявших (только при полном сборе дня)."""
    written = upsert_products(sb, rows) if (apply and rows) else 0
    if apply:
        print(f"✅ {TABLE} {day}: upsert {written} строк")
    deleted = cleanup_stale_products(sb, day, rows, complete, apply)
    return written, deleted


def run(days_back=DEFAULT_DAYS_BACK, today=None, dry_run=False, sb=None, sleep_fn=None):
    """Окно days_back дней до вчера: за каждый день — все страницы, сводка дня и строки по товарам."""
    sb = sb or supabase
    sleep_fn = sleep_fn or time.sleep
    today = today or date.today()
    observed_at = datetime.now(timezone.utc).isoformat()
    counters = Counter()
    days = [(today - timedelta(days=i)).isoformat() for i in range(days_back, 0, -1)]
    print(f"WB Sales Funnel: окно {days[0]} … {days[-1]} ({days_back} дней до вчера), страницы по {PAGE_LIMIT}, "
          f"пауза {PAGE_SLEEP_SECONDS} с; {'dry-run, db_writes = 0' if dry_run else 'запись'}")
    totals = Counter()
    products_errors = []
    for n, day in enumerate(days):
        if n:
            sleep_fn(PAGE_SLEEP_SECONDS)
        result = fetch_wb_sales_funnel_day(day, counters, sleep_fn)
        rows = [r for r in (build_product_row(day, p, observed_at) for p in result["products"]) if r is not None]
        totals["days"] += 1
        totals["cards"] += len(rows)
        totals["pages"] += result["pages"]
        if dry_run:
            print(f"dry-run {day}: сводка {result['orders_qty']:.0f} шт / {result['orders_amount']:.0f} руб, карточек {len(rows)}, "
                  f"страниц {result['pages']}, полный {result['complete']} — не пишу")
            continue
        save_day(result, sb)
        try:
            written, deleted = save_products(sb, day, rows, result["complete"], apply=True)
            totals["written"] += written
            totals["deleted"] += deleted
        except Exception as error:  # таблицы нет / отказ PostgREST — сводка дня уже записана, скажем в конце и упадём
            products_errors.append((day, f"{type(error).__name__}: {str(error)[:160]}"))
            print(f"❌ {TABLE} {day}: строки по товарам не записаны — {type(error).__name__}: {str(error)[:200]}")
    print(f"WB Sales Funnel: дней {totals['days']}, страниц {totals['pages']}, карточек {totals['cards']}, "
          f"строк по товарам записано {totals['written']}, застрявших удалено {totals['deleted']}; "
          f"обращений {counters['requests']}, 429 — {counters['429']}, сетевых отказов {counters['transient']}")
    if products_errors:
        raise RuntimeError(f"WB Sales Funnel: строки по товарам не записаны за {len(products_errors)} дн.: "
                           + "; ".join(f"{d} — {e}" for d, e in products_errors))
    return {"days": totals["days"], "cards": totals["cards"], "written": totals["written"], "deleted": totals["deleted"],
            "requests": counters["requests"], "429": counters["429"], "transient": counters["transient"]}


def main(days_back=None, argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=days_back or get_days_back())
    ap.add_argument("--dry-run", action="store_true", help="ничего не писать, db_writes = 0")
    args = ap.parse_args(argv)
    run(days_back=args.days_back, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
