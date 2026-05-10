import argparse
import csv
import io
import json
import os
import re
import time
import zipfile
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv


load_dotenv()

BASE_URL = (os.getenv("OZON_PERFORMANCE_BASE_URL") or "https://api-performance.ozon.ru").rstrip("/")
CLIENT_ID = os.getenv("OZON_PERFORMANCE_CLIENT_ID")
CLIENT_SECRET = os.getenv("OZON_PERFORMANCE_CLIENT_SECRET")

READY_STATUSES = {"DONE", "SUCCESS", "OK", "READY", "COMPLETED", "GENERATED"}
PENDING_STATUSES = {"PENDING", "IN_PROGRESS", "RUNNING", "PROCESSING", "NOT_STARTED"}
ERROR_STATUSES = {"ERROR", "FAILED", "REJECTED"}

SPEND_COLUMN_CANDIDATES = {
    "moneyspent",
    "money_spent",
    "spent",
    "cost",
    "expense",
    "расход",
    "расходы",
    "сумма",
    "сумма расхода",
    "списано",
    "spent_money",
    "price",
    "amount",
}
SKU_COLUMN_CANDIDATES = {
    "sku",
    "артикул",
    "ozon sku",
    "id товара",
    "товар",
    "product_id",
    "offer_id",
    "offerid",
}
ORDER_COLUMN_CANDIDATES = {
    "order_id",
    "orderid",
    "posting_number",
    "postingnumber",
    "номер заказа",
    "номер отправления",
    "заказ",
    "отправление",
}
DATE_COLUMN_CANDIDATES = {
    "date",
    "day",
    "дата",
    "дата заказа",
    "order_date",
    "created_at",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll, download, and dry-parse an existing Ozon SEARCH_PROMO report UUID without re-submitting.",
    )
    parser.add_argument("--uuid", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--live-dry-run", action="store_true")
    parser.add_argument("--max-polls", type=int, default=12)
    parser.add_argument("--poll-interval-sec", type=int, default=10)
    parser.add_argument("--allow-organisation-wide-date-valid", action="store_true")
    return parser.parse_args()


def sanitize_text(value: Optional[str], limit: int = 4000) -> str:
    text = value or ""
    text = text[:limit]
    for secret in (CLIENT_ID, CLIENT_SECRET):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_json(v) for v in value]
    if isinstance(value, str):
        return sanitize_text(value, limit=500)
    return value


def build_submit_plan_candidates(campaign_id: str, target_date: str) -> Dict[str, Any]:
    target_ts_from = f"{target_date}T00:00:00"
    del target_ts_from
    # Europe/Moscow D-1 bounds already validated in prior discovery.
    utc_from = "2026-05-05T21:00:00Z" if target_date == "2026-05-06" else None
    utc_to = "2026-05-06T20:59:59Z" if target_date == "2026-05-06" else None
    return {
        "endpoint": "POST /api/client/statistic/orders/generate",
        "http_requests": 0,
        "db_writes": 0,
        "new_submit_performed": False,
        "used_statistics_json": False,
        "used_general_statistics_submit": False,
        "expected_echo": {
            "campaignId": str(campaign_id),
            "from_to_should_correspond_to": f"{target_date} Europe/Moscow",
        },
        "payload_candidates": [
            {
                "campaignId": str(campaign_id),
                "from": utc_from,
                "to": utc_to,
            },
            {
                "campaignId": int(campaign_id),
                "from": utc_from,
                "to": utc_to,
            },
            {
                "campaignId": str(campaign_id),
                "timeBounds": {
                    "from": utc_from,
                    "to": utc_to,
                },
            },
            {
                "campaignIds": [str(campaign_id)],
                "timeBounds": {
                    "from": utc_from,
                    "to": utc_to,
                },
            },
        ],
    }


def ensure_auth() -> str:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("Не заполнены OZON_PERFORMANCE_CLIENT_ID и OZON_PERFORMANCE_CLIENT_SECRET")

    response = requests.post(
        f"{BASE_URL}/api/client/token",
        json={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Не получили access_token: {sanitize_text(response.text)}")
    return token


def normalize_status(value: Any) -> str:
    return str(value or "").strip().upper()


def try_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def extract_report_status(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    for key in ("state", "status", "reportStatus", "report_status"):
        if key in data:
            return normalize_status(data.get(key))
    if isinstance(data.get("meta"), dict):
        for key in ("state", "status"):
            if key in data["meta"]:
                return normalize_status(data["meta"].get(key))
    return None


def make_response_record(endpoint: str, method: str, response: requests.Response) -> Dict[str, Any]:
    body_json = try_json(response)
    body = sanitize_json(body_json) if body_json is not None else sanitize_text(response.text)
    record: Dict[str, Any] = {
        "endpoint": endpoint,
        "method": method,
        "status": response.status_code,
        "response_body": body,
    }
    return record


def request_get(token: str, endpoint: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
    return requests.request(
        "GET",
        f"{BASE_URL}{endpoint}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        params=params,
        timeout=120,
    )


def poll_status(uuid: str, token: str, max_polls: int, poll_interval_sec: int) -> Dict[str, Any]:
    candidates = [
        ("singular", f"/api/client/statistic/{uuid}"),
        ("plural", f"/api/client/statistics/{uuid}"),
    ]
    attempts: List[Dict[str, Any]] = []
    endpoint_used = None

    for namespace_index, (label, endpoint) in enumerate(candidates):
        polls_used = 0
        while polls_used < max_polls:
            response = request_get(token, endpoint)
            record = make_response_record(endpoint, "GET", response)
            attempts.append(record)

            if response.status_code == 404 and label == "singular":
                break
            if response.status_code == 404 and label == "plural":
                return {
                    "result": "uuid_not_found_or_expired",
                    "attempts": attempts,
                    "endpoint_used": endpoint,
                    "polls_used": polls_used + 1,
                    "final_http_status": 404,
                    "status_response_body_sanitized": record["response_body"],
                }
            if response.status_code in {401, 403, 429, 400, 405} or response.status_code >= 500:
                return {
                    "result": f"http_{response.status_code}",
                    "attempts": attempts,
                    "endpoint_used": endpoint,
                    "polls_used": polls_used + 1,
                    "final_http_status": response.status_code,
                    "status_response_body_sanitized": record["response_body"],
                }
            if response.status_code != 200:
                return {
                    "result": f"http_{response.status_code}",
                    "attempts": attempts,
                    "endpoint_used": endpoint,
                    "polls_used": polls_used + 1,
                    "final_http_status": response.status_code,
                    "status_response_body_sanitized": record["response_body"],
                }

            data = try_json(response)
            report_status = extract_report_status(data)
            endpoint_used = endpoint
            polls_used += 1

            if report_status in READY_STATUSES:
                return {
                    "result": "ready",
                    "attempts": attempts,
                    "endpoint_used": endpoint_used,
                    "polls_used": polls_used,
                    "final_http_status": 200,
                    "final_report_status": report_status,
                    "status_body": sanitize_json(data),
                }
            if report_status in PENDING_STATUSES:
                if polls_used >= max_polls:
                    return {
                        "result": "poll_timeout",
                        "attempts": attempts,
                        "endpoint_used": endpoint_used,
                        "polls_used": polls_used,
                        "final_http_status": 200,
                        "final_report_status": report_status,
                        "status_body": sanitize_json(data),
                    }
                time.sleep(poll_interval_sec)
                continue
            if report_status in ERROR_STATUSES:
                return {
                    "result": "report_error_status",
                    "attempts": attempts,
                    "endpoint_used": endpoint_used,
                    "polls_used": polls_used,
                    "final_http_status": 200,
                    "final_report_status": report_status,
                    "status_body": sanitize_json(data),
                }

            return {
                "result": "unknown_status_structure",
                "attempts": attempts,
                "endpoint_used": endpoint_used,
                "polls_used": polls_used,
                "final_http_status": 200,
                "status_response_body_sanitized": sanitize_json(data),
            }

    return {
        "result": "uuid_not_found_or_expired",
        "attempts": attempts,
        "endpoint_used": endpoint_used,
        "polls_used": 0,
        "final_http_status": 404,
        "status_response_body_sanitized": None,
    }


def sniff_format(content_type: str, content: bytes) -> str:
    ctype = (content_type or "").lower()
    if "application/json" in ctype:
        return "json"
    if "text/csv" in ctype or "application/csv" in ctype:
        return "csv"
    if "tab-separated-values" in ctype or "text/tab-separated-values" in ctype:
        return "tsv"
    if "zip" in ctype or content[:4] == b"PK\x03\x04":
        return "zip"
    if content.startswith((b"{", b"[")):
        return "json"
    if b"," in content[:2000]:
        return "csv"
    if b"\t" in content[:2000]:
        return "tsv"
    try:
        content.decode("utf-8")
        return "unknown_text"
    except Exception:
        return "unknown_binary"


def read_delimited_text(text: str, delimiter: Optional[str] = None) -> Tuple[List[str], List[Dict[str, Any]]]:
    sample = text[:4096]
    if delimiter is None:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            delimiter = dialect.delimiter
        except Exception:
            delimiter = "," if sample.count(",") >= sample.count("\t") else "\t"
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    return list(reader.fieldnames or []), rows


def normalize_column_name(name: str) -> str:
    text = str(name or "").strip().lower()
    text = text.replace("₽", " ")
    text = re.sub(r"[^a-zA-Zа-яА-Я0-9]+", " ", text)
    return " ".join(text.split())


def find_candidate_columns(columns: List[str], candidates: set) -> List[str]:
    found = []
    for column in columns:
        normalized = normalize_column_name(column)
        normalized_candidates = {normalize_column_name(candidate) for candidate in candidates}
        if normalized in normalized_candidates or any(
            normalized == candidate
            or normalized.startswith(candidate + " ")
            or candidate in normalized
            for candidate in normalized_candidates
            if candidate
        ):
            found.append(column)
    return found


def parse_amount(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("\xa0", "").replace(" ", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def preview_rows(rows: List[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    return [sanitize_json(row) for row in rows[:limit]]


def is_total_row(row: Dict[str, Any], columns: List[str]) -> bool:
    date_value = str(row.get("Дата") or "").strip()
    if date_value == "Всего":
        return True

    for column in columns:
        value = str(row.get(column) or "").strip()
        if value:
            return value == "Всего"
    return False


def sum_amounts(rows: List[Dict[str, Any]], spend_column: Optional[str]) -> Optional[float]:
    if not spend_column:
        return None

    total = 0.0
    found = False
    for row in rows:
        amount = parse_amount(row.get(spend_column))
        if amount is None:
            continue
        total += amount
        found = True
    return round(total, 2) if found else None


def build_classification(campaign_id_exact_match: bool = False) -> Dict[str, Any]:
    return {
        "source_report": "search_promo_organisation_orders",
        "promotion_type": "cpo_selected_products",
        "scope": "organisation",
        "campaign_filter_supported": False,
        "campaign_id_exact_match": bool(campaign_id_exact_match),
        "campaign_scope": (
            "campaign_exact_match"
            if campaign_id_exact_match
            else "organisation_wide_campaign_unbound"
        ),
        "safe_for_db_load": False,
        "db_load_status": "not_implemented_dry_run_only",
    }


def analyze_rows(
    rows: List[Dict[str, Any]],
    files: Optional[List[str]] = None,
    detected_format: str = "unknown",
    columns: Optional[List[str]] = None,
    preamble_lines: Optional[List[str]] = None,
    campaign_id_exact_match: bool = False,
) -> Dict[str, Any]:
    columns = columns or (list(rows[0].keys()) if rows else [])
    spend_columns = find_candidate_columns(columns, SPEND_COLUMN_CANDIDATES)
    sku_columns = find_candidate_columns(columns, SKU_COLUMN_CANDIDATES)
    order_columns = find_candidate_columns(columns, ORDER_COLUMN_CANDIDATES)
    date_columns = find_candidate_columns(columns, DATE_COLUMN_CANDIDATES)
    spend_column = spend_columns[0] if spend_columns else None
    total_rows = [row for row in rows if is_total_row(row, columns)]
    data_rows = [row for row in rows if not is_total_row(row, columns)]

    spend_sum_including_total_rows = sum_amounts(rows, spend_column)
    spend_sum_data_rows = sum_amounts(data_rows, spend_column)
    spend_sum_total_rows = sum_amounts(total_rows, spend_column)
    spend_sum = spend_sum_data_rows

    expected = 25841.80
    absolute_diff = abs(spend_sum - expected) if spend_sum is not None else None
    close_to_expected = absolute_diff <= 1.00 if absolute_diff is not None else None
    return {
        "format": detected_format,
        "files": files or [],
        "columns": columns,
        "row_count": len(data_rows),
        "row_count_raw": len(rows),
        "data_row_count": len(data_rows),
        "total_row_count": len(total_rows),
        "preview_rows_sanitized": preview_rows(data_rows),
        "total_rows_preview_sanitized": preview_rows(total_rows),
        "candidate_spend_columns": spend_columns,
        "candidate_sku_columns": sku_columns,
        "candidate_order_columns": order_columns,
        "candidate_date_columns": date_columns,
        "spend_sum": spend_sum,
        "spend_sum_basis": "data_rows_excluding_total_rows",
        "spend_sum_including_total_rows": spend_sum_including_total_rows,
        "spend_sum_data_rows": spend_sum_data_rows,
        "spend_sum_total_rows": spend_sum_total_rows,
        "expected_missing_selected_cpo": expected,
        "absolute_diff": absolute_diff,
        "close_to_expected": close_to_expected,
        "preamble_lines": preamble_lines or [],
        "classification": build_classification(campaign_id_exact_match=campaign_id_exact_match),
    }


def parse_json_table(data: Any) -> Optional[List[Dict[str, Any]]]:
    if isinstance(data, list) and all(isinstance(item, dict) for item in data):
        return data
    if isinstance(data, dict):
        for key in ("rows", "items", "data", "report"):
            value = data.get(key)
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return value
    return None


def find_search_promo_header_index(lines: List[str]) -> int:
    for index, line in enumerate(lines):
        normalized = line.lower()
        if ";" not in line:
            continue
        if "дата" in normalized and "sku" in normalized and "расход" in normalized:
            return index
    return -1


def parse_search_promo_csv_text(text: str) -> Tuple[List[str], List[Dict[str, Any]], List[str]]:
    clean_text = text.lstrip("\ufeff")
    lines = clean_text.splitlines()
    header_index = find_search_promo_header_index(lines)
    if header_index < 0:
        columns, rows = read_delimited_text(clean_text, delimiter=";")
        return columns, rows, []

    preamble_lines = [line for line in lines[:header_index] if line.strip()]
    data_lines = lines[header_index:]
    if not data_lines:
        return [], [], preamble_lines
    reader = csv.DictReader(io.StringIO("\n".join(data_lines)), delimiter=";")
    rows = list(reader)
    columns = list(reader.fieldnames or [])
    return columns, rows, preamble_lines


def build_expected_utc_bounds(expected_date: str) -> Tuple[str, str]:
    plan = build_submit_plan_candidates("4471285", expected_date)
    candidate = plan["payload_candidates"][0]
    return str(candidate["from"] or ""), str(candidate["to"] or "")


def validate_request_echo(
    status_body: Dict[str, Any],
    expected_campaign_id: str,
    expected_date: str,
    allow_organisation_wide_date_valid: bool = False,
) -> Dict[str, Any]:
    request = (status_body or {}).get("request") or {}
    actual_kind = str((status_body or {}).get("kind") or "")
    actual_campaign_id = str(request.get("campaignId") or "")
    actual_campaigns = request.get("campaigns") or []
    actual_from = str(request.get("from") or "")
    actual_to = str(request.get("to") or "")
    actual_date_from = str(request.get("dateFrom") or "")
    actual_date_to = str(request.get("dateTo") or "")
    expected_from, expected_to = build_expected_utc_bounds(expected_date)
    campaign_id_exact_match = actual_campaign_id == str(expected_campaign_id)
    date_range_valid = actual_from.startswith(expected_from) and actual_to.startswith(expected_to)
    kind_valid = actual_kind == "SEARCH_PROMO_ORGANISATION_ORDERS"
    organisation_wide_campaign_unbound = (
        kind_valid
        and actual_campaign_id == "0"
        and date_range_valid
        and actual_date_from == ""
        and actual_date_to == ""
    )
    valid = campaign_id_exact_match and kind_valid and date_range_valid
    if allow_organisation_wide_date_valid and organisation_wide_campaign_unbound:
        valid = True

    reason = None
    if not valid:
        reason = "report backend generated SEARCH_PROMO_ORGANISATION_ORDERS report, but submit payload fields were not bound"
        if organisation_wide_campaign_unbound and not allow_organisation_wide_date_valid:
            reason = "report is organisation-wide and date-valid, but campaignId stayed unbound"

    return {
        "expected_campaign_id": str(expected_campaign_id),
        "expected_date": expected_date,
        "expected_from": expected_from,
        "expected_to": expected_to,
        "actual_kind": actual_kind,
        "actual_campaignId": actual_campaign_id,
        "actual_campaigns": actual_campaigns,
        "actual_from": actual_from,
        "actual_to": actual_to,
        "actual_dateFrom": actual_date_from,
        "actual_dateTo": actual_date_to,
        "campaign_id_exact_match": campaign_id_exact_match,
        "date_range_valid": date_range_valid,
        "kind_valid": kind_valid,
        "campaign_scope": (
            "campaign_exact_match"
            if campaign_id_exact_match
            else "organisation_wide_campaign_unbound"
            if organisation_wide_campaign_unbound
            else "campaign_unbound_or_invalid"
        ),
        "valid": valid,
        "reason": reason,
    }


def dry_parse_download(response: requests.Response) -> Dict[str, Any]:
    content = response.content
    content_type = response.headers.get("Content-Type", "")
    detected_format = sniff_format(content_type, content)
    download_info = {
        "content_type": content_type,
        "bytes": len(content),
        "format": detected_format,
    }

    if detected_format == "json":
        data = try_json(response)
        rows = parse_json_table(data) or []
        result = analyze_rows(rows, detected_format="json")
        result["json_keys"] = list(data.keys()) if isinstance(data, dict) else []
        return {"download": download_info, "dry_parse": result}

    if detected_format in {"csv", "tsv"}:
        text = content.decode("utf-8-sig", errors="replace")
        if detected_format == "csv":
            columns, rows, preamble_lines = parse_search_promo_csv_text(text)
            result = analyze_rows(
                rows,
                detected_format=detected_format,
                columns=columns,
                preamble_lines=preamble_lines,
            )
        else:
            delimiter = "\t"
            columns, rows = read_delimited_text(text, delimiter=delimiter)
            result = analyze_rows(rows, detected_format=detected_format, columns=columns)
        return {"download": download_info, "dry_parse": result}

    if detected_format == "zip":
        files = []
        rows: List[Dict[str, Any]] = []
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            files = zf.namelist()
            for name in files:
                lower = name.lower()
                if not (lower.endswith(".csv") or lower.endswith(".tsv") or lower.endswith(".txt")):
                    continue
                raw = zf.read(name)
                text = raw.decode("utf-8-sig", errors="replace")
                if lower.endswith(".csv"):
                    _, parsed_rows, _ = parse_search_promo_csv_text(text)
                else:
                    _, parsed_rows = read_delimited_text(text, delimiter="\t" if lower.endswith(".tsv") else None)
                rows.extend(parsed_rows)
        result = analyze_rows(rows, files=files, detected_format="zip")
        return {"download": download_info, "dry_parse": result}

    return {
        "download": download_info,
        "dry_parse": {
            "format": detected_format,
            "files": [],
            "columns": [],
            "row_count": 0,
            "preview_rows_sanitized": [],
            "total_rows_preview_sanitized": [],
            "candidate_spend_columns": [],
            "candidate_sku_columns": [],
            "candidate_order_columns": [],
            "candidate_date_columns": [],
            "spend_sum": None,
            "spend_sum_basis": "data_rows_excluding_total_rows",
            "spend_sum_including_total_rows": None,
            "spend_sum_data_rows": None,
            "spend_sum_total_rows": None,
            "row_count_raw": 0,
            "data_row_count": 0,
            "total_row_count": 0,
            "expected_missing_selected_cpo": 25841.80,
            "absolute_diff": None,
            "close_to_expected": None,
            "preamble_lines": [],
            "classification": build_classification(campaign_id_exact_match=False),
        },
    }


def download_report(uuid: str, token: str) -> Dict[str, Any]:
    candidates = [
        ("singular", "/api/client/statistic/report"),
        ("plural", "/api/client/statistics/report"),
    ]
    attempts: List[Dict[str, Any]] = []

    for label, endpoint in candidates:
        response = request_get(token, endpoint, params={"UUID": uuid})
        record = make_response_record(endpoint, "GET", response)
        attempts.append(record)

        if response.status_code == 404 and label == "singular":
            continue
        if response.status_code == 404 and label == "plural":
            return {
                "result": "report_download_not_found",
                "attempts": attempts,
                "endpoint_used": endpoint,
                "http_status": 404,
                "response_body_sanitized": record["response_body"],
            }
        if response.status_code == 202:
            return {
                "result": "download_processing_202",
                "attempts": attempts,
                "endpoint_used": endpoint,
                "http_status": 202,
                "response_body_sanitized": record["response_body"],
            }
        if response.status_code in {401, 403, 429, 400, 405} or response.status_code >= 500:
            return {
                "result": f"http_{response.status_code}",
                "attempts": attempts,
                "endpoint_used": endpoint,
                "http_status": response.status_code,
                "response_body_sanitized": record["response_body"],
            }
        if response.status_code != 200:
            return {
                "result": f"http_{response.status_code}",
                "attempts": attempts,
                "endpoint_used": endpoint,
                "http_status": response.status_code,
                "response_body_sanitized": record["response_body"],
            }

        parsed = dry_parse_download(response)
        parsed["download"]["endpoint_used"] = endpoint
        parsed["download"]["http_status"] = 200
        parsed["attempts"] = attempts
        parsed["result"] = "downloaded"
        return parsed

    return {
        "result": "report_download_not_found",
        "attempts": attempts,
        "endpoint_used": None,
        "http_status": 404,
        "response_body_sanitized": None,
    }


def build_guardrails() -> Dict[str, Any]:
    return {
        "db_writes": 0,
        "marketplace_expenses_writes": 0,
        "ozon_daily_sku_ad_attribution_writes": 0,
        "telegram_messages": 0,
        "wb_requests": 0,
        "render_changes": 0,
        "full_pipeline_runs": 0,
        "auto_actions": "off",
        "used_statistics_json": False,
        "used_general_statistics_submit": False,
        "new_submit_performed": False,
    }


def run(
    uuid: str,
    campaign_id: str,
    target_date: str,
    live_dry_run: bool,
    max_polls: int,
    poll_interval_sec: int,
    allow_organisation_wide_date_valid: bool = False,
) -> Dict[str, Any]:
    if not live_dry_run:
        raise RuntimeError("Без --live-dry-run HTTP-запросы не выполняются")

    summary: Dict[str, Any] = {
        "mode": "LIVE_DRY_RUN_REPORT_DRY_PARSE",
        "uuid": uuid,
        "campaign_id": campaign_id,
        "date": target_date,
        "base_url": BASE_URL,
        "submit_performed": False,
        "guardrails": build_guardrails(),
    }

    token = ensure_auth()
    status_result = poll_status(uuid, token, max_polls=max_polls, poll_interval_sec=poll_interval_sec)

    if status_result["result"] == "uuid_not_found_or_expired":
        summary["result"] = "uuid_not_found_or_expired"
        summary["status_response_body_sanitized"] = status_result.get("status_response_body_sanitized")
        summary["status"] = {
            "endpoint_used": status_result.get("endpoint_used"),
            "polls_used": status_result.get("polls_used", 0),
            "final_http_status": status_result.get("final_http_status"),
            "attempts": status_result.get("attempts", []),
        }
        return summary

    if status_result["result"] != "ready":
        summary["result"] = status_result["result"]
        summary["status"] = {
            "endpoint_used": status_result.get("endpoint_used"),
            "polls_used": status_result.get("polls_used", 0),
            "final_http_status": status_result.get("final_http_status"),
            "final_report_status": status_result.get("final_report_status"),
            "attempts": status_result.get("attempts", []),
            "status_response_body_sanitized": status_result.get("status_response_body_sanitized") or status_result.get("status_body"),
        }
        return summary

    summary["status"] = {
        "endpoint_used": status_result.get("endpoint_used"),
        "polls_used": status_result.get("polls_used", 0),
        "final_http_status": status_result.get("final_http_status"),
        "final_report_status": status_result.get("final_report_status"),
        "attempts": status_result.get("attempts", []),
    }
    request_echo_validation = validate_request_echo(
        status_result.get("status_body") or {},
        expected_campaign_id=campaign_id,
        expected_date=target_date,
        allow_organisation_wide_date_valid=allow_organisation_wide_date_valid,
    )
    summary["request_echo_validation"] = request_echo_validation

    if not request_echo_validation["valid"]:
        if request_echo_validation["campaign_scope"] == "organisation_wide_campaign_unbound":
            summary["classification"] = build_classification(campaign_id_exact_match=False)
        else:
            summary["result"] = "uuid_created_but_request_echo_invalid"
            return summary

    download_result = download_report(uuid, token)
    if download_result["result"] != "downloaded":
        summary["result"] = download_result["result"]
        summary["download"] = {
            "endpoint_used": download_result.get("endpoint_used"),
            "http_status": download_result.get("http_status"),
            "attempts": download_result.get("attempts", []),
            "response_body_sanitized": download_result.get("response_body_sanitized"),
        }
        return summary

    summary["download"] = {
        "endpoint_used": download_result["download"]["endpoint_used"],
        "http_status": download_result["download"]["http_status"],
        "content_type": download_result["download"]["content_type"],
        "bytes": download_result["download"]["bytes"],
        "attempts": download_result.get("attempts", []),
    }
    summary["dry_parse"] = download_result["dry_parse"]
    summary["classification"] = build_classification(
        campaign_id_exact_match=request_echo_validation["campaign_id_exact_match"]
    )
    summary["dry_parse"]["classification"] = summary["classification"]
    if not request_echo_validation["valid"] and request_echo_validation["campaign_scope"] != "organisation_wide_campaign_unbound":
        summary["result"] = "downloaded_but_default_1970_request_payload_schema_invalid"
    else:
        summary["result"] = "downloaded_and_dry_parsed"
    return summary


def main() -> None:
    args = parse_args()
    summary = run(
        uuid=args.uuid,
        campaign_id=args.campaign_id,
        target_date=args.date,
        live_dry_run=args.live_dry_run,
        max_polls=args.max_polls,
        poll_interval_sec=args.poll_interval_sec,
        allow_organisation_wide_date_valid=args.allow_organisation_wide_date_valid,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
