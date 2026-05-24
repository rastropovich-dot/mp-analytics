import argparse
import copy
import csv
import hashlib
import io
import json
import os
import random
import re
import tempfile
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from supabase import create_client

try:
    import fcntl
except ImportError:
    fcntl = None


load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

OZON_PERFORMANCE_CLIENT_ID = os.getenv("OZON_PERFORMANCE_CLIENT_ID")
OZON_PERFORMANCE_CLIENT_SECRET = os.getenv("OZON_PERFORMANCE_CLIENT_SECRET")
OZON_SELLER_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_PERFORMANCE_BASE_URL = os.getenv(
    "OZON_PERFORMANCE_BASE_URL",
    "https://api-performance.ozon.ru",
).rstrip("/")

DEFAULT_GROUP_BY = os.getenv("OZON_PERFORMANCE_GROUP_BY", "DATE")
DEFAULT_CAMPAIGN_SCOPE = os.getenv("OZON_PERFORMANCE_CAMPAIGN_SCOPE", "recent")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")
OZON_PERFORMANCE_DAILY_TARGET_MODE = (
    os.getenv("OZON_PERFORMANCE_DAILY_TARGET_MODE", "yesterday") or "yesterday"
).strip().lower()
RECON_WARNING_THRESHOLD = float(os.getenv("OZON_CPO_RECON_WARNING_THRESHOLD", "0.01"))
RECON_ERROR_THRESHOLD = float(os.getenv("OZON_CPO_RECON_ERROR_THRESHOLD", "1.0"))
CPC_BASE_SLEEP_SECONDS = int(os.getenv("OZON_PERFORMANCE_CPC_BASE_SLEEP_SECONDS", "300"))
CPC_MAX_SLEEP_SECONDS = int(os.getenv("OZON_PERFORMANCE_CPC_MAX_SLEEP_SECONDS", "3600"))
CPC_MAX_ATTEMPTS = int(os.getenv("OZON_PERFORMANCE_CPC_MAX_ATTEMPTS", "6"))
CPC_COOLDOWN_SECONDS = int(os.getenv("OZON_PERFORMANCE_CPC_COOLDOWN_SECONDS", "1800"))
BATCH_SIZE_RECOVERY_TTL_SECONDS = int(os.getenv("OZON_PERFORMANCE_BATCH_SIZE_RECOVERY_TTL_SECONDS", "21600"))
DEFAULT_CAMPAIGN_BATCH_SIZE = int(os.getenv("OZON_PERFORMANCE_CAMPAIGN_BATCH_SIZE", "10"))
DEFAULT_MAX_CPC_BATCHES_PER_RUN = int(os.getenv("OZON_PERFORMANCE_MAX_CPC_BATCHES_PER_RUN", "5"))
STATS_DAILY_CAMPAIGN_LIMIT = int(os.getenv("OZON_PERFORMANCE_STATS_DAILY_CAMPAIGN_LIMIT", "2000"))
STATS_DAILY_CAMPAIGN_RESERVE = int(os.getenv("OZON_PERFORMANCE_STATS_DAILY_CAMPAIGN_RESERVE", "200"))
DEFAULT_MAX_STATS_CAMPAIGNS_PER_DAILY_RUN = int(
    os.getenv("OZON_PERFORMANCE_MAX_STATS_CAMPAIGNS_PER_DAILY_RUN", "1800")
)
OZON_PERFORMANCE_DAILY_CPC_SELECTION_MODE = (
    os.getenv("OZON_PERFORMANCE_DAILY_CPC_SELECTION_MODE", "complete") or "complete"
).strip().lower()
REQUEST_AUDIT_LIMIT = int(os.getenv("OZON_PERFORMANCE_REQUEST_AUDIT_LIMIT", "1000"))
REQUEST_AUDIT_TTL_DAYS = int(os.getenv("OZON_PERFORMANCE_REQUEST_AUDIT_TTL_DAYS", "7"))
CPC_BACKFILL_START_HHMM = os.getenv("OZON_PERFORMANCE_CPC_BACKFILL_START_HHMM", "03:05")
STATE_BACKEND = (os.getenv("OZON_PERFORMANCE_STATE_BACKEND", "db") or "db").strip().lower()
ADV_OBJECT_TYPES = [
    value.strip()
    for value in os.getenv("OZON_PERFORMANCE_ADV_OBJECT_TYPES", "SKU").split(",")
    if value.strip()
]

EXPLICIT_CAMPAIGN_TYPES = {
    "advertising_order_5": {
        value.strip()
        for value in os.getenv("OZON_ADS_ORDER_5_CAMPAIGNS", "").split(",")
        if value.strip()
    },
    "advertising_order_10": {
        value.strip()
        for value in os.getenv("OZON_ADS_ORDER_10_CAMPAIGNS", "").split(",")
        if value.strip()
    },
    "advertising_clicks": {
        value.strip()
        for value in os.getenv("OZON_ADS_CLICK_CAMPAIGNS", "").split(",")
        if value.strip()
    },
}

ALLOWED_CPO_REPORT_TYPES = {"orders", "products"}
ALLOWED_SEARCH_PROMO_REPORT_TYPES = {"orders", "products"}
SEARCH_PROMO_REPORT_ENDPOINTS = {
    "orders": "/api/client/statistics/search_promo/orders/generate",
    "products": "/api/client/statistics/search_promo/products/generate",
}
SEARCH_PROMO_ORGANISATION_ORDERS_SUBMIT_ENDPOINT = "/api/client/statistic/orders/generate"
SEARCH_PROMO_ORGANISATION_ORDERS_KIND = "SEARCH_PROMO_ORGANISATION_ORDERS"
SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE = "ozon_search_promo_selected_cpo_orders"
SELECTED_CPO_MARKETPLACE_EXPENSE_TYPE = "advertising_order_selected_cpo"
SELECTED_CPO_AD_SOURCE = "cpo_selected_products"
OZON_PERFORMANCE_CAMPAIGN_METADATA_TABLE = "ozon_performance_campaign_metadata"
ENABLE_OZON_SELECTED_CPO_DAILY = (os.getenv("ENABLE_OZON_SELECTED_CPO_DAILY", "false") or "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
APPROVE_OZON_SELECTED_CPO_DAILY_WRITE = (
    os.getenv("APPROVE_OZON_SELECTED_CPO_DAILY_WRITE", "false") or "false"
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

AD_EXPENSE_TYPES = {
    "advertising_clicks",
    "advertising_order_5",
    "advertising_order_10",
    "advertising_order_other",
    "advertising_order_unknown",
    "advertising_other",
}

UPSERT_KEY_FIELDS = (
    "expense_date",
    "marketplace_code",
    "marketplace_sku",
    "expense_type",
)

AD_ATTRIBUTION_UPSERT_KEY_FIELDS = (
    "sale_date",
    "marketplace_code",
    "marketplace_sku",
    "ad_source",
    "attribution_type",
    "campaign_id",
)

PERFORMANCE_CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "ozon_performance"
PERFORMANCE_STATE_PATH = PERFORMANCE_CACHE_DIR / "state.json"
PERFORMANCE_STATE_LOCK_PATH = PERFORMANCE_CACHE_DIR / "state.lock"
PIPELINE_RUNTIME_STATE_TABLE = "pipeline_runtime_state"
RUNTIME_STATE_STALE_DELETE_CHUNK_SIZE = 25
DAILY_LOAD_STATUS_TABLE = "ozon_performance_daily_load_status"
PERSISTENT_STATE_SECTIONS = (
    "jobs",
    "cooldowns",
    "batch_recommendations",
    "cpc_progress",
)
VOLATILE_STATE_SECTIONS = (
    "runs",
    "request_history",
)

SENSITIVE_KEY_MARKERS = (
    "authorization",
    "api-key",
    "apikey",
    "client-id",
    "client_id",
    "cookie",
    "set-cookie",
    "token",
    "secret",
)

REQUEST_PROFILES = {
    "default": {
        "max_attempts": 5,
        "base_sleep_seconds": 5,
        "cap_sleep_seconds": 300,
        "cooldown_seconds": 0,
        "fail_fast_on_429": False,
    },
    "statistics_json": {
        "max_attempts": CPC_MAX_ATTEMPTS,
        "base_sleep_seconds": CPC_BASE_SLEEP_SECONDS,
        "cap_sleep_seconds": CPC_MAX_SLEEP_SECONDS,
        "cooldown_seconds": CPC_COOLDOWN_SECONDS,
        "fail_fast_on_429": True,
    },
}

POLL_PROFILES = {
    "default": {
        "max_attempts": 30,
        "base_sleep_seconds": 2,
        "cap_sleep_seconds": 30,
    },
    "statistics_json": {
        "max_attempts": 20,
        "base_sleep_seconds": 15,
        "cap_sleep_seconds": 60,
    },
    "all_sku_promo": {
        "max_attempts": 30,
        "base_sleep_seconds": 3,
        "cap_sleep_seconds": 30,
    },
}

METRIC_KEYS = {
    "views",
    "shows",
    "impressions",
    "clicks",
    "ctr",
    "moneySpent",
    "money_spent",
    "expense",
    "expenses",
    "spend",
    "cost",
    "orders",
    "ordersMoney",
    "orders_money",
    "revenue",
    "sales",
}

SKU_KEYS = (
    "sku",
    "SKU",
    "skuId",
    "sku_id",
    "productSku",
    "product_sku",
    "ozonSku",
    "ozon_sku",
    "Ozon SKU",
    "SKU Ozon",
    "id товара",
    "ID товара",
)

PROMOTED_SKU_KEYS = (
    "SKU продвигаемого товара",
    "promotedSku",
    "promoted_sku",
    "promotedProductSku",
    "promoted_product_sku",
)

ARTICLE_KEYS = (
    "offerId",
    "offer_id",
    "article",
    "Артикул",
    "articul",
    "vendorCode",
    "vendor_code",
)

DATE_KEYS = (
    "date",
    "dt",
    "day",
    "Дата",
    "operation_date",
    "operationDate",
)

SPEND_KEYS = (
    "moneySpent",
    "money_spent",
    "expense",
    "expenses",
    "spend",
    "cost",
    "Расход",
    "Расход, ₽",
    "Расход руб.",
)

REVENUE_KEYS = (
    "ordersMoney",
    "orders_money",
    "ordersSum",
    "orders_sum",
    "revenue",
    "sales",
    "salesMoney",
    "sales_money",
    "Выручка",
    "Сумма заказов",
)

CPO_ORDER_QTY_KEYS = (
    "Количество",
    "quantity",
    "qty",
    "orders",
    "Заказы",
)

CPO_REVENUE_KEYS = (
    "Стоимость продажи, ₽",
    "Стоимость продажи",
    "Стоимость, ₽",
    "Стоимость",
    "ordersMoney",
    "orders_money",
    "revenue",
    "sales",
    "salesMoney",
    "sales_money",
    "Выручка",
    "Сумма заказов",
)

CPO_RATE_KEYS = (
    "Ставка, %",
    "Ставка %",
    "rate",
    "promo_rate",
)


supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


class RateLimitPending(RuntimeError):
    def __init__(self, endpoint, retry_after_seconds, cooldown_until, attempt, message=None):
        self.endpoint = endpoint
        self.retry_after_seconds = retry_after_seconds
        self.cooldown_until = cooldown_until
        self.attempt = attempt
        super().__init__(
            message
            or (
                f"429 pending for {endpoint}: retry_after={retry_after_seconds}, "
                f"cooldown_until={cooldown_until}, attempt={attempt}"
            )
        )


class SelectedCpoDbMappingError(RuntimeError):
    """Raised when selected CPO rows are ready but current DB schema cannot store them safely."""


class SelectedCpoSchemaNotAppliedError(RuntimeError):
    """Raised when selected CPO source table mapping exists in code, but migration is not applied."""


class SelectedCpoDownstreamWriteNotApprovedError(RuntimeError):
    """Raised when selected CPO downstream aggregation exists, but live write is not approved yet."""


class CpcMaterializationGuardError(RuntimeError):
    """Raised when CPC progress is marked complete, but no CPC rows were materialized or verified downstream."""


class CpcRecoveryWriteNotApprovedError(RuntimeError):
    """Raised when CPC recovery write is requested without an explicit approval flag."""


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def ensure_cache_dir():
    PERFORMANCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def default_state():
    return {
        "jobs": {},
        "cooldowns": {},
        "batch_recommendations": {},
        "cpc_progress": {},
        "runs": {},
        "request_history": [],
    }


def normalize_state_backend(value):
    backend = str(value or "").strip().lower()
    if backend in {"db", "file"}:
        return backend
    return "db"


def is_sensitive_key(key):
    lowered = str(key or "").lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def sanitize_text(value):
    text = str(value or "")
    patterns = [
        r'(?i)("?(authorization|api[-_]?key|client[-_]?id|cookie|set-cookie|access_token|refresh_token|token|client_secret)"?\s*[:=]\s*"?)([^",\s}]+)',
        r'(?i)(bearer\s+)([A-Za-z0-9._\-]+)',
    ]

    for pattern in patterns:
        text = re.sub(pattern, r"\1[REDACTED]", text)

    return text


def sanitize_value(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                result[key] = "[REDACTED]"
            else:
                result[key] = sanitize_value(item)
        return result

    if isinstance(value, list):
        return [sanitize_value(item) for item in value]

    if isinstance(value, str):
        return sanitize_text(value)

    return value


def utcnow():
    return datetime.now(ZoneInfo("UTC"))


def now_local():
    return datetime.now(ZoneInfo(APP_TIMEZONE))


def today_local():
    return now_local().date()


def to_iso(dt):
    return dt.astimezone(ZoneInfo("UTC")).isoformat()


def from_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def local_now():
    return datetime.now(ZoneInfo(APP_TIMEZONE))


def prune_request_history(history):
    if not isinstance(history, list):
        return []

    cutoff = utcnow() - timedelta(days=REQUEST_AUDIT_TTL_DAYS)
    pruned = []

    for item in history:
        if not isinstance(item, dict):
            continue
        timestamp = from_iso(item.get("timestamp"))
        if timestamp and timestamp < cutoff:
            continue
        pruned.append(item)

    if len(pruned) > REQUEST_AUDIT_LIMIT:
        pruned = pruned[-REQUEST_AUDIT_LIMIT:]

    return pruned


def with_state_lock(exclusive=True):
    ensure_cache_dir()
    mode = "a+"
    lock_file = open(PERFORMANCE_STATE_LOCK_PATH, mode, encoding="utf-8")

    if fcntl is not None:
        lock_mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(lock_file.fileno(), lock_mode)

    return lock_file


def load_file_state():
    state = default_state()
    lock_file = with_state_lock(exclusive=False)
    try:
        if not PERFORMANCE_STATE_PATH.exists():
            return state

        try:
            loaded = json.loads(PERFORMANCE_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state.update(loaded)
            state["request_history"] = prune_request_history(state.get("request_history", []))
            return state
        except Exception:
            return state
    finally:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def save_file_state(state):
    ensure_cache_dir()
    state["request_history"] = prune_request_history(state.get("request_history", []))
    sanitized_state = sanitize_value(state)
    lock_file = with_state_lock(exclusive=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(PERFORMANCE_CACHE_DIR),
            prefix="state.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            json.dump(sanitized_state, tmp_file, ensure_ascii=False, indent=2, sort_keys=True)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
            tmp_path = Path(tmp_file.name)

        os.replace(tmp_path, PERFORMANCE_STATE_PATH)
    finally:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def build_state_row(state_key, state_type, payload, account_signature=None, expires_at=None):
    row = {
        "state_key": state_key,
        "state_type": state_type,
        "account_signature": account_signature,
        "payload": sanitize_value(payload),
        "updated_at": to_iso(utcnow()),
    }
    if expires_at is not None:
        row["expires_at"] = expires_at
    return row


def build_db_state_key(state_type, logical_key):
    return f"{state_type}:{logical_key}"


def parse_db_state_key(state_type, stored_key):
    prefix = f"{state_type}:"
    text = str(stored_key or "")
    if text.startswith(prefix):
        return text[len(prefix):]
    return text


def payload_hash(payload):
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def canonicalize_payload(value, key_name=None):
    if isinstance(value, dict):
        return {
            key: canonicalize_payload(value[key], key)
            for key in sorted(value.keys())
        }

    if isinstance(value, list):
        items = [canonicalize_payload(item, key_name) for item in value]
        if key_name == "campaigns":
            return sorted(str(item) for item in items)
        return items

    return value


def parse_retry_after_seconds(value):
    if value in (None, ""):
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.isdigit():
        return int(text)

    try:
        retry_time = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None

    seconds = (retry_time - utcnow()).total_seconds()
    return max(0, int(seconds))


def compute_backoff_seconds(base_sleep_seconds, cap_sleep_seconds, attempt):
    base = base_sleep_seconds * (2 ** max(attempt - 1, 0))
    jitter = random.uniform(0, max(1, base_sleep_seconds * 0.2))
    return min(cap_sleep_seconds, int(base + jitter))


def infer_request_kind(method, endpoint):
    endpoint_text = str(endpoint or "")
    method_text = str(method or "").upper()

    if endpoint_text == "/api/client/statistics/json" and method_text == "POST":
        return "statistics_job_create"
    if endpoint_text.startswith("/api/client/statistics/") and endpoint_text != "/api/client/statistics/report":
        return "statistics_job_status"
    if endpoint_text == "/api/client/statistics/report":
        return "statistics_report_download"
    if endpoint_text.startswith("/api/client/statistics/all_sku_promo/"):
        return "all_sku_promo_job_create"
    if endpoint_text == "/api/client/campaign":
        return "campaign_list"
    if endpoint_text == "/api/client/token":
        return "token"

    return "other"


def response_body_preview(response, limit=1000):
    try:
        text = response.text or ""
    except Exception:
        return ""
    return sanitize_text(text[:limit])


def log_http_response(endpoint, attempt, response, extra=None):
    payload = {
        "endpoint": endpoint,
        "attempt": attempt,
        "status": response.status_code,
        "headers": sanitize_value(dict(response.headers)),
        "body_preview": response_body_preview(response),
    }
    if extra:
        payload.update(sanitize_value(extra))
    print("Ozon Performance HTTP:")
    print(json.dumps(sanitize_value(payload), ensure_ascii=False))


def send_telegram_partial_ads_alert(summary):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    cpc = summary.get("cpc") or {}
    message = (
        "⚠️ Ozon Performance partial_ads\n"
        f"Период: {summary.get('date_from')}..{summary.get('date_to')}\n"
        f"campaign_count: {summary.get('campaign_count')}\n"
        f"batch_size: {summary.get('batch_size')}\n"
        f"CPC status: {cpc.get('status')}\n"
        f"progress total: {summary.get('cpc_campaign_units_completed_total')}/{summary.get('cpc_campaign_units_planned_total')}\n"
        f"pending total: {summary.get('cpc_campaign_units_pending_total')}\n"
        f"this run attempted: {summary.get('cpc_campaign_units_attempted_this_run')}\n"
        f"this run completed: {summary.get('cpc_campaign_units_completed_this_run')}\n"
        f"this run failed_429: {summary.get('cpc_campaign_units_failed_429_this_run')}\n"
        f"stop batch: {cpc.get('failed_batch_index')}\n"
        f"reason: 429\n"
        f"Retry-After: {cpc.get('retry_after_seconds')}\n"
        f"cooldown_until: {cpc.get('cooldown_until')}\n"
        f"CPO status: {(summary.get('cpo') or {}).get('status')}"
    )

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
    except Exception as exc:
        print(f"Не удалось отправить partial_ads alert в Telegram: {sanitize_text(exc)}")


def parse_args():
    default_mode = "daily-yesterday" if OZON_PERFORMANCE_DAILY_TARGET_MODE == "yesterday" else "full"
    parser = argparse.ArgumentParser(
        description="Load Ozon Performance advertising expenses by SKU.",
    )
    parser.add_argument(
        "--mode",
        choices=(
            "daily-yesterday",
            "full",
            "cpc-backfill",
            "cpc-recovery",
            "cpo-report-check",
            "search-promo-report-check",
            "statistics-json-probe",
        ),
        default=default_mode,
        help=(
            "daily-yesterday = production D-1 load in Europe/Moscow; "
            "full = explicit date/date-range historical run; "
            "cpc-backfill = retry only pending CPC for one day; "
            "cpc-recovery = quota-aware CPC-only refetch for one day without CPO; "
            "cpo-report-check = generate/poll/download one CPO report and dry-parse it; "
            "search-promo-report-check = plan or dry-run one SEARCH_PROMO report for selected CPO; "
            "statistics-json-probe = generate/poll/download one statistics/json report and dry-parse it"
        ),
    )
    parser.add_argument("--date", help="single-day shortcut, sets both --date-from and --date-to")
    parser.add_argument(
        "--campaign-id",
        action="append",
        default=[],
        help=(
            "Campaign id filter. statistics-json-probe uses the first value; "
            "cpc-recovery can repeat it to restrict recovery to specific campaigns."
        ),
    )
    parser.add_argument("--days-back", type=int, default=30)
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--group-by", default=DEFAULT_GROUP_BY)
    parser.add_argument("--campaign-limit", type=int)
    parser.add_argument("--campaign-batch-size", type=int, default=DEFAULT_CAMPAIGN_BATCH_SIZE)
    parser.add_argument(
        "--max-cpc-batches",
        type=int,
        help="Optional hard cap on CPC batches for one run. Mainly useful for cpc-backfill/manual probes.",
    )
    parser.add_argument(
        "--max-stats-campaigns",
        type=int,
        default=DEFAULT_MAX_STATS_CAMPAIGNS_PER_DAILY_RUN,
        help=(
            "Max CPC campaign units allowed for one run. "
            "1 campaign = 1 statistics/json daily quota unit."
        ),
    )
    parser.add_argument(
        "--campaign-scope",
        choices=("recent", "all"),
        default=DEFAULT_CAMPAIGN_SCOPE,
        help="recent = active or recently updated campaigns in the period; all = all campaigns",
    )
    parser.add_argument(
        "--daily-cpc-selection-mode",
        choices=("complete", "recent"),
        default=OZON_PERFORMANCE_DAILY_CPC_SELECTION_MODE,
        help=(
            "Daily D-1 CPC selection mode: complete = all CPC campaigns overlapping target_date; "
            "recent = only running or updated-in-period campaigns."
        ),
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help=(
            "Planning-only mode: list campaigns, apply local filters, calculate batches/quota, "
            "then exit before any report jobs, polling, downloads, or DB writes."
        ),
    )
    parser.add_argument(
        "--cpo-report-type",
        choices=sorted(ALLOWED_CPO_REPORT_TYPES),
        default="orders",
        help="CPO report type for cpo-report-check mode or plan-only inspection.",
    )
    parser.add_argument(
        "--search-promo-report-type",
        choices=sorted(ALLOWED_SEARCH_PROMO_REPORT_TYPES),
        default="orders",
        help="SEARCH_PROMO report type for search-promo-report-check mode or plan-only inspection.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--approve-cpc-recovery-write", action="store_true")
    parser.add_argument("--ignore-stale-progress-for-date-only", action="store_true")
    parser.add_argument(
        "--allow-recovery-worker-before-daily-status",
        action="store_true",
        help=(
            "Allow cpc-backfill to resume an existing pending progress before today's "
            "daily-yesterday status row exists. Intended only for the recovery worker."
        ),
    )
    parser.add_argument("--existing-report-uuid")
    parser.add_argument("--debug-sample", action="store_true")
    return parser.parse_args()


def parse_iso_date(value):
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    for candidate in (text, text[:10]):
        try:
            return datetime.fromisoformat(candidate.replace("Z", "+00:00")).date()
        except ValueError:
            continue

    return None


def is_running_campaign(campaign):
    return str(campaign.get("state") or "") == "CAMPAIGN_STATE_RUNNING"


def campaign_intersects_period(campaign, date_from, date_to):
    period_from = parse_iso_date(date_from)
    period_to = parse_iso_date(date_to)
    campaign_from = parse_iso_date(campaign.get("fromDate") or campaign.get("dateFrom"))
    campaign_to = parse_iso_date(campaign.get("toDate") or campaign.get("dateTo"))

    if period_from and campaign_to and campaign_to < period_from:
        return False

    if period_to and campaign_from and campaign_from > period_to:
        return False

    return True


def campaign_updated_in_period(campaign, date_from, date_to):
    updated_at = parse_iso_date(campaign.get("updatedAt") or campaign.get("updated_at"))
    period_from = parse_iso_date(date_from)
    period_to = parse_iso_date(date_to)

    if not updated_at:
        return False

    if period_from and updated_at < period_from:
        return False

    if period_to and updated_at > period_to:
        return False

    return True


def campaign_created_in_period(campaign, date_from, date_to):
    created_at = parse_iso_date(campaign.get("createdAt") or campaign.get("created_at"))
    period_from = parse_iso_date(date_from)
    period_to = parse_iso_date(date_to)

    if not created_at:
        return False

    if period_from and created_at < period_from:
        return False

    if period_to and created_at > period_to:
        return False

    return True


def filter_campaigns(campaigns, date_from, date_to, scope):
    intersecting = [
        campaign
        for campaign in campaigns
        if campaign_intersects_period(campaign, date_from, date_to)
    ]

    if scope == "all":
        filtered = intersecting
    else:
        filtered = [
            campaign
            for campaign in intersecting
            if is_running_campaign(campaign) or campaign_updated_in_period(campaign, date_from, date_to)
        ]

    filtered.sort(
        key=lambda campaign: (
            0 if is_running_campaign(campaign) else 1,
            str(campaign.get("updatedAt") or ""),
            str(campaign.get("id") or ""),
        ),
        reverse=False,
    )
    return filtered


def is_cpc_campaign(campaign):
    payment_type = str(campaign.get("PaymentType") or campaign.get("paymentType") or "").upper()
    if payment_type:
        return payment_type == "CPC"

    campaign_text_value = " ".join(
        str(value)
        for key, value in campaign.items()
        if not isinstance(value, (dict, list)) and key not in {"id", "campaignId"}
    ).lower()
    return "cpc" in campaign_text_value or "оплата за клик" in campaign_text_value


def cpc_activity_markers(campaign, date_from, date_to):
    markers = []

    if is_running_campaign(campaign):
        markers.append("running")

    if campaign_updated_in_period(campaign, date_from, date_to):
        markers.append("updated_in_period")

    if campaign_created_in_period(campaign, date_from, date_to):
        markers.append("created_in_period")

    return markers


def filter_cpc_campaigns(campaigns, date_from, date_to, scope):
    filtered = filter_campaigns(campaigns, date_from, date_to, scope)
    cpc_campaigns = []

    for campaign in filtered:
        if not is_cpc_campaign(campaign):
            continue

        markers = cpc_activity_markers(campaign, date_from, date_to)
        if scope != "all" and not markers:
            continue

        prepared = dict(campaign)
        prepared["_cpc_activity_markers"] = markers
        cpc_campaigns.append(prepared)

    return cpc_campaigns


def prepare_cpc_campaign(campaign, date_from, date_to):
    prepared = dict(campaign)
    prepared["_cpc_activity_markers"] = cpc_activity_markers(campaign, date_from, date_to)
    prepared["_running"] = is_running_campaign(campaign)
    prepared["_updated_in_period"] = campaign_updated_in_period(campaign, date_from, date_to)
    prepared["_created_in_period"] = campaign_created_in_period(campaign, date_from, date_to)
    prepared["_overlaps_period"] = campaign_intersects_period(campaign, date_from, date_to)
    return prepared


def daily_cpc_priority_key(campaign):
    running = bool(campaign.get("_running"))
    updated = bool(campaign.get("_updated_in_period"))

    if running and updated:
        priority = 0
    elif running:
        priority = 1
    elif updated:
        priority = 2
    else:
        priority = 3

    updated_at = from_iso(campaign.get("updatedAt") or campaign.get("updated_at"))
    updated_ts = updated_at.timestamp() if updated_at else 0.0
    campaign_id = str(campaign.get("id") or campaign.get("campaignId") or "")
    return (priority, -updated_ts, campaign_id)


def build_daily_cpc_selection(campaigns, date_from, date_to, selection_mode):
    raw_cpc_campaigns = [
        prepare_cpc_campaign(campaign, date_from, date_to)
        for campaign in campaigns
        if is_cpc_campaign(campaign)
    ]
    date_overlap_cpc_campaigns = [
        campaign for campaign in raw_cpc_campaigns if campaign.get("_overlaps_period")
    ]
    recent_cpc_campaigns = filter_cpc_campaigns(campaigns, date_from, date_to, "recent")

    if selection_mode == "recent":
        selected_campaigns = list(recent_cpc_campaigns)
    else:
        selected_campaigns = sorted(date_overlap_cpc_campaigns, key=daily_cpc_priority_key)

    recent_ids = {
        str(campaign.get("id") or campaign.get("campaignId") or "")
        for campaign in recent_cpc_campaigns
        if campaign.get("id") or campaign.get("campaignId")
    }
    overlap_ids = {
        str(campaign.get("id") or campaign.get("campaignId") or "")
        for campaign in date_overlap_cpc_campaigns
        if campaign.get("id") or campaign.get("campaignId")
    }
    excluded_by_recent_filter_count = len(overlap_ids - recent_ids)

    return {
        "raw_cpc_campaigns": raw_cpc_campaigns,
        "date_overlap_cpc_campaigns": date_overlap_cpc_campaigns,
        "recent_cpc_campaigns": recent_cpc_campaigns,
        "selected_campaigns": selected_campaigns,
        "excluded_by_recent_filter_count": excluded_by_recent_filter_count,
    }


def parse_number(value):
    if value is None:
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return 0.0

    text = (
        text.replace("\xa0", " ")
        .replace(" ", "")
        .replace("%", "")
        .replace("₽", "")
        .replace(",", ".")
    )
    text = re.sub(r"[^0-9.\-]", "", text)

    if not text or text in {"-", ".", "-."}:
        return 0.0

    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_percent(value):
    return parse_number(value)


def value_by_keys(row, keys):
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)

    normalized = {
        str(key).lower().replace(" ", "").replace("_", ""): value
        for key, value in row.items()
    }

    for key in keys:
        lookup = str(key).lower().replace(" ", "").replace("_", "")
        if lookup in normalized and normalized[lookup] not in (None, ""):
            return normalized[lookup]

    return None


def normalize_date(value, fallback=None):
    if not value:
        return fallback

    text = str(value).strip()
    if not text:
        return fallback

    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]

    if re.match(r"^\d{2}\.\d{2}\.\d{4}$", text):
        day, month, year = text.split(".")
        return f"{year}-{month}-{day}"

    return fallback


def build_utc_time_bounds(date_from, date_to):
    tz = ZoneInfo(APP_TIMEZONE)
    from_day = datetime.fromisoformat(date_from).date()
    to_day = datetime.fromisoformat(date_to).date()

    local_from = datetime.combine(from_day, datetime.min.time(), tzinfo=tz)
    local_to = datetime.combine(to_day, datetime.max.time(), tzinfo=tz)

    utc_from = local_from.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    utc_to = local_to.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    return utc_from, utc_to


def parse_hhmm(value):
    text = str(value or "").strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not match:
        raise ValueError(f"Invalid HH:MM value: {value}")

    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Invalid HH:MM value: {value}")

    return hour, minute


def ensure_cpc_backfill_window_open():
    # This is an assumed safe reset window for Ozon statistics jobs, pending Ozon confirmation.
    hour, minute = parse_hhmm(CPC_BACKFILL_START_HHMM)
    now_local = local_now()
    window_open = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now_local < window_open:
        raise RuntimeError(
            "cpc-backfill mode is allowed only after "
            f"{CPC_BACKFILL_START_HHMM} {APP_TIMEZONE}. "
            f"Current local time: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )


def resolve_date_range(args):
    if args.date:
        if args.date_from or args.date_to:
            raise RuntimeError("--date нельзя комбинировать с --date-from/--date-to")
        return args.date, args.date

    if args.mode == "daily-yesterday":
        if args.date_from or args.date_to:
            raise RuntimeError(
                "daily-yesterday mode uses target_date = yesterday and should not be combined "
                "with --date-from/--date-to"
            )
        target_date = (today_local() - timedelta(days=1)).isoformat()
        return target_date, target_date

    date_to = args.date_to or today_local().isoformat()
    if args.date_from:
        return args.date_from, date_to

    date_from = (datetime.fromisoformat(date_to).date() - timedelta(days=args.days_back)).isoformat()
    return date_from, date_to


def batch_campaign_units(campaign_batch):
    return len(campaign_batch or [])


def sum_campaign_units_for_batches(cpc_batches, batch_indexes):
    total = 0
    for batch_index in batch_indexes or []:
        if 0 <= int(batch_index) < len(cpc_batches):
            total += batch_campaign_units(cpc_batches[int(batch_index)])
    return total


def build_limited_batch_indexes(cpc_batches, pending_batch_indexes, max_campaign_units):
    if max_campaign_units <= 0:
        return [], 0

    selected = []
    consumed_units = 0

    for batch_index in pending_batch_indexes or []:
        if batch_index < 0 or batch_index >= len(cpc_batches):
            continue
        batch_units = batch_campaign_units(cpc_batches[batch_index])
        if batch_units <= 0:
            continue
        if selected and consumed_units + batch_units > max_campaign_units:
            break
        if not selected and batch_units > max_campaign_units:
            break
        selected.append(batch_index)
        consumed_units += batch_units

    return selected, consumed_units


def can_resume_pending_progress_without_daily_status(progress):
    progress = progress or {}
    pending_batch_indexes = list(progress.get("pending_batch_indexes") or [])
    pending_batches = int(progress.get("pending_batches") or 0)
    return bool(pending_batch_indexes or pending_batches > 0)


def should_allow_cpc_backfill_before_daily_status(args, progress):
    return bool(
        getattr(args, "allow_recovery_worker_before_daily_status", False)
        and can_resume_pending_progress_without_daily_status(progress)
    )


def mask_client_id(value):
    text = str(value or "").strip()
    if not text:
        return "unknown"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"acct_{digest}"


def split_config_values(value):
    text = str(value or "").strip()
    if not text:
        return []
    return [item.strip() for item in re.split(r"[\s,;]+", text) if item.strip()]


def ensure_single_account_config():
    config_values = {
        "OZON_PERFORMANCE_CLIENT_ID": split_config_values(OZON_PERFORMANCE_CLIENT_ID),
        "OZON_CLIENT_ID": split_config_values(OZON_SELLER_CLIENT_ID),
    }

    offenders = {
        name: values
        for name, values in config_values.items()
        if len(values) > 1
    }

    if offenders:
        details = ", ".join(
            f"{name}={[mask_client_id(item) for item in values]}"
            for name, values in offenders.items()
        )
        raise RuntimeError(
            "Обнаружен multi-account Ozon config, а marketplace_expenses работает в single-account режиме: "
            f"{details}. Разделите аккаунты по разным environment/database или добавьте account dimension в БД."
        )


def build_cpo_reconciliation_context(date_from, date_to, uuid, summary):
    return {
        "date_from": date_from,
        "date_to": date_to,
        "account": mask_client_id(OZON_PERFORMANCE_CLIENT_ID),
        "uuid": uuid,
        "total_cpo_expense_from_file": round(float(summary.get("total_cpo_expense_from_file") or 0), 2),
        "distributed_cpo_expense": round(float(summary.get("distributed_cpo_expense") or 0), 2),
        "difference": round(float(summary.get("difference") or 0), 2),
    }


def empty_stage_status(status="not_started", **extra):
    payload = {"status": status}
    payload.update(extra)
    return payload


def normalize_batch_indexes(values):
    normalized = []
    seen = set()

    for value in values or []:
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if index < 0 or index in seen:
            continue
        seen.add(index)
        normalized.append(index)

    normalized.sort()
    return normalized


def deterministic_campaign_id_order(campaign_ids):
    values = []
    seen = set()

    for campaign_id in campaign_ids or []:
        text = str(campaign_id or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        values.append(text)

    values.sort()
    return values


def preserve_campaign_id_order(campaign_ids):
    values = []
    seen = set()

    for campaign_id in campaign_ids or []:
        text = str(campaign_id or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        values.append(text)

    return values


def compute_campaign_list_hash(campaign_ids):
    return payload_hash(list(campaign_ids or []))


def build_cpc_batches(campaign_ids, batch_size):
    if not campaign_ids:
        return []
    size = max(1, int(batch_size or 1))
    return [list(batch) for batch in chunks(list(campaign_ids), size)]


def cpc_progress_cache_identity(date_from, date_to, batch_size, campaign_ids, group_by):
    ordered_campaign_ids = deterministic_campaign_id_order(campaign_ids)
    return {
        "date_from": date_from,
        "date_to": date_to,
        "account_signature": mask_client_id(OZON_PERFORMANCE_CLIENT_ID),
        "batch_size": int(batch_size or 1),
        "campaigns": ordered_campaign_ids,
        "group_by": str(group_by or ""),
    }


def build_cpc_progress_key(date_from, date_to, batch_size, campaign_ids, group_by):
    identity = cpc_progress_cache_identity(date_from, date_to, batch_size, campaign_ids, group_by)
    return f"cpc_progress:{payload_hash(identity)}"


def resolve_existing_cpc_backfill_progress(client, target_date):
    candidates = []
    for progress_key in (client.state.get("cpc_progress", {}) or {}).keys():
        progress = client.get_cpc_progress(progress_key)
        if not progress:
            continue
        if progress.get("date_from") != target_date or progress.get("date_to") != target_date:
            continue
        if not progress.get("pending_batches"):
            continue
        candidates.append((progress_key, progress))

    if not candidates:
        return None, None

    candidates.sort(
        key=lambda item: (
            1 if str(item[1].get("selection_mode") or "") == "complete" else 0,
            int(item[1].get("total_campaigns") or 0),
            int(item[1].get("batch_size") or 0),
            int(item[1].get("pending_batches") or 0),
            str(item[1].get("updated_at") or ""),
        ),
        reverse=True,
    )
    return candidates[0]


def resolve_daily_pending_cpc_progress_from_db(client, target_date):
    try:
        result = (
            supabase
            .table(PIPELINE_RUNTIME_STATE_TABLE)
            .select("state_key,payload,updated_at")
            .eq("account_signature", client.account_signature)
            .eq("state_type", "cpc_progress")
            .order("updated_at", desc=True)
            .execute()
        )
    except Exception as exc:
        print(
            "WARNING: Не удалось прочитать cpc_progress из pipeline_runtime_state. "
            f"Ошибка: {sanitize_text(exc)}"
        )
        return None, None

    candidates = []
    for row in result.data or []:
        payload = row.get("payload") or {}
        if payload.get("date_from") != target_date or payload.get("date_to") != target_date:
            continue
        if str(payload.get("account_signature") or client.account_signature) != str(client.account_signature):
            continue
        if str(payload.get("selection_mode") or "") != "complete":
            continue
        pending_batch_indexes = normalize_batch_indexes(payload.get("pending_batch_indexes"))
        pending_batches = int(payload.get("pending_batches") or len(pending_batch_indexes) or 0)
        if not pending_batch_indexes and pending_batches <= 0:
            continue
        logical_key = parse_db_state_key("cpc_progress", row.get("state_key"))
        if not logical_key:
            continue
        progress = dict(payload)
        progress["updated_at"] = progress.get("updated_at") or row.get("updated_at")
        progress["pending_batch_indexes"] = pending_batch_indexes
        progress["pending_batches"] = pending_batches
        candidates.append((logical_key, progress))

    if len(candidates) != 1:
        return None, None

    return candidates[0]


def resolve_cpc_backfill_progress(client, target_date):
    progress_key, progress = resolve_existing_cpc_backfill_progress(client, target_date)
    if progress:
        return progress_key, progress, "existing_backfill_progress"

    progress_key, progress = resolve_daily_pending_cpc_progress_from_db(client, target_date)
    if progress:
        client.state.setdefault("cpc_progress", {})[progress_key] = progress
        return progress_key, progress, "daily_yesterday_pending"

    return None, None, None


def is_metric_key(key):
    normalized = str(key).lower().replace(" ", "").replace("_", "")
    return any(metric.lower().replace("_", "") == normalized for metric in METRIC_KEYS)


def scalar_dimensions(row):
    result = {}
    for key, value in row.items():
        if isinstance(value, (dict, list)):
            continue
        if is_metric_key(key):
            continue
        result[key] = value
    return result


def flatten_report_rows(data, inherited=None):
    inherited = inherited or {}

    if isinstance(data, dict):
        current = dict(inherited)
        current.update(scalar_dimensions(data))

        if any(key in data for key in SPEND_KEYS) or value_by_keys(data, SPEND_KEYS) is not None:
            row = dict(current)
            for key, value in data.items():
                if not isinstance(value, (dict, list)):
                    row[key] = value
            yield row

        for key, value in data.items():
            if isinstance(value, (dict, list)):
                nested = dict(current)
                if str(key).isdigit() and "campaignId" not in nested:
                    nested["campaignId"] = str(key)
                yield from flatten_report_rows(value, nested)

    elif isinstance(data, list):
        for item in data:
            yield from flatten_report_rows(item, inherited)


def extract_campaign_id(row):
    value = value_by_keys(row, ("campaignId", "campaign_id", "id", "ID кампании"))
    return str(value) if value not in (None, "") else ""


def normalize_sku_value(value):
    if value in (None, ""):
        return ""

    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]

    return text


def extract_sku(row):
    return normalize_sku_value(value_by_keys(row, SKU_KEYS))


def extract_promoted_sku(row):
    return normalize_sku_value(value_by_keys(row, PROMOTED_SKU_KEYS))


def extract_raw_sku(row):
    value = value_by_keys(row, SKU_KEYS)
    return str(value).strip() if value not in (None, "") else ""


def extract_raw_promoted_sku(row):
    value = value_by_keys(row, PROMOTED_SKU_KEYS)
    return str(value).strip() if value not in (None, "") else ""


def extract_article(row):
    value = value_by_keys(row, ARTICLE_KEYS)
    return str(value).strip() if value not in (None, "") else ""


def extract_product_name(row):
    value = value_by_keys(
        row,
        (
            "title",
            "name",
            "productName",
            "product_name",
            "Название",
            "Название товара",
        ),
    )
    return str(value).strip() if value not in (None, "") else ""


def extract_spend(row):
    return abs(parse_number(value_by_keys(row, SPEND_KEYS)))


def extract_clicks(row):
    return parse_number(value_by_keys(row, ("clicks", "Клики")))


def extract_views(row):
    return parse_number(value_by_keys(row, ("views", "shows", "impressions", "Показы")))


def extract_orders(row):
    return parse_number(value_by_keys(row, ("orders", "Заказы")))


def extract_revenue(row):
    return parse_number(value_by_keys(row, REVENUE_KEYS))


def extract_cpo_order_qty(row):
    return parse_number(value_by_keys(row, CPO_ORDER_QTY_KEYS))


def extract_cpo_revenue(row):
    return parse_number(value_by_keys(row, CPO_REVENUE_KEYS))


def campaign_text(row, campaign):
    parts = []
    for source in (campaign, row):
        for value in source.values():
            if isinstance(value, (dict, list)):
                continue
            if value in (None, ""):
                continue
            parts.append(str(value))
    return " ".join(parts).lower()


def classify_by_rate(spend, revenue):
    if spend <= 0 or revenue <= 0:
        return None

    rate = spend / revenue

    if 0.035 <= rate <= 0.075:
        return "advertising_order_5"

    if 0.075 < rate <= 0.14:
        return "advertising_order_10"

    return None


def classify_expense_type(row, campaign):
    campaign_id = extract_campaign_id(row) or str(campaign.get("id") or "")

    for expense_type, campaign_ids in EXPLICIT_CAMPAIGN_TYPES.items():
        if campaign_id and campaign_id in campaign_ids:
            return expense_type

    text = campaign_text(row, campaign)

    if any(marker in text for marker in ("оплата за клик", "за клик", "cost per click", "cpc")):
        return "advertising_clicks"

    if re.search(r"(^|[^0-9])5\s*%", text):
        return "advertising_order_5"

    if re.search(r"(^|[^0-9])10\s*%", text):
        return "advertising_order_10"

    if any(marker in text for marker in ("оплата за заказ", "за заказ", "cost per order", "cpo")):
        by_rate = classify_by_rate(extract_spend(row), extract_revenue(row))
        if by_rate:
            return by_rate
        return "advertising_order_other"

    return "advertising_other"


class OzonPerformanceClient:
    def __init__(self):
        self.token = None
        self.token_expires_at = 0
        self.account_signature = mask_client_id(OZON_PERFORMANCE_CLIENT_ID)
        self.state_backend = self.resolve_state_backend()
        self.state = self.load_state()
        self.migrate_legacy_rate_limit_state()

    def resolve_state_backend(self):
        backend = normalize_state_backend(STATE_BACKEND)
        if backend == "db" and (not SUPABASE_URL or not SUPABASE_SERVICE_KEY):
            print(
                "Ozon Performance runtime state backend fallback: SUPABASE credentials missing, "
                "using file backend."
            )
            return "file"
        return backend

    def load_state(self):
        state = default_state()
        file_state = load_file_state()

        for section in VOLATILE_STATE_SECTIONS:
            state[section] = file_state.get(section, state[section])

        if self.state_backend == "file":
            for section in PERSISTENT_STATE_SECTIONS:
                state[section] = file_state.get(section, state[section])
            return state

        state.update(self.load_persistent_state_from_db())
        return state

    def save_state(self):
        if self.state_backend == "file":
            save_file_state(self.state)
            return

        self.save_persistent_state_to_db()
        file_state = default_state()
        for section in VOLATILE_STATE_SECTIONS:
            file_state[section] = self.state.get(section, file_state[section])
        save_file_state(file_state)

    def cleanup_runtime_state_keys_nonfatal(self, keys, warning_label="runtime_state_stale_cleanup_warning"):
        key_list = [str(key or "").strip() for key in (keys or []) if str(key or "").strip()]
        if not key_list:
            return {"deleted": 0, "failed": 0, "chunks": 0}

        deleted = 0
        failed = 0
        chunks_total = 0
        sample_errors = []

        for chunk_start in range(0, len(key_list), RUNTIME_STATE_STALE_DELETE_CHUNK_SIZE):
            chunk = key_list[chunk_start : chunk_start + RUNTIME_STATE_STALE_DELETE_CHUNK_SIZE]
            chunks_total += 1
            try:
                (
                    supabase
                    .table(PIPELINE_RUNTIME_STATE_TABLE)
                    .delete()
                    .in_("state_key", chunk)
                    .execute()
                )
                deleted += len(chunk)
            except Exception as exc:
                failed += len(chunk)
                if len(sample_errors) < 3:
                    sample_errors.append(
                        {
                            "error_class": exc.__class__.__name__,
                            "message": sanitize_text(exc),
                            "chunk_size": len(chunk),
                        }
                    )

        if failed:
            print(
                json.dumps(
                    {
                        "warning": warning_label,
                        "account_signature": self.account_signature,
                        "deleted_count": deleted,
                        "failed_count": failed,
                        "chunk_count": chunks_total,
                        "sample_errors": sample_errors,
                    },
                    ensure_ascii=False,
                )
            )

        return {"deleted": deleted, "failed": failed, "chunks": chunks_total}

    def load_persistent_state_from_db(self):
        state = {section: {} for section in PERSISTENT_STATE_SECTIONS}

        try:
            result = (
                supabase
                .table(PIPELINE_RUNTIME_STATE_TABLE)
                .select("state_key,state_type,payload,expires_at")
                .eq("account_signature", self.account_signature)
                .in_("state_type", list(PERSISTENT_STATE_SECTIONS))
                .execute()
            )
        except Exception as exc:
            print(
                "Не удалось загрузить Ozon Performance runtime state из БД, "
                f"используем file fallback: {sanitize_text(exc)}"
            )
            file_state = load_file_state()
            for section in PERSISTENT_STATE_SECTIONS:
                state[section] = file_state.get(section, {})
            return state

        rows = result.data or []
        expired_keys = []
        now = utcnow()

        for row in rows:
            expires_at = from_iso(row.get("expires_at"))
            if expires_at and expires_at <= now:
                expired_keys.append(row.get("state_key"))
                continue

            state_type = row.get("state_type")
            state_key = parse_db_state_key(state_type, row.get("state_key"))
            payload = row.get("payload") or {}
            if state_type not in state or not state_key:
                continue
            state[state_type][state_key] = payload

        if expired_keys:
            self.cleanup_runtime_state_keys_nonfatal(
                expired_keys,
                warning_label="runtime_state_stale_cleanup_warning",
            )

        return state

    def save_persistent_state_to_db(self):
        rows = []
        now_iso = to_iso(utcnow())

        for state_type in PERSISTENT_STATE_SECTIONS:
            section = self.state.get(state_type, {}) or {}
            for state_key, payload in section.items():
                expires_at = None
                if state_type == "cooldowns" and isinstance(payload, str):
                    expires_at = payload
                elif state_type == "batch_recommendations" and isinstance(payload, dict):
                    expires_at = payload.get("expires_at")

                row = build_state_row(
                    state_key=build_db_state_key(state_type, state_key),
                    state_type=state_type,
                    payload=payload,
                    account_signature=self.account_signature,
                    expires_at=expires_at,
                )
                row["updated_at"] = now_iso
                rows.append(row)

        if rows:
            try:
                supabase.table(PIPELINE_RUNTIME_STATE_TABLE).upsert(
                    rows,
                    on_conflict="state_key",
                ).execute()
            except Exception as exc:
                raise RuntimeError(
                    "Не удалось записать runtime state в Supabase: "
                    f"{sanitize_text(exc)}"
                ) from exc

        existing_keys = set()
        stale_cleanup_read_failed = None
        try:
            existing = (
                supabase
                .table(PIPELINE_RUNTIME_STATE_TABLE)
                .select("state_key")
                .eq("account_signature", self.account_signature)
                .in_("state_type", list(PERSISTENT_STATE_SECTIONS))
                .execute()
            )
            existing_keys = {row.get("state_key") for row in (existing.data or []) if row.get("state_key")}
        except Exception as exc:
            stale_cleanup_read_failed = exc
            print(
                json.dumps(
                    {
                        "warning": "runtime_state_stale_cleanup_warning",
                        "account_signature": self.account_signature,
                        "deleted_count": 0,
                        "failed_count": 0,
                        "chunk_count": 0,
                        "sample_errors": [
                            {
                                "error_class": exc.__class__.__name__,
                                "message": sanitize_text(exc),
                                "stage": "read_existing_keys",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            )

        current_keys = {row["state_key"] for row in rows}
        keys_to_delete = sorted(existing_keys - current_keys) if not stale_cleanup_read_failed else []

        if keys_to_delete:
            self.cleanup_runtime_state_keys_nonfatal(
                keys_to_delete,
                warning_label="runtime_state_stale_cleanup_warning",
            )

    def snapshot_runtime_state(self):
        return {
            section: copy.deepcopy(self.state.get(section, {}))
            for section in PERSISTENT_STATE_SECTIONS
        }

    def restore_runtime_state(self, snapshot):
        for section in PERSISTENT_STATE_SECTIONS:
            self.state[section] = copy.deepcopy((snapshot or {}).get(section, {}))
        self.save_state()

    def scoped_state_key(self, key):
        return f"{key}:{self.account_signature}"

    def migrate_legacy_rate_limit_state(self):
        changed = False

        for section_name in ("cooldowns", "batch_recommendations"):
            section = self.state.get(section_name, {})
            if "statistics_json" in section:
                section.pop("statistics_json", None)
                changed = True

        if changed:
            self.save_state()

    def get_job_cache_key(self, endpoint, payload):
        cache_identity = {
            "endpoint": endpoint,
            "account_signature": self.account_signature,
            "payload": canonicalize_payload(payload),
        }
        return f"{endpoint}:{payload_hash(cache_identity)}"

    def get_cached_job(self, endpoint, payload):
        key = self.get_job_cache_key(endpoint, payload)
        return key, self.state.get("jobs", {}).get(key)

    def remember_job(self, endpoint, payload, uuid):
        key = self.get_job_cache_key(endpoint, payload)
        self.state.setdefault("jobs", {})[key] = {
            "endpoint": endpoint,
            "account_signature": self.account_signature,
            "payload": canonicalize_payload(payload),
            "payload_hash": key.split(":", 1)[1],
            "uuid": uuid,
            "updated_at": to_iso(utcnow()),
        }
        self.save_state()

    def forget_jobs_by_uuid(self, uuid):
        jobs = self.state.get("jobs", {})
        keys_to_delete = [
            key
            for key, value in jobs.items()
            if str(value.get("uuid") or "") == str(uuid or "")
        ]
        for key in keys_to_delete:
            jobs.pop(key, None)
        if keys_to_delete:
            self.save_state()

    def invalidate_job(self, endpoint, payload=None, uuid=None):
        jobs = self.state.get("jobs", {})
        keys_to_delete = set()

        if payload is not None:
            keys_to_delete.add(self.get_job_cache_key(endpoint, payload))

        if uuid is not None:
            for key, value in jobs.items():
                if str(value.get("uuid") or "") == str(uuid or ""):
                    keys_to_delete.add(key)

        for key in keys_to_delete:
            jobs.pop(key, None)

        if keys_to_delete:
            self.save_state()

    def write_run_status(self, summary):
        run_key = f"{summary.get('date_from')}:{summary.get('date_to')}"
        self.state.setdefault("runs", {})[run_key] = summary
        self.save_state()

    def get_run_status(self, date_from, date_to):
        run_key = f"{date_from}:{date_to}"
        return self.state.get("runs", {}).get(run_key) or {}

    def get_cpc_progress(self, progress_key):
        progress = (self.state.get("cpc_progress", {}) or {}).get(progress_key) or {}
        if not progress:
            return {}
        normalized = dict(progress)
        normalized["completed_batch_indexes"] = normalize_batch_indexes(progress.get("completed_batch_indexes"))
        normalized["pending_batch_indexes"] = normalize_batch_indexes(progress.get("pending_batch_indexes"))
        normalized["failed_429_batch_indexes"] = normalize_batch_indexes(progress.get("failed_429_batch_indexes"))

        total_batches = int(progress.get("total_batches") or 0)
        completed_set = set(normalized["completed_batch_indexes"])
        pending_set = set(normalized["pending_batch_indexes"])
        failed_set = set(normalized["failed_429_batch_indexes"])

        max_index = max(total_batches - 1, -1)
        normalized["completed_batch_indexes"] = [idx for idx in normalized["completed_batch_indexes"] if idx <= max_index]
        normalized["pending_batch_indexes"] = [
            idx for idx in normalized["pending_batch_indexes"]
            if idx <= max_index and idx not in completed_set
        ]
        normalized["failed_429_batch_indexes"] = [
            idx for idx in normalized["failed_429_batch_indexes"]
            if idx <= max_index and idx not in completed_set
        ]

        if total_batches > 0:
            if not normalized["pending_batch_indexes"]:
                normalized["pending_batch_indexes"] = [
                    idx
                    for idx in range(total_batches)
                    if idx not in set(normalized["completed_batch_indexes"])
                ]
            else:
                pending_union = set(normalized["pending_batch_indexes"]) | set(normalized["failed_429_batch_indexes"])
                normalized["pending_batch_indexes"] = sorted(
                    idx for idx in pending_union if idx not in set(normalized["completed_batch_indexes"])
                )

        normalized["completed_batches"] = len(normalized["completed_batch_indexes"])
        normalized["pending_batches"] = len(normalized["pending_batch_indexes"])
        normalized["failed_429_batches"] = len(normalized["failed_429_batch_indexes"])
        next_batch_index = None
        if normalized["pending_batch_indexes"]:
            next_batch_index = normalized["pending_batch_indexes"][0]
        normalized["next_batch_index"] = next_batch_index
        return normalized

    def save_cpc_progress(self, progress_key, progress):
        normalized = self.get_cpc_progress(progress_key) or {}
        normalized.update(progress or {})
        normalized["completed_batch_indexes"] = normalize_batch_indexes(
            normalized.get("completed_batch_indexes")
        )
        normalized["pending_batch_indexes"] = normalize_batch_indexes(
            normalized.get("pending_batch_indexes")
        )
        normalized["failed_429_batch_indexes"] = normalize_batch_indexes(
            normalized.get("failed_429_batch_indexes")
        )

        completed_set = set(normalized["completed_batch_indexes"])
        failed_set = set(normalized["failed_429_batch_indexes"]) - completed_set
        pending_set = (
            set(normalized["pending_batch_indexes"])
            | failed_set
        ) - completed_set

        total_batches = int(normalized.get("total_batches") or 0)
        valid_indexes = {idx for idx in range(total_batches)}
        completed_set &= valid_indexes
        pending_set &= valid_indexes
        failed_set &= valid_indexes

        normalized["completed_batch_indexes"] = sorted(completed_set)
        normalized["pending_batch_indexes"] = sorted(pending_set)
        normalized["failed_429_batch_indexes"] = sorted(failed_set)
        normalized["completed_batches"] = len(normalized["completed_batch_indexes"])
        normalized["pending_batches"] = len(normalized["pending_batch_indexes"])
        normalized["failed_429_batches"] = len(normalized["failed_429_batch_indexes"])
        normalized["next_batch_index"] = (
            normalized["pending_batch_indexes"][0]
            if normalized["pending_batch_indexes"]
            else None
        )
        normalized["updated_at"] = to_iso(utcnow())

        self.state.setdefault("cpc_progress", {})[progress_key] = normalized
        self.save_state()
        return normalized

    def init_cpc_progress(self, progress_key, progress_context, batches):
        existing = (self.state.get("cpc_progress", {}) or {}).get(progress_key)
        if existing:
            return self.get_cpc_progress(progress_key)

        total_batches = len(batches)
        progress = {
            "date_from": progress_context["date_from"],
            "date_to": progress_context["date_to"],
            "account_signature": self.account_signature,
            "group_by": progress_context["group_by"],
            "batch_size": progress_context["batch_size"],
            "campaign_hash": progress_context["campaign_hash"],
            "campaign_list_hash": progress_context.get("campaign_list_hash"),
            "total_campaigns": progress_context["total_campaigns"],
            "ordered_campaign_ids": list(progress_context.get("ordered_campaign_ids") or []),
            "selection_mode": progress_context.get("selection_mode"),
            "campaign_scope": progress_context.get("campaign_scope"),
            "total_batches": total_batches,
            "completed_batch_indexes": [],
            "pending_batch_indexes": list(range(total_batches)),
            "failed_429_batch_indexes": [],
        }
        return self.save_cpc_progress(progress_key, progress)

    def mark_cpc_batch_completed(self, progress_key, batch_index):
        progress = self.get_cpc_progress(progress_key)
        completed = set(progress.get("completed_batch_indexes") or [])
        pending = set(progress.get("pending_batch_indexes") or [])
        failed = set(progress.get("failed_429_batch_indexes") or [])
        completed.add(int(batch_index))
        pending.discard(int(batch_index))
        failed.discard(int(batch_index))
        return self.save_cpc_progress(
            progress_key,
            {
                "completed_batch_indexes": sorted(completed),
                "pending_batch_indexes": sorted(pending),
                "failed_429_batch_indexes": sorted(failed),
            },
        )

    def mark_cpc_batch_pending_429(self, progress_key, batch_index):
        progress = self.get_cpc_progress(progress_key)
        pending = set(progress.get("pending_batch_indexes") or [])
        failed = set(progress.get("failed_429_batch_indexes") or [])
        pending.add(int(batch_index))
        failed.add(int(batch_index))
        return self.save_cpc_progress(
            progress_key,
            {
                "pending_batch_indexes": sorted(pending),
                "failed_429_batch_indexes": sorted(failed),
            },
        )

    def build_cpc_progress_context(self, date_from, date_to, batch_size, campaign_ids, group_by, selection_mode=None, campaign_scope=None):
        identity = cpc_progress_cache_identity(date_from, date_to, batch_size, campaign_ids, group_by)
        ordered_campaign_ids = list(identity["campaigns"])
        campaign_list_hash = compute_campaign_list_hash(ordered_campaign_ids)
        return {
            "date_from": date_from,
            "date_to": date_to,
            "group_by": str(group_by or ""),
            "batch_size": int(batch_size or 1),
            "campaign_hash": campaign_list_hash,
            "campaign_list_hash": campaign_list_hash,
            "ordered_campaign_ids": ordered_campaign_ids,
            "total_campaigns": len(ordered_campaign_ids),
            "account_signature": identity["account_signature"],
            "selection_mode": selection_mode,
            "campaign_scope": campaign_scope,
        }

    def set_cooldown(self, key, cooldown_until):
        self.state.setdefault("cooldowns", {})[key] = cooldown_until
        self.save_state()

    def get_cooldown(self, key):
        value = self.state.get("cooldowns", {}).get(key)
        if not value:
            return None

        cooldown_until = from_iso(value)
        if not cooldown_until:
            return None

        if cooldown_until <= utcnow():
            self.state.get("cooldowns", {}).pop(key, None)
            self.save_state()
            return None

        return cooldown_until

    def set_batch_recommendation(self, key, value, ttl_seconds=None):
        expires_at = None
        if ttl_seconds:
            expires_at = to_iso(utcnow() + timedelta(seconds=ttl_seconds))
        self.state.setdefault("batch_recommendations", {})[key] = {
            "value": value,
            "expires_at": expires_at,
        }
        self.save_state()

    def get_batch_recommendation(self, key, default_value):
        item = self.state.get("batch_recommendations", {}).get(key)
        if item in (None, ""):
            return default_value
        if isinstance(item, dict):
            expires_at = from_iso(item.get("expires_at"))
            if expires_at and expires_at <= utcnow():
                self.state.get("batch_recommendations", {}).pop(key, None)
                self.save_state()
                return default_value
            value = item.get("value")
        else:
            value = item
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default_value

    def clear_batch_recommendation(self, key):
        if key in self.state.get("batch_recommendations", {}):
            self.state["batch_recommendations"].pop(key, None)
            self.save_state()

    def record_request_event(self, method, endpoint, response=None, extra=None):
        history = self.state.setdefault("request_history", [])
        event = {
            "timestamp": to_iso(utcnow()),
            "account_signature": self.account_signature,
            "method": str(method or "").upper(),
            "endpoint": endpoint,
            "request_kind": infer_request_kind(method, endpoint),
        }

        if response is not None:
            event["status"] = int(response.status_code)

        extra = extra or {}
        if "retry_after_seconds" in extra:
            event["retry_after_seconds"] = extra.get("retry_after_seconds")
        if "cooldown_until" in extra:
            event["cooldown_until"] = extra.get("cooldown_until")

        history.append(event)
        self.state["request_history"] = prune_request_history(history)
        self.save_state()

    def ensure_token(self):
        if self.token and time.time() < self.token_expires_at - 60:
            return self.token

        if not OZON_PERFORMANCE_CLIENT_ID or not OZON_PERFORMANCE_CLIENT_SECRET:
            raise RuntimeError(
                "Не заполнены OZON_PERFORMANCE_CLIENT_ID и OZON_PERFORMANCE_CLIENT_SECRET",
            )

        url = f"{OZON_PERFORMANCE_BASE_URL}/api/client/token"
        payload = {
            "client_id": OZON_PERFORMANCE_CLIENT_ID,
            "client_secret": OZON_PERFORMANCE_CLIENT_SECRET,
            "grant_type": "client_credentials",
        }

        response = requests.post(
            url,
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=60,
        )
        response.raise_for_status()

        data = response.json()
        self.token = data["access_token"]
        self.token_expires_at = time.time() + int(data.get("expires_in") or 1800)
        return self.token

    def request(self, method, path, retry_profile="default", cooldown_key=None, **kwargs):
        endpoint = path
        if str(path).startswith("http://") or str(path).startswith("https://"):
            url = str(path)
        else:
            url = f"{OZON_PERFORMANCE_BASE_URL}{path}"
        headers = kwargs.pop("headers", {})
        headers.update({
            "Authorization": f"Bearer {self.ensure_token()}",
            "Accept": "application/json",
        })
        profile = REQUEST_PROFILES[retry_profile]

        if cooldown_key:
            cooldown_until = self.get_cooldown(cooldown_key)
            if cooldown_until:
                retry_after_seconds = max(0, int((cooldown_until - utcnow()).total_seconds()))
                raise RateLimitPending(
                    endpoint=endpoint,
                    retry_after_seconds=retry_after_seconds,
                    cooldown_until=to_iso(cooldown_until),
                    attempt=0,
                    message=(
                        f"Circuit breaker active for {endpoint}: "
                        f"retry_after={retry_after_seconds}, cooldown_until={to_iso(cooldown_until)}"
                    ),
                )

        for attempt in range(1, int(profile["max_attempts"]) + 1):
            response = requests.request(
                method,
                url,
                headers=headers,
                timeout=120,
                **kwargs,
            )

            if response.status_code == 401 and attempt == 1:
                log_http_response(endpoint, attempt, response, extra={"reason": "refresh_token"})
                self.token = None
                headers["Authorization"] = f"Bearer {self.ensure_token()}"
                continue

            if response.status_code == 429:
                retry_after_header = response.headers.get("Retry-After")
                retry_after_seconds = parse_retry_after_seconds(retry_after_header)
                if retry_after_seconds is None:
                    retry_after_seconds = compute_backoff_seconds(
                        int(profile["base_sleep_seconds"]),
                        int(profile["cap_sleep_seconds"]),
                        attempt,
                    )

                cooldown_until = utcnow() + timedelta(
                    seconds=max(retry_after_seconds, int(profile.get("cooldown_seconds") or 0))
                )
                cooldown_until_iso = to_iso(cooldown_until)
                log_http_response(
                    endpoint,
                    attempt,
                    response,
                    extra={
                        "retry_after": retry_after_header,
                        "retry_after_seconds": retry_after_seconds,
                        "cooldown_until": cooldown_until_iso,
                        "retry_profile": retry_profile,
                    },
                )
                self.record_request_event(
                    method,
                    endpoint,
                    response=response,
                    extra={
                        "attempt": attempt,
                        "retry_profile": retry_profile,
                        "retry_after_seconds": retry_after_seconds,
                        "cooldown_until": cooldown_until_iso,
                    },
                )

                if cooldown_key:
                    self.set_cooldown(cooldown_key, cooldown_until_iso)

                if profile.get("fail_fast_on_429"):
                    raise RateLimitPending(
                        endpoint=endpoint,
                        retry_after_seconds=retry_after_seconds,
                        cooldown_until=cooldown_until_iso,
                        attempt=attempt,
                    )

                if attempt >= int(profile["max_attempts"]):
                    raise RateLimitPending(
                        endpoint=endpoint,
                        retry_after_seconds=retry_after_seconds,
                        cooldown_until=cooldown_until_iso,
                        attempt=attempt,
                    )

                print(
                    f"Ozon Performance 429 for {endpoint}, attempt {attempt}, "
                    f"Retry-After={retry_after_header}, cooldown_until={cooldown_until_iso}, "
                    f"sleep={retry_after_seconds}"
                )
                time.sleep(retry_after_seconds)
                continue

            log_http_response(endpoint, attempt, response, extra={"retry_profile": retry_profile})
            self.record_request_event(
                method,
                endpoint,
                response=response,
                extra={
                    "attempt": attempt,
                    "retry_profile": retry_profile,
                },
            )
            response.raise_for_status()
            return response

        print(f"Ozon Performance exhausted attempts for {endpoint}")
        response.raise_for_status()
        return response

    def list_campaigns(self):
        campaigns = []
        seen_ids = set()

        for adv_object_type in ADV_OBJECT_TYPES or ["SKU"]:
            response = self.request(
                "GET",
                "/api/client/campaign",
                params={"advObjectType": adv_object_type},
            )
            data = response.json()
            batch = data.get("list") or data.get("campaigns") or data.get("items") or []

            for campaign in batch:
                campaign_id = str(campaign.get("id") or campaign.get("campaignId") or "")
                if not campaign_id or campaign_id in seen_ids:
                    continue
                seen_ids.add(campaign_id)
                campaigns.append(campaign)

        return campaigns

    def request_statistics(self, campaign_ids, date_from, date_to, group_by, force_new=False):
        payload = {
            "campaigns": [str(campaign_id) for campaign_id in campaign_ids],
            "dateFrom": date_from,
            "dateTo": date_to,
            "groupBy": group_by,
        }

        _, cached_job = self.get_cached_job("/api/client/statistics/json", payload)
        if not force_new and cached_job and cached_job.get("uuid"):
            print(
                "Используем кэшированный statistics/json job: "
                f"UUID={cached_job['uuid']} payload_hash={cached_job.get('payload_hash')}"
            )
            return cached_job["uuid"], True, payload

        response = self.request(
            "POST",
            "/api/client/statistics/json",
            retry_profile="statistics_json",
            cooldown_key=self.scoped_state_key("statistics_json"),
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        data = response.json()
        uuid = data.get("UUID") or data.get("uuid")

        if not uuid:
            raise RuntimeError(f"Ozon Performance не вернул UUID: {data}")

        self.remember_job("/api/client/statistics/json", payload, uuid)

        return uuid, False, payload

    def request_all_sku_promo_report(self, report_type, date_from, date_to, force_new=False):
        if report_type not in ALLOWED_CPO_REPORT_TYPES:
            raise ValueError(
                f"Недопустимый CPO report_type: {report_type}. "
                f"Разрешено только: {sorted(ALLOWED_CPO_REPORT_TYPES)}"
            )

        utc_from, utc_to = build_utc_time_bounds(date_from, date_to)
        print(
            f"CPO {report_type} timeBounds ({APP_TIMEZONE}, inclusive): "
            f"{date_from}..{date_to} -> {utc_from}..{utc_to}"
        )
        payload = {
            "timeBounds.from": utc_from,
            "timeBounds.to": utc_to,
        }

        endpoint = f"/api/client/statistics/all_sku_promo/{report_type}/generate"
        _, cached_job = self.get_cached_job(endpoint, payload)
        if not force_new and cached_job and cached_job.get("uuid"):
            print(
                "Используем кэшированный all_sku_promo job: "
                f"UUID={cached_job['uuid']} payload_hash={cached_job.get('payload_hash')}"
            )
            return cached_job["uuid"], True, payload, endpoint

        response = self.request(
            "GET",
            endpoint,
            params=payload,
        )
        data = response.json()
        uuid = data.get("UUID") or data.get("uuid")

        if not uuid:
            raise RuntimeError(f"Ozon all_sku_promo не вернул UUID: {data}")

        self.remember_job(endpoint, payload, uuid)

        return uuid, False, payload, endpoint

    def request_search_promo_report(
        self,
        report_type,
        date_from,
        date_to,
        campaign_id=None,
        force_new=False,
    ):
        if report_type not in ALLOWED_SEARCH_PROMO_REPORT_TYPES:
            raise ValueError(
                f"Недопустимый SEARCH_PROMO report_type: {report_type}. "
                f"Разрешено только: {sorted(ALLOWED_SEARCH_PROMO_REPORT_TYPES)}"
            )

        utc_from, utc_to = build_utc_time_bounds(date_from, date_to)
        print(
            f"SEARCH_PROMO {report_type} timeBounds ({APP_TIMEZONE}, inclusive): "
            f"{date_from}..{date_to} -> {utc_from}..{utc_to}"
        )
        payload = {
            "timeBounds.from": utc_from,
            "timeBounds.to": utc_to,
        }
        if campaign_id:
            payload["campaignId"] = str(campaign_id)

        endpoint = SEARCH_PROMO_REPORT_ENDPOINTS[report_type]
        _, cached_job = self.get_cached_job(endpoint, payload)
        if not force_new and cached_job and cached_job.get("uuid"):
            print(
                "Используем кэшированный search_promo job: "
                f"UUID={cached_job['uuid']} payload_hash={cached_job.get('payload_hash')}"
            )
            return cached_job["uuid"], True, payload, endpoint

        response = self.request(
            "GET",
            endpoint,
            params=payload,
        )
        data = response.json()
        uuid = data.get("UUID") or data.get("uuid")

        if not uuid:
            raise RuntimeError(f"Ozon search_promo не вернул UUID: {data}")

        self.remember_job(endpoint, payload, uuid)

        return uuid, False, payload, endpoint

    def wait_statistics(self, uuid, poll_profile="default"):
        profile = POLL_PROFILES[poll_profile]

        for attempt in range(1, int(profile["max_attempts"]) + 1):
            response = self.request("GET", f"/api/client/statistics/{uuid}")
            data = response.json()
            state = str(data.get("state") or data.get("status") or "").upper()

            if state in {"OK", "SUCCESS", "DONE", "COMPLETED", "READY"}:
                return data

            if state in {"ERROR", "FAILED", "FAIL"}:
                self.forget_jobs_by_uuid(uuid)
                raise RuntimeError(f"Ozon Performance report failed: {data}")

            sleep_seconds = min(
                int(profile["cap_sleep_seconds"]),
                int(profile["base_sleep_seconds"]) * (2 ** max(attempt - 1, 0)),
            )
            print(
                f"Ozon Performance polling UUID={uuid} state={state or 'PENDING'} "
                f"attempt={attempt} sleep={sleep_seconds} profile={poll_profile}"
            )
            time.sleep(sleep_seconds)

        self.forget_jobs_by_uuid(uuid)
        raise TimeoutError(f"Ozon Performance report timeout: {uuid}")

    def download_report(self, uuid, return_meta=False):
        response = self.request(
            "GET",
            "/api/client/statistics/report",
            params={"UUID": uuid},
        )

        text = response.text.strip()
        if not text:
            if return_meta:
                return {}, sanitize_value(dict(response.headers))
            return {}

        try:
            data = response.json()
        except ValueError:
            data = json.loads(text)

        if return_meta:
            return data, sanitize_value(dict(response.headers))
        return data

    def download_report_by_link(self, link, uuid=None, return_meta=False):
        if link:
            response = self.request("GET", urljoin(OZON_PERFORMANCE_BASE_URL + "/", link))
            text = response.text.lstrip("\ufeff")
            if text:
                if return_meta:
                    return text, sanitize_value(dict(response.headers))
                return text

        response = self.request(
            "GET",
            "/api/client/statistics/report",
            params={"UUID": uuid},
        )
        text = response.text.lstrip("\ufeff")
        if return_meta:
            return text, sanitize_value(dict(response.headers))
        return text

    def fetch_statistics_json_report(
        self,
        campaign_ids,
        date_from,
        date_to,
        group_by,
        allow_recreate=True,
        return_meta=False,
    ):
        endpoint = "/api/client/statistics/json"
        last_exc = None

        for attempt in range(1, 3):
            uuid, cache_hit, payload = self.request_statistics(
                campaign_ids,
                date_from,
                date_to,
                group_by,
                force_new=(attempt == 2),
            )
            status = self.wait_statistics(uuid, poll_profile="statistics_json")

            try:
                if return_meta:
                    report_data, download_headers = self.download_report(uuid, return_meta=True)
                    return uuid, status, report_data, download_headers
                report_data = self.download_report(uuid)
                return uuid, status, report_data
            except requests.HTTPError as exc:
                status_code = getattr(exc.response, "status_code", None)
                if cache_hit and status_code in {403, 404} and allow_recreate:
                    print(
                        f"Cached statistics/json UUID invalidated: uuid={uuid}, "
                        f"status={status_code}, recreating report"
                    )
                    self.invalidate_job(endpoint, payload=payload, uuid=uuid)
                    last_exc = exc
                    continue
                raise

        if last_exc:
            raise last_exc

        raise RuntimeError("Не удалось получить statistics/json report")

    def fetch_all_sku_promo_csv(self, report_type, date_from, date_to, return_meta=False):
        last_exc = None

        for attempt in range(1, 3):
            uuid, cache_hit, payload, endpoint = self.request_all_sku_promo_report(
                report_type,
                date_from,
                date_to,
                force_new=(attempt == 2),
            )
            status = self.wait_statistics(uuid, poll_profile="all_sku_promo")

            try:
                if return_meta:
                    csv_text, download_headers = self.download_report_by_link(
                        status.get("link"),
                        uuid=uuid,
                        return_meta=True,
                    )
                    return uuid, status, csv_text, download_headers
                csv_text = self.download_report_by_link(status.get("link"), uuid=uuid)
                return uuid, status, csv_text
            except requests.HTTPError as exc:
                status_code = getattr(exc.response, "status_code", None)
                if cache_hit and status_code in {403, 404}:
                    print(
                        f"Cached all_sku_promo UUID invalidated: uuid={uuid}, "
                        f"status={status_code}, recreating report"
                    )
                    self.invalidate_job(endpoint, payload=payload, uuid=uuid)
                    last_exc = exc
                    continue
                raise

        if last_exc:
            raise last_exc

        raise RuntimeError("Не удалось получить all_sku_promo report")

    def fetch_search_promo_csv(
        self,
        report_type,
        date_from,
        date_to,
        campaign_id=None,
        return_meta=False,
    ):
        last_exc = None

        for attempt in range(1, 3):
            uuid, cache_hit, payload, endpoint = self.request_search_promo_report(
                report_type,
                date_from,
                date_to,
                campaign_id=campaign_id,
                force_new=(attempt == 2),
            )
            status = self.wait_statistics(uuid, poll_profile="all_sku_promo")

            try:
                if return_meta:
                    csv_text, download_headers = self.download_report_by_link(
                        status.get("link"),
                        uuid=uuid,
                        return_meta=True,
                    )
                    return uuid, status, csv_text, download_headers
                csv_text = self.download_report_by_link(status.get("link"), uuid=uuid)
                return uuid, status, csv_text
            except requests.HTTPError as exc:
                status_code = getattr(exc.response, "status_code", None)
                if cache_hit and status_code in {403, 404}:
                    print(
                        f"Cached search_promo UUID invalidated: uuid={uuid}, "
                        f"status={status_code}, recreating report"
                    )
                    self.invalidate_job(endpoint, payload=payload, uuid=uuid)
                    last_exc = exc
                    continue
                raise

        if last_exc:
            raise last_exc

        raise RuntimeError("Не удалось получить search_promo report")

    def fetch_search_promo_orders_csv(
        self,
        date,
        plan_only=False,
        dry_run=True,
        write=False,
        max_polls=12,
        poll_interval_sec=10,
        schema_applied=False,
        db_client=None,
    ):
        del max_polls
        del poll_interval_sec

        utc_from, utc_to = build_utc_time_bounds(date, date)
        payload = {
            "from": utc_from,
            "to": utc_to,
        }
        classification = {
            **build_search_promo_selected_cpo_classification(),
        }
        plan = {
            "endpoint": SEARCH_PROMO_ORGANISATION_ORDERS_SUBMIT_ENDPOINT,
            "method": "POST",
            "payload": payload,
            "status_endpoint": "/api/client/statistics/{UUID}",
            "download_endpoint": "/api/client/statistics/report?UUID={UUID}",
            "classification": classification,
            "target_table": SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE,
            "expected_parser_behavior": {
                "delimiter": ";",
                "skip_preamble": True,
                "detect_header": ["Дата", "SKU", "Расход"],
                "exclude_total_row": "Всего",
                "spend_sum_basis": "data_rows_excluding_total_rows",
            },
            "db_writes": 0,
            "writes_marketplace_expenses": False,
            "writes_ozon_daily_sku_ad_attribution": False,
            "used_statistics_json": False,
            "used_general_statistics_submit": False,
            "safe_write_blockers": build_selected_cpo_would_write_summary([], {}).copy(),
        }

        if plan_only:
            return {
                "mode": "search_promo_organisation_orders_plan",
                "date": date,
                "plan": plan,
            }

        if write:
            if not schema_applied:
                raise SelectedCpoSchemaNotAppliedError(
                    f"{SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE} migration is not applied in this environment. "
                    "Apply the dedicated schema artifact in a separate approved task before live write=True."
                )
            if db_client is None:
                raise SelectedCpoDbMappingError(
                    "write=True requires an explicit DB client in this guarded path to avoid accidental live writes."
                )
            if not dry_run:
                raise RuntimeError("write=True without dry_run is not allowed in this task")

        if not dry_run and not write:
            raise RuntimeError("fetch_search_promo_orders_csv currently supports only plan_only, dry_run, or guarded write")

        if not dry_run and write:
            raise RuntimeError("write=True live execution is not allowed in this task")

        if not dry_run:
            raise RuntimeError("fetch_search_promo_orders_csv currently supports only plan_only or dry_run mode")

        response = self.request(
            "POST",
            SEARCH_PROMO_ORGANISATION_ORDERS_SUBMIT_ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        data = response.json()
        uuid = data.get("UUID") or data.get("uuid")
        if not uuid:
            raise RuntimeError(f"Ozon search_promo organisation orders did not return UUID: {data}")

        status = self.wait_statistics(uuid, poll_profile="all_sku_promo")
        csv_text, download_headers = self.download_report_by_link(
            status.get("link"),
            uuid=uuid,
            return_meta=True,
        )
        parsed = parse_search_promo_organisation_orders_csv(csv_text)
        normalized_rows = normalize_search_promo_selected_cpo_rows(
            parsed,
            source_uuid=uuid,
            source_kind=str(status.get("kind") or SEARCH_PROMO_ORGANISATION_ORDERS_KIND),
        )
        source_table_rows = build_selected_cpo_source_table_rows(normalized_rows)
        aggregation = aggregate_search_promo_selected_cpo_rows(normalized_rows, parsed)
        would_write = build_selected_cpo_would_write_summary(normalized_rows, aggregation)

        db_writes = 0
        if write and schema_applied:
            db_writes = upsert_selected_cpo_source_rows(db_client, source_table_rows)

        return {
            "mode": "search_promo_organisation_orders_dry_run" if not write else "search_promo_organisation_orders_write",
            "date": date,
            "uuid": uuid,
            "status": status,
            "download_headers": download_headers,
            "classification": classification,
            "parsed": parsed,
            "normalized_rows": normalized_rows,
            "source_table_rows": source_table_rows,
            "aggregation": aggregation,
            "would_write": would_write,
            "target_table": SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE,
            "db_writes": db_writes,
            "writes_marketplace_expenses": False,
            "writes_ozon_daily_sku_ad_attribution": False,
            "used_statistics_json": False,
            "used_general_statistics_submit": False,
        }

    def selected_cpo_downstream_dry_run(
        self,
        date,
        write=False,
        approve_downstream_write=False,
        db_client=None,
        source_rows=None,
    ):
        del self

        if write:
            if not approve_downstream_write:
                raise SelectedCpoDownstreamWriteNotApprovedError(
                    "selected CPO downstream write requires explicit approve_downstream_write=True"
                )

        client = db_client or supabase
        rows = copy.deepcopy(source_rows) if source_rows is not None else load_selected_cpo_source_rows(date, db_client=client)
        marketplace_expenses_rows = build_selected_cpo_marketplace_expenses_rows(rows)
        ad_attribution_rows = build_selected_cpo_ad_attribution_rows(rows)
        summary = build_selected_cpo_downstream_would_write_summary(rows)
        marketplace_expenses_writes = 0
        ozon_daily_sku_ad_attribution_writes = 0

        if write:
            marketplace_expenses_writes = upsert_selected_cpo_marketplace_expenses_rows(client, marketplace_expenses_rows)
            ozon_daily_sku_ad_attribution_writes = upsert_selected_cpo_ad_attribution_rows(client, ad_attribution_rows)

        return {
            "mode": "selected_cpo_downstream_write" if write else "selected_cpo_downstream_dry_run",
            "date": date,
            "source_table": SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE,
            "source_row_count": len(rows),
            "source_sum_spend": round(sum(float(row.get("spend") or 0) for row in rows), 2),
            "marketplace_expenses_rows": marketplace_expenses_rows,
            "marketplace_expenses_total": round(
                sum(float(row.get("expense_amount") or 0) for row in marketplace_expenses_rows),
                2,
            ),
            "ad_attribution_rows": ad_attribution_rows,
            "ad_attribution_total_spend": round(sum(float(row.get("ad_spend") or 0) for row in ad_attribution_rows), 2),
            "would_write": summary,
            "db_writes": marketplace_expenses_writes + ozon_daily_sku_ad_attribution_writes,
            "marketplace_expenses_writes": marketplace_expenses_writes,
            "ozon_daily_sku_ad_attribution_writes": ozon_daily_sku_ad_attribution_writes,
            "used_statistics_json": False,
            "used_general_statistics_submit": False,
        }

    def load_ozon_selected_cpo_for_date(
        self,
        date,
        write=False,
        dry_run=True,
        approve_write=False,
        enabled=None,
        db_client=None,
        skip_write_if_not_approved=False,
    ):
        if enabled is None:
            enabled = ENABLE_OZON_SELECTED_CPO_DAILY

        summary = {
            "selected_cpo_enabled": bool(enabled),
            "write_approved": bool(approve_write),
            "date": date,
            "source_rows": 0,
            "source_sum": 0.0,
            "marketplace_expenses_rows": 0,
            "marketplace_expenses_sum": 0.0,
            "ad_attribution_rows": 0,
            "ad_attribution_sum": 0.0,
            "totals_match": False,
            "db_writes": 0,
            "marketplace_expenses_writes": 0,
            "ozon_daily_sku_ad_attribution_writes": 0,
            "used_statistics_json": False,
            "used_general_statistics_submit": False,
            "status": "skipped",
            "reason": "feature_flag_disabled" if not enabled else None,
        }

        if not enabled:
            return summary

        if write and not approve_write:
            if not skip_write_if_not_approved:
                raise SelectedCpoDownstreamWriteNotApprovedError(
                    "selected CPO daily integration requires explicit approve_write=True"
                )
            write = False
            dry_run = True
            summary["status"] = "dry_run_no_write"
            summary["reason"] = "write_not_approved"

        client = db_client or supabase
        if dry_run and not write:
            source_rows = load_selected_cpo_source_rows(date, db_client=client)
            source_summary = {
                "mode": "selected_cpo_source_table_dry_run",
                "date": date,
                "source_table_rows": copy.deepcopy(source_rows),
                "aggregation": {
                    "total_spend_data_rows": round(
                        sum(float(row.get("spend") or 0) for row in source_rows),
                        2,
                    )
                },
                "db_writes": 0,
                "used_statistics_json": False,
                "used_general_statistics_submit": False,
            }
        else:
            source_summary = self.fetch_search_promo_orders_csv(
                date=date,
                dry_run=True,
                write=write,
                schema_applied=bool(write),
                db_client=client if write else None,
            )
        downstream_summary = self.selected_cpo_downstream_dry_run(
            date=date,
            write=write,
            approve_downstream_write=approve_write,
            db_client=client if write else None,
            source_rows=source_summary.get("source_table_rows"),
        )

        source_sum = round(float((source_summary.get("aggregation") or {}).get("total_spend_data_rows") or 0), 2)
        marketplace_sum = round(float(downstream_summary.get("marketplace_expenses_total") or 0), 2)
        attribution_sum = round(float(downstream_summary.get("ad_attribution_total_spend") or 0), 2)

        summary.update(
            {
                "source_rows": len(source_summary.get("source_table_rows") or []),
                "source_sum": source_sum,
                "marketplace_expenses_rows": len(downstream_summary.get("marketplace_expenses_rows") or []),
                "marketplace_expenses_sum": marketplace_sum,
                "ad_attribution_rows": len(downstream_summary.get("ad_attribution_rows") or []),
                "ad_attribution_sum": attribution_sum,
                "totals_match": source_sum == marketplace_sum == attribution_sum,
                "db_writes": int(source_summary.get("db_writes") or 0) + int(downstream_summary.get("db_writes") or 0),
                "marketplace_expenses_writes": int(downstream_summary.get("marketplace_expenses_writes") or 0),
                "ozon_daily_sku_ad_attribution_writes": int(
                    downstream_summary.get("ozon_daily_sku_ad_attribution_writes") or 0
                ),
                "used_statistics_json": False,
                "used_general_statistics_submit": False,
                "status": (
                    "dry_run_no_write"
                    if (not write and summary.get("reason") == "write_not_approved")
                    else ("success" if not write else "written")
                ),
                "reason": summary.get("reason"),
                "source_summary": source_summary,
                "downstream_summary": downstream_summary,
            }
        )
        return summary


def load_catalog():
    rows = []
    start = 0
    page_size = 1000

    while True:
        try:
            result = (
                supabase
                .table("sku_catalog")
                .select("marketplace_sku,article,product_name")
                .eq("marketplace_code", "ozon")
                .range(start, start + page_size - 1)
                .execute()
            )
        except Exception as exc:
            print(f"Не удалось загрузить sku_catalog Ozon: {exc}")
            return {}

        batch = result.data or []
        rows.extend(batch)

        if len(batch) < page_size:
            break

        start += page_size

    return {str(row.get("marketplace_sku")): row for row in rows}


def build_rows(report_data, campaigns_by_id, date_from):
    grouped = {}
    counters = defaultdict(int)

    for raw_row in flatten_report_rows(report_data):
        sku = extract_sku(raw_row)
        spend = extract_spend(raw_row)

        if not sku:
            counters["without_sku"] += 1
            continue

        if spend == 0:
            counters["zero_spend"] += 1
            continue

        campaign_id = extract_campaign_id(raw_row)
        campaign = campaigns_by_id.get(campaign_id, {})
        expense_type = classify_expense_type(raw_row, campaign)

        expense_date = normalize_date(value_by_keys(raw_row, DATE_KEYS), fallback=date_from)
        if not expense_date:
            counters["without_date"] += 1
            continue

        key = (expense_date, sku, expense_type)
        if key not in grouped:
            grouped[key] = {
                "expense_date": expense_date,
                "marketplace_code": "ozon",
                "marketplace_sku": sku,
                "article": extract_article(raw_row),
                "expense_type": expense_type,
                "expense_amount": 0,
            }

        grouped[key]["expense_amount"] += spend
        counters[expense_type] += 1

    return list(grouped.values()), counters


def build_cpc_attribution_rows(report_data, date_from):
    grouped = {}
    counters = defaultdict(int)

    for raw_row in flatten_report_rows(report_data):
        sale_date = normalize_date(value_by_keys(raw_row, DATE_KEYS), fallback=date_from)
        sku = extract_sku(raw_row)

        if not sale_date:
            counters["without_date"] += 1
            continue

        if not sku:
            counters["without_sku"] += 1
            continue

        ad_orders_qty = extract_orders(raw_row)
        ad_orders_revenue = extract_revenue(raw_row)
        ad_clicks = extract_clicks(raw_row)
        ad_views = extract_views(raw_row)
        ad_spend = extract_spend(raw_row)

        if all(value == 0 for value in (ad_orders_qty, ad_orders_revenue, ad_clicks, ad_views, ad_spend)):
            counters["zero_metrics"] += 1
            continue

        campaign_id = extract_campaign_id(raw_row)
        key = (sale_date, sku, "cpc", campaign_id)
        if key not in grouped:
            grouped[key] = {
                "sale_date": sale_date,
                "marketplace_code": "ozon",
                "marketplace_sku": sku,
                "order_sku": sku,
                "promoted_sku": "",
                "promoted_article": None,
                "raw_sku": extract_raw_sku(raw_row),
                "raw_promoted_sku": "",
                "ad_source": "cpc",
                "attribution_type": "direct",
                "campaign_id": campaign_id,
                "article": extract_article(raw_row),
                "product_name": extract_product_name(raw_row),
                "ad_orders_qty": 0.0,
                "ad_orders_revenue": 0.0,
                "ad_clicks": 0.0,
                "ad_views": 0.0,
                "ad_spend": 0.0,
                "warning": None,
            }

        grouped[key]["ad_orders_qty"] += ad_orders_qty
        grouped[key]["ad_orders_revenue"] += ad_orders_revenue
        grouped[key]["ad_clicks"] += ad_clicks
        grouped[key]["ad_views"] += ad_views
        grouped[key]["ad_spend"] += ad_spend
        counters["rows"] += 1

    return list(grouped.values()), counters


def classify_cpo_rate(rate_value):
    rate = parse_percent(rate_value)

    if 4.5 <= rate <= 5.5:
        return "advertising_order_5"

    if 9.0 <= rate <= 11.0:
        return "advertising_order_10"

    return "advertising_order_other"


def build_cpo_rows(csv_text):
    grouped = {}
    counters = defaultdict(int)
    total_cpo_expense_from_file = 0.0
    lines = csv_text.splitlines()
    if lines and lines[0].startswith(";"):
        lines = lines[1:]

    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=";")

    for raw_row in reader:
        expense_date = normalize_date(value_by_keys(raw_row, DATE_KEYS))
        sku = extract_sku(raw_row)
        amount = abs(parse_number(value_by_keys(raw_row, SPEND_KEYS)))
        total_cpo_expense_from_file += amount

        if not expense_date:
            counters["without_date"] += 1
            continue

        if not sku:
            counters["without_sku"] += 1
            continue

        if amount == 0:
            counters["zero_spend"] += 1
            continue

        expense_type = classify_cpo_rate(value_by_keys(raw_row, CPO_RATE_KEYS))
        key = (expense_date, sku, expense_type)

        if key not in grouped:
            grouped[key] = {
                "expense_date": expense_date,
                "marketplace_code": "ozon",
                "marketplace_sku": sku,
                "article": extract_article(raw_row),
                "expense_type": expense_type,
                "expense_amount": 0,
            }

        grouped[key]["expense_amount"] += amount
        counters[expense_type] += 1

    rows = list(grouped.values())
    distributed_total = sum(float(row.get("expense_amount") or 0) for row in rows)
    difference = round(total_cpo_expense_from_file - distributed_total, 2)
    summary = {
        "total_cpo_expense_from_file": round(total_cpo_expense_from_file, 2),
        "distributed_cpo_expense": round(distributed_total, 2),
        "difference": difference,
    }

    return rows, counters, summary


def build_cpo_attribution_rows(csv_text):
    grouped = {}
    counters = defaultdict(int)
    lines = csv_text.splitlines()
    if lines and lines[0].startswith(";"):
        lines = lines[1:]

    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=";")

    for raw_row in reader:
        sale_date = normalize_date(value_by_keys(raw_row, DATE_KEYS))
        order_sku = extract_sku(raw_row)
        promoted_sku = extract_promoted_sku(raw_row)

        if not sale_date:
            counters["without_date"] += 1
            continue

        if not order_sku:
            counters["without_sku"] += 1
            continue

        ad_orders_qty = extract_cpo_order_qty(raw_row)
        ad_orders_revenue = extract_cpo_revenue(raw_row)
        ad_spend = abs(parse_number(value_by_keys(raw_row, SPEND_KEYS)))

        if all(value == 0 for value in (ad_orders_qty, ad_orders_revenue, ad_spend)):
            counters["zero_metrics"] += 1
            continue

        campaign_id = extract_campaign_id(raw_row)
        key = (sale_date, order_sku, "cpo", campaign_id)
        if key not in grouped:
            grouped[key] = {
                "sale_date": sale_date,
                "marketplace_code": "ozon",
                "marketplace_sku": order_sku,
                "order_sku": order_sku,
                "promoted_sku": promoted_sku,
                "promoted_article": None,
                "raw_sku": extract_raw_sku(raw_row),
                "raw_promoted_sku": extract_raw_promoted_sku(raw_row),
                "ad_source": "cpo",
                "attribution_type": "direct",
                "campaign_id": campaign_id,
                "article": extract_article(raw_row),
                "product_name": extract_product_name(raw_row),
                "ad_orders_qty": 0.0,
                "ad_orders_revenue": 0.0,
                "ad_clicks": 0.0,
                "ad_views": 0.0,
                "ad_spend": 0.0,
                "warning": None,
            }

        grouped[key]["ad_orders_qty"] += ad_orders_qty
        grouped[key]["ad_orders_revenue"] += ad_orders_revenue
        grouped[key]["ad_spend"] += ad_spend
        counters["rows"] += 1

    return list(grouped.values()), counters


def enrich_rows(rows, catalog):
    for row in rows:
        catalog_row = catalog.get(row["marketplace_sku"])
        if not catalog_row:
            continue
        if not row.get("article"):
            row["article"] = catalog_row.get("article") or ""
        if not row.get("product_name"):
            row["product_name"] = catalog_row.get("product_name") or ""

        promoted_sku = str(row.get("promoted_sku") or "")
        if promoted_sku:
            promoted_catalog_row = catalog.get(promoted_sku)
            if promoted_catalog_row:
                if not row.get("promoted_article"):
                    row["promoted_article"] = promoted_catalog_row.get("article") or None

    return rows


def aggregate_rows(rows):
    grouped = {}

    for row in rows:
        key = tuple(row.get(field) or "" for field in UPSERT_KEY_FIELDS)

        if key not in grouped:
            grouped[key] = {
                "expense_date": row.get("expense_date"),
                "marketplace_code": row.get("marketplace_code"),
                "marketplace_sku": row.get("marketplace_sku"),
                "article": row.get("article") or "",
                "expense_type": row.get("expense_type"),
                "expense_amount": 0.0,
            }

        grouped[key]["expense_amount"] += float(row.get("expense_amount") or 0)

        if not grouped[key].get("article") and row.get("article"):
            grouped[key]["article"] = row.get("article")

    return list(grouped.values())


def aggregate_ad_attribution_rows(rows):
    grouped = {}

    for row in rows:
        key = tuple(row.get(field) or "" for field in AD_ATTRIBUTION_UPSERT_KEY_FIELDS)

        if key not in grouped:
            grouped[key] = {
                "sale_date": row.get("sale_date"),
                "marketplace_code": row.get("marketplace_code"),
                "marketplace_sku": row.get("marketplace_sku"),
                "order_sku": row.get("order_sku") or row.get("marketplace_sku"),
                "promoted_sku": row.get("promoted_sku") or "",
                "promoted_article": row.get("promoted_article"),
                "raw_sku": row.get("raw_sku") or "",
                "raw_promoted_sku": row.get("raw_promoted_sku") or "",
                "ad_source": row.get("ad_source"),
                "attribution_type": row.get("attribution_type") or "direct",
                "campaign_id": row.get("campaign_id") or "",
                "article": row.get("article") or "",
                "product_name": row.get("product_name") or "",
                "ad_orders_qty": 0.0,
                "ad_orders_revenue": 0.0,
                "ad_clicks": 0.0,
                "ad_views": 0.0,
                "ad_spend": 0.0,
                "warning": row.get("warning"),
            }

        grouped[key]["ad_orders_qty"] += float(row.get("ad_orders_qty") or 0)
        grouped[key]["ad_orders_revenue"] += float(row.get("ad_orders_revenue") or 0)
        grouped[key]["ad_clicks"] += float(row.get("ad_clicks") or 0)
        grouped[key]["ad_views"] += float(row.get("ad_views") or 0)
        grouped[key]["ad_spend"] += float(row.get("ad_spend") or 0)

        if not grouped[key].get("article") and row.get("article"):
            grouped[key]["article"] = row.get("article")
        if not grouped[key].get("promoted_article") and row.get("promoted_article"):
            grouped[key]["promoted_article"] = row.get("promoted_article")
        if not grouped[key].get("product_name") and row.get("product_name"):
            grouped[key]["product_name"] = row.get("product_name")
        if not grouped[key].get("warning") and row.get("warning"):
            grouped[key]["warning"] = row.get("warning")

    return list(grouped.values())


def estimate_all_sku_promo_requests():
    return {
        "generate_requests": 1,
        "poll_requests_estimate": "1-3",
        "download_requests": 1,
        "total_requests_estimate": "3-5",
    }


def estimate_search_promo_requests():
    return {
        "generate_requests": 1,
        "poll_requests_estimate": "1-3",
        "download_requests": 1,
        "total_requests_estimate": "3-5",
    }


def estimate_statistics_json_probe_requests():
    return {
        "generate_requests": 1,
        "poll_requests_estimate": "1-3",
        "download_requests": 1,
        "total_requests_estimate": "3-5",
    }


def analyze_cpo_csv(csv_text):
    lines = csv_text.splitlines()
    title = ""
    if lines and lines[0].startswith(";"):
        title = lines[0].lstrip(";").strip()
        lines = lines[1:]

    if not lines:
        return {
            "title": title,
            "columns": [],
            "sample_rows": [],
            "row_count": 0,
            "total_spend": 0.0,
            "keyword_hits": {},
        }

    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=";")
    columns = list(reader.fieldnames or [])
    sample_rows = []
    total_spend = 0.0
    row_count = 0

    for raw_row in reader:
        row_count += 1
        total_spend += abs(parse_number(value_by_keys(raw_row, SPEND_KEYS)))
        if len(sample_rows) < 5:
            sample_rows.append(raw_row)

    joined_text = " ".join([title] + columns).lower()
    keyword_hits = {
        "selected": "selected" in joined_text,
        "selected_products": "selected_products" in joined_text,
        "выбранные": "выбранные" in joined_text,
        "promotion_type": "promotion_type" in joined_text,
        "product_selection": "product_selection" in joined_text,
        "campaign_type": "campaign_type" in joined_text,
        "все_товары": "все товары" in joined_text,
    }

    return {
        "title": title,
        "columns": columns,
        "sample_rows": sample_rows,
        "row_count": row_count,
        "total_spend": round(total_spend, 2),
        "keyword_hits": keyword_hits,
    }


def is_search_promo_total_row(row, columns):
    date_value = str(row.get("Дата") or "").strip()
    if date_value == "Всего":
        return True

    for column in columns or []:
        value = str(row.get(column) or "").strip()
        if value:
            return value == "Всего"
    return False


def parse_search_promo_organisation_orders_csv(csv_text):
    clean_text = (csv_text or "").lstrip("\ufeff")
    lines = clean_text.splitlines()
    preamble_lines = []
    header_index = -1

    for index, line in enumerate(lines):
        normalized = line.lower()
        if ";" in line and "дата" in normalized and "sku" in normalized and "расход" in normalized:
            header_index = index
            break

    if header_index < 0:
        return {
            "preamble_lines": [],
            "title": "",
            "columns": [],
            "row_count_raw": 0,
            "data_row_count": 0,
            "total_row_count": 0,
            "sample_rows": [],
            "total_rows_preview": [],
            "spend_sum_including_total_rows": None,
            "spend_sum_data_rows": None,
            "spend_sum_total_rows": None,
            "spend_sum": None,
            "spend_sum_basis": "data_rows_excluding_total_rows",
            "keyword_hits": {},
        }

    preamble_lines = [line for line in lines[:header_index] if line.strip()]
    title = preamble_lines[0].lstrip(";").strip() if preamble_lines else ""
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])), delimiter=";")
    columns = list(reader.fieldnames or [])
    rows = list(reader)
    data_rows = [row for row in rows if not is_search_promo_total_row(row, columns)]
    total_rows = [row for row in rows if is_search_promo_total_row(row, columns)]

    def sum_rows(target_rows):
        if not target_rows:
            return None
        total = 0.0
        found = False
        for row in target_rows:
            amount = abs(parse_number(value_by_keys(row, SPEND_KEYS)))
            if amount:
                total += amount
                found = True
        return round(total, 2) if found else None

    spend_sum_including_total_rows = sum_rows(rows)
    spend_sum_data_rows = sum_rows(data_rows)
    spend_sum_total_rows = sum_rows(total_rows)

    joined_text = " ".join([title] + columns).lower()
    keyword_hits = {
        "selected": "selected" in joined_text,
        "selected_products": "selected_products" in joined_text,
        "выбранные": "выбранные" in joined_text,
        "promotion_type": "promotion_type" in joined_text,
        "product_selection": "product_selection" in joined_text,
        "campaign_type": "campaign_type" in joined_text,
        "search_promo": "search_promo" in joined_text,
    }

    return {
        "preamble_lines": preamble_lines,
        "title": title,
        "columns": columns,
        "data_rows": data_rows,
        "total_rows": total_rows,
        "row_count_raw": len(rows),
        "data_row_count": len(data_rows),
        "total_row_count": len(total_rows),
        "sample_rows": data_rows[:5],
        "total_rows_preview": total_rows[:5],
        "spend_sum_including_total_rows": spend_sum_including_total_rows,
        "spend_sum_data_rows": spend_sum_data_rows,
        "spend_sum_total_rows": spend_sum_total_rows,
        "spend_sum": spend_sum_data_rows,
        "spend_sum_basis": "data_rows_excluding_total_rows",
        "keyword_hits": keyword_hits,
    }


def build_search_promo_selected_cpo_classification():
    return {
        "source_report": "search_promo_organisation_orders",
        "promotion_type": "cpo_selected_products",
        "scope": "organisation",
        "campaign_filter_supported": False,
        "campaign_id_exact_match": False,
        "campaign_scope": "organisation_wide_campaign_unbound",
        "safe_for_db_load": True,
        "db_load_status": "schema_target_defined_but_not_live_applied",
    }


def normalize_search_promo_selected_cpo_rows(parsed, source_uuid=None, source_kind=None):
    normalized_rows = []
    classification = build_search_promo_selected_cpo_classification()

    for raw_row in parsed.get("data_rows") or []:
        report_date = normalize_date(value_by_keys(raw_row, DATE_KEYS))
        ordered_sku = extract_sku(raw_row)
        promoted_sku = extract_promoted_sku(raw_row)
        order_id = str(value_by_keys(raw_row, ("ID заказа", "order_id", "orderId")) or "").strip()
        posting_number = str(
            value_by_keys(raw_row, ("Номер заказа", "Номер отправления", "posting_number", "postingNumber")) or ""
        ).strip()
        sale_amount = parse_number(value_by_keys(raw_row, ("Стоимость продажи, ₽",)))
        item_amount = parse_number(value_by_keys(raw_row, ("Стоимость, ₽",)))
        bid_percent = parse_percent(value_by_keys(raw_row, ("Ставка, %",)))
        bid_amount = parse_number(value_by_keys(raw_row, ("Ставка, ₽",)))
        spend = abs(parse_number(value_by_keys(raw_row, SPEND_KEYS)))
        quantity = parse_number(value_by_keys(raw_row, ("Количество", "orders", "Заказы")))

        normalized_rows.append(
            {
                "report_date": report_date,
                "order_id": order_id,
                "posting_number": posting_number,
                "ordered_sku": ordered_sku,
                "promoted_sku": promoted_sku,
                "attribution_sku": ordered_sku,
                "attribution_sku_basis": (
                    "existing_ozon_daily_sku_ad_attribution_convention_uses_ordered_sku_as_marketplace_sku"
                ),
                "offer_id": extract_article(raw_row),
                "promoted_article": None,
                "order_source_raw": str(value_by_keys(raw_row, ("Источник заказов",)) or "").strip(),
                "product_name": extract_product_name(raw_row),
                "quantity": quantity,
                "sale_amount": sale_amount,
                "item_amount": item_amount,
                "bid_percent": bid_percent,
                "bid_amount": bid_amount,
                "spend": spend,
                "source_report": classification["source_report"],
                "promotion_type": classification["promotion_type"],
                "scope": classification["scope"],
                "source_kind": source_kind or SEARCH_PROMO_ORGANISATION_ORDERS_KIND,
                "source_uuid": str(source_uuid or ""),
                "campaign_id": "",
                "raw_row": copy.deepcopy(raw_row),
            }
        )

    return normalized_rows


def aggregate_search_promo_selected_cpo_rows(normalized_rows, parsed):
    rows_by_promoted_sku = defaultdict(lambda: {"row_count": 0, "spend": 0.0})
    rows_by_ordered_sku = defaultdict(lambda: {"row_count": 0, "spend": 0.0})
    order_ids = set()
    promoted_skus = set()
    ordered_skus = set()

    for row in normalized_rows:
        promoted_sku = str(row.get("promoted_sku") or "")
        ordered_sku = str(row.get("ordered_sku") or "")
        spend = float(row.get("spend") or 0)

        if row.get("order_id"):
            order_ids.add(str(row["order_id"]))
        if promoted_sku:
            promoted_skus.add(promoted_sku)
            rows_by_promoted_sku[promoted_sku]["row_count"] += 1
            rows_by_promoted_sku[promoted_sku]["spend"] += spend
        if ordered_sku:
            ordered_skus.add(ordered_sku)
            rows_by_ordered_sku[ordered_sku]["row_count"] += 1
            rows_by_ordered_sku[ordered_sku]["spend"] += spend

    return {
        "total_spend_data_rows": parsed.get("spend_sum_data_rows"),
        "total_spend_total_rows": parsed.get("spend_sum_total_rows"),
        "spend_sum_including_total_rows": parsed.get("spend_sum_including_total_rows"),
        "data_row_count": parsed.get("data_row_count"),
        "total_row_count": parsed.get("total_row_count"),
        "order_count": len(order_ids),
        "unique_promoted_sku_count": len(promoted_skus),
        "unique_ordered_sku_count": len(ordered_skus),
        "rows_by_promoted_sku": {
            sku: {
                "row_count": data["row_count"],
                "spend": round(data["spend"], 2),
            }
            for sku, data in sorted(rows_by_promoted_sku.items())
        },
        "rows_by_ordered_sku": {
            sku: {
                "row_count": data["row_count"],
                "spend": round(data["spend"], 2),
            }
            for sku, data in sorted(rows_by_ordered_sku.items())
        },
    }


def build_selected_cpo_source_table_rows(normalized_rows):
    rows = []
    for row in normalized_rows:
        rows.append(
            {
                "sale_date": row.get("report_date"),
                "marketplace_code": "ozon",
                "order_id": row.get("order_id"),
                "posting_number": row.get("posting_number"),
                "ordered_sku": row.get("ordered_sku"),
                "promoted_sku": row.get("promoted_sku"),
                "attribution_sku": row.get("attribution_sku"),
                "attribution_sku_basis": row.get("attribution_sku_basis"),
                "offer_id": row.get("offer_id"),
                "promoted_article": row.get("promoted_article"),
                "order_source_raw": row.get("order_source_raw"),
                "product_name": row.get("product_name"),
                "quantity": row.get("quantity"),
                "sale_amount": row.get("sale_amount"),
                "item_amount": row.get("item_amount"),
                "bid_percent": row.get("bid_percent"),
                "bid_amount": row.get("bid_amount"),
                "spend": row.get("spend"),
                "source_report": row.get("source_report"),
                "promotion_type": row.get("promotion_type"),
                "scope": row.get("scope"),
                "source_kind": row.get("source_kind"),
                "source_uuid": row.get("source_uuid"),
                "raw_row": copy.deepcopy(row.get("raw_row") or {}),
            }
        )
    return rows


def build_selected_cpo_marketplace_expenses_rows(source_rows):
    grouped = {}

    for row in source_rows or []:
        expense_date = str(row.get("sale_date") or row.get("report_date") or "").strip()
        marketplace_sku = str(row.get("ordered_sku") or row.get("marketplace_sku") or "").strip()
        if not expense_date or not marketplace_sku:
            continue

        key = (expense_date, "ozon", marketplace_sku, SELECTED_CPO_MARKETPLACE_EXPENSE_TYPE)
        if key not in grouped:
            grouped[key] = {
                "expense_date": expense_date,
                "marketplace_code": "ozon",
                "marketplace_sku": marketplace_sku,
                "article": str(row.get("offer_id") or row.get("article") or "").strip(),
                "expense_type": SELECTED_CPO_MARKETPLACE_EXPENSE_TYPE,
                "expense_amount": 0.0,
            }

        grouped[key]["expense_amount"] += float(row.get("spend") or 0)
        if not grouped[key].get("article") and row.get("offer_id"):
            grouped[key]["article"] = str(row.get("offer_id") or "").strip()

    rows = []
    for row in grouped.values():
        row["expense_amount"] = round(float(row.get("expense_amount") or 0), 2)
        rows.append(row)
    rows.sort(key=lambda item: (item["expense_date"], item["marketplace_sku"], item["expense_type"]))
    return rows


def build_selected_cpo_ad_attribution_rows(source_rows):
    grouped = {}

    for row in source_rows or []:
        sale_date = str(row.get("sale_date") or row.get("report_date") or "").strip()
        marketplace_sku = str(row.get("ordered_sku") or row.get("marketplace_sku") or "").strip()
        if not sale_date or not marketplace_sku:
            continue

        key = (sale_date, "ozon", marketplace_sku, SELECTED_CPO_AD_SOURCE, "direct", "")
        if key not in grouped:
            grouped[key] = {
                "sale_date": sale_date,
                "marketplace_code": "ozon",
                "marketplace_sku": marketplace_sku,
                "order_sku": marketplace_sku,
                "promoted_sku": "",
                "promoted_article": None,
                "raw_sku": marketplace_sku,
                "raw_promoted_sku": "",
                "ad_source": SELECTED_CPO_AD_SOURCE,
                "attribution_type": "direct",
                "campaign_id": "",
                "article": str(row.get("offer_id") or row.get("article") or "").strip(),
                "product_name": str(row.get("product_name") or "").strip(),
                "ad_orders_qty": 0.0,
                "ad_orders_revenue": 0.0,
                "ad_clicks": 0.0,
                "ad_views": 0.0,
                "ad_spend": 0.0,
                "warning": "selected_cpo_search_promo_organisation_level_spend_only",
            }

        grouped[key]["ad_spend"] += float(row.get("spend") or 0)
        if not grouped[key].get("article") and row.get("offer_id"):
            grouped[key]["article"] = str(row.get("offer_id") or "").strip()
        if not grouped[key].get("product_name") and row.get("product_name"):
            grouped[key]["product_name"] = str(row.get("product_name") or "").strip()

    rows = []
    for row in grouped.values():
        row["ad_spend"] = round(float(row.get("ad_spend") or 0), 2)
        rows.append(row)
    rows.sort(key=lambda item: (item["sale_date"], item["marketplace_sku"], item["ad_source"]))
    return rows


def load_selected_cpo_source_rows(date, db_client=None):
    client = db_client or supabase
    rows = []
    start = 0
    page_size = 1000

    while True:
        result = (
            client.table(SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE)
            .select("*")
            .eq("sale_date", date)
            .range(start, start + page_size - 1)
            .execute()
        )
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size

    return rows


def upsert_selected_cpo_source_rows(db_client, rows):
    if not rows:
        return 0

    db_client.table(SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE).upsert(
        rows,
        on_conflict=(
            "sale_date,marketplace_code,source_report,promotion_type,"
            "order_id,posting_number,ordered_sku,promoted_sku"
        ),
    ).execute()
    return len(rows)


def build_selected_cpo_would_write_summary(normalized_rows, aggregation):
    return {
        "normalized_row_count": len(normalized_rows),
        "preferred_target": SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE,
        "source_table": {
            "table_name": SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE,
            "supported": True,
            "requires_migration_apply": True,
            "proposed_mapping": {
                "sale_date": "report_date",
                "marketplace_code": "ozon",
                "order_id": "order_id",
                "posting_number": "posting_number",
                "ordered_sku": "ordered_sku",
                "promoted_sku": "promoted_sku",
                "attribution_sku": "attribution_sku",
                "attribution_sku_basis": "attribution_sku_basis",
                "offer_id": "offer_id",
                "promoted_article": "promoted_article",
                "order_source_raw": "order_source_raw",
                "product_name": "product_name",
                "quantity": "quantity",
                "sale_amount": "sale_amount",
                "item_amount": "item_amount",
                "bid_percent": "bid_percent",
                "bid_amount": "bid_amount",
                "spend": "spend",
                "source_report": "source_report",
                "promotion_type": "promotion_type",
                "scope": "scope",
                "source_kind": "source_kind",
                "source_uuid": "source_uuid",
                "raw_row": "raw_row",
            },
            "idempotency_key": [
                "sale_date",
                "marketplace_code",
                "source_report",
                "promotion_type",
                "order_id",
                "posting_number",
                "ordered_sku",
                "promoted_sku",
            ],
        },
        "ozon_daily_sku_ad_attribution": {
            "supported": False,
            "blocker": (
                "current primary key (sale_date, marketplace_code, marketplace_sku, ad_source, attribution_type, campaign_id) "
                "cannot distinguish selected CPO from all-products CPO because source_report/promotion_type "
                "are absent from the key and organisation-level selected CPO rows have no campaign_id"
            ),
        },
        "marketplace_expenses": {
            "supported": False,
            "blocker": (
                "marketplace_expenses groups ad spend by expense_type and has no confirmed selected-CPO expense_type "
                "or promotion_type field; writing there now would mix all-products and selected-products CPO"
            ),
            "proposed_expense_type": "advertising_order_selected_cpo",
        },
        "aggregation": aggregation,
    }


def build_selected_cpo_downstream_would_write_summary(source_rows):
    marketplace_expenses_rows = build_selected_cpo_marketplace_expenses_rows(source_rows)
    ad_attribution_rows = build_selected_cpo_ad_attribution_rows(source_rows)
    total_spend = round(sum(float(row.get("spend") or 0) for row in source_rows or []), 2)

    return {
        "preferred_targets": [
            "marketplace_expenses",
            "ozon_daily_sku_ad_attribution",
        ],
        "marketplace_expenses": {
            "supported": True,
            "expense_type": SELECTED_CPO_MARKETPLACE_EXPENSE_TYPE,
            "rows": marketplace_expenses_rows,
            "row_count": len(marketplace_expenses_rows),
            "sum_expense_amount": round(
                sum(float(row.get("expense_amount") or 0) for row in marketplace_expenses_rows),
                2,
            ),
            "on_conflict": ",".join(UPSERT_KEY_FIELDS),
        },
        "ozon_daily_sku_ad_attribution": {
            "supported": True,
            "ad_source": SELECTED_CPO_AD_SOURCE,
            "attribution_type": "direct",
            "campaign_id": "",
            "rows": ad_attribution_rows,
            "row_count": len(ad_attribution_rows),
            "sum_ad_spend": round(sum(float(row.get("ad_spend") or 0) for row in ad_attribution_rows), 2),
            "on_conflict": ",".join(AD_ATTRIBUTION_UPSERT_KEY_FIELDS),
        },
        "source_row_count": len(source_rows or []),
        "source_sum_spend": total_spend,
        "writes_marketplace_expenses": False,
        "writes_ozon_daily_sku_ad_attribution": False,
    }


def upsert_selected_cpo_marketplace_expenses_rows(db_client, rows):
    aggregated_rows = aggregate_rows(rows)
    if not aggregated_rows:
        return 0

    for batch in chunks(aggregated_rows, 500):
        db_client.table("marketplace_expenses").upsert(
            batch,
            on_conflict="expense_date,marketplace_code,marketplace_sku,expense_type",
        ).execute()
    return len(aggregated_rows)


def upsert_selected_cpo_ad_attribution_rows(db_client, rows):
    aggregated_rows = aggregate_ad_attribution_rows(rows)
    if not aggregated_rows:
        return 0

    for batch in chunks(aggregated_rows, 500):
        db_client.table("ozon_daily_sku_ad_attribution").upsert(
            batch,
            on_conflict="sale_date,marketplace_code,marketplace_sku,ad_source,attribution_type,campaign_id",
        ).execute()
    return len(aggregated_rows)


def verify_cpc_downstream_materialized(target_date, db_client=None, marketplace_code="ozon"):
    client = db_client or supabase

    marketplace_expenses_rows = []
    start = 0
    page_size = 1000
    while True:
        result = (
            client.table("marketplace_expenses")
            .select("expense_type,expense_amount")
            .eq("marketplace_code", marketplace_code)
            .eq("expense_date", target_date)
            .in_("expense_type", ["advertising_clicks", "advertising_other"])
            .range(start, start + page_size - 1)
            .execute()
        )
        batch = result.data or []
        marketplace_expenses_rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size

    ad_attribution_rows = []
    start = 0
    while True:
        result = (
            client.table("ozon_daily_sku_ad_attribution")
            .select("ad_spend")
            .eq("marketplace_code", marketplace_code)
            .eq("sale_date", target_date)
            .eq("ad_source", "cpc")
            .range(start, start + page_size - 1)
            .execute()
        )
        batch = result.data or []
        ad_attribution_rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size

    marketplace_sum = round(
        sum(float(row.get("expense_amount") or 0) for row in marketplace_expenses_rows),
        2,
    )
    ad_attribution_sum = round(
        sum(float(row.get("ad_spend") or 0) for row in ad_attribution_rows),
        2,
    )

    return {
        "target_date": target_date,
        "marketplace_expenses_cpc_rows": len(marketplace_expenses_rows),
        "marketplace_expenses_cpc_sum": marketplace_sum,
        "ad_attribution_cpc_rows": len(ad_attribution_rows),
        "ad_attribution_cpc_sum": ad_attribution_sum,
        "materialized": bool(
            marketplace_expenses_rows
            or ad_attribution_rows
            or marketplace_sum > 0
            or ad_attribution_sum > 0
        ),
    }


def guard_cpc_materialization(
    target_date,
    cpc_status,
    pending_batches,
    processed_batches_this_run,
    current_run_cpc_expense_rows_count,
    current_run_cpc_ad_attribution_rows_count,
    db_client=None,
):
    summary = {
        "target_date": target_date,
        "cpc_status": cpc_status,
        "pending_batches": int(pending_batches or 0),
        "processed_batches_this_run": int(processed_batches_this_run or 0),
        "current_run_cpc_expense_rows_count": int(current_run_cpc_expense_rows_count or 0),
        "current_run_cpc_ad_attribution_rows_count": int(current_run_cpc_ad_attribution_rows_count or 0),
        "verification_performed": False,
        "downstream_verification": None,
        "guard_triggered": False,
    }

    should_verify = (
        cpc_status == "success"
        and int(pending_batches or 0) == 0
        and int(processed_batches_this_run or 0) == 0
        and int(current_run_cpc_expense_rows_count or 0) == 0
        and int(current_run_cpc_ad_attribution_rows_count or 0) == 0
    )
    if not should_verify:
        return summary

    summary["verification_performed"] = True
    verification = verify_cpc_downstream_materialized(target_date, db_client=db_client)
    summary["downstream_verification"] = verification

    if verification.get("materialized"):
        summary["guard_triggered"] = False
        summary["status_override"] = "success_existing_downstream_verified"
        return summary

    summary["guard_triggered"] = True
    raise CpcMaterializationGuardError(
        "CPC progress complete but no current-run CPC rows and no downstream CPC materialization "
        f"for target date {target_date}. Remediation: run controlled CPC refetch/backfill for this date."
    )


def find_cpc_progress_for_date(client, target_date):
    candidates = []
    for progress_key in (client.state.get("cpc_progress", {}) or {}).keys():
        progress = client.get_cpc_progress(progress_key)
        if not progress:
            continue
        if progress.get("date_from") != target_date or progress.get("date_to") != target_date:
            continue
        candidates.append((progress_key, progress))

    if not candidates:
        return None, None

    candidates.sort(
        key=lambda item: (
            1 if str(item[1].get("selection_mode") or "") == "complete" else 0,
            1 if int(item[1].get("pending_batches") or 0) == 0 else 0,
            int(item[1].get("completed_batches") or 0),
            int(item[1].get("total_campaigns") or 0),
            str(item[1].get("updated_at") or ""),
        ),
        reverse=True,
    )
    return candidates[0]


def stateless_ozon_request(method, path, token, retry_profile="default", **kwargs):
    del retry_profile

    endpoint = path
    if str(path).startswith("http://") or str(path).startswith("https://"):
        url = str(path)
    else:
        url = f"{OZON_PERFORMANCE_BASE_URL}{path}"

    headers = kwargs.pop("headers", {})
    headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })

    response = requests.request(
        method,
        url,
        headers=headers,
        timeout=120,
        **kwargs,
    )

    if response.status_code == 429:
        retry_after_seconds = parse_retry_after_seconds(response.headers.get("Retry-After"))
        if retry_after_seconds is None:
            retry_after_seconds = compute_backoff_seconds(
                CPC_BASE_SLEEP_SECONDS,
                CPC_MAX_SLEEP_SECONDS,
                1,
            )
        cooldown_until = to_iso(utcnow() + timedelta(seconds=max(0, int(retry_after_seconds or 0))))
        raise RateLimitPending(
            endpoint=endpoint,
            retry_after_seconds=retry_after_seconds,
            cooldown_until=cooldown_until,
            attempt=1,
        )

    response.raise_for_status()
    return response


def list_campaigns_stateless(token):
    campaigns = []
    seen_ids = set()

    for adv_object_type in ADV_OBJECT_TYPES or ["SKU"]:
        response = stateless_ozon_request(
            "GET",
            "/api/client/campaign",
            token,
            params={"advObjectType": adv_object_type},
        )
        data = response.json()
        batch = data.get("list") or data.get("campaigns") or data.get("items") or []

        for campaign in batch:
            campaign_id = str(campaign.get("id") or campaign.get("campaignId") or "")
            if not campaign_id or campaign_id in seen_ids:
                continue
            seen_ids.add(campaign_id)
            campaigns.append(campaign)

    return campaigns


def request_statistics_stateless(token, campaign_ids, date_from, date_to, group_by):
    payload = {
        "campaigns": [str(campaign_id) for campaign_id in campaign_ids],
        "dateFrom": date_from,
        "dateTo": date_to,
        "groupBy": group_by,
    }
    response = stateless_ozon_request(
        "POST",
        "/api/client/statistics/json",
        token,
        retry_profile="statistics_json",
        json=payload,
        headers={"Content-Type": "application/json"},
    )
    data = response.json()
    uuid = data.get("UUID") or data.get("uuid")
    if not uuid:
        raise RuntimeError(f"Ozon Performance did not return UUID for CPC recovery: {data}")
    return uuid


def wait_statistics_stateless(token, uuid, poll_profile="statistics_json"):
    profile = POLL_PROFILES[poll_profile]

    for attempt in range(1, int(profile["max_attempts"]) + 1):
        response = stateless_ozon_request("GET", f"/api/client/statistics/{uuid}", token)
        data = response.json()
        state = str(data.get("state") or data.get("status") or "").upper()

        if state in {"OK", "SUCCESS", "DONE", "COMPLETED", "READY"}:
            return data

        if state in {"ERROR", "FAILED", "FAIL"}:
            raise RuntimeError(f"Ozon Performance report failed during CPC recovery: {data}")

        sleep_seconds = min(
            int(profile["cap_sleep_seconds"]),
            int(profile["base_sleep_seconds"]) * (2 ** max(attempt - 1, 0)),
        )
        print(
            f"Ozon Performance CPC recovery polling UUID={uuid} state={state or 'PENDING'} "
            f"attempt={attempt} sleep={sleep_seconds}"
        )
        time.sleep(sleep_seconds)

    raise TimeoutError(f"Ozon Performance CPC recovery report timeout: {uuid}")


def download_statistics_report_stateless(token, uuid):
    response = stateless_ozon_request(
        "GET",
        "/api/client/statistics/report",
        token,
        params={"UUID": uuid},
    )
    text = response.text.strip()
    if not text:
        return {}
    try:
        return response.json()
    except ValueError:
        return json.loads(text)


def fetch_cpc_recovery_existing_report_stateless(client, uuid):
    token = client.ensure_token()
    wait_statistics_stateless(token, uuid, poll_profile="statistics_json")
    report_data = download_statistics_report_stateless(token, uuid)
    return {"uuid": uuid, "report_data": report_data}


def filter_statistics_report_to_campaign_ids(report_data, campaign_ids):
    requested_campaign_ids = preserve_campaign_id_order(campaign_ids or [])
    requested_campaign_ids_set = set(requested_campaign_ids)
    if not requested_campaign_ids_set:
        return report_data or {}
    return {
        str(campaign_id): data
        for campaign_id, data in (report_data or {}).items()
        if str(campaign_id) in requested_campaign_ids_set
    }


def build_cpc_recovery_plan(
    client,
    target_date,
    requested_batch_size,
    max_stats_campaigns,
    db_client=None,
    campaigns=None,
    campaign_ids=None,
):
    campaigns = campaigns if campaigns is not None else list_campaigns_stateless(client.ensure_token())
    selection = build_daily_cpc_selection(campaigns, target_date, target_date, "complete")
    selected_campaigns = list(selection["selected_campaigns"])
    requested_campaign_ids = preserve_campaign_id_order(campaign_ids or [])
    requested_campaign_ids_set = set(requested_campaign_ids)
    if requested_campaign_ids_set:
        selected_campaigns = [
            campaign
            for campaign in selected_campaigns
            if str(campaign.get("id") or campaign.get("campaignId") or "") in requested_campaign_ids_set
        ]
    ordered_campaign_ids = preserve_campaign_id_order(
        [campaign.get("id") or campaign.get("campaignId") for campaign in selected_campaigns]
    )
    batch_size = max(1, int(requested_batch_size or DEFAULT_CAMPAIGN_BATCH_SIZE))
    cpc_batches = build_cpc_batches(ordered_campaign_ids, batch_size)
    stale_progress_key, stale_progress = find_cpc_progress_for_date(client, target_date)
    downstream_verification = verify_cpc_downstream_materialized(target_date, db_client=db_client)
    safe_campaign_budget = min(
        max(0, int(max_stats_campaigns or DEFAULT_MAX_STATS_CAMPAIGNS_PER_DAILY_RUN)),
        max(0, STATS_DAILY_CAMPAIGN_LIMIT - STATS_DAILY_CAMPAIGN_RESERVE),
    )

    return {
        "target_date": target_date,
        "selection_mode": "complete",
        "campaign_count": len(ordered_campaign_ids),
        "batch_size": batch_size,
        "total_batches": len(cpc_batches),
        "campaign_units": len(ordered_campaign_ids),
        "expected_statistics_json_submit_count": len(cpc_batches),
        "safe_campaign_budget": safe_campaign_budget,
        "fits_safe_budget": len(ordered_campaign_ids) <= safe_campaign_budget,
        "stale_progress_key": stale_progress_key,
        "stale_progress": stale_progress or {},
        "downstream_verification": downstream_verification,
        "cpc_missing_globally": not bool(downstream_verification.get("materialized")),
        "requested_campaign_ids": requested_campaign_ids,
        "selected_campaign_ids": ordered_campaign_ids,
        "ordered_campaign_ids": ordered_campaign_ids,
        "cpc_batches": cpc_batches,
        "campaigns_by_id": {
            str(campaign.get("id") or campaign.get("campaignId") or ""): campaign
            for campaign in campaigns
            if campaign.get("id") or campaign.get("campaignId")
        },
        "planning_counts": {
            "raw_cpc_count": len(selection["raw_cpc_campaigns"]),
            "date_overlap_cpc_count": len(selection["date_overlap_cpc_campaigns"]),
            "recent_cpc_count": len(selection["recent_cpc_campaigns"]),
            "excluded_by_recent_filter_count": int(selection["excluded_by_recent_filter_count"] or 0),
        },
    }


def run_cpc_recovery_mode(
    client,
    target_date,
    group_by,
    requested_batch_size,
    max_stats_campaigns,
    dry_run=True,
    write=False,
    approve_write=False,
    ignore_stale_progress_for_date_only=False,
    no_write=True,
    db_client=None,
    campaigns=None,
    campaign_ids=None,
    fetch_batch_fn=None,
    existing_report_uuid=None,
    fetch_existing_report_fn=None,
):
    if write and not approve_write:
        raise CpcRecoveryWriteNotApprovedError(
            "CPC recovery write requires explicit --write --approve-cpc-recovery-write"
        )

    plan = build_cpc_recovery_plan(
        client,
        target_date,
        requested_batch_size,
        max_stats_campaigns,
        db_client=db_client,
        campaigns=campaigns,
        campaign_ids=campaign_ids,
    )
    stale_progress = plan.get("stale_progress") or {}
    stale_progress_summary = {
        "progress_key": plan.get("stale_progress_key"),
        "selection_mode": stale_progress.get("selection_mode"),
        "campaign_count": stale_progress.get("total_campaigns"),
        "batch_size": stale_progress.get("batch_size"),
        "completed_batches": stale_progress.get("completed_batches"),
        "pending_batches": stale_progress.get("pending_batches"),
        "updated_at": stale_progress.get("updated_at"),
    }
    summary = {
        "mode": "cpc-recovery",
        "target_date": target_date,
        "date_from": target_date,
        "date_to": target_date,
        "dry_run": bool(dry_run),
        "write": bool(write),
        "no_write": bool(no_write or not write),
        "write_approved": bool(approve_write),
        "ignore_stale_progress_for_date_only": bool(ignore_stale_progress_for_date_only),
        "existing_report_uuid": str(existing_report_uuid or ""),
        "status": "planned",
        "reason": None,
        "db_writes": 0,
        "marketplace_expenses_writes": 0,
        "ozon_daily_sku_ad_attribution_writes": 0,
        "used_statistics_json": not bool(existing_report_uuid),
        "used_general_statistics_submit": False,
        "used_existing_report_uuid": bool(existing_report_uuid),
        "cpo_touched": False,
        "selected_cpo_touched": False,
        "preflight": {
            "cpc_missing_globally": plan["cpc_missing_globally"],
            "downstream_verification": plan["downstream_verification"],
            "stale_progress": stale_progress_summary,
            "campaign_count": plan["campaign_count"],
            "requested_campaign_ids": list(plan.get("requested_campaign_ids") or []),
            "selected_campaign_ids": list(plan.get("selected_campaign_ids") or []),
            "batch_size": plan["batch_size"],
            "total_batches": 1 if existing_report_uuid else plan["total_batches"],
            "campaign_units": plan["campaign_units"],
            "expected_statistics_json_submit_count": (
                0 if existing_report_uuid else plan["expected_statistics_json_submit_count"]
            ),
            "safe_campaign_budget": plan["safe_campaign_budget"],
            "fits_safe_budget": plan["fits_safe_budget"],
            "stale_progress_ignored": bool(ignore_stale_progress_for_date_only and plan.get("stale_progress_key")),
        },
        "statistics_json_submit_attempts": 0,
        "processed_batches": 0,
        "advertising_clicks_total": 0.0,
        "cpc_attribution_spend_total": 0.0,
        "expense_rows_count": 0,
        "attribution_rows_count": 0,
    }

    if not existing_report_uuid and not plan["fits_safe_budget"]:
        summary["status"] = "plan_exceeds_safe_budget"
        summary["reason"] = "estimated_requests_exceed_safe_budget"
        return summary

    if not dry_run and not write:
        summary["status"] = "planned"
        summary["reason"] = "explicit_write_not_requested"
        return summary

    expense_rows_by_key = {}
    ad_attribution_rows_by_key = {}
    batch_fetcher = fetch_batch_fn or fetch_cpc_recovery_batch_stateless
    existing_report_fetcher = fetch_existing_report_fn or fetch_cpc_recovery_existing_report_stateless

    report_batches = []
    if existing_report_uuid:
        report_batches.append(
            {
                "batch_index": 0,
                "campaign_batch": list(plan.get("selected_campaign_ids") or plan.get("ordered_campaign_ids") or []),
                "batch_result": existing_report_fetcher(client=client, uuid=existing_report_uuid),
            }
        )
    else:
        for batch_index, campaign_batch in enumerate(plan["cpc_batches"]):
            try:
                batch_result = batch_fetcher(
                    client=client,
                    campaign_batch=campaign_batch,
                    date_from=target_date,
                    date_to=target_date,
                    group_by=group_by,
                )
            except RateLimitPending as exc:
                summary["statistics_json_submit_attempts"] += 1
                summary["status"] = "quota_limited_before_refetch" if batch_index == 0 else "quota_limited_during_refetch"
                summary["reason"] = "statistics_json_429"
                summary["retry_after_seconds"] = exc.retry_after_seconds
                summary["cooldown_until"] = exc.cooldown_until
                summary["failed_batch_index"] = batch_index
                summary["failed_batch_campaign_ids"] = list(campaign_batch)
                return summary

            summary["statistics_json_submit_attempts"] += 1
            report_batches.append(
                {
                    "batch_index": batch_index,
                    "campaign_batch": list(campaign_batch),
                    "batch_result": batch_result,
                }
            )

    for batch_item in report_batches:
        summary["processed_batches"] += 1
        report_data = batch_item["batch_result"].get("report_data") or {}
        if campaign_ids:
            report_data = filter_statistics_report_to_campaign_ids(report_data, campaign_ids)

        rows, _ = build_rows(report_data, plan["campaigns_by_id"], target_date)
        attribution_rows, _ = build_cpc_attribution_rows(report_data, target_date)

        for row in rows:
            if str(row.get("expense_type") or "") not in {"advertising_clicks", "advertising_other"}:
                continue
            key = tuple(str(row.get(field) or "") for field in UPSERT_KEY_FIELDS)
            existing = expense_rows_by_key.get(key)
            if existing is None:
                expense_rows_by_key[key] = dict(row)
            else:
                existing["expense_amount"] = round(
                    float(existing.get("expense_amount") or 0) + float(row.get("expense_amount") or 0),
                    2,
                )

        for row in attribution_rows:
            if str(row.get("ad_source") or "") != "cpc":
                continue
            key = tuple(str(row.get(field) or "") for field in AD_ATTRIBUTION_UPSERT_KEY_FIELDS)
            existing = ad_attribution_rows_by_key.get(key)
            if existing is None:
                ad_attribution_rows_by_key[key] = dict(row)
            else:
                for field in ("ad_orders_qty", "ad_orders_revenue", "ad_clicks", "ad_views", "ad_spend"):
                    existing[field] = round(
                        float(existing.get(field) or 0) + float(row.get(field) or 0),
                        2,
                    )

    expense_rows = aggregate_rows(list(expense_rows_by_key.values()))
    ad_attribution_rows = aggregate_ad_attribution_rows(list(ad_attribution_rows_by_key.values()))

    if campaign_ids and not expense_rows and not ad_attribution_rows:
        summary["status"] = "expected_row_not_found"
        summary["reason"] = "campaign_scoped_report_has_no_matching_rows"
        summary["expense_rows_count"] = 0
        summary["attribution_rows_count"] = 0
        return summary

    if write:
        save_rows(expense_rows)
        save_ad_attribution_rows(ad_attribution_rows)
        summary["marketplace_expenses_writes"] = len(expense_rows)
        summary["ozon_daily_sku_ad_attribution_writes"] = len(ad_attribution_rows)
        summary["db_writes"] = len(expense_rows) + len(ad_attribution_rows)
        summary["status"] = "written"
    else:
        summary["status"] = "dry_run_no_write"

    summary["expense_rows_count"] = len(expense_rows)
    summary["attribution_rows_count"] = len(ad_attribution_rows)
    summary["advertising_clicks_total"] = round(
        sum(float(row.get("expense_amount") or 0) for row in expense_rows),
        2,
    )
    summary["cpc_attribution_spend_total"] = round(
        sum(float(row.get("ad_spend") or 0) for row in ad_attribution_rows),
        2,
    )
    summary["marketplace_expenses_rows"] = expense_rows
    summary["ad_attribution_rows"] = ad_attribution_rows
    return summary


def fetch_cpc_recovery_batch_stateless(client, campaign_batch, date_from, date_to, group_by):
    uuid = request_statistics_stateless(
        client.ensure_token(),
        campaign_batch,
        date_from,
        date_to,
        group_by,
    )
    wait_statistics_stateless(client.ensure_token(), uuid, poll_profile="statistics_json")
    report_data = download_statistics_report_stateless(client.ensure_token(), uuid)
    return {
        "uuid": uuid,
        "report_data": report_data,
    }


def print_cpo_report_check_plan(date_from, date_to, report_type):
    utc_from, utc_to = build_utc_time_bounds(date_from, date_to)
    summary = {
        "mode": "cpo-report-check",
        "cpo_report_type": report_type,
        "endpoint": f"/api/client/statistics/all_sku_promo/{report_type}/generate",
        "date_from": date_from,
        "date_to": date_to,
        "time_bounds_utc": {
            "from": utc_from,
            "to": utc_to,
        },
        "estimated_requests": estimate_all_sku_promo_requests(),
        "creates_report_job": True,
        "uses_statistics_json": False,
        "uses_campaign_unit_quota_2000": False,
        "is_cpc": False,
        "layer": "cpo_reporting",
        "writes_marketplace_expenses": False,
        "writes_ozon_daily_sku_ad_attribution": False,
        "expected_columns": [
            "Дата",
            "SKU",
            "SKU продвигаемого товара",
            "Артикул",
            "Название товара",
            "Количество",
            "Стоимость продажи, ₽",
            "Стоимость, ₽",
            "Ставка, %",
            "Ставка, ₽",
            "Расход, ₽",
        ],
        "possible_selected_markers": [
            "selected",
            "выбранные",
            "product_selection",
            "promotion_type",
            "campaign_type",
            "все товары",
        ],
    }
    print("Ozon Performance CPO report check plan:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def print_search_promo_report_check_plan(date_from, date_to, report_type, campaign_id=None):
    utc_from, utc_to = build_utc_time_bounds(date_from, date_to)
    payload = {
        "timeBounds.from": utc_from,
        "timeBounds.to": utc_to,
    }
    if campaign_id:
        payload["campaignId"] = str(campaign_id)

    summary = {
        "mode": "search-promo-report-check",
        "search_promo_report_type": report_type,
        "endpoint": SEARCH_PROMO_REPORT_ENDPOINTS[report_type],
        "date_from": date_from,
        "date_to": date_to,
        "campaign_id": str(campaign_id) if campaign_id else None,
        "payload": payload,
        "estimated_requests": estimate_search_promo_requests(),
        "creates_report_job": True,
        "uses_statistics_json": False,
        "uses_campaign_unit_quota_2000": False,
        "is_cpc": False,
        "layer": "search_promo_selected_cpo_reporting",
        "writes_marketplace_expenses": False,
        "writes_ozon_daily_sku_ad_attribution": False,
        "expected_report_format": "csv",
        "expected_follow_up": {
            "status_endpoint": "/api/client/statistics/{UUID}",
            "download_endpoint": "/api/client/statistics/report?UUID={UUID}",
        },
        "expected_columns_hint": [
            "Дата",
            "SKU",
            "SKU продвигаемого товара",
            "Артикул",
            "Название товара",
            "Количество",
            "Стоимость продажи, ₽",
            "Расход, ₽",
            "Ставка, %",
            "Продвижение",
        ],
        "possible_selected_markers": [
            "selected",
            "выбранные",
            "product_selection",
            "promotion_type",
            "campaign_type",
            "search_promo",
        ],
    }
    print("Ozon Performance SEARCH_PROMO report check plan:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def analyze_statistics_json_report(report_data):
    if not isinstance(report_data, dict):
        return {
            "campaign_title": "",
            "row_keys": [],
            "sample_rows": [],
            "total_spend": 0.0,
            "orders_qty": 0.0,
            "revenue": 0.0,
            "rate_markers": {
                "has_5_percent": False,
                "has_10_percent": False,
                "has_other_rate": False,
            },
        }

    campaign_id, campaign_payload = next(iter(report_data.items()), ("", {}))
    campaign_payload = campaign_payload or {}
    title = str(campaign_payload.get("title") or "")
    report_section = campaign_payload.get("report") or {}
    rows = list(report_section.get("rows") or [])
    totals = report_section.get("totals") or {}
    row_keys = sorted({key for row in rows[:5] for key in row.keys()})
    sample_rows = rows[:5]

    total_spend = abs(parse_number(value_by_keys(totals, SPEND_KEYS)))
    orders_qty = parse_number(value_by_keys(totals, ("orders", "Заказы")))
    revenue = parse_number(value_by_keys(totals, REVENUE_KEYS))

    joined_text = " ".join(
        [title]
        + [json.dumps(totals, ensure_ascii=False)]
        + [json.dumps(row, ensure_ascii=False) for row in sample_rows]
    ).lower()
    has_5 = bool(re.search(r"(^|[^0-9])5\s*%", joined_text))
    has_10 = bool(re.search(r"(^|[^0-9])10\s*%", joined_text))
    has_other = any(marker in joined_text for marker in ("ставка", "rate", "drr"))

    return {
        "campaign_id": str(campaign_id),
        "campaign_title": title,
        "row_keys": row_keys,
        "sample_rows": sample_rows,
        "total_spend": round(total_spend, 2),
        "orders_qty": orders_qty,
        "revenue": round(revenue, 2),
        "rate_markers": {
            "has_5_percent": has_5,
            "has_10_percent": has_10,
            "has_other_rate": has_other and not (has_5 or has_10),
        },
    }


def build_ozon_campaign_metadata_snapshot_rows(campaigns, snapshot_date, marketplace_code="ozon"):
    rows = []

    for campaign in campaigns or []:
        campaign_id = str(campaign.get("id") or campaign.get("campaignId") or "").strip()
        if not campaign_id:
            continue

        placement = campaign.get("placement")
        if placement is None:
            placement = campaign.get("placements")

        rows.append(
            {
                "snapshot_date": str(snapshot_date),
                "marketplace_code": str(marketplace_code or "ozon"),
                "campaign_id": campaign_id,
                "title": str(campaign.get("title") or ""),
                "state": str(campaign.get("state") or ""),
                "adv_object_type": str(campaign.get("advObjectType") or ""),
                "payment_type": str(campaign.get("PaymentType") or campaign.get("paymentType") or ""),
                "placement": copy.deepcopy(placement),
                "budget": str(campaign.get("budget") or ""),
                "daily_budget": str(campaign.get("dailyBudget") or ""),
                "weekly_budget": str(campaign.get("weeklyBudget") or ""),
                "budget_type": str(campaign.get("budgetType") or ""),
                "expense_strategy": str(campaign.get("expenseStrategy") or ""),
                "product_campaign_mode": str(campaign.get("productCampaignMode") or ""),
                "product_autopilot_strategy": str(campaign.get("productAutopilotStrategy") or ""),
                "created_at": str(campaign.get("createdAt") or ""),
                "updated_at": str(campaign.get("updatedAt") or ""),
                "raw_campaign_json": copy.deepcopy(campaign),
                "captured_at": to_iso(utcnow()),
            }
        )

    return rows


def build_campaign_metadata_field_summary(rows):
    expected_fields = [
        "campaign_id",
        "title",
        "state",
        "adv_object_type",
        "payment_type",
        "placement",
        "budget",
        "daily_budget",
        "weekly_budget",
        "budget_type",
        "expense_strategy",
        "product_campaign_mode",
        "product_autopilot_strategy",
        "created_at",
        "updated_at",
    ]
    present_fields = []
    missing_fields = []

    for field_name in expected_fields:
        has_value = False
        for row in rows or []:
            value = row.get(field_name)
            if isinstance(value, list) and value:
                has_value = True
                break
            if value not in (None, "", []):
                has_value = True
                break
        if has_value:
            present_fields.append(field_name)
        else:
            missing_fields.append(field_name)

    return {
        "present_fields": present_fields,
        "missing_fields": missing_fields,
    }


def build_campaign_metadata_snapshot_plan(snapshot_date, marketplace_code="ozon"):
    return {
        "mode": "campaign-metadata-snapshot-plan",
        "snapshot_date": str(snapshot_date),
        "marketplace_code": str(marketplace_code or "ozon"),
        "endpoint": "/api/client/campaign",
        "method": "GET",
        "adv_object_types": list(ADV_OBJECT_TYPES or ["SKU"]),
        "estimated_request_count": len(ADV_OBJECT_TYPES or ["SKU"]),
        "target_table": OZON_PERFORMANCE_CAMPAIGN_METADATA_TABLE,
        "db_writes": 0,
        "campaign_mutations": 0,
        "writes_marketplace_expenses": False,
        "writes_ozon_daily_sku_ad_attribution": False,
        "writes_campaign_metadata": False,
    }


def campaign_metadata_snapshot_dry_run(client, snapshot_date, campaign_ids=None, campaigns=None, marketplace_code="ozon"):
    campaign_ids = [str(value) for value in (campaign_ids or []) if str(value)]
    requested_ids = set(campaign_ids)
    plan = build_campaign_metadata_snapshot_plan(snapshot_date, marketplace_code=marketplace_code)
    campaigns = list(campaigns) if campaigns is not None else client.list_campaigns()
    rows = build_ozon_campaign_metadata_snapshot_rows(campaigns, snapshot_date, marketplace_code=marketplace_code)

    if requested_ids:
        filtered_rows = [row for row in rows if row.get("campaign_id") in requested_ids]
    else:
        filtered_rows = list(rows)

    field_summary = build_campaign_metadata_field_summary(filtered_rows)

    return {
        "mode": "campaign_metadata_snapshot_dry_run",
        "snapshot_date": str(snapshot_date),
        "marketplace_code": str(marketplace_code or "ozon"),
        "plan": plan,
        "total_campaigns": len(rows),
        "requested_campaign_ids": campaign_ids,
        "matched_campaign_count": len(filtered_rows),
        "campaign_rows": filtered_rows,
        "present_fields": field_summary["present_fields"],
        "missing_fields": field_summary["missing_fields"],
        "db_writes": 0,
        "campaign_mutations": 0,
        "writes_marketplace_expenses": False,
        "writes_ozon_daily_sku_ad_attribution": False,
        "writes_campaign_metadata": False,
    }


def print_statistics_json_probe_plan(campaign_id, date_from, date_to, cooldown_until):
    summary = {
        "mode": "statistics-json-probe",
        "campaign_id": str(campaign_id),
        "date_from": date_from,
        "date_to": date_to,
        "planned_campaign_units": 1,
        "uses_statistics_json": True,
        "uses_campaign_unit_quota_2000": True,
        "is_daily_flow": False,
        "is_full_cpc_flow": False,
        "writes_marketplace_expenses": False,
        "writes_ozon_daily_sku_ad_attribution": False,
        "cooldown_active": bool(cooldown_until),
        "cooldown_until": to_iso(cooldown_until) if cooldown_until else None,
        "estimated_requests": estimate_statistics_json_probe_requests(),
    }
    print("Ozon Performance statistics/json probe plan:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def save_rows(rows):
    if not rows:
        print("Нет рекламных расходов Ozon Performance для записи")
        return

    aggregated_rows = aggregate_rows(rows)
    print(
        "Ozon Performance rows before/after final aggregation: "
        f"{len(rows)} -> {len(aggregated_rows)}"
    )

    for batch in chunks(aggregated_rows, 500):
        supabase.table("marketplace_expenses").upsert(
            batch,
            on_conflict="expense_date,marketplace_code,marketplace_sku,expense_type",
        ).execute()

    print(f"✅ Ozon Performance ads записаны в marketplace_expenses: {len(aggregated_rows)} строк")


def save_ad_attribution_rows(rows):
    if not rows:
        print("Нет Ozon Performance ad-attribution строк для записи")
        return

    aggregated_rows = aggregate_ad_attribution_rows(rows)
    print(
        "Ozon Performance ad-attribution rows before/after aggregation: "
        f"{len(rows)} -> {len(aggregated_rows)}"
    )

    for batch in chunks(aggregated_rows, 500):
        try:
            supabase.table("ozon_daily_sku_ad_attribution").upsert(
                batch,
                on_conflict="sale_date,marketplace_code,marketplace_sku,ad_source,attribution_type,campaign_id",
            ).execute()
        except Exception as exc:
            print(
                "WARNING: Не удалось записать ad-attribution в ozon_daily_sku_ad_attribution. "
                "Проверьте миграцию sql/20260506_create_ozon_daily_sku_organic.sql. "
                f"Ошибка: {sanitize_text(exc)}"
            )
            return

    print(
        "✅ Ozon Performance ad-attribution записан в ozon_daily_sku_ad_attribution: "
        f"{len(aggregated_rows)} строк"
    )


def read_attempted_campaign_units_for_load_date(load_date, account_signature):
    try:
        result = (
            supabase
            .table(DAILY_LOAD_STATUS_TABLE)
            .select("cpc_campaign_units_attempted")
            .eq("load_date", load_date)
            .eq("marketplace_code", "ozon")
            .eq("account_signature", account_signature)
            .execute()
        )
    except Exception as exc:
        print(
            "WARNING: Не удалось прочитать ozon_performance_daily_load_status для quota budget. "
            f"Ошибка: {sanitize_text(exc)}"
        )
        return 0

    total = 0
    for row in result.data or []:
        total += int(float(row.get("cpc_campaign_units_attempted") or 0))
    return total


def get_daily_load_status(load_date, target_date, account_signature):
    try:
        result = (
            supabase
            .table(DAILY_LOAD_STATUS_TABLE)
            .select("*")
            .eq("load_date", load_date)
            .eq("target_date", target_date)
            .eq("marketplace_code", "ozon")
            .eq("account_signature", account_signature)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        print(
            "WARNING: Не удалось прочитать daily load status. "
            f"Ошибка: {sanitize_text(exc)}"
        )
        return {}

    rows = result.data or []
    return rows[0] if rows else {}


def save_daily_load_status(summary):
    target_date = summary.get("target_date")
    if not target_date:
        return

    payload = {
        "load_date": summary.get("load_date") or today_local().isoformat(),
        "target_date": target_date,
        "marketplace_code": "ozon",
        "account_signature": summary.get("account_signature"),
        "mode": summary.get("mode"),
        "cpc_campaign_count": int(summary.get("campaign_count") or 0),
        "cpc_campaign_units_attempted": int(summary.get("cpc_campaign_units_attempted") or 0),
        "cpc_campaign_units_completed": int(summary.get("cpc_campaign_units_completed") or 0),
        "cpc_pending_campaigns": int(summary.get("cpc_pending_campaigns") or 0),
        "cpc_campaign_units_planned_total": int(summary.get("cpc_campaign_units_planned_total") or 0),
        "cpc_campaign_units_completed_total": int(summary.get("cpc_campaign_units_completed_total") or 0),
        "cpc_campaign_units_pending_total": int(summary.get("cpc_campaign_units_pending_total") or 0),
        "cpc_campaign_units_attempted_this_run": int(summary.get("cpc_campaign_units_attempted_this_run") or 0),
        "cpc_campaign_units_completed_this_run": int(summary.get("cpc_campaign_units_completed_this_run") or 0),
        "cpc_campaign_units_failed_429_this_run": int(summary.get("cpc_campaign_units_failed_429_this_run") or 0),
        "cpc_stop_batch_index": summary.get("cpc_stop_batch_index"),
        "cpc_stop_reason": summary.get("cpc_stop_reason"),
        "cpc_status": (summary.get("cpc") or {}).get("status"),
        "cpo_status": (summary.get("cpo") or {}).get("status"),
        "run_status": summary.get("overall_status"),
        "ad_spend_loaded": float(summary.get("ad_spend_loaded") or 0),
        "ad_attribution_loaded": float(summary.get("ad_attribution_loaded") or 0),
        "created_at": summary.get("created_at") or to_iso(utcnow()),
        "updated_at": summary.get("updated_at") or to_iso(utcnow()),
    }

    try:
        supabase.table(DAILY_LOAD_STATUS_TABLE).upsert(
            payload,
            on_conflict="load_date,target_date,marketplace_code",
        ).execute()
    except Exception as exc:
        print(
            "WARNING: Не удалось записать ozon_performance_daily_load_status. "
            "Проверьте миграцию таблицы daily load status. "
            f"Ошибка: {sanitize_text(exc)}"
        )


def run():
    args = parse_args()

    if not OZON_PERFORMANCE_CLIENT_ID or not OZON_PERFORMANCE_CLIENT_SECRET:
        print("Ozon Performance API не настроен: заполните OZON_PERFORMANCE_CLIENT_ID и OZON_PERFORMANCE_CLIENT_SECRET")
        return

    ensure_single_account_config()

    print(
        "Ozon Performance loader uses single-account storage model: "
        "marketplace_expenses has no account/client_id dimension, "
        "so this project must load exactly one Ozon account per environment."
    )

    date_from, date_to = resolve_date_range(args)
    load_date = today_local().isoformat()
    target_date = date_to if date_from == date_to else None
    client = OzonPerformanceClient()
    requested_batch_size = max(1, int(args.campaign_batch_size or 5))

    if args.mode == "statistics-json-probe":
        if not args.campaign_id:
            raise RuntimeError("statistics-json-probe mode requires --campaign-id")
        probe_campaign_id = str((args.campaign_id or [None])[0] or "")
        if date_from != date_to:
            raise RuntimeError(
                "statistics-json-probe mode supports exactly one calendar day. "
                f"Got {date_from}..{date_to}"
            )

        cooldown_until = client.get_cooldown(client.scoped_state_key("statistics_json"))
        print_statistics_json_probe_plan(probe_campaign_id, date_from, date_to, cooldown_until)
        if args.plan_only:
            return
        if cooldown_until:
            raise RuntimeError(
                f"statistics_json cooldown is active until {to_iso(cooldown_until)}"
            )

        original_runtime_state = client.snapshot_runtime_state()
        http_requests_before = len(client.state.get("request_history", []) or [])

        try:
            try:
                uuid, status, report_data, download_headers = client.fetch_statistics_json_report(
                    [probe_campaign_id],
                    date_from,
                    date_to,
                    args.group_by,
                    allow_recreate=False,
                    return_meta=True,
                )
            except RateLimitPending as exc:
                print("Ozon Performance statistics/json probe result:")
                print(json.dumps({
                    "status": "PENDING_429",
                    "endpoint": exc.endpoint,
                    "retry_after_seconds": exc.retry_after_seconds,
                    "cooldown_until": exc.cooldown_until,
                    "campaign_units_spent": 0,
                }, ensure_ascii=False, indent=2))
                return

            http_requests_after = len(client.state.get("request_history", []) or [])
            probe = analyze_statistics_json_report(report_data)
            content_disposition = str((download_headers or {}).get("content-disposition") or "")
            filename = ""
            match = re.search(r'filename=\"?([^\";]+)\"?', content_disposition, flags=re.IGNORECASE)
            if match:
                filename = match.group(1)
            delta = round(probe["total_spend"] - 25841.80, 2)

            print("Ozon Performance statistics/json probe result:")
            print(json.dumps({
                "status": "PASSED",
                "uuid": uuid,
                "http_requests_count": max(0, http_requests_after - http_requests_before),
                "campaign_units_spent": 1,
                "filename": filename,
                "report_title": probe["campaign_title"],
                "campaign_id": probe["campaign_id"],
                "row_keys": probe["row_keys"],
                "sample_rows": probe["sample_rows"],
                "total_spend": probe["total_spend"],
                "orders_qty": probe["orders_qty"],
                "revenue": probe["revenue"],
                "rate_markers": probe["rate_markers"],
                "matches_25841_80": abs(delta) < 1,
                "difference_vs_25841_80": delta,
                "writes_marketplace_expenses": False,
                "writes_ozon_daily_sku_ad_attribution": False,
            }, ensure_ascii=False, indent=2))
        finally:
            client.restore_runtime_state(original_runtime_state)
            print("statistics/json probe: runtime state restored, no ad table writes")
        return

    if args.mode == "search-promo-report-check":
        if date_from != date_to:
            raise RuntimeError(
                "search-promo-report-check currently supports exactly one calendar day. "
                f"Got {date_from}..{date_to}"
            )
        print_search_promo_report_check_plan(
            date_from,
            date_to,
            args.search_promo_report_type,
            campaign_id=str((args.campaign_id or [None])[0] or ""),
        )
        if args.plan_only:
            return
        if not args.dry_run:
            raise RuntimeError(
                "search-promo-report-check live run is allowed only with --dry-run"
            )

        original_runtime_state = client.snapshot_runtime_state()
        http_requests_before = len(client.state.get("request_history", []) or [])

        try:
            uuid, status, search_promo_csv, download_headers = client.fetch_search_promo_csv(
                args.search_promo_report_type,
                date_from,
                date_to,
                campaign_id=str((args.campaign_id or [None])[0] or ""),
                return_meta=True,
            )
            http_requests_after = len(client.state.get("request_history", []) or [])
            report_check = analyze_cpo_csv(search_promo_csv)
            content_disposition = str((download_headers or {}).get("content-disposition") or "")
            filename = ""
            match = re.search(r'filename=\"?([^\";]+)\"?', content_disposition, flags=re.IGNORECASE)
            if match:
                filename = match.group(1)

            print("Ozon Performance SEARCH_PROMO report check result:")
            print(json.dumps({
                "uuid": uuid,
                "http_requests_count": max(0, http_requests_after - http_requests_before),
                "filename": filename,
                "header_title": report_check["title"],
                "columns": report_check["columns"],
                "row_count": report_check["row_count"],
                "total_spend": report_check["total_spend"],
                "keyword_hits": report_check["keyword_hits"],
                "sample_rows": report_check["sample_rows"],
                "campaign_id": str((args.campaign_id or [None])[0] or ""),
                "writes_marketplace_expenses": False,
                "writes_ozon_daily_sku_ad_attribution": False,
            }, ensure_ascii=False, indent=2))
        finally:
            client.restore_runtime_state(original_runtime_state)
            print("search_promo report check: runtime state restored, no ad table writes")
        return

    if args.mode == "cpo-report-check":
        print_cpo_report_check_plan(date_from, date_to, args.cpo_report_type)
        if args.plan_only:
            return

        original_runtime_state = client.snapshot_runtime_state()
        http_requests_before = len(client.state.get("request_history", []) or [])

        try:
            uuid, status, cpo_csv, download_headers = client.fetch_all_sku_promo_csv(
                args.cpo_report_type,
                date_from,
                date_to,
                return_meta=True,
            )
            http_requests_after = len(client.state.get("request_history", []) or [])
            report_check = analyze_cpo_csv(cpo_csv)
            content_disposition = str((download_headers or {}).get("content-disposition") or "")
            filename = ""
            match = re.search(r'filename=\"?([^\";]+)\"?', content_disposition, flags=re.IGNORECASE)
            if match:
                filename = match.group(1)

            print("Ozon Performance CPO report check result:")
            print(json.dumps({
                "uuid": uuid,
                "http_requests_count": max(0, http_requests_after - http_requests_before),
                "filename": filename,
                "header_title": report_check["title"],
                "columns": report_check["columns"],
                "row_count": report_check["row_count"],
                "total_spend": report_check["total_spend"],
                "keyword_hits": report_check["keyword_hits"],
                "sample_rows": report_check["sample_rows"],
                "writes_marketplace_expenses": False,
                "writes_ozon_daily_sku_ad_attribution": False,
            }, ensure_ascii=False, indent=2))
        finally:
            client.restore_runtime_state(original_runtime_state)
            print("CPO report check: runtime state restored, no ad table writes")
        return

    if args.mode == "cpc-recovery":
        if date_from != date_to:
            raise RuntimeError(
                "cpc-recovery mode supports exactly one calendar day. "
                f"Got {date_from}..{date_to}"
            )
        if args.write and args.no_write:
            raise RuntimeError("cpc-recovery mode does not allow --write and --no-write together")

        summary = run_cpc_recovery_mode(
            client=client,
            target_date=date_from,
            group_by=args.group_by,
            requested_batch_size=requested_batch_size,
            max_stats_campaigns=int(args.max_stats_campaigns or DEFAULT_MAX_STATS_CAMPAIGNS_PER_DAILY_RUN),
            dry_run=bool(args.dry_run or not args.write),
            write=bool(args.write),
            approve_write=bool(args.approve_cpc_recovery_write),
            ignore_stale_progress_for_date_only=bool(args.ignore_stale_progress_for_date_only),
            no_write=bool(args.no_write or not args.write),
            db_client=supabase,
            campaign_ids=list(args.campaign_id or []),
            existing_report_uuid=str(args.existing_report_uuid or ""),
        )
        print("Ozon Performance CPC recovery summary:")
        print(json.dumps(sanitize_value(summary), ensure_ascii=False, indent=2))
        return

    if args.mode == "cpc-backfill" and args.plan_only:
        ensure_cpc_backfill_window_open()
        if date_from != date_to:
            raise RuntimeError(
                "cpc-backfill mode supports exactly one calendar day. "
                f"Got {date_from}..{date_to}"
            )
        existing_progress_key, existing_progress, source_progress_kind = resolve_cpc_backfill_progress(
            client,
            target_date,
        )
        if not existing_progress:
            raise RuntimeError(
                f"No pending CPC progress found for target_date={target_date}"
            )
        daily_target = (today_local() - timedelta(days=1)).isoformat()
        daily_status = get_daily_load_status(load_date, daily_target, client.account_signature)
        allow_before_daily_status = should_allow_cpc_backfill_before_daily_status(args, existing_progress)
        if not daily_status and not allow_before_daily_status:
            raise RuntimeError(
                "cpc-backfill mode requires today's daily-yesterday run to be written first. "
                f"No daily load status found for load_date={load_date}, target_date={daily_target}."
            )

        saved_ordered_campaign_ids = preserve_campaign_id_order(
            existing_progress.get("ordered_campaign_ids") or []
        )
        ordering_source = (
            "saved_ordered_campaign_ids"
            if saved_ordered_campaign_ids
            else "deterministic_sort_fallback"
        )
        ordering_warning = None
        if not saved_ordered_campaign_ids:
            ordering_warning = (
                "existing progress has no ordered_campaign_ids; batch resume ordering cannot be "
                "strictly confirmed"
            )

        saved_campaign_list_hash = (
            existing_progress.get("campaign_list_hash")
            or existing_progress.get("campaign_hash")
        )
        if saved_ordered_campaign_ids and not saved_campaign_list_hash:
            saved_campaign_list_hash = compute_campaign_list_hash(saved_ordered_campaign_ids)

        batch_size = int(existing_progress.get("batch_size") or requested_batch_size or 1)
        total_campaigns = int(existing_progress.get("total_campaigns") or len(saved_ordered_campaign_ids))
        pending_batch_indexes = list(existing_progress.get("pending_batch_indexes") or [])
        completed_batch_indexes = list(existing_progress.get("completed_batch_indexes") or [])
        completed_campaign_units = (
            sum_campaign_units_for_batches(
                build_cpc_batches(saved_ordered_campaign_ids, batch_size),
                completed_batch_indexes,
            )
            if saved_ordered_campaign_ids
            else int(existing_progress.get("completed_batches") or 0) * batch_size
        )
        pending_campaign_units = max(0, total_campaigns - completed_campaign_units)
        first_pending_batch_index = pending_batch_indexes[0] if pending_batch_indexes else None
        first_pending_batch_campaign_ids = []
        if saved_ordered_campaign_ids:
            saved_batches = build_cpc_batches(saved_ordered_campaign_ids, batch_size)
            if first_pending_batch_index is not None and 0 <= int(first_pending_batch_index) < len(saved_batches):
                first_pending_batch_campaign_ids = saved_batches[int(first_pending_batch_index)]
        pending_campaign_ids = []
        if saved_ordered_campaign_ids and pending_batch_indexes:
            pending_campaign_ids = [
                campaign_id
                for batch_index in pending_batch_indexes
                if 0 <= int(batch_index) < len(saved_batches)
                for campaign_id in saved_batches[int(batch_index)]
            ]

        planning_summary = {
            "mode": args.mode,
            "target_date": target_date,
            "date_from": date_from,
            "date_to": date_to,
            "campaign_scope": "complete_resume",
            "daily_cpc_selection_mode": "complete",
            "planned_operation": "cpc_pending_resume_only",
            "source_progress_kind": source_progress_kind,
            "raw_campaign_count": None,
            "raw_cpc_count": None,
            "filtered_recent_count": None,
            "date_overlap_cpc_count": None,
            "selected_cpc_count": total_campaigns,
            "cpc_campaign_count": total_campaigns,
            "excluded_by_recent_filter_count": None,
            "excluded_by_quota_count": 0,
            "batch_size": batch_size,
            "total_batches": int(existing_progress.get("total_batches") or 0),
            "campaign_units": total_campaigns,
            "daily_limit": STATS_DAILY_CAMPAIGN_LIMIT,
            "reserve": STATS_DAILY_CAMPAIGN_RESERVE,
            "usable_limit": max(0, STATS_DAILY_CAMPAIGN_LIMIT - STATS_DAILY_CAMPAIGN_RESERVE),
            "would_fit_daily_limit": total_campaigns <= max(0, STATS_DAILY_CAMPAIGN_LIMIT - STATS_DAILY_CAMPAIGN_RESERVE),
            "head_campaign_ids": saved_ordered_campaign_ids[:10],
            "ordering_source": ordering_source,
            "campaign_list_hash": saved_campaign_list_hash,
            "saved_campaign_count": len(saved_ordered_campaign_ids) if saved_ordered_campaign_ids else None,
            "current_campaign_count": None,
            "saved_campaign_list_hash": saved_campaign_list_hash,
            "current_campaign_list_hash": None,
            "existing_progress_selected": True,
            "existing_progress_key": existing_progress_key,
            "existing_progress_selection_mode": existing_progress.get("selection_mode"),
            "existing_progress_campaign_count": existing_progress.get("total_campaigns"),
            "existing_progress_batch_size": existing_progress.get("batch_size"),
            "existing_progress_completed_batches": existing_progress.get("completed_batches"),
            "existing_progress_pending_batches": existing_progress.get("pending_batches"),
            "existing_progress_next_batch_index": existing_progress.get("next_batch_index"),
            "pending_batch_indexes": pending_batch_indexes,
            "first_pending_batch_index": first_pending_batch_index,
            "first_pending_batch_campaign_ids": first_pending_batch_campaign_ids,
            "estimated_campaign_units_to_run": pending_campaign_units,
            "pending_campaign_units": pending_campaign_units,
            "pending_campaign_ids": pending_campaign_ids,
            "cpo_status": daily_status.get("cpo_status"),
            "allowed_before_daily_status": allow_before_daily_status,
            "db_writes": 0,
            "create_new_progress_key": False,
            "warning": ordering_warning,
        }
        print("Ozon Performance planning summary:")
        print(json.dumps(sanitize_value(planning_summary), ensure_ascii=False, indent=2))
        return

    campaigns = client.list_campaigns()
    print(f"Ozon Performance campaigns total: {len(campaigns)}")

    period_campaigns = filter_campaigns(campaigns, date_from, date_to, args.campaign_scope)
    print(
        f"Ozon Performance campaigns after {args.campaign_scope} period filter: "
        f"{len(period_campaigns)}"
    )

    daily_selection_mode = (args.daily_cpc_selection_mode or "complete").strip().lower()
    daily_selection = None
    raw_cpc_count = None
    date_overlap_cpc_count = None
    recent_cpc_count = None
    excluded_by_recent_filter_count = 0
    existing_backfill_progress_key = None
    existing_backfill_progress = None
    existing_backfill_progress_source_kind = None

    if args.mode == "daily-yesterday":
        daily_selection = build_daily_cpc_selection(
            campaigns,
            date_from,
            date_to,
            daily_selection_mode,
        )
        raw_cpc_count = len(daily_selection["raw_cpc_campaigns"])
        date_overlap_cpc_count = len(daily_selection["date_overlap_cpc_campaigns"])
        recent_cpc_count = len(daily_selection["recent_cpc_campaigns"])
        excluded_by_recent_filter_count = int(daily_selection["excluded_by_recent_filter_count"] or 0)
        cpc_campaigns = list(daily_selection["selected_campaigns"])
        print(
            "Ozon Performance daily D-1 CPC selection: "
            f"mode={daily_selection_mode} raw_cpc={raw_cpc_count} "
            f"date_overlap_cpc={date_overlap_cpc_count} recent_cpc={recent_cpc_count} "
            f"selected_cpc={len(cpc_campaigns)}"
        )
        if daily_selection_mode == "recent":
            print(
                "WARNING: recent mode may miss D-1 spend; use complete mode for management decisions."
            )
    elif args.mode == "cpc-backfill":
        daily_selection_mode = "complete"
        cpc_campaigns = []
    else:
        cpc_campaigns = filter_cpc_campaigns(campaigns, date_from, date_to, args.campaign_scope)
        print(f"Ozon Performance CPC campaigns after activity filter: {len(cpc_campaigns)}")

    if args.campaign_limit:
        cpc_campaigns = cpc_campaigns[:args.campaign_limit]

    cpc_campaigns_by_id = {
        str(campaign.get("id") or campaign.get("campaignId")): campaign
        for campaign in cpc_campaigns
        if campaign.get("id") or campaign.get("campaignId")
    }
    current_cpc_campaign_ids = deterministic_campaign_id_order(cpc_campaigns_by_id.keys())
    cpc_campaign_ids = list(current_cpc_campaign_ids)
    cpc_activity_sample = {
        campaign_id: cpc_campaigns_by_id[campaign_id].get("_cpc_activity_markers", [])
        for campaign_id in cpc_campaign_ids[:10]
    }

    campaigns_by_id = {
        str(campaign.get("id") or campaign.get("campaignId")): campaign
        for campaign in period_campaigns
        if campaign.get("id") or campaign.get("campaignId")
    }
    print(f"Ozon Performance period campaigns for CPO/context: {len(campaigns_by_id)}")
    print(f"Ozon Performance CPC campaigns for statistics/json: {len(cpc_campaign_ids)}")
    if cpc_activity_sample:
        print("Ozon Performance CPC activity markers sample:")
        print(json.dumps(cpc_activity_sample, ensure_ascii=False))

    if args.mode == "cpc-backfill":
        ensure_cpc_backfill_window_open()
        if date_from != date_to:
            raise RuntimeError(
                "cpc-backfill mode supports exactly one calendar day. "
                f"Got {date_from}..{date_to}"
            )
        existing_backfill_progress_key, existing_backfill_progress, existing_backfill_progress_source_kind = resolve_cpc_backfill_progress(
            client,
            target_date,
        )
        if not existing_backfill_progress:
            raise RuntimeError(
                f"No pending CPC progress found for target_date={target_date}"
            )
        daily_target = (today_local() - timedelta(days=1)).isoformat()
        daily_status = get_daily_load_status(load_date, daily_target, client.account_signature)
        allow_before_daily_status = should_allow_cpc_backfill_before_daily_status(args, existing_backfill_progress)
        if not daily_status and not allow_before_daily_status:
            raise RuntimeError(
                "cpc-backfill mode requires today's daily-yesterday run to be written first. "
                f"No daily load status found for load_date={load_date}, target_date={daily_target}."
            )
        saved_ordered_campaign_ids = preserve_campaign_id_order(
            existing_backfill_progress.get("ordered_campaign_ids") or []
        )
        cpc_campaigns = [
            campaign
            for campaign in campaigns
            if str(campaign.get("id") or campaign.get("campaignId") or "") in set(saved_ordered_campaign_ids)
        ]
        raw_cpc_count = len(saved_ordered_campaign_ids)
        date_overlap_cpc_count = len(saved_ordered_campaign_ids)
        recent_cpc_count = len(saved_ordered_campaign_ids)
        excluded_by_recent_filter_count = 0

    ordering_source = "deterministic_sort"
    ordering_warning = None
    saved_campaign_count = None
    saved_campaign_list_hash = None
    current_campaign_list_hash = compute_campaign_list_hash(current_cpc_campaign_ids)
    ordered_campaign_ids = list(current_cpc_campaign_ids)

    if args.mode == "cpc-backfill" and existing_backfill_progress:
        saved_ordered_campaign_ids = preserve_campaign_id_order(
            existing_backfill_progress.get("ordered_campaign_ids") or []
        )
        saved_campaign_count = len(saved_ordered_campaign_ids) if saved_ordered_campaign_ids else None
        saved_campaign_list_hash = (
            existing_backfill_progress.get("campaign_list_hash")
            or existing_backfill_progress.get("campaign_hash")
        )

        if saved_ordered_campaign_ids:
            ordered_campaign_ids = list(saved_ordered_campaign_ids)
            ordering_source = "saved_ordered_campaign_ids"
            if not saved_campaign_list_hash:
                saved_campaign_list_hash = compute_campaign_list_hash(saved_ordered_campaign_ids)
            if current_campaign_list_hash != saved_campaign_list_hash:
                ordering_warning = (
                    "current campaign list differs from saved ordered progress; "
                    "resume will use saved_ordered_campaign_ids"
                )
        else:
            ordering_source = "deterministic_sort_fallback"
            ordering_warning = (
                "existing progress has no ordered_campaign_ids; batch resume ordering cannot be "
                "strictly confirmed"
            )

    if args.mode == "cpc-backfill" and existing_backfill_progress:
        batch_size = int(existing_backfill_progress.get("batch_size") or requested_batch_size or 1)
    else:
        batch_size = min(
            requested_batch_size,
            client.get_batch_recommendation(
                client.scoped_state_key("statistics_json"),
                requested_batch_size,
            ),
        )
    cpc_batches = build_cpc_batches(ordered_campaign_ids, batch_size)
    usable_daily_limit = max(0, STATS_DAILY_CAMPAIGN_LIMIT - STATS_DAILY_CAMPAIGN_RESERVE)
    excluded_by_quota_count = max(0, len(ordered_campaign_ids) - usable_daily_limit)
    first_pending_batch_index = (
        ((existing_backfill_progress or {}).get("pending_batch_indexes") or [None])[0]
        if existing_backfill_progress
        else None
    )
    first_pending_batch_campaign_ids = (
        cpc_batches[first_pending_batch_index]
        if first_pending_batch_index is not None and 0 <= int(first_pending_batch_index) < len(cpc_batches)
        else []
    )
    planning_summary = {
        "mode": args.mode,
        "target_date": target_date,
        "date_from": date_from,
        "date_to": date_to,
        "campaign_scope": args.campaign_scope,
        "daily_cpc_selection_mode": daily_selection_mode if args.mode == "daily-yesterday" else None,
        "raw_campaign_count": len(campaigns),
        "raw_cpc_count": raw_cpc_count if raw_cpc_count is not None else len(
            [campaign for campaign in campaigns if is_cpc_campaign(campaign)]
        ),
        "filtered_recent_count": recent_cpc_count if recent_cpc_count is not None else len(current_cpc_campaign_ids),
        "date_overlap_cpc_count": date_overlap_cpc_count if date_overlap_cpc_count is not None else len(current_cpc_campaign_ids),
        "selected_cpc_count": len(ordered_campaign_ids),
        "cpc_campaign_count": len(ordered_campaign_ids),
        "excluded_by_recent_filter_count": excluded_by_recent_filter_count,
        "excluded_by_quota_count": excluded_by_quota_count,
        "batch_size": batch_size,
        "total_batches": len(cpc_batches),
        "campaign_units": len(ordered_campaign_ids),
        "daily_limit": STATS_DAILY_CAMPAIGN_LIMIT,
        "reserve": STATS_DAILY_CAMPAIGN_RESERVE,
        "usable_limit": usable_daily_limit,
        "would_fit_daily_limit": len(ordered_campaign_ids) <= usable_daily_limit,
        "head_campaign_ids": ordered_campaign_ids[:10],
        "ordering_source": ordering_source,
        "campaign_list_hash": compute_campaign_list_hash(ordered_campaign_ids),
        "saved_campaign_count": saved_campaign_count,
        "current_campaign_count": len(current_cpc_campaign_ids),
        "saved_campaign_list_hash": saved_campaign_list_hash,
        "current_campaign_list_hash": current_campaign_list_hash,
        "existing_progress_selected": bool(existing_backfill_progress),
        "existing_progress_key": existing_backfill_progress_key,
        "source_progress_kind": existing_backfill_progress_source_kind,
        "existing_progress_selection_mode": (
            (existing_backfill_progress or {}).get("selection_mode")
            if existing_backfill_progress
            else None
        ),
        "existing_progress_campaign_count": (
            (existing_backfill_progress or {}).get("total_campaigns")
            if existing_backfill_progress
            else None
        ),
        "existing_progress_batch_size": (
            (existing_backfill_progress or {}).get("batch_size")
            if existing_backfill_progress
            else None
        ),
        "existing_progress_completed_batches": (
            (existing_backfill_progress or {}).get("completed_batches")
            if existing_backfill_progress
            else None
        ),
        "existing_progress_pending_batches": (
            (existing_backfill_progress or {}).get("pending_batches")
            if existing_backfill_progress
            else None
        ),
        "existing_progress_next_batch_index": (
            (existing_backfill_progress or {}).get("next_batch_index")
            if existing_backfill_progress
            else None
        ),
        "first_pending_batch_index": first_pending_batch_index,
        "first_pending_batch_campaign_ids": first_pending_batch_campaign_ids,
        "pending_batch_indexes": (
            (existing_backfill_progress or {}).get("pending_batch_indexes")
            if existing_backfill_progress
            else None
        ),
        "estimated_campaign_units_to_run": sum_campaign_units_for_batches(
            cpc_batches,
            (existing_backfill_progress or {}).get("pending_batch_indexes") or [],
        ) if existing_backfill_progress else len(ordered_campaign_ids),
        "create_new_progress_key": not bool(existing_backfill_progress),
        "warning": ordering_warning,
    }
    print("Ozon Performance planning summary:")
    print(json.dumps(sanitize_value(planning_summary), ensure_ascii=False, indent=2))

    if args.plan_only:
        return

    daily_campaign_budget = max(
        0,
        min(
            int(args.max_stats_campaigns or DEFAULT_MAX_STATS_CAMPAIGNS_PER_DAILY_RUN),
            usable_daily_limit,
        ),
    )
    attempted_units_before_run = read_attempted_campaign_units_for_load_date(load_date, client.account_signature)
    remaining_campaign_budget = max(0, daily_campaign_budget - attempted_units_before_run)
    max_cpc_batches = args.max_cpc_batches
    if max_cpc_batches in (None, 0):
        if args.mode == "cpc-backfill":
            max_cpc_batches = DEFAULT_MAX_CPC_BATCHES_PER_RUN
        else:
            max_cpc_batches = None
    if args.mode == "cpc-backfill" and existing_backfill_progress_key:
        progress_key = existing_backfill_progress_key
    else:
        progress_key = build_cpc_progress_key(date_from, date_to, batch_size, ordered_campaign_ids, args.group_by)
    original_runtime_state = client.snapshot_runtime_state()
    progress_context = client.build_cpc_progress_context(
        date_from,
        date_to,
        batch_size,
        ordered_campaign_ids,
        args.group_by,
        selection_mode=daily_selection_mode if args.mode in {"daily-yesterday", "cpc-backfill"} else None,
        campaign_scope=args.campaign_scope,
    )
    cpc_progress = client.init_cpc_progress(progress_key, progress_context, cpc_batches)

    if args.mode == "cpc-backfill":
        if not cpc_progress.get("pending_batch_indexes"):
            raise RuntimeError(
                "Нет pending CPC batches для backfill. Сначала нужен bounded/partial run "
                "с незавершённым CPC-хвостом за этот день."
            )
        max_cpc_batches = max(1, int(args.max_cpc_batches or 1))

    run_summary = {
        "mode": args.mode,
        "load_date": load_date,
        "target_date": target_date,
        "date_from": date_from,
        "date_to": date_to,
        "account_signature": client.account_signature,
        "period_campaign_count": len(campaigns_by_id),
        "campaign_count": len(ordered_campaign_ids),
        "cpc_campaign_units_total": len(ordered_campaign_ids),
        "requested_batch_size": requested_batch_size,
        "batch_size": batch_size,
        "daily_cpc_selection_mode": daily_selection_mode if args.mode == "daily-yesterday" else None,
        "raw_cpc_count": planning_summary.get("raw_cpc_count"),
        "date_overlap_cpc_count": planning_summary.get("date_overlap_cpc_count"),
        "excluded_by_recent_filter_count": planning_summary.get("excluded_by_recent_filter_count"),
        "excluded_by_quota_count": planning_summary.get("excluded_by_quota_count"),
        "ordering_source": ordering_source,
        "campaign_list_hash": planning_summary.get("campaign_list_hash"),
        "max_batches_per_run": max_cpc_batches,
        "daily_stats_campaign_limit": STATS_DAILY_CAMPAIGN_LIMIT,
        "daily_stats_campaign_reserve": STATS_DAILY_CAMPAIGN_RESERVE,
        "daily_campaign_budget": daily_campaign_budget,
        "attempted_campaign_units_before_run": attempted_units_before_run,
        "remaining_campaign_budget": remaining_campaign_budget,
        "max_stats_campaigns_per_run": int(args.max_stats_campaigns or DEFAULT_MAX_STATS_CAMPAIGNS_PER_DAILY_RUN),
        "state_backend": client.state_backend,
        "cpc_progress_key": progress_key,
        "cpc": empty_stage_status("not_started"),
        "cpo": empty_stage_status("not_started"),
        "selected_cpo": {
            "selected_cpo_enabled": ENABLE_OZON_SELECTED_CPO_DAILY,
            "date": target_date,
            "status": "skipped",
            "reason": "feature_flag_disabled" if not ENABLE_OZON_SELECTED_CPO_DAILY else "not_run_yet",
            "source_rows": 0,
            "source_sum": 0.0,
            "marketplace_expenses_rows": 0,
            "marketplace_expenses_sum": 0.0,
            "ad_attribution_rows": 0,
            "ad_attribution_sum": 0.0,
            "totals_match": False,
            "db_writes": 0,
        },
        "overall_status": "running",
        "created_at": to_iso(utcnow()),
        "updated_at": to_iso(utcnow()),
    }
    print("Ozon Performance run context:")
    print(json.dumps(sanitize_value(run_summary), ensure_ascii=False))
    client.write_run_status(run_summary)

    catalog = load_catalog()
    rows_by_key = {}
    ad_attribution_rows = []
    total_counters = defaultdict(int)
    cpc_current_run_expense_rows_count = 0
    cpc_current_run_ad_attribution_rows_count = 0

    cpc_batches_total = len(cpc_batches)
    processed_batch_indexes = []
    cpc_progress_snapshot = client.get_cpc_progress(progress_key)
    completed_batch_indexes_before_run = list(cpc_progress_snapshot.get("completed_batch_indexes") or [])
    failed_429_batch_indexes_before_run = list(cpc_progress_snapshot.get("failed_429_batch_indexes") or [])

    if not ordered_campaign_ids:
        run_summary["cpc"] = empty_stage_status(
            "skipped",
            reason="no_cpc_campaigns_for_period",
            batch_size=batch_size,
            campaign_count=0,
            campaign_units_attempted=0,
            campaign_units_completed=0,
            total_batches=0,
            processed_batches=0,
            completed_batches=0,
            pending_batches=0,
        )
    else:
        if args.mode == "cpc-backfill":
            target_batch_indexes = list(cpc_progress_snapshot.get("pending_batch_indexes") or [])
        else:
            target_batch_indexes = list(cpc_progress_snapshot.get("pending_batch_indexes") or [])

        if args.mode in {"daily-yesterday", "cpc-backfill"}:
            target_batch_indexes, planned_campaign_units = build_limited_batch_indexes(
                cpc_batches,
                target_batch_indexes,
                remaining_campaign_budget,
            )
            if max_cpc_batches:
                target_batch_indexes = target_batch_indexes[:max_cpc_batches]
                planned_campaign_units = sum_campaign_units_for_batches(cpc_batches, target_batch_indexes)
        else:
            if max_cpc_batches:
                target_batch_indexes = target_batch_indexes[:max_cpc_batches]
            planned_campaign_units = sum_campaign_units_for_batches(cpc_batches, target_batch_indexes)

        run_summary["cpc_campaign_units_planned"] = planned_campaign_units
        run_summary["cpc_pending_campaigns_before_run"] = max(0, len(ordered_campaign_ids) - planned_campaign_units)
        run_summary["quota_limited"] = planned_campaign_units < len(ordered_campaign_ids)

        print(
            "Ozon Performance CPC batch plan: "
            f"campaign_count={len(ordered_campaign_ids)} batch_size={batch_size} "
            f"planned_campaign_units={planned_campaign_units} "
            f"max_batches_per_run={max_cpc_batches} total_batches={cpc_batches_total} "
            f"next_batch_index={cpc_progress_snapshot.get('next_batch_index')} "
            f"target_batches={target_batch_indexes}"
        )

        for batch_index in target_batch_indexes:
            if batch_index >= len(cpc_batches):
                continue
            campaign_batch = cpc_batches[batch_index]
            processed_batch_indexes.append(batch_index)
            print(f"Запрашиваем Ozon Performance CPC statistics/json batch: {campaign_batch}")
            try:
                uuid, _, report_data = client.fetch_statistics_json_report(
                    campaign_batch,
                    date_from,
                    date_to,
                    args.group_by,
                    allow_recreate=(args.mode != "cpc-backfill"),
                )
            except RateLimitPending as exc:
                client.set_batch_recommendation(
                    client.scoped_state_key("statistics_json"),
                    1,
                    ttl_seconds=BATCH_SIZE_RECOVERY_TTL_SECONDS,
                )
                cpc_progress_snapshot = client.mark_cpc_batch_pending_429(progress_key, batch_index)
                run_summary["cpc"] = empty_stage_status(
                    "pending_429",
                    endpoint=exc.endpoint,
                    batch_size=batch_size,
                    retry_after_seconds=exc.retry_after_seconds,
                    cooldown_until=exc.cooldown_until,
                    attempt=exc.attempt,
                    failed_batch_index=batch_index,
                    failed_batch=campaign_batch,
                    campaign_count=len(ordered_campaign_ids),
                    campaign_units_attempted=sum_campaign_units_for_batches(cpc_batches, processed_batch_indexes),
                    campaign_units_completed=sum_campaign_units_for_batches(
                        cpc_batches,
                        cpc_progress_snapshot.get("completed_batch_indexes"),
                    ),
                    total_batches=cpc_batches_total,
                    max_batches_per_run=max_cpc_batches,
                    processed_batches=len(processed_batch_indexes),
                    completed_batches=cpc_progress_snapshot.get("completed_batches", 0),
                    pending_batches=cpc_progress_snapshot.get("pending_batches", 0),
                    failed_429_batches=cpc_progress_snapshot.get("failed_429_batches", 0),
                    pending_campaigns=max(
                        0,
                        len(ordered_campaign_ids)
                        - sum_campaign_units_for_batches(
                            cpc_batches,
                            cpc_progress_snapshot.get("completed_batch_indexes"),
                        ),
                    ),
                )
                print("CPC statistics/json pending_429:")
                print(json.dumps(sanitize_value(run_summary["cpc"]), ensure_ascii=False))
                break
            except Exception as exc:
                run_summary["cpc"] = empty_stage_status(
                    "failed",
                    batch_size=batch_size,
                    failed_batch_index=batch_index,
                    failed_batch=campaign_batch,
                    error=str(exc),
                )
                run_summary["overall_status"] = "failed"
                run_summary["updated_at"] = to_iso(utcnow())
                client.write_run_status(run_summary)
                save_daily_load_status(run_summary)
                raise
            print(f"CPC UUID: {uuid}")
            cpc_progress_snapshot = client.mark_cpc_batch_completed(progress_key, batch_index)

            if args.debug_sample:
                print(json.dumps(report_data, ensure_ascii=False, indent=2)[:5000])

            rows, counters = build_rows(report_data, campaigns_by_id, date_from)
            attribution_rows, attribution_counters = build_cpc_attribution_rows(report_data, date_from)
            cpc_current_run_expense_rows_count += sum(
                1
                for row in rows
                if str(row.get("expense_type") or "") in {"advertising_clicks", "advertising_other"}
            )
            cpc_current_run_ad_attribution_rows_count += sum(
                1
                for row in attribution_rows
                if str(row.get("ad_source") or "") == "cpc"
            )

            for row in rows:
                key = (
                    row["expense_date"],
                    row["marketplace_sku"],
                    row["expense_type"],
                )
                if key not in rows_by_key:
                    rows_by_key[key] = row
                else:
                    rows_by_key[key]["expense_amount"] += row["expense_amount"]

            for key, value in counters.items():
                total_counters[key] += value
            ad_attribution_rows.extend(attribution_rows)
            for key, value in attribution_counters.items():
                total_counters[f"cpc_attribution_{key}"] += value

            time.sleep(2)

        if run_summary["cpc"]["status"] == "not_started":
            cpc_progress_snapshot = client.get_cpc_progress(progress_key)
            cpc_status = "success"
            if target_batch_indexes == [] and cpc_progress_snapshot.get("pending_batches"):
                cpc_status = "pending_quota" if args.mode in {"daily-yesterday", "cpc-backfill"} else "pending_backfill"
            elif cpc_progress_snapshot.get("pending_batches"):
                if args.mode == "daily-yesterday" and run_summary.get("quota_limited"):
                    cpc_status = "pending_quota"
                else:
                    cpc_status = "pending_backfill"
            completed_campaign_units = sum_campaign_units_for_batches(
                cpc_batches,
                cpc_progress_snapshot.get("completed_batch_indexes"),
            )
            run_summary["cpc"] = empty_stage_status(
                cpc_status,
                batch_size=batch_size,
                campaign_count=len(ordered_campaign_ids),
                campaign_units_attempted=sum_campaign_units_for_batches(cpc_batches, processed_batch_indexes),
                campaign_units_completed=completed_campaign_units,
                total_batches=cpc_batches_total,
                max_batches_per_run=max_cpc_batches,
                processed_batches=len(processed_batch_indexes),
                completed_batches=cpc_progress_snapshot.get("completed_batches", 0),
                pending_batches=cpc_progress_snapshot.get("pending_batches", 0),
                failed_429_batches=cpc_progress_snapshot.get("failed_429_batches", 0),
                next_batch_index=cpc_progress_snapshot.get("next_batch_index"),
                pending_campaigns=max(0, len(ordered_campaign_ids) - completed_campaign_units),
            )
            if cpc_status == "success":
                client.clear_batch_recommendation(client.scoped_state_key("statistics_json"))

        guard_summary = None
        if run_summary.get("cpc", {}).get("status") == "success":
            try:
                guard_summary = guard_cpc_materialization(
                    target_date=target_date,
                    cpc_status=run_summary["cpc"]["status"],
                    pending_batches=run_summary["cpc"].get("pending_batches", 0),
                    processed_batches_this_run=len(processed_batch_indexes),
                    current_run_cpc_expense_rows_count=cpc_current_run_expense_rows_count,
                    current_run_cpc_ad_attribution_rows_count=cpc_current_run_ad_attribution_rows_count,
                    db_client=supabase,
                )
            except CpcMaterializationGuardError as exc:
                run_summary["cpc"] = empty_stage_status(
                    "failed_materialization_guard",
                    batch_size=batch_size,
                    campaign_count=len(ordered_campaign_ids),
                    processed_batches=len(processed_batch_indexes),
                    completed_batches=cpc_progress_snapshot.get("completed_batches", 0),
                    pending_batches=cpc_progress_snapshot.get("pending_batches", 0),
                    error=str(exc),
                    current_run_cpc_expense_rows_count=cpc_current_run_expense_rows_count,
                    current_run_cpc_ad_attribution_rows_count=cpc_current_run_ad_attribution_rows_count,
                )
                run_summary["overall_status"] = "failed"
                run_summary["updated_at"] = to_iso(utcnow())
                client.write_run_status(run_summary)
                save_daily_load_status(run_summary)
                raise

            if guard_summary:
                run_summary["cpc"]["current_run_cpc_expense_rows_count"] = cpc_current_run_expense_rows_count
                run_summary["cpc"]["current_run_cpc_ad_attribution_rows_count"] = (
                    cpc_current_run_ad_attribution_rows_count
                )
                run_summary["cpc"]["downstream_verification"] = guard_summary.get("downstream_verification")
                if guard_summary.get("status_override"):
                    run_summary["cpc"]["status"] = guard_summary["status_override"]

    if args.mode == "cpc-backfill":
        run_summary["cpo"] = empty_stage_status(
            "skipped",
            reason="cpc_backfill_mode",
        )
    else:
        print("Запрашиваем Ozon all_sku_promo orders report")
        try:
            cpo_uuid, cpo_status, cpo_csv = client.fetch_all_sku_promo_csv("orders", date_from, date_to)
            print(f"CPO UUID: {cpo_uuid}")
            cpo_rows, cpo_counters, cpo_summary = build_cpo_rows(cpo_csv)
            cpo_attribution_rows, cpo_attribution_counters = build_cpo_attribution_rows(cpo_csv)
        except Exception as exc:
            run_summary["cpo"] = empty_stage_status("failed", error=str(exc))
            run_summary["overall_status"] = "failed"
            run_summary["updated_at"] = to_iso(utcnow())
            client.write_run_status(run_summary)
            save_daily_load_status(run_summary)
            raise

        for row in cpo_rows:
            key = (
                row["expense_date"],
                row["marketplace_sku"],
                row["expense_type"],
            )
            if key not in rows_by_key:
                rows_by_key[key] = row
            else:
                rows_by_key[key]["expense_amount"] += row["expense_amount"]

        for key, value in cpo_counters.items():
            total_counters[key] += value
        ad_attribution_rows.extend(cpo_attribution_rows)
        for key, value in cpo_attribution_counters.items():
            total_counters[f"cpo_attribution_{key}"] += value

        print("CPO reconciliation:")
        print(cpo_summary)
        cpo_context = build_cpo_reconciliation_context(date_from, date_to, cpo_uuid, cpo_summary)
        print("CPO reconciliation context:")
        print(json.dumps(cpo_context, ensure_ascii=False))
        difference_abs = abs(float(cpo_summary["difference"]))
        if difference_abs > RECON_ERROR_THRESHOLD:
            message = (
                "CPO expense reconciliation difference is above error threshold: "
                f"{json.dumps(cpo_context, ensure_ascii=False)}"
            )
            print(f"ERROR: {message}")
            run_summary["cpo"] = empty_stage_status(
                "failed",
                uuid=cpo_uuid,
                reconciliation=cpo_summary,
                error=message,
            )
            run_summary["overall_status"] = "failed"
            run_summary["updated_at"] = to_iso(utcnow())
            client.write_run_status(run_summary)
            save_daily_load_status(run_summary)
            raise RuntimeError(message)
        elif difference_abs > RECON_WARNING_THRESHOLD:
            print(
                "WARNING: CPO expense reconciliation difference is above rounding tolerance: "
                f"{json.dumps(cpo_context, ensure_ascii=False)}"
            )

        if cpo_counters.get("advertising_order_other"):
            print(
                "WARNING: Найдены CPO-ставки вне 5% и 10%, "
                f"строк: {cpo_counters['advertising_order_other']}"
            )

        run_summary["cpo"] = empty_stage_status(
            "success",
            uuid=cpo_uuid,
            rows=len(cpo_rows),
            counters=dict(cpo_counters),
            reconciliation=cpo_summary,
        )

    rows = enrich_rows(list(rows_by_key.values()), catalog)
    for row in ad_attribution_rows:
        catalog_row = catalog.get(row["marketplace_sku"])
        if not catalog_row:
            continue
        if not row.get("article"):
            row["article"] = catalog_row.get("article") or ""
        if not row.get("product_name"):
            row["product_name"] = catalog_row.get("product_name") or ""

    print("Ozon Performance counters:")
    print(dict(total_counters))
    print("Ozon Performance rows by type:")
    by_type = defaultdict(float)
    for row in rows:
        by_type[row["expense_type"]] += float(row.get("expense_amount") or 0)
    print({key: round(value, 2) for key, value in sorted(by_type.items())})

    cpc_progress_snapshot = client.get_cpc_progress(progress_key)
    completed_batch_indexes_after_run = list(cpc_progress_snapshot.get("completed_batch_indexes") or [])
    failed_429_batch_indexes_after_run = list(cpc_progress_snapshot.get("failed_429_batch_indexes") or [])
    completed_campaign_units_total = sum_campaign_units_for_batches(
        cpc_batches,
        completed_batch_indexes_after_run,
    )
    completed_campaign_units_before_run = sum_campaign_units_for_batches(
        cpc_batches,
        completed_batch_indexes_before_run,
    )
    completed_campaign_units_this_run = max(
        0,
        completed_campaign_units_total - completed_campaign_units_before_run,
    )
    attempted_campaign_units_this_run = sum_campaign_units_for_batches(cpc_batches, processed_batch_indexes)
    failed_429_campaign_units_this_run = max(
        0,
        sum_campaign_units_for_batches(cpc_batches, failed_429_batch_indexes_after_run)
        - sum_campaign_units_for_batches(cpc_batches, failed_429_batch_indexes_before_run),
    )
    pending_campaign_units_total = max(0, len(ordered_campaign_ids) - completed_campaign_units_total)
    run_summary["processed_batches"] = len(processed_batch_indexes)
    run_summary["completed_batches"] = cpc_progress_snapshot.get("completed_batches", 0)
    run_summary["pending_batches"] = cpc_progress_snapshot.get("pending_batches", 0)
    run_summary["failed_429_batches"] = cpc_progress_snapshot.get("failed_429_batches", 0)
    run_summary["cpc_campaign_units_attempted"] = attempted_campaign_units_this_run
    run_summary["cpc_campaign_units_completed"] = completed_campaign_units_this_run
    run_summary["cpc_pending_campaigns"] = pending_campaign_units_total
    run_summary["cpc_campaign_units_planned_total"] = len(ordered_campaign_ids)
    run_summary["cpc_campaign_units_completed_total"] = completed_campaign_units_total
    run_summary["cpc_campaign_units_pending_total"] = pending_campaign_units_total
    run_summary["cpc_campaign_units_attempted_this_run"] = attempted_campaign_units_this_run
    run_summary["cpc_campaign_units_completed_this_run"] = completed_campaign_units_this_run
    run_summary["cpc_campaign_units_failed_429_this_run"] = failed_429_campaign_units_this_run
    run_summary["cpc_stop_batch_index"] = (run_summary.get("cpc") or {}).get("failed_batch_index")
    run_summary["cpc_stop_reason"] = "429" if (run_summary.get("cpc") or {}).get("status") == "pending_429" else None
    if run_summary.get("cpc"):
        run_summary["cpc"]["campaign_units_attempted"] = attempted_campaign_units_this_run
        run_summary["cpc"]["campaign_units_completed"] = completed_campaign_units_this_run
        run_summary["cpc"]["campaign_units_completed_total"] = completed_campaign_units_total
        run_summary["cpc"]["campaign_units_pending_total"] = pending_campaign_units_total
        run_summary["cpc"]["campaign_units_failed_429_this_run"] = failed_429_campaign_units_this_run
    run_summary["ad_spend_loaded"] = round(sum(by_type.values()), 2)
    run_summary["ad_attribution_loaded"] = round(
        sum(float(row.get("ad_orders_revenue") or 0) for row in ad_attribution_rows),
        2,
    )

    if args.mode == "cpc-backfill":
        if run_summary["cpc"]["status"] in {"success", "success_existing_downstream_verified"}:
            run_summary["overall_status"] = "success"
        elif run_summary["cpc"]["status"] == "pending_quota":
            run_summary["overall_status"] = "partial_quota"
        elif run_summary["cpc"]["status"] in {"pending_429", "pending_backfill"}:
            run_summary["overall_status"] = "partial_ads"
        elif run_summary["cpc"]["status"] == "skipped":
            run_summary["overall_status"] = "skipped"
        else:
            run_summary["overall_status"] = "failed"
    elif run_summary["cpc"]["status"] == "pending_quota" and run_summary["cpo"]["status"] == "success":
        run_summary["overall_status"] = "partial_quota"
    elif run_summary["cpc"]["status"] in {"pending_429", "pending_backfill"} and run_summary["cpo"]["status"] == "success":
        run_summary["overall_status"] = "partial_ads"
    elif run_summary["cpc"]["status"] in {"success", "success_existing_downstream_verified"} and run_summary["cpo"]["status"] == "success":
        run_summary["overall_status"] = "success"
    elif run_summary["cpc"]["status"] == "skipped" and run_summary["cpo"]["status"] == "success":
        run_summary["overall_status"] = "success"
    else:
        run_summary["overall_status"] = "failed"

    run_summary["rows_by_type"] = {
        key: round(value, 2)
        for key, value in sorted(by_type.items())
    }

    if args.mode == "daily-yesterday":
        run_summary["selected_cpo"] = client.load_ozon_selected_cpo_for_date(
            target_date,
            write=not args.dry_run,
            dry_run=True,
            approve_write=APPROVE_OZON_SELECTED_CPO_DAILY_WRITE,
            enabled=ENABLE_OZON_SELECTED_CPO_DAILY,
            db_client=supabase if not args.dry_run else None,
            skip_write_if_not_approved=True,
        )

    run_summary["updated_at"] = to_iso(utcnow())
    print("Ozon Performance run summary:")
    print(json.dumps(sanitize_value(run_summary), ensure_ascii=False))

    if args.dry_run:
        client.restore_runtime_state(original_runtime_state)
        if not args.no_write:
            client.write_run_status(run_summary)
        print("Dry run: данные не записывались")
        return

    save_rows(rows)
    save_ad_attribution_rows(ad_attribution_rows)
    client.write_run_status(run_summary)
    save_daily_load_status(run_summary)

    if run_summary["overall_status"] == "partial_ads":
        print("WARNING: Ozon Performance completed with partial_ads status")
        send_telegram_partial_ads_alert(run_summary)


if __name__ == "__main__":
    run()
