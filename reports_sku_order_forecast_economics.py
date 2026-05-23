import argparse
import json
import os
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv
from postgrest.exceptions import APIError
from supabase import create_client

from reports_ozon_ad_diagnostic_rule import load_article_unit_costs, resolve_cogs_for_sku
from reports_stock_data_quality_issues import execute_read_with_retry


load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def parse_args():
    parser = argparse.ArgumentParser(description="Read-only SKU order-date forecast economics dry-run.")
    parser.add_argument("--marketplace-code", default="ozon")
    parser.add_argument("--sku", required=True)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--unit-cost", type=float)
    parser.add_argument("--target-profit-amount", type=float, default=0.0)
    parser.add_argument("--target-profit-rate", type=float, default=0.0)
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


def rounded(value, digits=2):
    if value is None:
        return None
    return round(float(value), digits)


def date_key(row, *field_names):
    for field in field_names:
        value = row.get(field)
        if value:
            return str(value)
    return None


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
            label=f"order-forecast:{table}:{start}",
        )
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return rows


def fetch_with_date_fallback(table: str, marketplace_code: str, sku: str, date_from: str, date_to: str, date_fields: Iterable[str]):
    last_error = None
    for date_field in date_fields:
        try:
            rows = fetch_all(
                table,
                filters=[
                    ("marketplace_code", "eq", marketplace_code),
                    ("marketplace_sku", "eq", str(sku)),
                    (date_field, "gte", date_from),
                    (date_field, "lte", date_to),
                ],
                order=date_field,
            )
            return rows, date_field
        except APIError as exc:
            last_error = exc
            if f"'{date_field}'" in str(exc) or "column" in str(exc).lower():
                continue
            raise
    if last_error:
        raise last_error
    return [], None


def load_daily_kpi_rows(marketplace_code: str, sku: str, date_from: str, date_to: str):
    return fetch_with_date_fallback("daily_sku_kpi", marketplace_code, sku, date_from, date_to, ("report_date", "kpi_date"))


def load_decision_rows(marketplace_code: str, sku: str, date_from: str, date_to: str):
    return fetch_with_date_fallback("sku_decision_daily_input", marketplace_code, sku, date_from, date_to, ("report_date", "kpi_date"))


def load_organic_rows(marketplace_code: str, sku: str, date_from: str, date_to: str):
    rows = fetch_all(
        "ozon_daily_sku_organic",
        filters=[
            ("marketplace_code", "eq", marketplace_code),
            ("marketplace_sku", "eq", str(sku)),
            ("sale_date", "gte", date_from),
            ("sale_date", "lte", date_to),
        ],
        order="sale_date",
    )
    return rows


def load_attribution_rows(marketplace_code: str, sku: str, date_from: str, date_to: str):
    rows = fetch_all(
        "ozon_daily_sku_ad_attribution",
        filters=[
            ("marketplace_code", "eq", marketplace_code),
            ("marketplace_sku", "eq", str(sku)),
            ("sale_date", "gte", date_from),
            ("sale_date", "lte", date_to),
        ],
        order="sale_date",
    )
    return rows


def load_selected_cpo_source_rows(date_from: str, date_to: str):
    return fetch_all(
        "ozon_search_promo_selected_cpo_orders",
        filters=[
            ("sale_date", "gte", date_from),
            ("sale_date", "lte", date_to),
        ],
        order="sale_date",
    )


def load_selected_cpo_expense_rows(marketplace_code: str, sku: str, date_from: str, date_to: str):
    return fetch_all(
        "marketplace_expenses",
        filters=[
            ("marketplace_code", "eq", marketplace_code),
            ("marketplace_sku", "eq", str(sku)),
            ("expense_date", "gte", date_from),
            ("expense_date", "lte", date_to),
            ("expense_type", "eq", "advertising_order_selected_cpo"),
        ],
        order="expense_date",
    )


def classify_selected_cpo_coverage(
    target_date: str,
    sku: str,
    source_rows_for_date: List[Dict],
    expense_rows_for_date: List[Dict],
    attribution_rows_for_date: List[Dict],
):
    ordered_rows = [row for row in source_rows_for_date if str(row.get("ordered_sku") or "") == str(sku)]
    promoted_rows = [row for row in source_rows_for_date if str(row.get("promoted_sku") or "") == str(sku)]
    ordered_spend = sum(num(row.get("spend")) for row in ordered_rows)
    promoted_spend = sum(num(row.get("spend")) for row in promoted_rows)
    expense_spend = sum(num(row.get("expense_amount")) for row in expense_rows_for_date)
    attr_rows = [
        row
        for row in attribution_rows_for_date
        if str(row.get("ad_source") or "") == "cpo_selected_products"
    ]
    attr_spend = sum(num(row.get("ad_spend")) for row in attr_rows)
    downstream_spend = max(expense_spend, attr_spend)
    mismatch_count = sum(1 for row in promoted_rows if str(row.get("ordered_sku") or "") != str(sku))

    warning = None
    if ordered_spend > 0 or promoted_spend > 0:
        status = "confirmed_present"
    elif source_rows_for_date:
        status = "confirmed_zero"
    elif downstream_spend > 0:
        status = "downstream_only"
        warning = "selected_cpo_source_missing_downstream_only"
    else:
        status = "not_loaded_unknown"
        warning = "selected_cpo_not_loaded_may_understate_ad_spend"

    if (ordered_spend > 0 or promoted_spend > 0) and (
        abs(expense_spend - ordered_spend) > 0.01 or abs(attr_spend - ordered_spend) > 0.01
    ):
        status = "inconsistent"
        warning = "selected_cpo_source_downstream_mismatch"

    if status in {"confirmed_present", "inconsistent"}:
        selected_cpo_spend_for_economics = max(ordered_spend, downstream_spend)
    elif status == "downstream_only":
        selected_cpo_spend_for_economics = downstream_spend
    else:
        selected_cpo_spend_for_economics = 0.0

    return {
        "date": target_date,
        "selected_cpo_status": status,
        "selected_cpo_source_rows_for_date": len(source_rows_for_date),
        "selected_cpo_source_total_spend_for_date": round(sum(num(row.get("spend")) for row in source_rows_for_date), 2),
        "selected_cpo_ordered_sku_source_spend": round(ordered_spend, 2),
        "selected_cpo_promoted_sku_source_spend": round(promoted_spend, 2),
        "selected_cpo_downstream_spend": round(downstream_spend, 2),
        "selected_cpo_warning": warning,
        "ordered_promoted_mismatch_count": mismatch_count,
        "selected_cpo_spend_for_economics": round(selected_cpo_spend_for_economics, 2),
    }


def select_expected_buyout_rate(kpi_rows: List[Dict]) -> Dict:
    rate_rows = []
    for row in sorted(kpi_rows, key=lambda item: date_key(item, "report_date", "kpi_date") or ""):
        orders_qty = num(row.get("orders_qty"))
        orders_revenue = num(row.get("orders_amount_seller"))
        buyouts_qty = num(row.get("buyouts_qty"))
        buyouts_revenue = num(row.get("buyouts_amount_seller"))
        if orders_qty <= 0 or orders_revenue <= 0:
            continue
        rate_rows.append(
            {
                "date": date_key(row, "report_date", "kpi_date"),
                "orders_qty": orders_qty,
                "orders_revenue": orders_revenue,
                "buyouts_qty": buyouts_qty,
                "buyouts_revenue": buyouts_revenue,
            }
        )

    def aggregate(last_n: int):
        sample = rate_rows[-last_n:]
        total_orders_qty = sum(item["orders_qty"] for item in sample)
        total_orders_revenue = sum(item["orders_revenue"] for item in sample)
        total_buyouts_qty = sum(item["buyouts_qty"] for item in sample)
        total_buyouts_revenue = sum(item["buyouts_revenue"] for item in sample)
        return {
            "window": last_n,
            "sample_days": len(sample),
            "sample_orders_qty": total_orders_qty,
            "sample_orders_revenue": total_orders_revenue,
            "rate_qty": safe_div(total_buyouts_qty, total_orders_qty),
            "rate_amount": safe_div(total_buyouts_revenue, total_orders_revenue),
        }

    candidates = {
        "3d": aggregate(3),
        "7d": aggregate(7),
        "14d": aggregate(14),
    }

    if candidates["7d"]["sample_orders_qty"] >= 15:
        selected = dict(candidates["7d"], source="sku_7d_weighted", confidence="high")
    elif candidates["14d"]["sample_orders_qty"] >= 15:
        selected = dict(candidates["14d"], source="sku_14d_weighted", confidence="medium")
    elif candidates["7d"]["sample_orders_qty"] >= 8:
        selected = dict(candidates["7d"], source="sku_7d_weighted", confidence="medium")
    elif candidates["14d"]["sample_orders_qty"] > 0:
        selected = dict(candidates["14d"], source="sku_14d_weighted", confidence="low")
    else:
        selected = dict(candidates["3d"], source="sku_3d_weighted", confidence="low")

    return {"candidates": candidates, "selected": selected}


def derive_variable_cost_assumptions(kpi_rows: List[Dict]) -> Dict:
    samples = []
    for row in sorted(kpi_rows, key=lambda item: date_key(item, "report_date", "kpi_date") or ""):
        buyouts_revenue = num(row.get("buyouts_amount_seller"))
        buyouts_qty = num(row.get("buyouts_qty"))
        if buyouts_revenue <= 0:
            continue
        samples.append(
            {
                "commission_rate": safe_div(row.get("commission_amount"), buyouts_revenue) or 0.0,
                "logistics_per_unit": safe_div(row.get("logistics_amount"), buyouts_qty) or 0.0,
                "other_rate": safe_div(row.get("other_expenses_amount"), buyouts_revenue) or 0.0,
            }
        )

    recent = samples[-7:] if len(samples) > 7 else samples

    def avg(key: str):
        if not recent:
            return 0.0
        return sum(item[key] for item in recent) / len(recent)

    assumption_flags = []
    acquiring_rate = 0.0
    assumption_flags.append("acquiring_rate_assumed_zero")

    return {
        "commission_rate": avg("commission_rate"),
        "commission_rate_source": "recent_sku_buyout_history_from_daily_sku_kpi",
        "acquiring_rate": acquiring_rate,
        "acquiring_rate_source": "assumed_zero_missing_explicit_layer",
        "logistics_per_unit": avg("logistics_per_unit"),
        "logistics_rate_source": "recent_sku_buyout_history_from_daily_sku_kpi",
        "other_rate": avg("other_rate"),
        "other_rate_source": "recent_sku_buyout_history_from_daily_sku_kpi",
        "assumption_flags": assumption_flags,
        "sample_days": len(recent),
    }


def build_forecast_row(
    base_row: Dict,
    selected_rate: Dict,
    cost_assumptions: Dict,
    unit_cost: float,
    cogs_source: str,
    target_profit_amount: float = 0.0,
    target_profit_rate: float = 0.0,
):
    orders_qty = num(base_row.get("orders_qty"))
    orders_revenue = num(base_row.get("orders_revenue"))
    ad_orders_revenue = num(base_row.get("ad_orders_revenue"))
    total_orders_revenue = ad_orders_revenue + num(base_row.get("organic_orders_revenue"))
    total_ad_spend = num(base_row.get("total_ad_spend"))
    expected_buyout_rate_qty = num(selected_rate.get("rate_qty"))
    expected_buyout_rate_amount = num(selected_rate.get("rate_amount"))

    expected_buyouts_qty = orders_qty * expected_buyout_rate_qty
    expected_buyouts_revenue = orders_revenue * expected_buyout_rate_amount
    expected_cogs = expected_buyouts_qty * unit_cost
    expected_commission = expected_buyouts_revenue * num(cost_assumptions.get("commission_rate"))
    expected_acquiring = expected_buyouts_revenue * num(cost_assumptions.get("acquiring_rate"))
    expected_logistics = expected_buyouts_qty * num(cost_assumptions.get("logistics_per_unit"))
    expected_other = expected_buyouts_revenue * num(cost_assumptions.get("other_rate"))
    expected_gross_margin = (
        expected_buyouts_revenue
        - expected_cogs
        - expected_commission
        - expected_acquiring
        - expected_logistics
        - expected_other
    )

    target_profit_from_rate = expected_buyouts_revenue * num(target_profit_rate)
    effective_target_profit_amount = max(num(target_profit_amount), target_profit_from_rate)
    max_affordable_ad_spend = expected_gross_margin - effective_target_profit_amount
    ad_spend_headroom = max_affordable_ad_spend - total_ad_spend
    expected_fin_result = expected_gross_margin - total_ad_spend

    assumption_flags = list(cost_assumptions.get("assumption_flags") or [])
    if not cogs_source or cogs_source == "missing":
        assumption_flags.append("cogs_missing")
    if num(cost_assumptions.get("acquiring_rate")) == 0:
        assumption_flags.append("acquiring_rate_zero_assumption")
    selected_cpo_status = base_row.get("selected_cpo_status")
    selected_cpo_warning = base_row.get("selected_cpo_warning")
    expected_fin_result_confidence = "high"
    if selected_cpo_status in {"not_loaded_unknown", "downstream_only", "inconsistent"}:
        expected_fin_result_confidence = "lower"
    if selected_cpo_warning:
        assumption_flags.append(selected_cpo_warning)

    return {
        "date": base_row.get("date"),
        "marketplace_code": base_row.get("marketplace_code"),
        "marketplace_sku": base_row.get("marketplace_sku"),
        "article": base_row.get("article"),
        "product_name": base_row.get("product_name"),
        "orders_qty": orders_qty,
        "orders_revenue": orders_revenue,
        "ad_orders_qty": num(base_row.get("ad_orders_qty")),
        "ad_orders_revenue": ad_orders_revenue,
        "organic_orders_qty": num(base_row.get("organic_orders_qty")),
        "organic_orders_revenue": num(base_row.get("organic_orders_revenue")),
        "cpc_spend": num(base_row.get("cpc_spend")),
        "cpo_all_spend": num(base_row.get("cpo_all_spend")),
        "selected_cpo_spend": num(base_row.get("selected_cpo_spend")),
        "total_ad_spend": total_ad_spend,
        "selected_cpo_status": selected_cpo_status,
        "selected_cpo_source_rows_for_date": num(base_row.get("selected_cpo_source_rows_for_date")),
        "selected_cpo_source_total_spend_for_date": num(base_row.get("selected_cpo_source_total_spend_for_date")),
        "selected_cpo_ordered_sku_source_spend": num(base_row.get("selected_cpo_ordered_sku_source_spend")),
        "selected_cpo_promoted_sku_source_spend": num(base_row.get("selected_cpo_promoted_sku_source_spend")),
        "selected_cpo_downstream_spend": num(base_row.get("selected_cpo_downstream_spend")),
        "selected_cpo_warning": selected_cpo_warning,
        "cpc_acos": safe_div(base_row.get("cpc_spend"), ad_orders_revenue),
        "total_order_tacos": safe_div(total_ad_spend, total_orders_revenue),
        "cpc_order_tacos": safe_div(base_row.get("cpc_spend"), total_orders_revenue),
        "selected_cpo_order_tacos": safe_div(base_row.get("selected_cpo_spend"), total_orders_revenue),
        "expected_buyout_rate_qty": expected_buyout_rate_qty,
        "expected_buyout_rate_amount": expected_buyout_rate_amount,
        "expected_buyout_rate_source": selected_rate.get("source"),
        "expected_buyout_rate_sample_orders": num(selected_rate.get("sample_orders_qty")),
        "expected_buyout_rate_confidence": selected_rate.get("confidence"),
        "expected_buyouts_qty": expected_buyouts_qty,
        "expected_buyouts_revenue": expected_buyouts_revenue,
        "unit_cost": unit_cost,
        "cogs_source": cogs_source,
        "expected_cogs": expected_cogs,
        "commission_rate": num(cost_assumptions.get("commission_rate")),
        "commission_rate_source": cost_assumptions.get("commission_rate_source"),
        "expected_commission": expected_commission,
        "acquiring_rate": num(cost_assumptions.get("acquiring_rate")),
        "acquiring_rate_source": cost_assumptions.get("acquiring_rate_source"),
        "expected_acquiring": expected_acquiring,
        "logistics_per_unit": num(cost_assumptions.get("logistics_per_unit")),
        "logistics_rate_source": cost_assumptions.get("logistics_rate_source"),
        "expected_logistics": expected_logistics,
        "other_rate": num(cost_assumptions.get("other_rate")),
        "other_rate_source": cost_assumptions.get("other_rate_source"),
        "expected_other": expected_other,
        "expected_gross_margin": expected_gross_margin,
        "expected_fin_result": expected_fin_result,
        "expected_fin_result_margin": safe_div(expected_fin_result, orders_revenue),
        "expected_fin_result_confidence": expected_fin_result_confidence,
        "target_profit_amount": num(target_profit_amount),
        "target_profit_rate": num(target_profit_rate),
        "max_affordable_ad_spend": max_affordable_ad_spend,
        "ad_spend_headroom": ad_spend_headroom,
        "decision_status": base_row.get("decision_status"),
        "data_quality_status": base_row.get("data_quality_status"),
        "organic_reconciliation_status": base_row.get("organic_reconciliation_status"),
        "assumption_flags": sorted(set(assumption_flags)),
    }


def build_report(
    marketplace_code: str,
    sku: str,
    date_from: str,
    date_to: str,
    unit_cost_override: Optional[float] = None,
    target_profit_amount: float = 0.0,
    target_profit_rate: float = 0.0,
    kpi_rows: Optional[List[Dict]] = None,
    organic_rows: Optional[List[Dict]] = None,
    attribution_rows: Optional[List[Dict]] = None,
    decision_rows: Optional[List[Dict]] = None,
    selected_cpo_source_rows: Optional[List[Dict]] = None,
    selected_cpo_expense_rows: Optional[List[Dict]] = None,
):
    kpi_rows = kpi_rows if kpi_rows is not None else load_daily_kpi_rows(marketplace_code, sku, date_from, date_to)[0]
    organic_rows = organic_rows if organic_rows is not None else load_organic_rows(marketplace_code, sku, date_from, date_to)
    attribution_rows = attribution_rows if attribution_rows is not None else load_attribution_rows(marketplace_code, sku, date_from, date_to)
    decision_rows = decision_rows if decision_rows is not None else load_decision_rows(marketplace_code, sku, date_from, date_to)[0]
    selected_cpo_source_rows = (
        selected_cpo_source_rows if selected_cpo_source_rows is not None else load_selected_cpo_source_rows(date_from, date_to)
    )
    selected_cpo_expense_rows = (
        selected_cpo_expense_rows
        if selected_cpo_expense_rows is not None
        else load_selected_cpo_expense_rows(marketplace_code, sku, date_from, date_to)
    )

    organic_by_date = {date_key(row, "sale_date"): row for row in organic_rows}
    decision_by_date = {date_key(row, "report_date", "kpi_date"): row for row in decision_rows}
    attr_by_date = {}
    for row in attribution_rows:
        attr_by_date.setdefault(date_key(row, "sale_date"), []).append(row)
    selected_cpo_source_by_date = {}
    for row in selected_cpo_source_rows:
        selected_cpo_source_by_date.setdefault(date_key(row, "sale_date"), []).append(row)
    selected_cpo_expense_by_date = {}
    for row in selected_cpo_expense_rows:
        selected_cpo_expense_by_date.setdefault(date_key(row, "expense_date"), []).append(row)

    ordered_kpi_rows = sorted(kpi_rows, key=lambda item: date_key(item, "report_date", "kpi_date") or "")
    article = next((str(row.get("article") or "").strip() for row in ordered_kpi_rows if str(row.get("article") or "").strip()), "")
    article_costs, article_costs_warning = load_article_unit_costs(marketplace_code, [article], date_to)
    unit_cost, cogs_source, cogs_lookup_warning = resolve_cogs_for_sku(
        marketplace_code,
        sku,
        article,
        date_to,
        unit_cost_override,
        "manual_or_default",
        article_costs=article_costs,
        article_costs_warning=article_costs_warning,
    )

    buyout_rate_info = select_expected_buyout_rate(ordered_kpi_rows)
    selected_rate = buyout_rate_info["selected"]
    cost_assumptions = derive_variable_cost_assumptions(ordered_kpi_rows)

    rows = []
    blockers = []
    for row in ordered_kpi_rows:
        report_date = date_key(row, "report_date", "kpi_date")
        organic_row = organic_by_date.get(report_date, {})
        decision_row = decision_by_date.get(report_date, {})
        attrs = attr_by_date.get(report_date, [])
        cpc_spend = sum(num(item.get("ad_spend")) for item in attrs if str(item.get("ad_source") or "") == "cpc")
        cpo_all_spend = sum(
            num(item.get("ad_spend"))
            for item in attrs
            if str(item.get("ad_source") or "").startswith("cpo_") and str(item.get("ad_source") or "") != "cpo_selected_products"
        )
        selected_cpo_coverage = classify_selected_cpo_coverage(
            report_date,
            sku,
            selected_cpo_source_by_date.get(report_date, []),
            selected_cpo_expense_by_date.get(report_date, []),
            attrs,
        )
        selected_cpo_spend = selected_cpo_coverage["selected_cpo_spend_for_economics"]
        total_ad_spend = cpc_spend + cpo_all_spend + selected_cpo_spend

        base_row = {
            "date": report_date,
            "marketplace_code": marketplace_code,
            "marketplace_sku": str(sku),
            "article": row.get("article"),
            "product_name": row.get("product_name"),
            "orders_qty": row.get("orders_qty"),
            "orders_revenue": row.get("orders_amount_seller"),
            "ad_orders_qty": organic_row.get("ad_orders_qty") if organic_row else row.get("ad_orders_qty"),
            "ad_orders_revenue": row.get("ad_orders_revenue"),
            "organic_orders_qty": organic_row.get("organic_orders_qty"),
            "organic_orders_revenue": row.get("organic_orders_revenue"),
            "cpc_spend": cpc_spend,
            "cpo_all_spend": cpo_all_spend,
            "selected_cpo_spend": selected_cpo_spend,
            "total_ad_spend": total_ad_spend,
            **selected_cpo_coverage,
            "decision_status": decision_row.get("decision_status") if decision_row else None,
            "data_quality_status": decision_row.get("data_quality_status") if decision_row else None,
            "organic_reconciliation_status": decision_row.get("organic_reconciliation_status") if decision_row else None,
        }
        forecast_row = build_forecast_row(
            base_row,
            selected_rate=selected_rate,
            cost_assumptions=cost_assumptions,
            unit_cost=unit_cost,
            cogs_source=cogs_source,
            target_profit_amount=target_profit_amount,
            target_profit_rate=target_profit_rate,
        )
        if forecast_row["acquiring_rate"] == 0:
            blockers.append({"date": report_date, "blocker": "acquiring_rate_assumed_zero"})
        if forecast_row.get("selected_cpo_warning"):
            blockers.append({"date": report_date, "blocker": forecast_row["selected_cpo_warning"]})
        rows.append(forecast_row)

    return {
        "marketplace_code": marketplace_code,
        "marketplace_sku": str(sku),
        "article": article,
        "date_from": date_from,
        "date_to": date_to,
        "unit_cost": unit_cost,
        "cogs_source": cogs_source,
        "cogs_lookup_warning": cogs_lookup_warning,
        "buyout_rate_candidates": buyout_rate_info["candidates"],
        "selected_expected_buyout_rate": selected_rate,
        "variable_cost_assumptions": cost_assumptions,
        "rows": rows,
        "blockers": blockers,
        "db_writes": 0,
        "migration_applied": False,
    }


def format_report_for_json(report: Dict):
    def normalize(value):
        if isinstance(value, float):
            return round(value, 4)
        if isinstance(value, dict):
            return {key: normalize(val) for key, val in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    return normalize(report)


def main():
    args = parse_args()
    report = build_report(
        marketplace_code=args.marketplace_code,
        sku=args.sku,
        date_from=args.date_from,
        date_to=args.date_to,
        unit_cost_override=args.unit_cost,
        target_profit_amount=args.target_profit_amount,
        target_profit_rate=args.target_profit_rate,
    )
    print(json.dumps(format_report_for_json(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
