#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loaders.ozon_performance_ads_loader import (
    OzonPerformanceClient,
    SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE,
    SELECTED_CPO_AD_SOURCE,
    SELECTED_CPO_MARKETPLACE_EXPENSE_TYPE,
    aggregate_search_promo_selected_cpo_rows,
    build_selected_cpo_source_table_rows,
    normalize_search_promo_selected_cpo_rows,
    parse_search_promo_organisation_orders_csv,
    supabase,
    upsert_selected_cpo_source_rows,
)


def sum_field(rows: List[dict], field: str) -> float:
    return round(sum(float(row.get(field) or 0) for row in rows), 2)


def load_marketplace_expenses_selected_cpo(date: str, sku: str) -> List[dict]:
    return (
        supabase.table("marketplace_expenses")
        .select("expense_date,marketplace_sku,expense_type,expense_amount,article")
        .eq("expense_date", date)
        .eq("marketplace_sku", sku)
        .eq("expense_type", SELECTED_CPO_MARKETPLACE_EXPENSE_TYPE)
        .execute()
        .data
        or []
    )


def load_marketplace_expenses_selected_cpo_date_total(date: str) -> List[dict]:
    return (
        supabase.table("marketplace_expenses")
        .select("expense_date,marketplace_sku,expense_type,expense_amount,article")
        .eq("expense_date", date)
        .eq("expense_type", SELECTED_CPO_MARKETPLACE_EXPENSE_TYPE)
        .execute()
        .data
        or []
    )


def load_ad_attribution_selected_cpo(date: str, sku: str) -> List[dict]:
    return (
        supabase.table("ozon_daily_sku_ad_attribution")
        .select(
            "sale_date,marketplace_sku,ad_source,ad_spend,ad_orders_qty,ad_orders_revenue,warning"
        )
        .eq("sale_date", date)
        .eq("marketplace_sku", sku)
        .eq("ad_source", SELECTED_CPO_AD_SOURCE)
        .execute()
        .data
        or []
    )


def load_ad_attribution_selected_cpo_date_total(date: str) -> List[dict]:
    return (
        supabase.table("ozon_daily_sku_ad_attribution")
        .select(
            "sale_date,marketplace_sku,ad_source,ad_spend,ad_orders_qty,ad_orders_revenue,warning"
        )
        .eq("sale_date", date)
        .eq("ad_source", SELECTED_CPO_AD_SOURCE)
        .execute()
        .data
        or []
    )


def load_existing_source_rows(date: str, sku: str) -> Dict[str, object]:
    rows = (
        supabase.table(SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE)
        .select(
            "sale_date,ordered_sku,promoted_sku,spend,source_uuid,source_report,promotion_type,source_kind"
        )
        .eq("sale_date", date)
        .execute()
        .data
        or []
    )
    ordered_rows = [row for row in rows if str(row.get("ordered_sku") or "") == sku]
    promoted_rows = [row for row in rows if str(row.get("promoted_sku") or "") == sku]
    mismatch_count = sum(
        1
        for row in rows
        if str(row.get("ordered_sku") or "") != str(row.get("promoted_sku") or "")
        and (str(row.get("ordered_sku") or "") == sku or str(row.get("promoted_sku") or "") == sku)
    )
    source_uuids = sorted({str(row.get("source_uuid") or "").strip() for row in rows if row.get("source_uuid")})
    source_reports = sorted({str(row.get("source_report") or "").strip() for row in rows if row.get("source_report")})
    promotion_types = sorted(
        {str(row.get("promotion_type") or "").strip() for row in rows if row.get("promotion_type")}
    )
    return {
        "source_rows": len(rows),
        "source_total_spend": sum_field(rows, "spend"),
        "ordered_sku_rows": len(ordered_rows),
        "ordered_sku_spend": sum_field(ordered_rows, "spend"),
        "promoted_sku_rows": len(promoted_rows),
        "promoted_sku_spend": sum_field(promoted_rows, "spend"),
        "mismatch_count": mismatch_count,
        "source_uuid": source_uuids,
        "source_report": source_reports,
        "promotion_type": promotion_types,
    }


def load_source_rows_for_date(date: str) -> List[dict]:
    return (
        supabase.table(SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE)
        .select("*")
        .eq("sale_date", date)
        .execute()
        .data
        or []
    )


def count_source_duplicates(date: str) -> int:
    rows = (
        supabase.table(SEARCH_PROMO_SELECTED_CPO_SOURCE_TABLE)
        .select("sale_date,marketplace_code,source_report,promotion_type,order_id,posting_number,ordered_sku,promoted_sku")
        .eq("sale_date", date)
        .execute()
        .data
        or []
    )
    seen = set()
    duplicates = 0
    for row in rows:
        key = (
            str(row.get("sale_date") or ""),
            str(row.get("marketplace_code") or ""),
            str(row.get("source_report") or ""),
            str(row.get("promotion_type") or ""),
            str(row.get("order_id") or ""),
            str(row.get("posting_number") or ""),
            str(row.get("ordered_sku") or ""),
            str(row.get("promoted_sku") or ""),
        )
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return duplicates


def count_marketplace_expenses_duplicates(date: str) -> int:
    rows = (
        supabase.table("marketplace_expenses")
        .select("expense_date,marketplace_code,marketplace_sku,expense_type")
        .eq("expense_date", date)
        .eq("expense_type", SELECTED_CPO_MARKETPLACE_EXPENSE_TYPE)
        .execute()
        .data
        or []
    )
    seen = set()
    duplicates = 0
    for row in rows:
        key = (
            str(row.get("expense_date") or ""),
            str(row.get("marketplace_code") or ""),
            str(row.get("marketplace_sku") or ""),
            str(row.get("expense_type") or ""),
        )
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return duplicates


def count_ad_attribution_duplicates(date: str) -> int:
    rows = (
        supabase.table("ozon_daily_sku_ad_attribution")
        .select("sale_date,marketplace_code,marketplace_sku,ad_source,attribution_type,campaign_id")
        .eq("sale_date", date)
        .eq("ad_source", SELECTED_CPO_AD_SOURCE)
        .execute()
        .data
        or []
    )
    seen = set()
    duplicates = 0
    for row in rows:
        key = (
            str(row.get("sale_date") or ""),
            str(row.get("marketplace_code") or ""),
            str(row.get("marketplace_sku") or ""),
            str(row.get("ad_source") or ""),
            str(row.get("attribution_type") or ""),
            str(row.get("campaign_id") or ""),
        )
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return duplicates


def classify_status(source_rows: int, source_total_spend: float, ordered_spend: float, promoted_spend: float) -> str:
    if source_rows > 0 and (ordered_spend > 0 or promoted_spend > 0):
        return "confirmed_present"
    if source_rows > 0 and source_total_spend == 0:
        return "report_empty_confirmed_zero"
    if source_rows > 0:
        return "confirmed_zero"
    return "not_loaded_unknown"


def summarize_rows(date: str, sku: str, rows: List[dict], source_uuid: str, source_report: str, promotion_type: str, http_requests_count: int, db_writes: int) -> Dict[str, object]:
    ordered_rows = [row for row in rows if str(row.get("ordered_sku") or "") == sku]
    promoted_rows = [row for row in rows if str(row.get("promoted_sku") or "") == sku]
    mismatch_count = sum(
        1
        for row in rows
        if str(row.get("ordered_sku") or "") != str(row.get("promoted_sku") or "")
        and (str(row.get("ordered_sku") or "") == sku or str(row.get("promoted_sku") or "") == sku)
    )
    source_total_spend = sum_field(rows, "spend")
    ordered_spend = sum_field(ordered_rows, "spend")
    promoted_spend = sum_field(promoted_rows, "spend")
    return {
        "date": date,
        "source_uuid": source_uuid,
        "source_rows": len(rows),
        "source_total_spend": source_total_spend,
        "ordered_sku_rows": len(ordered_rows),
        "ordered_sku_spend": ordered_spend,
        "promoted_sku_rows": len(promoted_rows),
        "promoted_sku_spend": promoted_spend,
        "mismatch_count": mismatch_count,
        "source_report": source_report,
        "promotion_type": promotion_type,
        "http_requests_count": http_requests_count,
        "db_writes": db_writes,
        "status": classify_status(len(rows), source_total_spend, ordered_spend, promoted_spend),
    }


def run_live_dry(date: str, sku: str) -> Dict[str, object]:
    client = OzonPerformanceClient()
    client.save_state = lambda *args, **kwargs: None
    original_runtime_state = client.snapshot_runtime_state()
    http_before = len(client.state.get("request_history", []) or [])
    try:
        summary = client.fetch_search_promo_orders_csv(
            date=date,
            write=False,
        )
        http_after = len(client.state.get("request_history", []) or [])
        rows = summary.get("source_table_rows") or []
        return summarize_rows(
            date=date,
            sku=sku,
            rows=rows,
            source_uuid=str(summary.get("uuid") or ""),
            source_report=str((summary.get("classification") or {}).get("source_report") or ""),
            promotion_type=str((summary.get("classification") or {}).get("promotion_type") or ""),
            http_requests_count=max(0, http_after - http_before),
            db_writes=int(summary.get("db_writes") or 0),
        )
    finally:
        client.restore_runtime_state(original_runtime_state)


def fetch_existing_uuid(date: str, sku: str, uuid: str, write_source_and_downstream: bool = False, approve_write: bool = False) -> Dict[str, object]:
    if write_source_and_downstream and not approve_write:
        raise RuntimeError("write requires --approve-selected-cpo-write")

    client = OzonPerformanceClient()
    client.save_state = lambda *args, **kwargs: None
    original_runtime_state = client.snapshot_runtime_state()
    http_before = len(client.state.get("request_history", []) or [])
    try:
        status = client.wait_statistics(uuid, poll_profile="all_sku_promo")
        csv_text, _download_headers = client.download_report_by_link(
            status.get("link"),
            uuid=uuid,
            return_meta=True,
        )
        parsed = parse_search_promo_organisation_orders_csv(csv_text)
        normalized_rows = normalize_search_promo_selected_cpo_rows(
            parsed,
            source_uuid=uuid,
            source_kind=str(status.get("kind") or ""),
        )
        source_table_rows = build_selected_cpo_source_table_rows(normalized_rows)
        aggregation = aggregate_search_promo_selected_cpo_rows(normalized_rows, parsed)
        source_writes = 0
        downstream_writes = 0
        marketplace_expenses_writes = 0
        ad_attribution_writes = 0
        if write_source_and_downstream:
            source_writes = upsert_selected_cpo_source_rows(supabase, source_table_rows)
            downstream = client.selected_cpo_downstream_dry_run(
                date=date,
                write=True,
                approve_downstream_write=True,
                db_client=supabase,
                source_rows=source_table_rows,
            )
            downstream_writes = int(downstream.get("db_writes") or 0)
            marketplace_expenses_writes = int(downstream.get("marketplace_expenses_writes") or 0)
            ad_attribution_writes = int(downstream.get("ozon_daily_sku_ad_attribution_writes") or 0)
        http_after = len(client.state.get("request_history", []) or [])
        result = summarize_rows(
            date=date,
            sku=sku,
            rows=source_table_rows,
            source_uuid=uuid,
            source_report="search_promo_organisation_orders",
            promotion_type="cpo_selected_products",
            http_requests_count=max(0, http_after - http_before),
            db_writes=source_writes + downstream_writes,
        )
        result["marketplace_expenses_writes"] = marketplace_expenses_writes
        result["ad_attribution_writes"] = ad_attribution_writes
        result["source_writes"] = source_writes
        result["source_total_spend_from_aggregation"] = round(float(aggregation.get("total_spend_data_rows") or 0), 2)
        return result
    finally:
        client.restore_runtime_state(original_runtime_state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Controlled selected CPO source fetch dry-run for one date.")
    parser.add_argument("--date", required=True)
    parser.add_argument("--sku", required=True)
    parser.add_argument("--no-write", action="store_true", default=True)
    parser.add_argument("--existing-report-uuid")
    parser.add_argument("--write-source-and-downstream", action="store_true")
    parser.add_argument("--approve-selected-cpo-write", action="store_true")
    parser.add_argument(
        "--use-existing-source-only",
        action="store_true",
        help="Read only existing source table rows for the date instead of live submit/poll/download.",
    )
    args = parser.parse_args()

    if args.use_existing_source_only:
        result = {
            "date": args.date,
            **load_existing_source_rows(args.date, args.sku),
            "http_requests_count": 0,
            "db_writes": 0,
        }
        result["status"] = classify_status(
            int(result["source_rows"]),
            float(result["source_total_spend"]),
            float(result["ordered_sku_spend"]),
            float(result["promoted_sku_spend"]),
        )
    elif args.existing_report_uuid:
        result = fetch_existing_uuid(
            date=args.date,
            sku=args.sku,
            uuid=args.existing_report_uuid,
            write_source_and_downstream=bool(args.write_source_and_downstream),
            approve_write=bool(args.approve_selected_cpo_write),
        )
    else:
        result = run_live_dry(args.date, args.sku)

    expenses_rows = load_marketplace_expenses_selected_cpo(args.date, args.sku)
    ad_attr_rows = load_ad_attribution_selected_cpo(args.date, args.sku)
    expenses_rows_total = load_marketplace_expenses_selected_cpo_date_total(args.date)
    ad_attr_rows_total = load_ad_attribution_selected_cpo_date_total(args.date)
    result["marketplace_expenses_spend"] = sum_field(expenses_rows, "expense_amount")
    result["marketplace_expenses_rows"] = len(expenses_rows)
    result["marketplace_expenses_total_for_date"] = sum_field(expenses_rows_total, "expense_amount")
    result["marketplace_expenses_rows_for_date"] = len(expenses_rows_total)
    result["ad_attribution_spend"] = sum_field(ad_attr_rows, "ad_spend")
    result["ad_attribution_orders"] = sum_field(ad_attr_rows, "ad_orders_qty")
    result["ad_attribution_revenue"] = sum_field(ad_attr_rows, "ad_orders_revenue")
    result["ad_attribution_total_spend_for_date"] = sum_field(ad_attr_rows_total, "ad_spend")
    result["ad_attribution_rows_for_date"] = len(ad_attr_rows_total)
    result["source_duplicates"] = count_source_duplicates(args.date)
    result["marketplace_expenses_duplicates"] = count_marketplace_expenses_duplicates(args.date)
    result["ad_attribution_duplicates"] = count_ad_attribution_duplicates(args.date)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
