import argparse
from decimal import Decimal
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from supabase import create_client

try:
    from loaders import http_retry
except ImportError:  # пайплайн зовёт как скрипт: python3 loaders/<файл>.py
    import http_retry


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


def load_catalog(client=None):
    """SKU → {article, product_name} из ozon_products (sku, offer_id, name), постранично с сортировкой по sku.

    Раньше читался sku_catalog, но его marketplace_sku — product_id Ozon (во всех трёх базах: 0 совпадений с SKU, RBH
    2026-10-31), и поиск по SKU строки рекламы / аналитики не находил ничего: за 22–28.10 без артикула осталось
    4 507 из 5 648 строк атрибуции KARATOV, 534 из 1 797 у РБХ-1, 254 из 1 264 у РБХ-2. ozon_products ведёт ночной шаг
    «Ozon: каталог, добор недостающих»."""
    client = client or supabase
    rows = []
    start = 0
    page_size = 1000

    while True:
        result = (
            client
            .table("ozon_products")
            .select("sku,offer_id,name")
            .order("sku")
            .range(start, start + page_size - 1)
            .execute()
        )

        batch = result.data or []
        rows.extend(batch)

        if len(batch) < page_size:
            break

        start += page_size

    return {
        str(row.get("sku")): {"article": row.get("offer_id") or "", "product_name": row.get("name") or ""}
        for row in rows if row.get("sku")
    }


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
# Ночь 09-22: три 429 подряд (паузы 1 / 2 / 4 с), прошло с четвёртой попытки из четырёх — запаса не было.
# Было до 2026-11-08: шесть попыток с паузами 1, 2, 4, 8, 16 с (потолок 32) и 1,5 с между страницами.
#
# С 2026-11-08 (ночь 08.11: третья страница — 429 на всех шести попытках за 34 с, шаг упал; так же 02 и 05.11):
# спека (`spec/ozon-seller.json`, /v1/analytics/data) — «Метод можно использовать не больше 1 раза в минуту».
# Мы ходили страницами раз в ~2 с, то есть сами нарушали лимит; секундные паузы его не изживали.
# Поэтому:
#   * между запросами — не меньше SELLER_PAGE_INTERVAL_SECONDS (60 с, от начала предыдущего запроса);
#   * 429 code 8 — ждать по заголовкам: Retry-After (не меньше интервала), при ratelimit-remaining = 0 или без
#     заголовков — интервал 60 с; не больше SELLER_RATE_LIMIT_MAX_ATTEMPTS попыток на страницу, затем стоп (CLAUDE.md §2: «пауза 60 с, не более 3 попыток»);
#     Retry-After больше SELLER_RATE_LIMIT_MAX_HEADER_WAIT_SECONDS — стоп сразу (чат id 67219: Retry-After 1126,
#     и после ожидания тот же 429 — ночь на это не тратим);
#   * все ожидания по отказам за прогон (429 и 5xx) — не больше SELLER_RETRY_WAIT_BUDGET_SECONDS, дальше стоп с
#     причиной: шаг нефатальный, ночь он растягивать не должен;
#   * 5xx — прежний backoff 1, 2, 4, 8, 16 с, но не меньше интервала (в счёт бюджета);
#   * число 429 и пауз — строкой шага «Ozon Seller Analytics ожидания: …» и в request_summary.
SELLER_RETRY_MAX_ATTEMPTS = 6
SELLER_RETRY_BASE_SLEEP_SECONDS = 1
SELLER_RETRY_CAP_SLEEP_SECONDS = 32
SELLER_RATE_LIMIT_PER_SECOND_CODE = 8
SELLER_PAGE_INTERVAL_SECONDS = float(os.getenv("OZON_ANALYTICS_PAGE_INTERVAL_SECONDS", "60"))
SELLER_RATE_LIMIT_MAX_ATTEMPTS = 3
SELLER_RATE_LIMIT_MAX_HEADER_WAIT_SECONDS = 120.0
SELLER_RETRY_WAIT_BUDGET_SECONDS = float(os.getenv("OZON_ANALYTICS_RETRY_WAIT_BUDGET_SECONDS", "600"))


class SellerAnalyticsStop(RuntimeError):
    """Стоп с явной причиной: повторы по 429 исчерпаны, Retry-After выше потолка или бюджет ожиданий кончился."""


class SellerAnalyticsPacer:
    """Темп запросов /v1/analytics/data за один прогон: интервал между запросами, ожидания по отказам, счётчики.

    clock и sleep_fn подставляются в тестах; sleep_fn реального прогона — time.sleep."""

    def __init__(self, sleep_fn=time.sleep, clock=time.monotonic,
                 interval=None, budget=None):
        self.sleep_fn = sleep_fn
        self.clock = clock
        self.interval = SELLER_PAGE_INTERVAL_SECONDS if interval is None else float(interval)
        self.budget = SELLER_RETRY_WAIT_BUDGET_SECONDS if budget is None else float(budget)
        self.last_request_at = None
        self.not_before = None          # от ratelimit-remaining = 0 у удачного ответа
        self.requests = 0
        self.rate_limited = 0           # ответов 429
        self.server_errors = 0          # ответов 5xx
        self.retry_waits = 0
        self.retry_wait_seconds = 0.0
        self.interval_pauses = 0
        self.interval_pause_seconds = 0.0

    def _sleep(self, seconds):
        if seconds > 0:
            self.sleep_fn(seconds)

    def before_request(self):
        """Дождаться интервала от начала предыдущего запроса (и not_before), затем отметить запрос."""
        now = self.clock()
        targets = []
        if self.last_request_at is not None:
            targets.append(self.last_request_at + self.interval)
        if self.not_before is not None:
            targets.append(self.not_before)
        gap = max(targets) - now if targets else 0
        if gap > 0:
            self._sleep(gap)
            self.interval_pauses += 1
            self.interval_pause_seconds += gap
        self.not_before = None
        self.last_request_at = self.clock()
        self.requests += 1

    def retry_wait(self, seconds, reason):
        """Ожидание по отказу в счёт бюджета прогона; бюджета нет — стоп с причиной (до ожидания)."""
        seconds = max(0.0, float(seconds))
        if self.retry_wait_seconds + seconds > self.budget:
            raise SellerAnalyticsStop(
                f"Ozon Seller Analytics: бюджет ожиданий по отказам исчерпан ({reason}): уже ждали "
                f"{self.retry_wait_seconds:.0f} с, нужно ещё {seconds:.0f} с, бюджет {self.budget:.0f} с; {self.line()}"
            )
        self._sleep(seconds)
        self.retry_waits += 1
        self.retry_wait_seconds += seconds

    def after_success(self, response):
        """ratelimit-remaining = 0 у удачного ответа — следующий запрос не раньше Retry-After (если он больше интервала)."""
        if http_retry.ratelimit_remaining(response) == 0:
            wait = _header_seconds(response, "Retry-After")
            if wait is not None and wait > self.interval:
                self.not_before = self.clock() + min(wait, SELLER_RATE_LIMIT_MAX_HEADER_WAIT_SECONDS)

    def summary(self):
        return {
            "requests": self.requests,
            "rate_limited_429": self.rate_limited,
            "server_errors_5xx": self.server_errors,
            "retry_waits": self.retry_waits,
            "retry_wait_seconds": round(self.retry_wait_seconds, 1),
            "interval_pauses": self.interval_pauses,
            "interval_pause_seconds": round(self.interval_pause_seconds, 1),
            "interval_seconds": self.interval,
            "retry_wait_budget_seconds": self.budget,
        }

    def line(self):
        return (
            "Ozon Seller Analytics ожидания: "
            f"запросов {self.requests}, 429 — {self.rate_limited}, 5xx — {self.server_errors}, "
            f"пауз по отказам {self.retry_waits} ({self.retry_wait_seconds:.0f} с из {self.budget:.0f}), "
            f"пауз интервала {self.interval_pauses} ({self.interval_pause_seconds:.0f} с, интервал {self.interval:.0f} с)"
        )


def _header_seconds(response, name):
    raw = http_retry.header(response, name)
    if raw in (None, ""):
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def rate_limit_wait_seconds(response, interval=None):
    """Пауза после 429 code 8: (секунды, источник). None вместо секунд — Retry-After выше потолка, ждать нельзя."""
    interval = SELLER_PAGE_INTERVAL_SECONDS if interval is None else float(interval)
    retry_after = _header_seconds(response, "Retry-After")
    if retry_after is not None:
        if retry_after > SELLER_RATE_LIMIT_MAX_HEADER_WAIT_SECONDS:
            return None, f"retry_after={retry_after:.0f}s"
        return max(retry_after, interval), f"retry_after={retry_after:.0f}s"
    if http_retry.ratelimit_remaining(response) == 0:
        return interval, "ratelimit_remaining=0"
    return interval, "no_headers"


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


def request_page(date_from, date_to, page_size, offset, sleep_fn=time.sleep, pacer=None):
    """Одна страница. pacer — общий на прогон (интервал между страницами и бюджет ожиданий); нет — свой на вызов."""
    payload = build_payload(date_from, date_to, page_size, offset)
    pacer = pacer or SellerAnalyticsPacer(sleep_fn=sleep_fn)
    rate_limit_attempts = 0

    for attempt in range(1, SELLER_RETRY_MAX_ATTEMPTS + 1):
        pacer.before_request()
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
            pacer.after_success(response)
            return response.json(), payload

        retryable, reason = is_retryable_seller_response(response)
        status = int(response.status_code)
        if status == 429:
            pacer.rate_limited += 1
        elif 500 <= status <= 599:
            pacer.server_errors += 1

        if not retryable or attempt >= SELLER_RETRY_MAX_ATTEMPTS:
            # Попытки исчерпаны или отказ не транзиентный — ошибка наружу.
            # Глотать нельзя: шаг кормит органику и KPI, и молчаливый пропуск
            # оставил бы витрину неполной без единого следа.
            print(pacer.line())
            raise RuntimeError(
                f"Ozon Seller Analytics error {response.status_code} "
                f"(reason={reason}, attempts={attempt}): {response.text[:2000]}"
            )

        if status == 429:
            sleep_seconds, source = rate_limit_wait_seconds(response, pacer.interval)
            if sleep_seconds is None:
                print(pacer.line())
                raise SellerAnalyticsStop(
                    f"Ozon Seller Analytics: 429 (reason={reason}), {source} выше потолка "
                    f"{SELLER_RATE_LIMIT_MAX_HEADER_WAIT_SECONDS:.0f} с — не ждём, стоп; offset={offset}, attempts={attempt}"
                )
            rate_limit_attempts += 1
            if rate_limit_attempts >= SELLER_RATE_LIMIT_MAX_ATTEMPTS:
                print(pacer.line())
                raise SellerAnalyticsStop(
                    f"Ozon Seller Analytics: 429 (reason={reason}) {rate_limit_attempts} раза подряд, паузы по "
                    f"{pacer.interval:.0f}+ с — стоп (CLAUDE.md §2: не более {SELLER_RATE_LIMIT_MAX_ATTEMPTS} попыток); "
                    f"offset={offset}, attempts={attempt}"
                )
        else:
            # 5xx: прежний backoff, но не меньше интервала метода — ожидание идёт в бюджет, а не в паузы интервала
            sleep_seconds, source = max(seller_retry_sleep_seconds(response, attempt), pacer.interval), "backoff"

        print(
            "Ozon Seller Analytics transient error: "
            f"reason={reason} attempt={attempt}/{SELLER_RETRY_MAX_ATTEMPTS} "
            f"sleep={sleep_seconds:g}s source={source}"
        )
        try:
            pacer.retry_wait(sleep_seconds, reason)
        except SellerAnalyticsStop:
            print(pacer.line())
            raise

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


def fetch_total_orders(date_from, date_to, page_size=DEFAULT_PAGE_SIZE, max_pages=0, sleep_fn=time.sleep,
                       clock=time.monotonic, pacer=None):
    all_rows = []
    page_count = 0
    total_rows_reported = None
    # Один темп на прогон: между страницами — интервал метода (спека: не больше 1 раза в минуту), бюджет ожиданий общий.
    pacer = pacer or SellerAnalyticsPacer(sleep_fn=sleep_fn, clock=clock)

    while True:
        offset = page_count * page_size
        response_data, payload = request_page(date_from, date_to, page_size, offset, sleep_fn=sleep_fn, pacer=pacer)
        rows, total_rows_reported = parse_rows(response_data)
        all_rows.extend(rows)
        page_count += 1

        if len(rows) < page_size:
            break

        if max_pages and page_count >= max_pages:
            print("Остановлено по max_pages")
            break

        # «total» в ответе analytics/data равен размеру страницы (проверено 2026-10-21 на РБХ-1: при 60-дневном окне
        # offset 1000 и 2000 отдают по 1 000 строк при total = 1000) — на нём останавливаться нельзя, иначе всё, что
        # больше одной страницы, молча теряется. Конец — короткая страница (выше) или max_pages; total — справочно.

        print(
            "Ozon Seller Analytics paging progress: "
            f"rows={len(all_rows)} total_reported={total_rows_reported} next_offset={payload['offset'] + page_size}"
        )

    print(pacer.line())
    return all_rows, {
        "page_count": page_count,
        "total_rows_reported": total_rows_reported,
        "estimated_request_count": page_count,
        "pacing": pacer.summary(),
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


def created_orders_totals(date_from, date_to, client=None):
    """Созданные заказы Ozon (подтверждённые + отменённые) из marketplace_orders за те же даты: (Σ руб., Σ шт, строк)."""
    client = client or supabase
    amount = qty = Decimal(0)
    rows = 0
    start = 0
    while True:
        page = (client.table("marketplace_orders").select("orders_qty,orders_amount_seller,cancelled_orders_qty,cancelled_orders_amount_seller")
                .eq("marketplace_code", "ozon").gte("order_date", date_from).lte("order_date", date_to)
                .order("id").range(start, start + 999).execute().data or [])
        for r in page:
            amount += Decimal(str(r.get("orders_amount_seller") or 0)) + Decimal(str(r.get("cancelled_orders_amount_seller") or 0))
            qty += Decimal(str(r.get("orders_qty") or 0)) + Decimal(str(r.get("cancelled_orders_qty") or 0))
        rows += len(page)
        if len(page) < 1000:
            return amount, qty, rows
        start += 1000


def revenue_guard_line(analytics_revenue, analytics_qty, created_amount, created_qty, orders_rows):
    """Сторож: revenue аналитики к созданным заказам за те же даты. Только печать — порог и тревога после решения советника.

    Замер 2026-09-24 (тридцать пятая задача, §3 п. 2): на 21 дне из 26 (08-20 … 09-21) отношение ровно 1,0000 — analytics/data
    и marketplace_orders считают одно множество по МСК-суткам; 08-31 — 1,0113, 09-08 — 0,9978; а за 09-22 аналитика отдала
    11 279 201 против 8 074 089 созданных (1,397; отчёты ЛК подтвердили наши 8 074 089 до рубля) и за 09-23 — 1,090.
    Без этой строки завышение было бы невидимо: таблицу читает только выключенная органика.
    """
    if orders_rows == 0:
        return (f"сторож revenue: заказов за период в marketplace_orders нет (шаг заказов не прошёл или дата ещё не собрана) — "
                f"аналитика {analytics_revenue:,.2f} руб. / {analytics_qty:,.0f} шт, сравнить не с чем")
    ratio = (Decimal(str(analytics_revenue)) / created_amount) if created_amount else None
    ratio_qty = (Decimal(str(analytics_qty)) / created_qty) if created_qty else None
    return (f"сторож revenue: аналитика {analytics_revenue:,.2f} руб. / {analytics_qty:,.0f} шт против созданных заказов "
            f"{created_amount:,.2f} руб. / {created_qty:,.0f} шт ({orders_rows} строк) — отношение по деньгам "
            f"{'—' if ratio is None else f'{ratio:.4f}'}, по штукам {'—' if ratio_qty is None else f'{ratio_qty:.4f}'}; "
            "на дозревших днях ровно 1,0000 (порог тревоги — после решения советника)")


def main():
    args = parse_args()
    date_from, date_to = resolve_date_range(args)

    if not OZON_CLIENT_ID or not OZON_API_KEY:
        raise RuntimeError("Заполните OZON_CLIENT_ID и OZON_API_KEY")

    print(
        "Ozon Seller Analytics loader request budget estimate: "
        f"page_size={args.page_size}, max_pages={args.max_pages or 'unbounded'}, "
        f"interval={SELLER_PAGE_INTERVAL_SECONDS:g}s, retry_wait_budget={SELLER_RETRY_WAIT_BUDGET_SECONDS:g}s"
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
    try:
        created_amount, created_qty, orders_rows = created_orders_totals(date_from, date_to)
        print(revenue_guard_line(summary["totals"]["total_orders_revenue"], summary["totals"]["total_orders_qty"], created_amount, created_qty, orders_rows))
    except Exception as exc:  # сторож не должен ронять шаг: он только печатает
        print(f"сторож revenue: не посчитан — {type(exc).__name__}: {exc}")

    if args.debug_sample or args.dry_run:
        print_sample(aggregated_rows)

    if args.dry_run:
        print("Dry run: ozon_daily_sku_total_orders не обновлялась")
        return

    save_rows(aggregated_rows)


if __name__ == "__main__":
    main()
