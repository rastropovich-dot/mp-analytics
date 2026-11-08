import argparse
import json
import os
import time
from collections import Counter, defaultdict
from datetime import date

import requests

try:
    from loaders import http_retry
except ImportError:  # пайплайн зовёт как скрипт: python3 loaders/<файл>.py
    import http_retry
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
try:
    import cabinet
except ImportError:  # запуск как python3 loaders/<файл>.py: корня проекта нет в sys.path
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    import cabinet
CABINET = cabinet.assert_env()  # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
DEFAULT_IDENTITY_LOOKBACK_DAYS = int(os.getenv("OZON_STOCKS_IDENTITY_LOOKBACK_DAYS", "90"))

STOCKS_URL = "https://api-seller.ozon.ru/v4/product/info/stocks"
STOCKS_PAGE_LIMIT = 1000   # максимум метода
# Товары берутся из самого метода, а не из sku_catalog (RBH 2026-10-31, вариант (в)): каталог Ozon хранит product_id и
# ночью никем не пишется — у KARATOV он с 2026-04-25, и 225 SKU с заказами недели 22–28.10 (9,9 % ₽) не получали строки
# остатка. `ALL` по спеке — «все товары, кроме архивных», поэтому второй проход `ARCHIVED`: из архива пишутся только
# товары с остатком или резервом (архив РБХ-1 — 3 776 карточек, почти все пустые).
STOCKS_VISIBILITY_PASSES = ("ALL", "ARCHIVED")
STOCKS_MAX_PAGES_PER_PASS = 200   # страховка от зацикливания курсора: 200 000 товаров


def ozon_headers():
    return {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Load Ozon stocks into stock_daily")
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Print what the step would request (visibility passes, page size) without calling Ozon API or writing to DB.",
    )
    parser.add_argument(
        "--identity-lookback-days",
        type=int,
        default=DEFAULT_IDENTITY_LOOKBACK_DAYS,
        help=f"Lookback window for the fallback article -> decision SKU mapping (default: {DEFAULT_IDENTITY_LOOKBACK_DAYS}).",
    )
    return parser.parse_args()


def fetch_all(table, filters=None, order=None):
    rows = []
    offset = 0
    page_size = 1000

    while True:
        query = supabase.table(table).select("*")

        for field, operator, value in filters or []:
            if operator == "eq":
                query = query.eq(field, value)
            elif operator == "gte":
                query = query.gte(field, value)
            elif operator == "lte":
                query = query.lte(field, value)

        if order:
            query = query.order(order)

        page = query.range(offset, offset + page_size - 1).execute().data or []
        rows.extend(page)

        if len(page) < page_size:
            break

        offset += page_size

    return rows


def stock_identity_columns_supported():
    try:
        supabase.table("stock_daily").select(
            "product_id,stock_marketplace_sku,decision_marketplace_sku,stock_identity_status"
        ).limit(1).execute()
        return True
    except Exception:
        return False


def build_decision_sku_by_article_map(lookback_days):
    date_to = date.today().isoformat()
    date_from = date.fromordinal(date.today().toordinal() - lookback_days).isoformat()
    counters = defaultdict(Counter)

    orders = fetch_all(
        "marketplace_orders",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("order_date", "gte", date_from),
            ("order_date", "lte", date_to),
        ],
        order="order_date",
    )
    for row in orders:
        article = str(row.get("article") or "").strip()
        sku = str(row.get("marketplace_sku") or "").strip()
        if article and sku:
            counters[article][sku] += 1

    attr_rows = fetch_all(
        "ozon_daily_sku_ad_attribution",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("sale_date", "gte", date_from),
            ("sale_date", "lte", date_to),
        ],
        order="sale_date",
    )
    for row in attr_rows:
        article = str(row.get("article") or "").strip()
        sku = str(row.get("marketplace_sku") or "").strip()
        if article and sku:
            counters[article][sku] += 1

    return {
        article: sku_counts.most_common(1)[0][0]
        for article, sku_counts in counters.items()
        if len(sku_counts) == 1
    }


def item_has_stock(item):
    return any((s.get("present") or 0) or (s.get("reserved") or 0) for s in item.get("stocks") or [])


def get_ozon_stocks(post=None, sleep=time.sleep, passes=STOCKS_VISIBILITY_PASSES):
    """Все товары кабинета из /v4/product/info/stocks по курсору, по проходам видимости.

    Проход ALL пишется целиком (как раньше весь каталог), из ARCHIVED — только товары с остатком или резервом и не
    встреченные в ALL. Ответ не 200 — RuntimeError: незаконченный курсор не продолжить, а частичный список остатков
    читался бы как нули (шаг нефатальный — прогон идёт дальше, тревога в Telegram). Возвращает (items, summary)."""
    post = post or (lambda payload: http_retry.post(STOCKS_URL, label="Ozon stocks", headers=ozon_headers(), json=payload, timeout=60))
    items, seen, summary = [], set(), {"requests": 0}
    for visibility in passes:
        cursor, pages, returned, kept = "", 0, 0, 0
        while True:
            payload = {"filter": {"visibility": visibility}, "limit": STOCKS_PAGE_LIMIT, "cursor": cursor}
            response = post(payload)
            summary["requests"] += 1
            pages += 1
            if response.status_code != 200:
                raise RuntimeError(f"Ozon stocks API {visibility}, страница {pages}: HTTP {response.status_code} {response.text[:500]}")
            data = response.json()
            page_items = data.get("items") or []
            returned += len(page_items)
            for item in page_items:
                pid = str(item.get("product_id") or "")
                if not pid or pid in seen:
                    continue
                if visibility != "ALL" and not item_has_stock(item):
                    continue
                seen.add(pid)
                items.append(item)
                kept += 1
            cursor = data.get("cursor") or ""
            if not cursor or not page_items:
                break
            if pages >= STOCKS_MAX_PAGES_PER_PASS:
                raise RuntimeError(f"Ozon stocks API {visibility}: курсор не кончился за {pages} страниц")
            sleep(0.25)
        summary[visibility] = {"pages": pages, "items_returned": returned, "items_kept": kept}
        print(json.dumps({"event": "ozon_stock_api_pass", "visibility": visibility, "pages": pages,
                          "items_returned": returned, "items_kept": kept}, ensure_ascii=False))
    summary["items"] = len(items)
    return items, summary


def stock_api_sku(stock):
    sku = str(stock.get("sku") or "").strip()
    return "" if sku in ("", "0") else sku


def build_stock_rows(items, identity_columns_enabled, decision_sku_by_article, today):
    """Строки stock_daily: ключ (дата, ozon, product_id, тип склада) — хранение не меняется, marketplace_sku = product_id.

    decision_marketplace_sku — SKU из того же ответа (`stocks[].sku`, «идентификатор товара в системе Ozon — SKU»): на
    ответах РБХ 29.10 он есть у 2 089 / 1 888 записей из 2 089 / 1 888, по одному на товар. Догадка по артикулу (ровно
    один SKU у артикула в заказах и атрибуции) — только если поля нет; из-за неё на 28.10 без SKU было РБХ-1 1 425,
    РБХ-2 242, KARATOV 33 218 шт."""
    grouped = {}
    counters = Counter()

    for item in items:
        product_id = item.get("product_id")
        offer_id = item.get("offer_id")

        for stock in item.get("stocks") or []:
            stock_type = stock.get("type", "unknown")
            api_sku = stock_api_sku(stock)
            if api_sku:
                decision_sku, status = api_sku, "sku_from_api"
            else:
                decision_sku = (decision_sku_by_article or {}).get(str(offer_id or "").strip())
                status = "mapped_by_article" if decision_sku else "unresolved"

            present = stock.get("present", 0) or 0
            reserved = stock.get("reserved", 0) or 0

            key = (today, "ozon", str(product_id), stock_type)

            if key not in grouped:
                grouped[key] = {
                    "stock_date": today,
                    "marketplace_code": "ozon",
                    # stock_daily.marketplace_sku хранит product_id Ozon — ключ таблицы, не менять (RBH 2026-10-31, вариант (б)).
                    "marketplace_sku": str(product_id),
                    "article": str(offer_id or ""),
                    "product_name": None,
                    "warehouse_name": stock_type,
                    "stock_qty": 0,
                    "reserved_qty": 0,
                    "available_qty": 0,
                }
                if identity_columns_enabled:
                    grouped[key]["product_id"] = str(product_id)
                    grouped[key]["stock_marketplace_sku"] = str(product_id)
                    grouped[key]["decision_marketplace_sku"] = decision_sku or None
                    grouped[key]["stock_identity_status"] = status
                counters[status] += 1

            grouped[key]["stock_qty"] += present
            grouped[key]["reserved_qty"] += reserved
            grouped[key]["available_qty"] = grouped[key]["stock_qty"] - grouped[key]["reserved_qty"]

    return list(grouped.values()), counters


def needs_article_fallback(items):
    return any(not stock_api_sku(s) for item in items for s in item.get("stocks") or [])


def save_stocks(rows):
    if not rows:
        print("Нет остатков для записи")
        return

    for i in range(0, len(rows), 500):
        supabase.table("stock_daily").upsert(
            rows[i:i + 500],
            on_conflict="stock_date,marketplace_code,marketplace_sku,warehouse_name"
        ).execute()

    print(f"✅ Остатки Ozon записаны в stock_daily: {len(rows)} строк")


if __name__ == "__main__":
    args = parse_args()
    plan_summary = {
        "source": "/v4/product/info/stocks, cursor",
        "visibility_passes": list(STOCKS_VISIBILITY_PASSES),
        "page_limit": STOCKS_PAGE_LIMIT,
        "archived_written": "only items with present or reserved > 0",
    }
    identity_columns_enabled = stock_identity_columns_supported()
    plan_summary["stock_identity_columns_enabled"] = identity_columns_enabled
    print(json.dumps(plan_summary, ensure_ascii=False, indent=2))

    if args.plan_only:
        raise SystemExit(0)

    stocks, api_summary = get_ozon_stocks()
    decision_sku_by_article = {}
    if identity_columns_enabled and needs_article_fallback(stocks):
        decision_sku_by_article = build_decision_sku_by_article_map(args.identity_lookback_days)
    rows, identity_counters = build_stock_rows(stocks, identity_columns_enabled, decision_sku_by_article, date.today().isoformat())
    print(
        json.dumps(
            {
                "event": "ozon_stock_api_summary",
                **api_summary,
                "rows": len(rows),
                "identity_columns_enabled": identity_columns_enabled,
                "identity_status_rows": dict(identity_counters),
                "decision_sku_by_article_count": len(decision_sku_by_article),
            },
            ensure_ascii=False,
        )
    )
    save_stocks(rows)
