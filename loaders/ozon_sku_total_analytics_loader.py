import argparse
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from supabase import create_client


load_dotenv()

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")

SELLER_ANALYTICS_URL = "https://api-seller.ozon.ru/v1/analytics/data"
ANALYTICS_METRICS = ["ordered_units", "revenue"]
ANALYTICS_DIMENSIONS = ["sku", "day"]
DEFAULT_PAGE_SIZE = 1000

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def ozon_headers():
    return {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Load Ozon Seller Analytics total orders by SKU/day.")
    parser.add_argument(
        "--mode",
        choices=("daily-yesterday", "full"),
        default="full",
        help="daily-yesterday = production D-1 load; full = explicit date/date-range",
    )
    parser.add_argument("--date", help="single-day shortcut, sets both --date-from and --date-to")
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--days-back", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug-sample", action="store_true")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--max-pages", type=int, default=0, help="0 means no explicit limit")
    return parser.parse_args()


def resolve_date_range(args):
    if args.date:
        if args.date_from or args.date_to:
            raise RuntimeError("--date нельзя комбинировать с --date-from/--date-to")
        return args.date, args.date

    if args.mode == "daily-yesterday":
        if args.date_from or args.date_to:
            raise RuntimeError("daily-yesterday mode нельзя комбинировать с --date-from/--date-to")
        target_date = datetime.now(ZoneInfo(APP_TIMEZONE)).date() - timedelta(days=1)
        return target_date.isoformat(), target_date.isoformat()

    date_to = args.date_to or date.today().isoformat()
    if args.date_from:
        return args.date_from, date_to

    date_from = (datetime.fromisoformat(date_to).date() - timedelta(days=args.days_back)).isoformat()
    return date_from, date_to


def parse_number(value):
    if value in (None, ""):
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace("\xa0", " ").replace(" ", "")
    text = text.replace("₽", "").replace("%", "").replace(",", ".")

    if not text:
        return 0.0

    try:
        return float(text)
    except ValueError:
        return 0.0


def normalize_sku(value):
    if value in (None, ""):
        return ""

    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]

    return text


def normalize_sale_date(value):
    if value in (None, ""):
        return None

    text = str(value).strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    return text[:10]


def dimension_value(entry):
    if isinstance(entry, dict):
        for key in ("value", "id", "name", "title"):
            if entry.get(key) not in (None, ""):
                return entry.get(key)
        return None
    return entry


def metric_value(entry):
    if isinstance(entry, dict):
        for key in ("value", "metric", "amount", "sum"):
            if entry.get(key) not in (None, ""):
                return entry.get(key)
        return None
    return entry


def row_to_dimension_map(row):
    direct = {}
    for key in ANALYTICS_DIMENSIONS:
        if row.get(key) not in (None, ""):
            direct[key] = row.get(key)

    if direct:
        return direct

    dimensions = row.get("dimensions") or row.get("dimension_values") or row.get("dimensionValues") or []
    if isinstance(dimensions, dict):
        return {key: dimensions.get(key) for key in ANALYTICS_DIMENSIONS}

    mapped = {}
    if isinstance(dimensions, list):
        for index, key in enumerate(ANALYTICS_DIMENSIONS):
            if index < len(dimensions):
                mapped[key] = dimension_value(dimensions[index])
    return mapped


def row_to_metric_map(row):
    direct = {}
    for key in ANALYTICS_METRICS:
        if row.get(key) not in (None, ""):
            direct[key] = row.get(key)

    if direct:
        return direct

    metrics = row.get("metrics") or row.get("metric_values") or row.get("metricValues") or []
    if isinstance(metrics, dict):
        return {key: metrics.get(key) for key in ANALYTICS_METRICS}

    mapped = {}
    if isinstance(metrics, list):
        for index, key in enumerate(ANALYTICS_METRICS):
            if index < len(metrics):
                mapped[key] = metric_value(metrics[index])
    return mapped


def build_payload(date_from, date_to, page_size, offset):
    return {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": ANALYTICS_METRICS,
        "dimension": ANALYTICS_DIMENSIONS,
        "sort": [{"key": "ordered_units", "order": "DESC"}],
        "limit": page_size,
        "offset": offset,
    }


def load_catalog():
    rows = []
    start = 0
    page_size = 1000

    while True:
        result = (
            supabase
            .table("sku_catalog")
            .select("marketplace_sku,article,product_name")
            .eq("marketplace_code", "ozon")
            .range(start, start + page_size - 1)
            .execute()
        )

        batch = result.data or []
        rows.extend(batch)

        if len(batch) < page_size:
            break

        start += page_size

    return {str(row.get("marketplace_sku")): row for row in rows}


# ДВА РАЗНЫХ 429. Не смешивать — правила прямо противоположные.
#
# 1. Performance API (api-performance.ozon.ru, /api/client/statistics/json).
#    429 означает исчерпание СУТОЧНОЙ квоты выгрузок. Повтор бессмыслен: до
#    сброса окна ответ будет тем же. Действует правило CLAUDE.md — «стоп на
#    первом 429, не retry storm». Реализовано в
#    loaders/ozon_performance_ads_loader.py: REQUEST_PROFILES["statistics_json"]
#    имеет fail_fast_on_429 = True, а RateLimitPending останавливает прогон.
#
# 2. Seller API (api-seller.ozon.ru, /v1/analytics/data) — здесь.
#    429 с "code": 8 означает превышение частоты запросов В СЕКУНДУ. Ошибка
#    транзиентная, и повтор через паузу — ровно то, что нужно.
#
#    Доказательство транзиентности: 2026-09-03 в 02:56:16 первый же запрос
#    страницы (offset=0) вернул 429 code 8 и уронил весь ночной пайплайн, а
#    накануне, 2026-09-01 в 05:08:10, тот же самый запрос вернул 200 с первой
#    попытки. Ретраев в этом файле не было вовсе: любой не-200 сразу шёл в
#    RuntimeError, шаг падал с кодом 1 и уносил витрину за весь день.
#
# Повторяем только то, что заведомо транзиентно: 429 с code 8 и 5xx. Любой
# другой ответ — включая 429 с иным кодом — остаётся немедленной ошибкой:
# неизвестный отказ не должен молча превращаться в серию повторов.
SELLER_RETRY_MAX_ATTEMPTS = 4
SELLER_RETRY_BASE_SLEEP_SECONDS = 1
SELLER_RETRY_CAP_SLEEP_SECONDS = 10
SELLER_RATE_LIMIT_PER_SECOND_CODE = 8


def parse_seller_error_code(response):
    """Код ошибки из тела ответа Ozon. None, если тело не разобрать."""
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    code = body.get("code")
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def is_retryable_seller_response(response):
    """Транзиентный отказ, который имеет смысл повторить.

    Возвращает (retryable, reason) — reason идёт в лог, чтобы по логу было
    видно, почему решили повторять или не повторять.
    """
    status = int(response.status_code)
    if 500 <= status <= 599:
        return True, f"http_{status}"
    if status == 429:
        code = parse_seller_error_code(response)
        if code == SELLER_RATE_LIMIT_PER_SECOND_CODE:
            return True, "rate_limit_per_second"
        # 429 с другим кодом — это НЕ секундный троттлинг. Возможно, суточный
        # лимит Seller API, для которого повтор так же бессмыслен, как для
        # Performance. Не угадываем: падаем и пишем код в лог.
        return False, f"429_code_{code}"
    return False, f"http_{status}"


def seller_retry_sleep_seconds(response, attempt):
    """Пауза перед следующей попыткой. Retry-After имеет приоритет."""
    retry_after = (response.headers or {}).get("Retry-After")
    if retry_after is not None:
        try:
            return max(0, min(SELLER_RETRY_CAP_SLEEP_SECONDS, int(float(str(retry_after).strip()))))
        except (TypeError, ValueError):
            pass
    return min(
        SELLER_RETRY_CAP_SLEEP_SECONDS,
        SELLER_RETRY_BASE_SLEEP_SECONDS * (2 ** max(attempt - 1, 0)),
    )


def request_page(date_from, date_to, page_size, offset, sleep_fn=time.sleep):
    payload = build_payload(date_from, date_to, page_size, offset)

    for attempt in range(1, SELLER_RETRY_MAX_ATTEMPTS + 1):
        response = requests.post(
            SELLER_ANALYTICS_URL,
            headers=ozon_headers(),
            json=payload,
            timeout=120,
        )

        print(
            "Ozon Seller Analytics page request: "
            f"offset={offset} limit={page_size} status={response.status_code} "
            f"attempt={attempt}/{SELLER_RETRY_MAX_ATTEMPTS}"
        )

        if response.status_code == 200:
            return response.json(), payload

        retryable, reason = is_retryable_seller_response(response)
        if not retryable or attempt >= SELLER_RETRY_MAX_ATTEMPTS:
            # Попытки исчерпаны или отказ не транзиентный — ошибка наружу.
            # Глотать нельзя: шаг кормит органику и KPI, и молчаливый пропуск
            # оставил бы витрину неполной без единого следа.
            raise RuntimeError(
                f"Ozon Seller Analytics error {response.status_code} "
                f"(reason={reason}, attempts={attempt}): {response.text[:2000]}"
            )

        sleep_seconds = seller_retry_sleep_seconds(response, attempt)
        print(
            "Ozon Seller Analytics transient error: "
            f"reason={reason} attempt={attempt}/{SELLER_RETRY_MAX_ATTEMPTS} "
            f"sleep={sleep_seconds}s"
        )
        sleep_fn(sleep_seconds)

    raise RuntimeError("Ozon Seller Analytics retry loop exited without a result")


def parse_rows(response_data):
    result = response_data.get("result") if isinstance(response_data, dict) else None

    if isinstance(result, dict):
        raw_rows = result.get("data") or result.get("rows") or result.get("items") or []
        total = result.get("total") or result.get("count") or result.get("total_rows")
    else:
        raw_rows = response_data.get("data") or response_data.get("rows") or response_data.get("result") or []
        total = response_data.get("total") or response_data.get("count")

    parsed_rows = []
    for raw_row in raw_rows or []:
        dimension_map = row_to_dimension_map(raw_row)
        metric_map = row_to_metric_map(raw_row)

        sale_date = normalize_sale_date(dimension_map.get("day"))
        marketplace_sku = normalize_sku(dimension_map.get("sku"))

        if not sale_date or not marketplace_sku:
            continue

        parsed_rows.append(
            {
                "sale_date": sale_date,
                "marketplace_code": "ozon",
                "marketplace_sku": marketplace_sku,
                "total_orders_qty": parse_number(metric_map.get("ordered_units")),
                "total_orders_revenue": parse_number(metric_map.get("revenue")),
                "total_revenue_source": "seller_analytics",
            }
        )

    return parsed_rows, int(total or len(parsed_rows))


def fetch_total_orders(date_from, date_to, page_size=DEFAULT_PAGE_SIZE, max_pages=0):
    all_rows = []
    page_count = 0
    total_rows_reported = None

    while True:
        offset = page_count * page_size
        response_data, payload = request_page(date_from, date_to, page_size, offset)
        rows, total_rows_reported = parse_rows(response_data)
        all_rows.extend(rows)
        page_count += 1

        if len(rows) < page_size:
            break

        if max_pages and page_count >= max_pages:
            print("Остановлено по max_pages")
            break

        if total_rows_reported is not None and len(all_rows) >= total_rows_reported:
            break

        print(
            "Ozon Seller Analytics paging progress: "
            f"rows={len(all_rows)} total_reported={total_rows_reported} next_offset={payload['offset'] + page_size}"
        )

    return all_rows, {
        "page_count": page_count,
        "total_rows_reported": total_rows_reported,
        "estimated_request_count": page_count,
    }


def enrich_rows(rows, catalog):
    for row in rows:
        catalog_row = catalog.get(row["marketplace_sku"])
        if not catalog_row:
            continue
        if not row.get("article"):
            row["article"] = catalog_row.get("article") or ""
        if not row.get("product_name"):
            row["product_name"] = catalog_row.get("product_name") or ""
    return rows


def aggregate_rows(rows):
    grouped = {}

    for row in rows:
        key = (
            row.get("sale_date"),
            row.get("marketplace_code"),
            row.get("marketplace_sku"),
            row.get("total_revenue_source"),
        )

        if key not in grouped:
            grouped[key] = {
                "sale_date": row.get("sale_date"),
                "marketplace_code": row.get("marketplace_code"),
                "marketplace_sku": row.get("marketplace_sku"),
                "article": row.get("article") or "",
                "product_name": row.get("product_name") or "",
                "total_orders_qty": 0.0,
                "total_orders_revenue": 0.0,
                "total_revenue_source": row.get("total_revenue_source") or "seller_analytics",
            }

        grouped[key]["total_orders_qty"] += float(row.get("total_orders_qty") or 0)
        grouped[key]["total_orders_revenue"] += float(row.get("total_orders_revenue") or 0)
        if not grouped[key].get("article") and row.get("article"):
            grouped[key]["article"] = row.get("article")
        if not grouped[key].get("product_name") and row.get("product_name"):
            grouped[key]["product_name"] = row.get("product_name")

    return list(grouped.values())


def save_rows(rows):
    if not rows:
        print("Нет Seller Analytics total rows для записи")
        return

    for batch in chunks(rows, 500):
        supabase.table("ozon_daily_sku_total_orders").upsert(
            batch,
            on_conflict="sale_date,marketplace_code,marketplace_sku,total_revenue_source",
        ).execute()

    print(f"✅ ozon_daily_sku_total_orders обновлена: {len(rows)} строк")


def print_sample(rows, limit=10):
    for row in rows[:limit]:
        print(row)


def main():
    args = parse_args()
    date_from, date_to = resolve_date_range(args)

    if not OZON_CLIENT_ID or not OZON_API_KEY:
        raise RuntimeError("Заполните OZON_CLIENT_ID и OZON_API_KEY")

    print(
        "Ozon Seller Analytics loader request budget estimate: "
        f"page_size={args.page_size}, max_pages={args.max_pages or 'unbounded'}"
    )

    rows, request_summary = fetch_total_orders(
        date_from,
        date_to,
        page_size=max(1, int(args.page_size or DEFAULT_PAGE_SIZE)),
        max_pages=max(0, int(args.max_pages or 0)),
    )

    catalog = load_catalog()
    rows = enrich_rows(rows, catalog)
    aggregated_rows = aggregate_rows(rows)

    summary = {
        "date_from": date_from,
        "date_to": date_to,
        "raw_rows": len(rows),
        "aggregated_rows": len(aggregated_rows),
        "request_summary": request_summary,
        "totals": {
            "total_orders_qty": round(sum(float(row.get("total_orders_qty") or 0) for row in aggregated_rows), 2),
            "total_orders_revenue": round(sum(float(row.get("total_orders_revenue") or 0) for row in aggregated_rows), 2),
        },
    }
    print("Ozon Seller Analytics total orders summary:")
    print(summary)

    if args.debug_sample or args.dry_run:
        print_sample(aggregated_rows)

    if args.dry_run:
        print("Dry run: ozon_daily_sku_total_orders не обновлялась")
        return

    save_rows(aggregated_rows)


if __name__ == "__main__":
    main()
