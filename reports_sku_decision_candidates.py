import argparse
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client


load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")

MIN_BUYOUT_RATE = float(os.getenv("SKU_DECISION_MIN_BUYOUT_RATE", "0.65"))
TARGET_ROAS = float(os.getenv("SKU_DECISION_TARGET_ROAS", "3.0"))
HIGH_AD_SHARE = float(os.getenv("SKU_DECISION_HIGH_AD_SHARE_REVENUE", "0.35"))
MIN_STOCK_QTY = float(os.getenv("SKU_DECISION_MIN_STOCK_QTY", "3"))
HIGH_STOCK_QTY = float(os.getenv("SKU_DECISION_HIGH_STOCK_QTY", "10"))
MIN_ORDERS_FOR_ACTION = float(os.getenv("SKU_DECISION_MIN_ORDERS_FOR_ACTION", "3"))
MAX_PRICE_STEP = float(os.getenv("SKU_DECISION_MAX_PRICE_STEP", "0.05"))

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def parse_args():
    parser = argparse.ArgumentParser(description="Generate dry-run SKU decision recommendations.")
    parser.add_argument("--date", help="single-day shortcut, sets both --date-from and --date-to")
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--days-back", type=int, default=1)
    parser.add_argument("--limit", type=int, default=100)
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


def fetch_all(filters):
    rows = []
    start = 0
    page_size = 1000

    while True:
        query = supabase.table("sku_decision_daily_input").select("*")
        for field, operator, value in filters:
            if operator == "eq":
                query = query.eq(field, value)
            elif operator == "gte":
                query = query.gte(field, value)
            elif operator == "lte":
                query = query.lte(field, value)
        try:
            result = query.range(start, start + page_size - 1).execute()
        except Exception as exc:
            print(f"WARNING: Не удалось загрузить sku_decision_daily_input: {exc}")
            return []
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


def evaluate_row(row):
    buyout_rate = num(row.get("buyout_rate_rolling_14d")) or num(row.get("buyout_rate_rolling_30d"))
    ad_spend = num(row.get("ad_spend"))
    ad_revenue = num(row.get("ad_attributed_revenue"))
    expected_margin = row.get("expected_margin_after_ads")
    expected_margin = None if expected_margin is None else num(expected_margin)
    orders_qty = num(row.get("orders_qty"))
    stock_qty = row.get("stock_qty")
    stock_qty = None if stock_qty is None else num(stock_qty)
    ad_share = num(row.get("ad_share_revenue"))
    data_quality = str(row.get("data_quality_status") or "")

    roas = None
    if ad_spend > 0:
        roas = ad_revenue / ad_spend

    risk_flags = []
    if data_quality and data_quality != "ok":
        risk_flags.append(data_quality)
    if stock_qty is not None and stock_qty < MIN_STOCK_QTY:
        risk_flags.append("stock_risk")
    if orders_qty < MIN_ORDERS_FOR_ACTION:
        risk_flags.append("low_volume")

    if data_quality and data_quality != "ok":
        return "hold", "Неполные или проблемные данные для уверенного решения", roas, risk_flags

    if stock_qty is not None and stock_qty < MIN_STOCK_QTY:
        return "watch", "Остатков мало, лучше не усиливать рекламу и не дёргать цену", roas, risk_flags

    if ad_spend > 0 and expected_margin is not None and expected_margin <= 0:
        return "stop_ads", "Ожидаемая маржа после рекламы неположительная", roas, risk_flags

    if ad_spend > 0 and buyout_rate < MIN_BUYOUT_RATE:
        return "decrease_ads", "Низкий процент выкупа на фоне рекламных затрат", roas, risk_flags

    if (
        ad_spend > 0
        and roas is not None
        and roas >= TARGET_ROAS
        and expected_margin is not None
        and expected_margin > 0
        and buyout_rate >= MIN_BUYOUT_RATE
        and (stock_qty is None or stock_qty >= MIN_STOCK_QTY)
    ):
        return "increase_ads", "Реклама окупается, выкуп нормальный, запас по остатку есть", roas, risk_flags

    if (
        orders_qty >= MIN_ORDERS_FOR_ACTION
        and buyout_rate >= MIN_BUYOUT_RATE
        and ad_share >= HIGH_AD_SHARE
        and expected_margin is not None
        and expected_margin > 0
        and (stock_qty is None or stock_qty >= MIN_STOCK_QTY)
    ):
        return "increase_price", f"Есть спрос, рекламная доля высокая; шаг цены держим в пределах {MAX_PRICE_STEP:.0%}", roas, risk_flags

    if (
        orders_qty < MIN_ORDERS_FOR_ACTION
        and (stock_qty is None or stock_qty >= HIGH_STOCK_QTY)
        and (ad_spend <= 0 or (roas is not None and roas < TARGET_ROAS))
        and expected_margin is not None
        and expected_margin > 0
    ):
        return "decrease_price", "Спрос низкий при достаточном остатке, можно проверить мягкое снижение цены", roas, risk_flags

    return "watch", "Пока лучше наблюдать без резких действий", roas, risk_flags


def build_candidates(date_from, date_to, limit=100):
    rows = fetch_all(
        [
            ("marketplace_code", "eq", "ozon"),
            ("kpi_date", "gte", date_from),
            ("kpi_date", "lte", date_to),
        ]
    )

    candidates = []
    for row in rows:
        action, reason, roas, risk_flags = evaluate_row(row)
        candidates.append(
            {
                "kpi_date": row.get("kpi_date"),
                "marketplace_sku": row.get("marketplace_sku"),
                "article": row.get("article"),
                "product_name": row.get("product_name"),
                "recommended_action": action,
                "reason": reason,
                "orders_qty": num(row.get("orders_qty")),
                "orders_revenue": num(row.get("orders_revenue")),
                "buyout_rate_rolling_14d": row.get("buyout_rate_rolling_14d"),
                "buyout_rate_rolling_30d": row.get("buyout_rate_rolling_30d"),
                "ad_spend": num(row.get("ad_spend")),
                "ad_attributed_revenue": num(row.get("ad_attributed_revenue")),
                "organic_revenue": num(row.get("organic_revenue")),
                "ad_share_revenue": row.get("ad_share_revenue"),
                "expected_revenue_after_buyout": row.get("expected_revenue_after_buyout"),
                "expected_margin_after_ads": row.get("expected_margin_after_ads"),
                "stock_qty": row.get("stock_qty"),
                "data_quality_status": row.get("data_quality_status"),
                "risk_flags": ",".join(risk_flags),
                "roas": round(roas, 2) if roas is not None else None,
            }
        )

    candidates.sort(
        key=lambda row: (
            0 if row["recommended_action"] not in {"hold", "watch"} else 1,
            -num(row.get("ad_spend")),
            -num(row.get("orders_revenue")),
        )
    )
    return candidates[:limit]


def main():
    args = parse_args()
    date_from, date_to = resolve_date_range(args)
    candidates = build_candidates(date_from, date_to, limit=max(1, int(args.limit or 100)))

    print(
        {
            "date_from": date_from,
            "date_to": date_to,
            "candidate_count": len(candidates),
        }
    )
    for row in candidates[: 20 if args.debug_sample else len(candidates)]:
        print(row)


if __name__ == "__main__":
    main()
