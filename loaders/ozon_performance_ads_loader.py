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
        choices=("daily-yesterday", "full", "cpc-backfill"),
        default=default_mode,
        help=(
            "daily-yesterday = production D-1 load in Europe/Moscow; "
            "full = explicit date/date-range historical run; "
            "cpc-backfill = retry only pending CPC for one day"
        ),
    )
    parser.add_argument("--date", help="single-day shortcut, sets both --date-from and --date-to")
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
    parser.add_argument("--dry-run", action="store_true")
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


def build_cpc_batches(campaign_ids, batch_size):
    if not campaign_ids:
        return []
    size = max(1, int(batch_size or 1))
    return [list(batch) for batch in chunks(list(campaign_ids), size)]


def cpc_progress_cache_identity(date_from, date_to, batch_size, campaign_ids, group_by):
    return {
        "date_from": date_from,
        "date_to": date_to,
        "account_signature": mask_client_id(OZON_PERFORMANCE_CLIENT_ID),
        "batch_size": int(batch_size or 1),
        "campaigns": sorted(str(campaign_id) for campaign_id in campaign_ids),
        "group_by": str(group_by or ""),
    }


def build_cpc_progress_key(date_from, date_to, batch_size, campaign_ids, group_by):
    identity = cpc_progress_cache_identity(date_from, date_to, batch_size, campaign_ids, group_by)
    return f"cpc_progress:{payload_hash(identity)}"


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
            try:
                supabase.table(PIPELINE_RUNTIME_STATE_TABLE).delete().in_("state_key", expired_keys).execute()
            except Exception as exc:
                print(f"Не удалось очистить expired runtime state rows: {sanitize_text(exc)}")

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
            raise RuntimeError(
                "Не удалось прочитать existing runtime state rows из Supabase: "
                f"{sanitize_text(exc)}"
            ) from exc

        current_keys = {row["state_key"] for row in rows}
        keys_to_delete = sorted(existing_keys - current_keys)

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

        if keys_to_delete:
            try:
                supabase.table(PIPELINE_RUNTIME_STATE_TABLE).delete().in_("state_key", keys_to_delete).execute()
            except Exception as exc:
                raise RuntimeError(
                    "Не удалось удалить stale runtime state rows из Supabase: "
                    f"{sanitize_text(exc)}"
                ) from exc

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
            "total_campaigns": progress_context["total_campaigns"],
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

    def build_cpc_progress_context(self, date_from, date_to, batch_size, campaign_ids, group_by):
        identity = cpc_progress_cache_identity(date_from, date_to, batch_size, campaign_ids, group_by)
        return {
            "date_from": date_from,
            "date_to": date_to,
            "group_by": str(group_by or ""),
            "batch_size": int(batch_size or 1),
            "campaign_hash": payload_hash(identity["campaigns"]),
            "total_campaigns": len(campaign_ids),
            "account_signature": identity["account_signature"],
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

    def download_report(self, uuid):
        response = self.request(
            "GET",
            "/api/client/statistics/report",
            params={"UUID": uuid},
        )

        text = response.text.strip()
        if not text:
            return {}

        try:
            return response.json()
        except ValueError:
            return json.loads(text)

    def download_report_by_link(self, link, uuid=None):
        if link:
            response = self.request("GET", urljoin(OZON_PERFORMANCE_BASE_URL + "/", link))
            text = response.text.lstrip("\ufeff")
            if text:
                return text

        response = self.request(
            "GET",
            "/api/client/statistics/report",
            params={"UUID": uuid},
        )
        return response.text.lstrip("\ufeff")

    def fetch_statistics_json_report(self, campaign_ids, date_from, date_to, group_by, allow_recreate=True):
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

    def fetch_all_sku_promo_csv(self, report_type, date_from, date_to):
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
    cpc_campaign_ids = list(cpc_campaigns_by_id.keys())
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
        daily_target = (today_local() - timedelta(days=1)).isoformat()
        daily_status = get_daily_load_status(load_date, daily_target, client.account_signature)
        if not daily_status:
            raise RuntimeError(
                "cpc-backfill mode requires today's daily-yesterday run to be written first. "
                f"No daily load status found for load_date={load_date}, target_date={daily_target}."
            )

    batch_size = min(
        requested_batch_size,
        client.get_batch_recommendation(
            client.scoped_state_key("statistics_json"),
            requested_batch_size,
        ),
    )
    cpc_batches = build_cpc_batches(cpc_campaign_ids, batch_size)
    usable_daily_limit = max(0, STATS_DAILY_CAMPAIGN_LIMIT - STATS_DAILY_CAMPAIGN_RESERVE)
    excluded_by_quota_count = max(0, len(cpc_campaign_ids) - usable_daily_limit)
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
        "filtered_recent_count": recent_cpc_count if recent_cpc_count is not None else len(cpc_campaign_ids),
        "date_overlap_cpc_count": date_overlap_cpc_count if date_overlap_cpc_count is not None else len(cpc_campaign_ids),
        "selected_cpc_count": len(cpc_campaign_ids),
        "cpc_campaign_count": len(cpc_campaign_ids),
        "excluded_by_recent_filter_count": excluded_by_recent_filter_count,
        "excluded_by_quota_count": excluded_by_quota_count,
        "batch_size": batch_size,
        "total_batches": len(cpc_batches),
        "campaign_units": len(cpc_campaign_ids),
        "daily_limit": STATS_DAILY_CAMPAIGN_LIMIT,
        "reserve": STATS_DAILY_CAMPAIGN_RESERVE,
        "usable_limit": usable_daily_limit,
        "would_fit_daily_limit": len(cpc_campaign_ids) <= usable_daily_limit,
        "head_campaign_ids": cpc_campaign_ids[:10],
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
    progress_key = build_cpc_progress_key(date_from, date_to, batch_size, cpc_campaign_ids, args.group_by)
    original_runtime_state = client.snapshot_runtime_state()
    progress_context = client.build_cpc_progress_context(
        date_from,
        date_to,
        batch_size,
        cpc_campaign_ids,
        args.group_by,
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
        "campaign_count": len(cpc_campaign_ids),
        "cpc_campaign_units_total": len(cpc_campaign_ids),
        "requested_batch_size": requested_batch_size,
        "batch_size": batch_size,
        "daily_cpc_selection_mode": daily_selection_mode if args.mode == "daily-yesterday" else None,
        "raw_cpc_count": planning_summary.get("raw_cpc_count"),
        "date_overlap_cpc_count": planning_summary.get("date_overlap_cpc_count"),
        "excluded_by_recent_filter_count": planning_summary.get("excluded_by_recent_filter_count"),
        "excluded_by_quota_count": planning_summary.get("excluded_by_quota_count"),
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

    cpc_batches_total = len(cpc_batches)
    processed_batch_indexes = []
    cpc_progress_snapshot = client.get_cpc_progress(progress_key)

    if not cpc_campaign_ids:
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
        run_summary["cpc_pending_campaigns_before_run"] = max(0, len(cpc_campaign_ids) - planned_campaign_units)
        run_summary["quota_limited"] = planned_campaign_units < len(cpc_campaign_ids)

        print(
            "Ozon Performance CPC batch plan: "
            f"campaign_count={len(cpc_campaign_ids)} batch_size={batch_size} "
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
                    campaign_count=len(cpc_campaign_ids),
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
                        len(cpc_campaign_ids)
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
                campaign_count=len(cpc_campaign_ids),
                campaign_units_attempted=sum_campaign_units_for_batches(cpc_batches, processed_batch_indexes),
                campaign_units_completed=completed_campaign_units,
                total_batches=cpc_batches_total,
                max_batches_per_run=max_cpc_batches,
                processed_batches=len(processed_batch_indexes),
                completed_batches=cpc_progress_snapshot.get("completed_batches", 0),
                pending_batches=cpc_progress_snapshot.get("pending_batches", 0),
                failed_429_batches=cpc_progress_snapshot.get("failed_429_batches", 0),
                next_batch_index=cpc_progress_snapshot.get("next_batch_index"),
                pending_campaigns=max(0, len(cpc_campaign_ids) - completed_campaign_units),
            )
            if cpc_status == "success":
                client.clear_batch_recommendation(client.scoped_state_key("statistics_json"))

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
    run_summary["processed_batches"] = len(processed_batch_indexes)
    run_summary["completed_batches"] = cpc_progress_snapshot.get("completed_batches", 0)
    run_summary["pending_batches"] = cpc_progress_snapshot.get("pending_batches", 0)
    run_summary["failed_429_batches"] = cpc_progress_snapshot.get("failed_429_batches", 0)
    run_summary["cpc_campaign_units_attempted"] = sum_campaign_units_for_batches(cpc_batches, processed_batch_indexes)
    run_summary["cpc_campaign_units_completed"] = sum_campaign_units_for_batches(
        cpc_batches,
        cpc_progress_snapshot.get("completed_batch_indexes"),
    )
    run_summary["cpc_pending_campaigns"] = max(
        0,
        len(cpc_campaign_ids) - run_summary["cpc_campaign_units_completed"],
    )
    run_summary["ad_spend_loaded"] = round(sum(by_type.values()), 2)
    run_summary["ad_attribution_loaded"] = round(
        sum(float(row.get("ad_orders_revenue") or 0) for row in ad_attribution_rows),
        2,
    )

    if args.mode == "cpc-backfill":
        if run_summary["cpc"]["status"] == "success":
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
    elif run_summary["cpc"]["status"] == "success" and run_summary["cpo"]["status"] == "success":
        run_summary["overall_status"] = "success"
    elif run_summary["cpc"]["status"] == "skipped" and run_summary["cpo"]["status"] == "success":
        run_summary["overall_status"] = "success"
    else:
        run_summary["overall_status"] = "failed"

    run_summary["rows_by_type"] = {
        key: round(value, 2)
        for key, value in sorted(by_type.items())
    }
    run_summary["updated_at"] = to_iso(utcnow())
    print("Ozon Performance run summary:")
    print(json.dumps(sanitize_value(run_summary), ensure_ascii=False))

    if args.dry_run:
        client.restore_runtime_state(original_runtime_state)
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
