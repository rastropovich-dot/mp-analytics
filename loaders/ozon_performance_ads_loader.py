import argparse
import csv
import io
import json
import os
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from supabase import create_client


load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

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
RECON_WARNING_THRESHOLD = float(os.getenv("OZON_CPO_RECON_WARNING_THRESHOLD", "0.01"))
RECON_ERROR_THRESHOLD = float(os.getenv("OZON_CPO_RECON_ERROR_THRESHOLD", "1.0"))
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

CPO_RATE_KEYS = (
    "Ставка, %",
    "Ставка %",
    "rate",
    "promo_rate",
)


supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Load Ozon Performance advertising expenses by SKU.",
    )
    parser.add_argument("--days-back", type=int, default=30)
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--group-by", default=DEFAULT_GROUP_BY)
    parser.add_argument("--campaign-limit", type=int)
    parser.add_argument("--campaign-batch-size", type=int, default=1)
    parser.add_argument(
        "--campaign-scope",
        choices=("recent", "all"),
        default=DEFAULT_CAMPAIGN_SCOPE,
        help="recent = active or recently updated campaigns in the period; all = all campaigns",
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


def mask_client_id(value):
    text = str(value or "").strip()
    if not text:
        return "unknown"
    if len(text) <= 4:
        return text
    return f"{text[:2]}***{text[-2:]}"


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
            f"{name}={values}"
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


def extract_sku(row):
    value = value_by_keys(row, SKU_KEYS)
    if value in (None, ""):
        return ""

    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]

    return text


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

    def request(self, method, path, **kwargs):
        url = f"{OZON_PERFORMANCE_BASE_URL}{path}"
        headers = kwargs.pop("headers", {})
        headers.update({
            "Authorization": f"Bearer {self.ensure_token()}",
            "Accept": "application/json",
        })

        for attempt in range(1, 6):
            response = requests.request(
                method,
                url,
                headers=headers,
                timeout=120,
                **kwargs,
            )

            if response.status_code == 401 and attempt == 1:
                self.token = None
                headers["Authorization"] = f"Bearer {self.ensure_token()}"
                continue

            if response.status_code == 429:
                sleep_for = min(60, 2 ** attempt)
                print(f"Ozon Performance 429, ждем {sleep_for} сек.")
                time.sleep(sleep_for)
                continue

            response.raise_for_status()
            return response

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

    def request_statistics(self, campaign_ids, date_from, date_to, group_by):
        payload = {
            "campaigns": [str(campaign_id) for campaign_id in campaign_ids],
            "dateFrom": date_from,
            "dateTo": date_to,
            "groupBy": group_by,
        }

        response = self.request(
            "POST",
            "/api/client/statistics/json",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        data = response.json()
        uuid = data.get("UUID") or data.get("uuid")

        if not uuid:
            raise RuntimeError(f"Ozon Performance не вернул UUID: {data}")

        return uuid

    def request_all_sku_promo_report(self, report_type, date_from, date_to):
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

        response = self.request(
            "GET",
            f"/api/client/statistics/all_sku_promo/{report_type}/generate",
            params={
                "timeBounds.from": utc_from,
                "timeBounds.to": utc_to,
            },
        )
        data = response.json()
        uuid = data.get("UUID") or data.get("uuid")

        if not uuid:
            raise RuntimeError(f"Ozon all_sku_promo не вернул UUID: {data}")

        return uuid

    def wait_statistics(self, uuid):
        for attempt in range(1, 31):
            response = self.request("GET", f"/api/client/statistics/{uuid}")
            data = response.json()
            state = str(data.get("state") or data.get("status") or "").upper()

            if state in {"OK", "SUCCESS", "DONE", "COMPLETED", "READY"}:
                return data

            if state in {"ERROR", "FAILED", "FAIL"}:
                raise RuntimeError(f"Ozon Performance report failed: {data}")

            time.sleep(min(30, attempt * 2))

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
            response = self.request("GET", urljoin("/", link))
            text = response.text.lstrip("\ufeff")
            if text:
                return text

        response = self.request(
            "GET",
            "/api/client/statistics/report",
            params={"UUID": uuid},
        )
        return response.text.lstrip("\ufeff")


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


def enrich_rows(rows, catalog):
    for row in rows:
        catalog_row = catalog.get(row["marketplace_sku"])
        if not catalog_row:
            continue
        if not row.get("article"):
            row["article"] = catalog_row.get("article") or ""

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

    if args.date_to:
        date_to = args.date_to
    else:
        date_to = date.today().isoformat()

    if args.date_from:
        date_from = args.date_from
    else:
        date_from = (datetime.fromisoformat(date_to).date() - timedelta(days=args.days_back)).isoformat()

    client = OzonPerformanceClient()
    campaigns = client.list_campaigns()
    print(f"Ozon Performance campaigns total: {len(campaigns)}")

    campaigns = filter_campaigns(campaigns, date_from, date_to, args.campaign_scope)
    print(f"Ozon Performance campaigns after {args.campaign_scope} filter: {len(campaigns)}")

    if args.campaign_limit:
        campaigns = campaigns[:args.campaign_limit]

    campaigns_by_id = {
        str(campaign.get("id") or campaign.get("campaignId")): campaign
        for campaign in campaigns
        if campaign.get("id") or campaign.get("campaignId")
    }
    campaign_ids = list(campaigns_by_id.keys())

    print(f"Ozon Performance campaigns: {len(campaign_ids)}")

    if not campaign_ids:
        print("Нет рекламных кампаний Ozon Performance")
        return

    catalog = load_catalog()
    rows_by_key = {}
    total_counters = defaultdict(int)

    batch_size = max(1, int(args.campaign_batch_size or 1))

    for campaign_batch in chunks(campaign_ids, batch_size):
        print(f"Запрашиваем Ozon Performance report: {campaign_batch}")
        try:
            uuid = client.request_statistics(campaign_batch, date_from, date_to, args.group_by)
        except Exception as exc:
            print(f"Пропускаем CPC batch {campaign_batch}: {exc}")
            continue
        print(f"UUID: {uuid}")
        client.wait_statistics(uuid)
        report_data = client.download_report(uuid)

        if args.debug_sample:
            print(json.dumps(report_data, ensure_ascii=False, indent=2)[:5000])

        rows, counters = build_rows(report_data, campaigns_by_id, date_from)

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

        time.sleep(2)

    print("Запрашиваем Ozon all_sku_promo orders report")
    cpo_uuid = client.request_all_sku_promo_report("orders", date_from, date_to)
    print(f"CPO UUID: {cpo_uuid}")
    cpo_status = client.wait_statistics(cpo_uuid)
    cpo_csv = client.download_report_by_link(cpo_status.get("link"), uuid=cpo_uuid)
    cpo_rows, cpo_counters, cpo_summary = build_cpo_rows(cpo_csv)

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

    rows = enrich_rows(list(rows_by_key.values()), catalog)

    print("Ozon Performance counters:")
    print(dict(total_counters))
    print("Ozon Performance rows by type:")
    by_type = defaultdict(float)
    for row in rows:
        by_type[row["expense_type"]] += float(row.get("expense_amount") or 0)
    print({key: round(value, 2) for key, value in sorted(by_type.items())})

    if args.dry_run:
        print("Dry run: данные не записывались")
        return

    save_rows(rows)


if __name__ == "__main__":
    run()
