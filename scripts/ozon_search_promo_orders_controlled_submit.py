import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scripts.ozon_search_promo_report_dry_parse as report_probe


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Controlled SEARCH_PROMO orders submit + status + download + dry-parse for one payload shape.",
    )
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument(
        "--payload-shape",
        required=True,
        choices=[
            "campaignId_number_from_to",
            "campaignId_timeBounds",
            "campaignIds_timeBounds",
        ],
    )
    parser.add_argument("--live-dry-run", action="store_true")
    parser.add_argument("--max-polls", type=int, default=12)
    parser.add_argument("--poll-interval-sec", type=int, default=10)
    return parser.parse_args()


def build_utc_bounds(target_date: str) -> Dict[str, str]:
    plan = report_probe.build_submit_plan_candidates("4471285", target_date)
    candidate = plan["payload_candidates"][0]
    return {"from": candidate["from"], "to": candidate["to"]}


def build_payload(campaign_id: str, target_date: str, payload_shape: str) -> Dict[str, Any]:
    bounds = build_utc_bounds(target_date)
    if payload_shape == "campaignId_number_from_to":
        return {
            "campaignId": int(campaign_id),
            "from": bounds["from"],
            "to": bounds["to"],
        }
    if payload_shape == "campaignId_timeBounds":
        return {
            "campaignId": str(campaign_id),
            "timeBounds": {
                "from": bounds["from"],
                "to": bounds["to"],
            },
        }
    if payload_shape == "campaignIds_timeBounds":
        return {
            "campaignIds": [str(campaign_id)],
            "timeBounds": {
                "from": bounds["from"],
                "to": bounds["to"],
            },
        }
    raise ValueError(f"Unsupported payload shape: {payload_shape}")


def extract_expected_bounds(payload: Dict[str, Any]) -> Dict[str, str]:
    if "from" in payload and "to" in payload:
        return {
            "from": str(payload["from"]),
            "to": str(payload["to"]),
        }
    time_bounds = payload.get("timeBounds") or {}
    return {
        "from": str(time_bounds.get("from") or ""),
        "to": str(time_bounds.get("to") or ""),
    }


def request_echo_validation(
    status_body: Dict[str, Any],
    expected_campaign_id: str,
    expected_from: str,
    expected_to: str,
    expected_kind: str = "SEARCH_PROMO_ORGANISATION_ORDERS",
) -> Dict[str, Any]:
    request = (status_body or {}).get("request") or {}
    actual_kind = str((status_body or {}).get("kind") or "")
    actual_campaign_id = str(request.get("campaignId") or "")
    actual_from = str(request.get("from") or "")
    actual_to = str(request.get("to") or "")
    valid = (
        actual_kind == expected_kind
        and
        actual_campaign_id == str(expected_campaign_id)
        and not actual_from.startswith("1970-01-01")
        and actual_from.startswith(expected_from)
        and actual_to.startswith(expected_to)
    )
    return {
        "valid": valid,
        "expected_campaign_id": str(expected_campaign_id),
        "expected_from": expected_from,
        "expected_to": expected_to,
        "expected_kind": expected_kind,
        "actual_kind": actual_kind,
        "actual_campaignId": actual_campaign_id,
        "actual_from": actual_from,
        "actual_to": actual_to,
        "reason": None if valid else "request echo does not match kind/campaignId/from/to payload shape",
    }


def submit_once(token: str, campaign_id: str, target_date: str, payload_shape: str) -> Dict[str, Any]:
    endpoint = "/api/client/statistic/orders/generate"
    payload = build_payload(campaign_id, target_date, payload_shape)
    response = requests.post(
        f"{report_probe.BASE_URL}{endpoint}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    body_json = report_probe.try_json(response)
    body = report_probe.sanitize_json(body_json) if body_json is not None else report_probe.sanitize_text(response.text)
    data = body_json if isinstance(body_json, dict) else {}
    uuid = data.get("UUID") or data.get("uuid") or data.get("id") or data.get("reportId")
    return {
        "endpoint": endpoint,
        "method": "POST",
        "payload": payload,
        "http_status": response.status_code,
        "response_body": body,
        "uuid_found": bool(uuid),
        "uuid": uuid,
    }


def build_guardrails(payload_shape: str) -> Dict[str, Any]:
    candidate_1_retried = False
    candidate_2_tried = payload_shape == "campaignId_number_from_to"
    candidate_3_tried = payload_shape == "campaignId_timeBounds"
    candidate_4_tried = payload_shape == "campaignIds_timeBounds"
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
        "candidates_tried": [payload_shape],
        "candidate_1_retried": candidate_1_retried,
        "candidate_2_tried": candidate_2_tried,
        "candidate_3_tried": candidate_3_tried,
        "candidate_4_tried": candidate_4_tried,
        "products_report_tried": False,
    }


def run(campaign_id: str, target_date: str, payload_shape: str, live_dry_run: bool, max_polls: int, poll_interval_sec: int) -> Dict[str, Any]:
    if not live_dry_run:
        raise RuntimeError("Без --live-dry-run HTTP-запросы не выполняются")

    summary: Dict[str, Any] = {
        "mode": "LIVE_DRY_RUN_CONTROLLED_SUBMIT_AND_DRY_PARSE",
        "campaign_id": str(campaign_id),
        "date": target_date,
        "payload_shape": payload_shape,
        "guardrails": build_guardrails(payload_shape),
    }

    token = report_probe.ensure_auth()
    submit = submit_once(token, campaign_id, target_date, payload_shape)
    summary["submit"] = {
        "endpoint": submit["endpoint"],
        "method": submit["method"],
        "payload": submit["payload"],
        "http_status": submit["http_status"],
        "uuid_found": submit["uuid_found"],
        "uuid": submit["uuid"],
    }

    if submit["http_status"] not in {200, 202}:
        summary["result"] = f"submit_http_{submit['http_status']}"
        summary["submit"]["response_body"] = submit["response_body"]
        return summary

    if not submit["uuid_found"]:
        summary["result"] = "submit_success_without_uuid"
        summary["submit"]["response_body"] = submit["response_body"]
        return summary

    uuid = str(submit["uuid"])
    status_result = report_probe.poll_status(uuid, token, max_polls=max_polls, poll_interval_sec=poll_interval_sec)
    summary["status"] = {
        "endpoint_used": status_result.get("endpoint_used"),
        "final_http_status": status_result.get("final_http_status"),
        "final_report_status": status_result.get("final_report_status"),
        "attempts": status_result.get("attempts", []),
        "kind": ((status_result.get("status_body") or {}).get("kind") if status_result.get("status_body") else None),
    }

    if status_result["result"] != "ready":
        summary["result"] = status_result["result"]
        if status_result.get("status_response_body_sanitized") is not None:
            summary["status"]["response_body_sanitized"] = status_result["status_response_body_sanitized"]
        return summary

    expected_bounds = extract_expected_bounds(submit["payload"])
    expected_from = expected_bounds["from"]
    expected_to = expected_bounds["to"]
    echo = request_echo_validation(
        status_result.get("status_body") or {},
        expected_campaign_id=str(campaign_id),
        expected_from=expected_from,
        expected_to=expected_to,
    )
    summary["request_echo_validation"] = echo

    if not echo["valid"]:
        summary["result"] = "uuid_created_but_request_echo_invalid"
        return summary

    download_result = report_probe.download_report(uuid, token)
    summary["download"] = {
        "endpoint_used": download_result.get("download", {}).get("endpoint_used") or download_result.get("endpoint_used"),
        "http_status": download_result.get("download", {}).get("http_status") or download_result.get("http_status"),
        "content_type": download_result.get("download", {}).get("content_type"),
        "bytes": download_result.get("download", {}).get("bytes"),
        "attempts": download_result.get("attempts", []),
    }

    if download_result["result"] != "downloaded":
        summary["result"] = download_result["result"]
        if download_result.get("response_body_sanitized") is not None:
            summary["download"]["response_body_sanitized"] = download_result["response_body_sanitized"]
        return summary

    summary["dry_parse"] = download_result["dry_parse"]
    summary["result"] = "downloaded_and_dry_parsed"
    return summary


def main() -> None:
    args = parse_args()
    summary = run(
        campaign_id=args.campaign_id,
        target_date=args.date,
        payload_shape=args.payload_shape,
        live_dry_run=args.live_dry_run,
        max_polls=args.max_polls,
        poll_interval_sec=args.poll_interval_sec,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
