import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client

from reports_stock_data_quality_issues import execute_read_with_retry


load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

KNOWN_CAMPAIGN_HINTS = {
    "24375352": {
        "title": "F000283615",
        "state": "CAMPAIGN_STATE_RUNNING",
        "adv_object_type": "SKU",
        "payment_type": "CPC",
        "placement": "PLACEMENT_TOP_PROMOTION",
        "product_campaign_mode": "PRODUCT_CAMPAIGN_MODE_AUTO",
        "product_autopilot_strategy": "TARGET_BIDS",
        "role": "primary",
    },
    "24375331": {
        "title": "F000283615",
        "state": "CAMPAIGN_STATE_RUNNING",
        "adv_object_type": "SKU",
        "payment_type": "CPC",
        "placement": "PLACEMENT_SEARCH_AND_CATEGORY",
        "product_campaign_mode": "PRODUCT_CAMPAIGN_MODE_AUTO",
        "product_autopilot_strategy": "TARGET_BIDS",
        "role": "secondary",
    },
}

KNOWN_PARTIAL_DATES = {"2026-05-12"}
LOOKBACK_WINDOWS = (3, 5, 7, 14)


def parse_args():
    parser = argparse.ArgumentParser(description="Dry-run diagnostic ad rule for one Ozon SKU.")
    parser.add_argument("--marketplace-code", default="ozon")
    parser.add_argument("--sku", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--campaign-id", action="append", dest="campaign_ids", default=[])
    parser.add_argument("--cogs", type=float, required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def safe_div(numerator, denominator):
    denominator = num(denominator)
    if denominator <= 0:
        return None
    return num(numerator) / denominator


def iso_days_back(target_date: str, days: int) -> str:
    return (datetime.fromisoformat(target_date).date() - timedelta(days=days - 1)).isoformat()


def fetch_all(table, filters=None, order=None, desc=False):
    rows = []
    start = 0
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
            elif operator == "in":
                query = query.in_(field, value)
            else:
                raise RuntimeError(f"Unsupported operator: {operator}")

        if order:
            query = query.order(order, desc=desc)

        result = execute_read_with_retry(
            lambda: query.range(start, start + page_size - 1).execute(),
            label=f"ad-diagnostic:{table}:{start}",
        )
        batch = result.data or []
        rows.extend(batch)

        if len(batch) < page_size:
            break

        start += page_size

    return rows


def load_daily_kpi_rows(marketplace_code: str, sku: str, date_from: str, date_to: str):
    return fetch_all(
        "daily_sku_kpi",
        filters=[
            ("marketplace_code", "eq", marketplace_code),
            ("marketplace_sku", "eq", str(sku)),
            ("kpi_date", "gte", date_from),
            ("kpi_date", "lte", date_to),
        ],
        order="kpi_date",
    )


def load_expense_rows(marketplace_code: str, sku: str, date_from: str, date_to: str):
    return fetch_all(
        "marketplace_expenses",
        filters=[
            ("marketplace_code", "eq", marketplace_code),
            ("marketplace_sku", "eq", str(sku)),
            ("expense_date", "gte", date_from),
            ("expense_date", "lte", date_to),
        ],
        order="expense_date",
    )


def load_attribution_rows(marketplace_code: str, sku: str, date_from: str, date_to: str, campaign_ids: Iterable[str]):
    filters = [
        ("marketplace_code", "eq", marketplace_code),
        ("marketplace_sku", "eq", str(sku)),
        ("sale_date", "gte", date_from),
        ("sale_date", "lte", date_to),
        ("ad_source", "eq", "cpc"),
    ]
    campaign_ids = [str(c) for c in campaign_ids if str(c).strip()]
    if campaign_ids:
        filters.append(("campaign_id", "in", campaign_ids))
    return fetch_all("ozon_daily_sku_ad_attribution", filters=filters, order="sale_date")


def load_decision_row(marketplace_code: str, sku: str, target_date: str):
    rows = fetch_all(
        "sku_decision_daily_input",
        filters=[
            ("marketplace_code", "eq", marketplace_code),
            ("marketplace_sku", "eq", str(sku)),
            ("kpi_date", "eq", target_date),
        ],
        order="kpi_date",
    )
    return rows[0] if rows else None


def aggregate_expenses_by_date(expense_rows: List[dict]) -> Dict[str, dict]:
    by_date = defaultdict(
        lambda: {
            "advertising_clicks": 0.0,
            "advertising_other": 0.0,
            "advertising_order_5": 0.0,
            "advertising_order_selected_cpo": 0.0,
            "commission": 0.0,
            "logistics": 0.0,
            "other": 0.0,
            "total_ad_spend": 0.0,
        }
    )
    for row in expense_rows:
        date_key = str(row.get("expense_date") or "")
        expense_type = str(row.get("expense_type") or "")
        amount = num(row.get("expense_amount"))
        bucket = by_date[date_key]
        if expense_type in bucket:
            bucket[expense_type] += amount
        elif expense_type.startswith("advertising"):
            bucket["total_ad_spend"] += amount
            continue
        else:
            bucket["other"] += amount
        if expense_type.startswith("advertising"):
            bucket["total_ad_spend"] += amount
    return dict(by_date)


def aggregate_attr_daily(attr_rows: List[dict], campaign_ids: Iterable[str]) -> Dict[str, Dict[str, dict]]:
    result = {str(c): {} for c in campaign_ids}
    for row in attr_rows:
        campaign_id = str(row.get("campaign_id") or "")
        if campaign_id not in result:
            continue
        sale_date = str(row.get("sale_date") or "")
        result[campaign_id][sale_date] = {
            "spend": num(row.get("ad_spend")),
            "orders": num(row.get("ad_orders_qty")),
            "revenue": num(row.get("ad_orders_revenue")),
        }
    return result


def row_map_by_date(rows: List[dict], date_field: str) -> Dict[str, dict]:
    return {str(row.get(date_field) or ""): row for row in rows}


def compute_window_metrics(daily_metrics: Dict[str, dict], target_date: str, window_days: int) -> dict:
    window_start = iso_days_back(target_date, window_days)
    date_keys = sorted([date_key for date_key in daily_metrics.keys() if window_start <= date_key <= target_date])
    spend = sum(num(daily_metrics[date_key].get("spend")) for date_key in date_keys)
    orders = sum(num(daily_metrics[date_key].get("orders")) for date_key in date_keys)
    revenue = sum(num(daily_metrics[date_key].get("revenue")) for date_key in date_keys)
    zero_order_spend_days = sum(
        1 for date_key in date_keys if num(daily_metrics[date_key].get("spend")) > 0 and num(daily_metrics[date_key].get("orders")) <= 0
    )
    return {
        "days_available": len(date_keys),
        "spend": round(spend, 2),
        "orders": round(orders, 2),
        "revenue": round(revenue, 2),
        "roas": round(safe_div(revenue, spend), 4) if safe_div(revenue, spend) is not None else None,
        "cost_per_ad_order": round(safe_div(spend, orders), 2) if safe_div(spend, orders) is not None else None,
        "zero_order_spend_days": zero_order_spend_days,
        "contains_partial_date": any(date_key in KNOWN_PARTIAL_DATES for date_key in date_keys),
    }


def trailing_zero_order_days(daily_metrics: Dict[str, dict], target_date: str, min_spend: float = 1000.0) -> int:
    streak = 0
    cursor = datetime.fromisoformat(target_date).date()
    if not daily_metrics:
        return 0
    earliest = min(datetime.fromisoformat(date_key).date() for date_key in daily_metrics.keys())
    while cursor >= earliest:
        date_key = cursor.isoformat()
        metric = daily_metrics.get(date_key)
        if not metric:
            break
        if num(metric.get("spend")) > min_spend and num(metric.get("orders")) <= 0:
            streak += 1
            cursor -= timedelta(days=1)
            continue
        break
    return streak


def compute_buyout_lookbacks(kpi_rows: List[dict], target_date: str) -> dict:
    by_date = row_map_by_date(kpi_rows, "kpi_date")
    result = {}
    for window_days in (7, 14):
        window_start = iso_days_back(target_date, window_days)
        rows = [row for date_key, row in by_date.items() if window_start <= date_key <= target_date]
        result[f"buyouts_{window_days}d"] = round(sum(num(row.get("buyouts_qty")) for row in rows), 2)
    return result


def build_sku_economics(target_kpi_row: dict, target_expense_summary: dict, cogs: float) -> dict:
    buyouts_qty = num(target_kpi_row.get("buyouts_qty"))
    buyouts_revenue = num(target_kpi_row.get("buyouts_amount_seller"))
    cogs_total = buyouts_qty * num(cogs)
    commission = num(target_expense_summary.get("commission"))
    logistics = num(target_expense_summary.get("logistics"))
    other = num(target_expense_summary.get("other"))
    cpc_spend = num(target_expense_summary.get("advertising_clicks")) + num(target_expense_summary.get("advertising_other"))
    cpo_all_spend = num(target_expense_summary.get("advertising_order_5"))
    selected_cpo_spend = num(target_expense_summary.get("advertising_order_selected_cpo"))
    controllable_ad_spend = cpc_spend
    non_controllable_ad_spend = cpo_all_spend + selected_cpo_spend
    total_ad_spend = controllable_ad_spend + non_controllable_ad_spend
    net_estimate = buyouts_revenue - cogs_total - commission - logistics - other - total_ad_spend
    total_tacos = safe_div(total_ad_spend, buyouts_revenue)
    cpc_tacos = safe_div(cpc_spend, buyouts_revenue)
    cpo_all_tacos = safe_div(cpo_all_spend, buyouts_revenue)
    selected_cpo_tacos = safe_div(selected_cpo_spend, buyouts_revenue)
    return {
        "orders": num(target_kpi_row.get("orders_qty")),
        "orders_revenue": num(target_kpi_row.get("orders_amount_seller")),
        "buyouts": buyouts_qty,
        "buyouts_revenue": buyouts_revenue,
        "cpc_spend": round(cpc_spend, 2),
        "cpo_all_spend": round(cpo_all_spend, 2),
        "selected_cpo_spend": round(selected_cpo_spend, 2),
        "controllable_ad_spend": round(controllable_ad_spend, 2),
        "non_controllable_ad_spend": round(non_controllable_ad_spend, 2),
        "total_ad_spend": round(total_ad_spend, 2),
        "actual_ad_spend": round(total_ad_spend, 2),
        "net_estimate": round(net_estimate, 2),
        "total_tacos": round(total_tacos, 4) if total_tacos is not None else None,
        "cpc_tacos": round(cpc_tacos, 4) if cpc_tacos is not None else None,
        "cpo_all_tacos": round(cpo_all_tacos, 4) if cpo_all_tacos is not None else None,
        "selected_cpo_tacos": round(selected_cpo_tacos, 4) if selected_cpo_tacos is not None else None,
        "tacos": round(total_tacos, 4) if total_tacos is not None else None,
        "commission": round(commission, 2),
        "logistics": round(logistics, 2),
        "other": round(other, 2),
        "cogs_total": round(cogs_total, 2),
    }


def evaluate_sku_eligibility(target_kpi_row: dict, decision_row: Optional[dict], sku_economics: dict, buyout_lookbacks: dict) -> dict:
    base_blockers = []
    total_economics_reasons = []
    cpc_control_reasons = []
    decision_status = str((decision_row or {}).get("decision_status") or "")
    data_quality_status = str((decision_row or {}).get("data_quality_status") or "")
    organic_reconciliation_status = str((decision_row or {}).get("organic_reconciliation_status") or "")
    stock_status = str((decision_row or {}).get("stock_status") or (target_kpi_row or {}).get("stock_status") or "")
    stock_qty = num((decision_row or {}).get("stock_qty"))
    if stock_qty <= 0:
        stock_qty = num((target_kpi_row or {}).get("stock_qty"))

    if decision_status != "ready":
        base_blockers.append(f"decision_status={decision_status or 'missing'}")
    if data_quality_status != "ok":
        base_blockers.append(f"data_quality_status={data_quality_status or 'missing'}")
    if organic_reconciliation_status != "clean":
        base_blockers.append(f"organic_reconciliation_status={organic_reconciliation_status or 'missing'}")
    if not (stock_status == "stock_ok" or stock_qty >= 30):
        base_blockers.append(f"stock_not_ok:{stock_status or 'unknown'}:{stock_qty:g}")
    if buyout_lookbacks["buyouts_14d"] < 5 and buyout_lookbacks["buyouts_7d"] < 3:
        base_blockers.append(
            f"insufficient_buyout_history:buyouts_7d={buyout_lookbacks['buyouts_7d']}:buyouts_14d={buyout_lookbacks['buyouts_14d']}"
        )
    if sku_economics["net_estimate"] <= 0:
        base_blockers.append(f"net_estimate_non_positive={sku_economics['net_estimate']}")

    total_tacos = sku_economics.get("total_tacos")
    cpc_tacos = sku_economics.get("cpc_tacos")
    selected_cpo_tacos = sku_economics.get("selected_cpo_tacos")
    if total_tacos is None:
        total_economics_reasons.append("total_tacos_unavailable")
    elif total_tacos > 0.08:
        total_economics_reasons.append(f"total_tacos_above_threshold={total_tacos}")
        if selected_cpo_tacos is not None and selected_cpo_tacos > 0.05:
            total_economics_reasons.append("selected_cpo_pressure")
        if sku_economics["non_controllable_ad_spend"] > sku_economics["controllable_ad_spend"]:
            total_economics_reasons.append("total_economics_caution")

    if cpc_tacos is None:
        cpc_control_reasons.append("cpc_tacos_unavailable")
    elif cpc_tacos > 0.08:
        cpc_control_reasons.append(f"cpc_tacos_above_threshold={cpc_tacos}")

    cpc_control_reasons.extend(base_blockers)

    cpc_control_status = "eligible_for_diagnostic" if not cpc_control_reasons else "blocked"
    return {
        "status": "eligible" if cpc_control_status == "eligible_for_diagnostic" else "not_eligible",
        "reasons": list(base_blockers + total_economics_reasons),
        "sku_total_economics_status": "YELLOW" if total_economics_reasons else "GREEN",
        "sku_total_economics_reasons": total_economics_reasons,
        "cpc_control_eligibility_status": cpc_control_status,
        "cpc_control_eligibility_reasons": cpc_control_reasons,
        "stock_qty": stock_qty,
        "stock_status": stock_status or None,
        "buyouts_7d": buyout_lookbacks["buyouts_7d"],
        "buyouts_14d": buyout_lookbacks["buyouts_14d"],
    }


def compare_campaign_strength(campaigns: List[dict]) -> dict:
    if not campaigns:
        return {}

    def revenue_volume_score(item):
        metrics_5d = item.get("windows", {}).get("5d", {})
        return (num(metrics_5d.get("revenue")), num(metrics_5d.get("orders")), num(metrics_5d.get("spend")))

    def roas_score(item):
        metrics_5d = item.get("windows", {}).get("5d", {})
        return (num(metrics_5d.get("roas") or 0), num(metrics_5d.get("revenue")), num(metrics_5d.get("orders")))

    def stability_score(item):
        metrics_5d = item.get("windows", {}).get("5d", {})
        metrics_3d = item.get("windows", {}).get("3d", {})
        return (
            -int(metrics_5d.get("zero_order_spend_days") or 0),
            -int(item.get("trailing_zero_order_days") or 0),
            num(metrics_5d.get("orders")),
            num(metrics_3d.get("orders")),
        )

    return {
        "stronger_by_revenue_volume": sorted(campaigns, key=revenue_volume_score, reverse=True)[0].get("campaign_id"),
        "stronger_by_roas": sorted(campaigns, key=roas_score, reverse=True)[0].get("campaign_id"),
        "stronger_by_stability": sorted(campaigns, key=stability_score, reverse=True)[0].get("campaign_id"),
    }


def evaluate_campaign(campaign_id: str, metadata: dict, daily_metrics: Dict[str, dict], target_date: str, sku_eligibility: dict, peer_comparison: dict) -> dict:
    windows = {
        f"{window_days}d": compute_window_metrics(daily_metrics, target_date, window_days)
        for window_days in LOOKBACK_WINDOWS
    }
    trailing_zero_days = trailing_zero_order_days(daily_metrics, target_date)
    reasons = []
    status = "YELLOW"
    recommendation = "hold_watch"

    if sku_eligibility["cpc_control_eligibility_status"] != "eligible_for_diagnostic":
        reasons.append("cpc_control_eligibility_blocked")

    spend_3d = windows["3d"]["spend"]
    orders_3d = windows["3d"]["orders"]
    orders_5d = windows["5d"]["orders"]
    roas_5d = windows["5d"]["roas"]

    if spend_3d > 3000 and orders_3d <= 0:
        status = "RED"
        recommendation = "reduce_candidate"
        reasons.append("spend_3d_gt_3000_and_orders_3d_eq_0")
    elif trailing_zero_days >= 2:
        status = "RED"
        recommendation = "reduce_candidate"
        reasons.append("trailing_high_spend_zero_order_days_gte_2")
    elif orders_5d <= 0 and windows["5d"]["spend"] > 0:
        status = "RED"
        recommendation = "reduce_candidate"
        reasons.append("spend_present_but_no_orders_5d")
    elif sku_eligibility["status"] == "eligible" and orders_5d > 0 and roas_5d and trailing_zero_days < 2:
        status = "GREEN"
        recommendation = "keep_or_cautious_increase"
        reasons.append("orders_present_and_roas_positive")
    else:
        status = "YELLOW"
        recommendation = "hold_watch"
        reasons.append("mixed_or_small_sample")

    stronger_by_revenue_volume = peer_comparison.get("stronger_by_revenue_volume")
    stronger_by_roas = peer_comparison.get("stronger_by_roas")
    stronger_by_stability = peer_comparison.get("stronger_by_stability")

    if stronger_by_revenue_volume == campaign_id:
        reasons.append("stronger_by_revenue_volume")
    elif stronger_by_revenue_volume:
        reasons.append(f"weaker_by_revenue_volume:{stronger_by_revenue_volume}")

    if stronger_by_roas == campaign_id:
        reasons.append("stronger_by_roas")
    elif stronger_by_roas:
        reasons.append(f"weaker_by_roas:{stronger_by_roas}")

    if stronger_by_stability == campaign_id:
        reasons.append("stronger_by_stability")
    elif stronger_by_stability:
        reasons.append(f"weaker_by_stability:{stronger_by_stability}")

    if (
        stronger_by_revenue_volume
        and campaign_id != stronger_by_revenue_volume
        and stronger_by_stability
        and campaign_id != stronger_by_stability
        and status == "GREEN"
    ):
        status = "YELLOW"
        recommendation = "hold_watch"

    if windows["5d"]["contains_partial_date"]:
        reasons.append("lookback_contains_known_partial_date")
        if status == "RED" and recommendation == "reduce_candidate" and orders_3d > 0:
            status = "YELLOW"
            recommendation = "hold_watch"

    return {
        "campaign_id": campaign_id,
        "title": metadata.get("title"),
        "state": metadata.get("state"),
        "adv_object_type": metadata.get("adv_object_type"),
        "payment_type": metadata.get("payment_type"),
        "placement": metadata.get("placement"),
        "product_campaign_mode": metadata.get("product_campaign_mode"),
        "product_autopilot_strategy": metadata.get("product_autopilot_strategy"),
        "role": metadata.get("role") or "unknown",
        "windows": windows,
        "trailing_zero_order_days": trailing_zero_days,
        "peer_comparison": {
            "stronger_by_revenue_volume": stronger_by_revenue_volume,
            "stronger_by_roas": stronger_by_roas,
            "stronger_by_stability": stronger_by_stability,
        },
        "status": status,
        "recommendation": recommendation,
        "reasons": reasons,
    }


def build_final_recommendation(eligibility: dict, campaigns: List[dict]) -> dict:
    if eligibility["cpc_control_eligibility_status"] != "eligible_for_diagnostic":
        return {
            "status": "RED",
            "action": "diagnostic_only_hold",
            "live_action_allowed": False,
            "reason": "diagnostic only: cpc control eligibility blocked",
        }

    if any(campaign["status"] == "RED" for campaign in campaigns):
        return {
            "status": "YELLOW",
            "action": "diagnostic_only_watch_or_reduce_candidate",
            "live_action_allowed": False,
            "reason": "diagnostic only: at least one campaign is a reduce candidate",
        }

    if eligibility["sku_total_economics_status"] != "GREEN":
        return {
            "status": "YELLOW",
            "action": "diagnostic_only_hold",
            "live_action_allowed": False,
            "reason": "diagnostic only: total economics caution",
        }

    if any(campaign["status"] == "YELLOW" for campaign in campaigns):
        return {
            "status": "YELLOW",
            "action": "diagnostic_only_hold",
            "live_action_allowed": False,
            "reason": "diagnostic only: campaigns require watchful hold",
        }

    return {
        "status": "GREEN",
        "action": "diagnostic_only_cautious_increase",
        "live_action_allowed": False,
        "reason": "diagnostic only",
    }


def build_report(marketplace_code: str, sku: str, target_date: str, campaign_ids: Iterable[str], cogs: float, *, kpi_rows: List[dict], expense_rows: List[dict], attribution_rows: List[dict], decision_row: Optional[dict]):
    campaign_ids = [str(c) for c in campaign_ids]
    kpi_by_date = row_map_by_date(kpi_rows, "kpi_date")
    target_kpi_row = kpi_by_date.get(target_date)
    if not target_kpi_row:
        raise RuntimeError(f"No daily_sku_kpi row for sku={sku} date={target_date}")

    expense_by_date = aggregate_expenses_by_date(expense_rows)
    target_expense_summary = expense_by_date.get(target_date, {})
    sku_economics = build_sku_economics(target_kpi_row, target_expense_summary, cogs)
    buyout_lookbacks = compute_buyout_lookbacks(kpi_rows, target_date)
    eligibility = evaluate_sku_eligibility(target_kpi_row, decision_row, sku_economics, buyout_lookbacks)

    daily_attr = aggregate_attr_daily(attribution_rows, campaign_ids)
    campaign_payloads = [
        {
            "campaign_id": campaign_id,
            "windows": {
                f"{window_days}d": compute_window_metrics(daily_attr.get(campaign_id, {}), target_date, window_days)
                for window_days in LOOKBACK_WINDOWS
            },
            "trailing_zero_order_days": trailing_zero_order_days(daily_attr.get(campaign_id, {}), target_date),
        }
        for campaign_id in campaign_ids
    ]
    peer_comparison = compare_campaign_strength(campaign_payloads)
    campaign_payloads = [
        evaluate_campaign(
            campaign["campaign_id"],
            dict(KNOWN_CAMPAIGN_HINTS.get(campaign["campaign_id"], {})),
            daily_attr.get(campaign["campaign_id"], {}),
            target_date,
            eligibility,
            peer_comparison,
        )
        for campaign in campaign_payloads
    ]

    return {
        "sku": str(sku),
        "date": target_date,
        "eligibility": eligibility,
        "sku_economics": sku_economics,
        "campaigns": campaign_payloads,
        "final_recommendation": build_final_recommendation(eligibility, campaign_payloads),
        "db_writes": 0,
        "api_calls": 0,
        "campaign_mutations": 0,
        "pipeline_runs": 0,
        "render_changes": 0,
    }


def run_dry_report(marketplace_code: str, sku: str, target_date: str, campaign_ids: Iterable[str], cogs: float):
    history_from = iso_days_back(target_date, 14)
    kpi_rows = load_daily_kpi_rows(marketplace_code, sku, history_from, target_date)
    expense_rows = load_expense_rows(marketplace_code, sku, history_from, target_date)
    attribution_rows = load_attribution_rows(marketplace_code, sku, history_from, target_date, campaign_ids)
    decision_row = load_decision_row(marketplace_code, sku, target_date)
    return build_report(
        marketplace_code,
        sku,
        target_date,
        campaign_ids,
        cogs,
        kpi_rows=kpi_rows,
        expense_rows=expense_rows,
        attribution_rows=attribution_rows,
        decision_row=decision_row,
    )


def main():
    args = parse_args()
    if not args.campaign_ids:
        raise RuntimeError("At least one --campaign-id is required")

    report = run_dry_report(
        args.marketplace_code,
        args.sku,
        args.date,
        args.campaign_ids,
        args.cogs,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
