import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client


load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")
TABLE_NAME = "ozon_organic_reconciliation_issues"

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def parse_args():
    parser = argparse.ArgumentParser(description="Build Ozon organic reconciliation issues.")
    parser.add_argument("--date", help="single-day shortcut, sets both --date-from and --date-to")
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--days-back", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug-sample", action="store_true")
    return parser.parse_args()


def resolve_date_range(args):
    if args.date:
        return args.date, args.date

    date_to = args.date_to or (datetime.now(ZoneInfo(APP_TIMEZONE)).date() - timedelta(days=1)).isoformat()
    if args.date_from:
        return args.date_from, date_to

    date_from = (datetime.fromisoformat(date_to).date() - timedelta(days=args.days_back - 1)).isoformat()
    return date_from, date_to


def fetch_all(table, filters=None, order=None):
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
        if order:
            query = query.order(order)
        result = query.range(start, start + page_size - 1).execute()
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size

    return rows


def num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def rows_by_key(rows, key):
    grouped = defaultdict(list)
    for row in rows:
        value = str(row.get(key) or "").strip()
        if value:
            grouped[value].append(row)
    return grouped


def build_reconciliation_rows(date_from, date_to):
    organic_rows = fetch_all(
        "ozon_daily_sku_organic",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("sale_date", "gte", date_from),
            ("sale_date", "lte", date_to),
        ],
        order="sale_date",
    )
    ad_attr_rows = fetch_all(
        "ozon_daily_sku_ad_attribution",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("sale_date", "gte", date_from),
            ("sale_date", "lte", date_to),
        ],
        order="sale_date",
    )
    total_rows = fetch_all(
        "ozon_daily_sku_total_orders",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("sale_date", "gte", date_from),
            ("sale_date", "lte", date_to),
        ],
        order="sale_date",
    )
    pm1_total_rows = fetch_all(
        "ozon_daily_sku_total_orders",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("sale_date", "gte", (datetime.fromisoformat(date_from).date() - timedelta(days=1)).isoformat()),
            ("sale_date", "lte", (datetime.fromisoformat(date_to).date() + timedelta(days=1)).isoformat()),
        ],
        order="sale_date",
    )
    pm1_order_rows = fetch_all(
        "marketplace_orders",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("order_date", "gte", (datetime.fromisoformat(date_from).date() - timedelta(days=1)).isoformat()),
            ("order_date", "lte", (datetime.fromisoformat(date_to).date() + timedelta(days=1)).isoformat()),
        ],
        order="order_date",
    )

    attr_by_key = defaultdict(list)
    for row in ad_attr_rows:
        key = (
            str(row.get("sale_date") or "").strip(),
            str(row.get("marketplace_sku") or "").strip(),
            str(row.get("article") or "").strip(),
        )
        attr_by_key[key].append(row)

    total_by_sku = rows_by_key(total_rows, "marketplace_sku")
    total_by_article = rows_by_key(total_rows, "article")
    pm1_total_by_sku = rows_by_key(pm1_total_rows, "marketplace_sku")
    pm1_order_by_sku = rows_by_key(pm1_order_rows, "marketplace_sku")

    output = []
    distribution = Counter()

    for row in organic_rows:
        sale_date = row.get("sale_date")
        sku = str(row.get("marketplace_sku") or "").strip()
        article = str(row.get("article") or "").strip()
        status = str(row.get("calculation_status") or "").strip()
        warning = str(row.get("warning") or "").strip()
        total_revenue = num(row.get("total_orders_revenue"))
        ad_revenue = num(row.get("ad_orders_revenue"))

        attrs = attr_by_key.get((str(sale_date), sku, article), []) or attr_by_key.get((str(sale_date), sku, ""), [])
        attr_sample = attrs[0] if attrs else {}
        order_sku = str(attr_sample.get("order_sku") or sku).strip()
        promoted_sku = str(attr_sample.get("promoted_sku") or "").strip()
        ad_source = str(attr_sample.get("ad_source") or "").strip() or None
        attribution_type = str(attr_sample.get("attribution_type") or "").strip() or None

        if status == "missing_total" or any(
            token in warning for token in ["ad_attribution_without_total", "ad_revenue_exceed_total", "ad_orders_exceed_total"]
        ):
            if "ad_revenue_exceed_total" in warning:
                reconciliation_status = "ad_revenue_exceed_total"
                unreconciled_revenue = max(ad_revenue - total_revenue, 0)
            elif promoted_sku and promoted_sku != order_sku and total_by_sku.get(promoted_sku) and not total_by_sku.get(order_sku):
                reconciliation_status = "promoted_sku_present_but_order_sku_absent"
                unreconciled_revenue = ad_revenue
            elif promoted_sku and promoted_sku != order_sku:
                reconciliation_status = "order_vs_promoted_sku_mismatch"
                unreconciled_revenue = ad_revenue
            elif (
                pm1_total_by_sku.get(order_sku)
                or pm1_order_by_sku.get(order_sku)
                or (promoted_sku and (pm1_total_by_sku.get(promoted_sku) or pm1_order_by_sku.get(promoted_sku)))
            ):
                reconciliation_status = "possible_date_semantics"
                unreconciled_revenue = ad_revenue
            elif article and total_by_article.get(article):
                reconciliation_status = "possible_union_associated"
                unreconciled_revenue = ad_revenue
            elif status == "missing_total" or "ad_attribution_without_total" in warning:
                reconciliation_status = "missing_total_order_sku_absent"
                unreconciled_revenue = ad_revenue
            else:
                reconciliation_status = "unknown_ad_attribution"
                unreconciled_revenue = ad_revenue
        else:
            reconciliation_status = "clean"
            unreconciled_revenue = 0.0

        evidence = {
            "order_sku_in_total_same_day": bool(total_by_sku.get(order_sku)),
            "promoted_sku_in_total_same_day": bool(promoted_sku and total_by_sku.get(promoted_sku)),
            "article_in_total_same_day": bool(article and total_by_article.get(article)),
            "order_sku_in_total_pm1": bool(pm1_total_by_sku.get(order_sku) or pm1_order_by_sku.get(order_sku)),
            "promoted_sku_in_total_pm1": bool(promoted_sku and (pm1_total_by_sku.get(promoted_sku) or pm1_order_by_sku.get(promoted_sku))),
            "warning": warning or None,
            "calculation_status": status or None,
        }

        distribution[reconciliation_status] += 1
        output.append(
            {
                "sale_date": sale_date,
                "marketplace_code": "ozon",
                "marketplace_sku": sku,
                "article": article or None,
                "product_name": row.get("product_name") or None,
                "order_sku": order_sku or None,
                "promoted_sku": promoted_sku or None,
                "ad_source": ad_source,
                "attribution_type": attribution_type,
                "ad_orders_qty": num(row.get("ad_orders_qty")),
                "ad_orders_revenue": ad_revenue,
                "total_orders_qty": num(row.get("total_orders_qty")),
                "total_orders_revenue": total_revenue,
                "organic_orders_revenue": num(row.get("organic_orders_revenue")),
                "unreconciled_revenue": unreconciled_revenue,
                "reconciliation_status": reconciliation_status,
                "reconciliation_reason": reconciliation_status,
                "evidence": evidence,
                "suggested_fix": (
                    "separate_marketing_attribution"
                    if reconciliation_status in {"order_vs_promoted_sku_mismatch", "promoted_sku_present_but_order_sku_absent", "possible_union_associated"}
                    else "none" if reconciliation_status == "clean" else "review_source_semantics"
                ),
                "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            }
        )

    summary = {
        "rows": len(output),
        "distribution": dict(distribution),
        "unreconciled_revenue": round(sum(num(row.get("unreconciled_revenue")) for row in output), 2),
        "clean_rows": distribution.get("clean", 0),
        "issue_rows": len(output) - distribution.get("clean", 0),
        "unknown_count": distribution.get("unknown_ad_attribution", 0),
        "clean_ad_revenue": round(
            sum(num(row.get("ad_orders_revenue")) for row in output if row.get("reconciliation_status") == "clean"),
            2,
        ),
        "clean_organic_revenue": round(
            sum(num(row.get("organic_orders_revenue")) for row in output if row.get("reconciliation_status") == "clean"),
            2,
        ),
    }
    return output, summary


def save_rows(rows):
    if not rows:
        print("Нет reconciliation rows для записи")
        return
    for i in range(0, len(rows), 500):
        supabase.table(TABLE_NAME).upsert(
            rows[i:i + 500],
            on_conflict="sale_date,marketplace_code,marketplace_sku",
        ).execute()
    print(f"✅ {TABLE_NAME} обновлена: {len(rows)} строк")


def print_sample(rows, limit=20):
    for row in rows[:limit]:
        print(row)


def main():
    args = parse_args()
    date_from, date_to = resolve_date_range(args)
    rows, summary = build_reconciliation_rows(date_from, date_to)
    print("Ozon organic reconciliation summary:")
    print(json.dumps({"date_from": date_from, "date_to": date_to, **summary}, ensure_ascii=False, indent=2))
    if args.debug_sample or args.dry_run:
        print_sample(rows)
    if args.dry_run:
        print("Dry run: ozon_organic_reconciliation_issues не обновлялась")
        return
    save_rows(rows)


if __name__ == "__main__":
    main()
