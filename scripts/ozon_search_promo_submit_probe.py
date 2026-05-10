import argparse
import json
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv


load_dotenv()

BASE_URL = (os.getenv("OZON_PERFORMANCE_BASE_URL") or "https://api-performance.ozon.ru").rstrip("/")
CLIENT_ID = os.getenv("OZON_PERFORMANCE_CLIENT_ID")
CLIENT_SECRET = os.getenv("OZON_PERFORMANCE_CLIENT_SECRET")
APP_TIMEZONE = os.getenv("APP_TIMEZONE") or "Europe/Moscow"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only SEARCH_PROMO submit endpoint probe for Ozon Performance API.",
    )
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--live-dry-run", action="store_true")
    parser.add_argument("--stop-on-first-non-404", action="store_true")
    return parser.parse_args()


def sanitize_text(value: Optional[str], limit: int = 4000) -> str:
    text = value or ""
    text = text[:limit]
    for secret in (CLIENT_ID, CLIENT_SECRET):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


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


def build_time_bounds(target_date: str) -> Dict[str, str]:
    parsed = date.fromisoformat(target_date)
    tz = ZoneInfo(APP_TIMEZONE)
    start_local = datetime(parsed.year, parsed.month, parsed.day, 0, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1) - timedelta(seconds=1)
    start_utc = start_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = end_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"from": start_utc, "to": end_utc}


def build_candidates(campaign_id: str, target_date: str) -> List[Dict[str, Any]]:
    tb = build_time_bounds(target_date)
    return [
        {
            "endpoint": "/api/client/statistic/orders/generate",
            "method": "POST",
            "json": {
                "campaigns": [str(campaign_id)],
                "dateFrom": target_date,
                "dateTo": target_date,
            },
        },
        {
            "endpoint": "/api/client/statistic/products/generate",
            "method": "POST",
            "json": {
                "campaigns": [str(campaign_id)],
                "dateFrom": target_date,
                "dateTo": target_date,
            },
        },
        {
            "endpoint": "/api/client/statistics/search_promo/orders/generate",
            "method": "GET",
            "params": {
                "timeBounds.from": tb["from"],
                "timeBounds.to": tb["to"],
                "campaignId": str(campaign_id),
            },
        },
        {
            "endpoint": "/api/client/campaign/search_promo/statistics/orders",
            "method": "POST",
            "json": {
                "campaignId": str(campaign_id),
                "dateFrom": target_date,
                "dateTo": target_date,
            },
        },
        {
            "endpoint": "/api/client/campaign/search_promo/statistics",
            "method": "GET",
            "params": {
                "campaignId": str(campaign_id),
                "dateFrom": target_date,
                "dateTo": target_date,
            },
        },
        {
            "endpoint": "/api/client/statistics/promo/orders/generate",
            "method": "POST",
            "json": {
                "campaigns": [str(campaign_id)],
                "dateFrom": target_date,
                "dateTo": target_date,
            },
        },
        {
            "endpoint": "/api/client/statistics/promo/orders/generate",
            "method": "GET",
            "params": {
                "timeBounds.from": tb["from"],
                "timeBounds.to": tb["to"],
                "campaignId": str(campaign_id),
            },
        },
    ]


def print_guardrails(args: argparse.Namespace) -> None:
    guardrails = {
        "mode": "LIVE_DRY_RUN_SUBMIT_PROBE",
        "campaign_id": str(args.campaign_id),
        "date": args.date,
        "base_url": BASE_URL,
        "used_statistics_json": False,
        "used_statistics_general_submit": False,
        "db_writes": 0,
        "marketplace_expenses_writes": 0,
        "ozon_daily_sku_ad_attribution_writes": 0,
        "telegram_messages": 0,
        "wb_requests": 0,
        "render_changes": 0,
        "full_pipeline_runs": 0,
        "auto_actions": "off",
    }
    print("Ozon SEARCH_PROMO submit probe guardrails:")
    print(json.dumps(guardrails, ensure_ascii=False, indent=2))


def main() -> None:
    args = parse_args()
    if not args.live_dry_run:
        raise RuntimeError("Этот script выполняется только с --live-dry-run")

    print_guardrails(args)
    token = ensure_auth()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    candidates = build_candidates(args.campaign_id, args.date)

    summary: Dict[str, Any] = {
        "mode": "LIVE_DRY_RUN_SUBMIT_PROBE",
        "campaign_id": str(args.campaign_id),
        "date": args.date,
        "base_url": BASE_URL,
        "requests_attempted": [],
        "stop_reason": None,
        "uuid_found": False,
        "winning_endpoint": None,
        "used_statistics_json": False,
        "used_statistics_general_submit": False,
        "db_writes": 0,
        "marketplace_expenses_writes": 0,
        "ozon_daily_sku_ad_attribution_writes": 0,
        "telegram_messages": 0,
        "wb_requests": 0,
        "render_changes": 0,
        "full_pipeline_runs": 0,
        "auto_actions": "off",
    }

    for index, candidate in enumerate(candidates, start=1):
        endpoint = candidate["endpoint"]
        method = candidate["method"]
        url = f"{BASE_URL}{endpoint}"
        kwargs: Dict[str, Any] = {"headers": headers, "timeout": 120}
        if "json" in candidate:
            kwargs["json"] = candidate["json"]
            kwargs["headers"] = dict(headers)
            kwargs["headers"]["Content-Type"] = "application/json"
        if "params" in candidate:
            kwargs["params"] = candidate["params"]

        response = requests.request(method, url, **kwargs)
        body = sanitize_text(response.text)
        attempt_record = {
            "index": index,
            "endpoint": endpoint,
            "method": method,
            "status": response.status_code,
            "request_body": candidate.get("json"),
            "request_params": candidate.get("params"),
            "response_body": body,
        }
        summary["requests_attempted"].append(attempt_record)

        print("Probe attempt:")
        print(json.dumps(attempt_record, ensure_ascii=False, indent=2))

        if response.status_code in {200, 202}:
            try:
                data = response.json()
            except Exception:
                data = {}
            uuid = data.get("UUID") or data.get("uuid") or data.get("id") or data.get("reportId")
            if uuid:
                summary["uuid_found"] = True
                summary["winning_endpoint"] = endpoint
                summary["stop_reason"] = "uuid_found"
                break
            summary["stop_reason"] = "success_without_uuid"
            break

        if response.status_code == 404:
            if args.stop_on_first_non_404:
                continue
            summary["stop_reason"] = "404_no_continue"
            break

        if response.status_code == 429:
            summary["stop_reason"] = "rate_limited_429"
            break

        if response.status_code == 401:
            summary["stop_reason"] = "auth_401"
            break

        if response.status_code == 403:
            summary["stop_reason"] = "forbidden_403"
            break

        if response.status_code == 400:
            summary["stop_reason"] = "schema_or_compatibility_400"
            break

        if response.status_code == 405:
            summary["stop_reason"] = "method_mismatch_405"
            break

        summary["stop_reason"] = f"http_{response.status_code}"
        break

    if summary["stop_reason"] is None:
        summary["stop_reason"] = "all_candidates_404"

    print("Ozon SEARCH_PROMO submit probe summary:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
