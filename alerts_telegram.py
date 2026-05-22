import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")


def now_local():
    return datetime.now(ZoneInfo(APP_TIMEZONE))


def today_local():
    return now_local().date()


supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    response = requests.post(url, json=payload, timeout=30)
    print(response.status_code)
    print(response.text)


def num(value):
    return float(value or 0)


def fmt_money(value):
    return f"{num(value):,.0f}".replace(",", " ")


def fmt_pct(value):
    if value is None:
        return "н/д"
    return f"{value:.0%}"


def pct_change(current, base):
    current = num(current)
    base = num(base)
    if base == 0:
        return None
    return (current - base) / base


def avg(values):
    values = [num(v) for v in values]
    if not values:
        return 0
    return sum(values) / len(values)


def get_kpi_rows(days_back=30):
    date_from = (today_local() - timedelta(days=days_back)).isoformat()

    result = (
        supabase
        .table("daily_marketplace_kpi")
        .select("*")
        .gte("kpi_date", date_from)
        .order("kpi_date")
        .execute()
    )

    return result.data or []


def table_has_rows(table_name, filters):
    query = supabase.table(table_name).select("*").limit(1)

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

    result = query.execute()
    return bool(result.data or [])


def get_ozon_report_completeness(target_date):
    daily_marketplace_kpi_present = table_has_rows(
        "daily_marketplace_kpi",
        [
            ("marketplace_code", "eq", "ozon"),
            ("kpi_date", "eq", target_date),
        ],
    )
    daily_sku_kpi_present = table_has_rows(
        "daily_sku_kpi",
        [
            ("marketplace_code", "eq", "ozon"),
            ("kpi_date", "eq", target_date),
        ],
    )
    organic_present = table_has_rows(
        "ozon_daily_sku_organic",
        [
            ("marketplace_code", "eq", "ozon"),
            ("sale_date", "eq", target_date),
        ],
    )
    ads_in_expenses = table_has_rows(
        "marketplace_expenses",
        [
            ("marketplace_code", "eq", "ozon"),
            ("expense_date", "eq", target_date),
            (
                "in",
                "expense_type",
                [
                    "advertising_clicks",
                    "advertising_other",
                    "advertising_order_10",
                    "advertising_order_5",
                    "advertising_order_other",
                    "advertising_order_unknown",
                    "advertising_order_selected_cpo",
                ],
            ),
        ],
    )
    ads_in_attribution = table_has_rows(
        "ozon_daily_sku_ad_attribution",
        [
            ("marketplace_code", "eq", "ozon"),
            ("sale_date", "eq", target_date),
        ],
    )
    ads_present = ads_in_expenses or ads_in_attribution

    blockers = []
    if not daily_marketplace_kpi_present:
        blockers.append("daily_marketplace_kpi_missing")
    if not daily_sku_kpi_present:
        blockers.append("daily_sku_kpi_missing")
    if not organic_present:
        blockers.append("ozon_daily_sku_organic_missing")
    if not ads_present:
        blockers.append("ozon_ads_layer_missing")

    return {
        "complete": not blockers,
        "blockers": blockers,
        "daily_marketplace_kpi_present": daily_marketplace_kpi_present,
        "daily_sku_kpi_present": daily_sku_kpi_present,
        "organic_present": organic_present,
        "ads_present": ads_present,
    }


def get_ozon_ads_breakdown(expense_date):
    result = (
        supabase
        .table("marketplace_expenses")
        .select("expense_type,expense_amount")
        .eq("marketplace_code", "ozon")
        .eq("expense_date", expense_date)
        .execute()
    )

    grouped = {
        "advertising_clicks": 0,
        "advertising_order_10": 0,
        "advertising_order_5": 0,
        "advertising_order_other": 0,
        "advertising_order_unknown": 0,
        "advertising_other": 0,
    }

    for row in result.data or []:
        expense_type = str(row.get("expense_type") or "")
        if not expense_type.startswith("advertising"):
            continue
        grouped[expense_type] = grouped.get(expense_type, 0) + num(row.get("expense_amount"))

    return grouped


def safe_ratio(numerator, denominator):
    numerator = num(numerator)
    denominator = num(denominator)
    if denominator <= 0:
        return None
    return numerator / denominator


def parse_warning_tokens(value):
    if not value:
        return set()
    return {token.strip() for token in str(value).split(",") if token and token.strip()}


def get_ozon_organic_reconciliation(sale_date):
    try:
        result = (
            supabase
            .table("ozon_daily_sku_organic")
            .select(
                "marketplace_sku,total_orders_revenue,ad_orders_revenue,"
                "organic_orders_revenue,calculation_status,warning"
            )
            .eq("marketplace_code", "ozon")
            .eq("sale_date", sale_date)
            .execute()
        )
    except Exception as e:
        print(f"Не удалось загрузить Ozon organic reconciliation: {e}")
        return {
            "available": False,
            "organic_rows_count": 0,
            "ok_count": 0,
            "missing_total_count": 0,
            "warning_count": 0,
            "problem_rows_count": 0,
            "ad_attribution_without_total_count": 0,
            "ad_revenue_exceed_total_count": 0,
            "ad_orders_exceed_total_count": 0,
            "unreconciled_revenue": 0.0,
            "reconciled_total_revenue": 0.0,
            "reconciled_ad_revenue": 0.0,
            "reconciled_organic_revenue": 0.0,
            "reconciled_ad_share_revenue": None,
            "raw_total_revenue": 0.0,
            "raw_ad_revenue": 0.0,
            "raw_organic_revenue": 0.0,
        }

    rows = result.data or []
    if not rows:
        return {
            "available": False,
            "organic_rows_count": 0,
            "ok_count": 0,
            "missing_total_count": 0,
            "warning_count": 0,
            "problem_rows_count": 0,
            "ad_attribution_without_total_count": 0,
            "ad_revenue_exceed_total_count": 0,
            "ad_orders_exceed_total_count": 0,
            "unreconciled_revenue": 0.0,
            "reconciled_total_revenue": 0.0,
            "reconciled_ad_revenue": 0.0,
            "reconciled_organic_revenue": 0.0,
            "reconciled_ad_share_revenue": None,
            "raw_total_revenue": 0.0,
            "raw_ad_revenue": 0.0,
            "raw_organic_revenue": 0.0,
        }

    organic_rows_count = len(rows)
    ok_count = 0
    missing_total_count = 0
    warning_count = 0
    problem_rows_count = 0
    ad_attribution_without_total_count = 0
    ad_revenue_exceed_total_count = 0
    ad_orders_exceed_total_count = 0

    raw_total_revenue = 0.0
    raw_ad_revenue = 0.0
    raw_organic_revenue = 0.0

    unreconciled_revenue = 0.0
    reconciled_total_revenue = 0.0
    reconciled_ad_revenue = 0.0
    reconciled_organic_revenue = 0.0

    for row in rows:
        total_revenue = num(row.get("total_orders_revenue"))
        ad_revenue = num(row.get("ad_orders_revenue"))
        organic_revenue = num(row.get("organic_orders_revenue"))
        calculation_status = row.get("calculation_status") or ""
        warning_tokens = parse_warning_tokens(row.get("warning"))

        raw_total_revenue += total_revenue
        raw_ad_revenue += ad_revenue
        raw_organic_revenue += organic_revenue

        if calculation_status == "ok":
            ok_count += 1
        if calculation_status == "missing_total":
            missing_total_count += 1
        if warning_tokens:
            warning_count += 1
            problem_rows_count += 1

        if "ad_attribution_without_total" in warning_tokens:
            ad_attribution_without_total_count += 1
            unreconciled_revenue += ad_revenue

        has_ad_revenue_exceed_total = "ad_revenue_exceed_total" in warning_tokens
        if has_ad_revenue_exceed_total:
            ad_revenue_exceed_total_count += 1
            if ad_revenue > total_revenue:
                unreconciled_revenue += ad_revenue - total_revenue

        if "ad_orders_exceed_total" in warning_tokens:
            ad_orders_exceed_total_count += 1

        exclude_from_reconciled = (
            calculation_status == "missing_total"
            or "ad_attribution_without_total" in warning_tokens
            or has_ad_revenue_exceed_total
        )
        if not exclude_from_reconciled:
            reconciled_total_revenue += total_revenue
            reconciled_ad_revenue += ad_revenue
            reconciled_organic_revenue += organic_revenue

    return {
        "available": True,
        "organic_rows_count": organic_rows_count,
        "ok_count": ok_count,
        "missing_total_count": missing_total_count,
        "warning_count": warning_count,
        "problem_rows_count": problem_rows_count,
        "ad_attribution_without_total_count": ad_attribution_without_total_count,
        "ad_revenue_exceed_total_count": ad_revenue_exceed_total_count,
        "ad_orders_exceed_total_count": ad_orders_exceed_total_count,
        "unreconciled_revenue": round(unreconciled_revenue, 2),
        "reconciled_total_revenue": round(reconciled_total_revenue, 2),
        "reconciled_ad_revenue": round(reconciled_ad_revenue, 2),
        "reconciled_organic_revenue": round(reconciled_organic_revenue, 2),
        "reconciled_ad_share_revenue": safe_ratio(reconciled_ad_revenue, reconciled_total_revenue),
        "raw_total_revenue": round(raw_total_revenue, 2),
        "raw_ad_revenue": round(raw_ad_revenue, 2),
        "raw_organic_revenue": round(raw_organic_revenue, 2),
    }


def format_ozon_attribution_summary(raw_ad_revenue, raw_organic_revenue, fallback_ad_share_revenue, reconciliation):
    if not reconciliation.get("available"):
        return "Атрибуция Ozon: данные organic attribution недоступны."

    warning_count = int(reconciliation.get("warning_count") or 0)
    problem_rows_count = int(reconciliation.get("problem_rows_count") or 0)
    missing_total_count = int(reconciliation.get("missing_total_count") or 0)
    unreconciled_revenue = num(reconciliation.get("unreconciled_revenue"))
    reconciled_share = reconciliation.get("reconciled_ad_share_revenue")

    if warning_count <= 0:
        ad_share_value = reconciled_share if reconciled_share is not None else fallback_ad_share_revenue
        return (
            f"Атрибуция: реклама {fmt_money(raw_ad_revenue)} руб., "
            f"органика {fmt_money(raw_organic_revenue)} руб., "
            f"ad share {fmt_pct(ad_share_value)}."
        )

    return (
        f"Атрибуция Ozon: реклама {fmt_money(raw_ad_revenue)} руб., "
        f"органика {fmt_money(raw_organic_revenue)} руб.\n"
        f"⚠️ Атрибуция частичная: {problem_rows_count} SKU с предупреждениями, "
        f"missing total {missing_total_count}, "
        f"неразнесено/расхождение {fmt_money(unreconciled_revenue)} руб.\n"
        f"Сверенная доля рекламы по сопоставленным SKU: {fmt_pct(reconciled_share)}."
    )


def save_today_snapshot(kpi_rows):
    """
    Сохраняем утренний срез текущих доступных данных.
    Берем по каждому маркетплейсу самую свежую дату из daily_marketplace_kpi.
    """
    now = now_local()
    snapshot_date = today_local().isoformat()
    snapshot_hour = now.hour

    latest_by_mp = {}

    for row in kpi_rows:
        mp = row.get("marketplace_code")
        kpi_date = row.get("kpi_date")

        if not mp or not kpi_date:
            continue

        # Для intraday не берем будущие даты данных.
        if kpi_date > snapshot_date:
            continue

        if mp not in latest_by_mp or kpi_date > latest_by_mp[mp].get("kpi_date"):
            latest_by_mp[mp] = row

    rows = []

    for mp, row in latest_by_mp.items():
        rows.append({
            "snapshot_date": snapshot_date,
            "snapshot_hour": snapshot_hour,
            "marketplace_code": mp,
            "data_date": row.get("kpi_date"),
            "orders_qty": num(row.get("orders_qty")),
            "orders_amount_seller": num(row.get("orders_amount_seller")),
            "buyouts_qty": num(row.get("buyouts_qty")),
            "buyouts_amount_seller": num(row.get("buyouts_amount_seller")),
        })

    if not rows:
        print("Нет данных для intraday snapshot")
        return []

    supabase.table("intraday_snapshots").upsert(
        rows,
        on_conflict="snapshot_date,snapshot_hour,marketplace_code,data_date"
    ).execute()

    print(f"✅ intraday snapshot записан: {len(rows)} строк")
    return rows


def get_yesterday_same_hour_snapshots(current_snapshots):
    """
    Ищем вчерашний срез за тот же час и ту же дату данных - 1 день.
    """
    if not current_snapshots:
        return {}

    snapshot_date_yesterday = (today_local() - timedelta(days=1)).isoformat()
    snapshot_hour = current_snapshots[0]["snapshot_hour"]

    result = (
        supabase
        .table("intraday_snapshots")
        .select("*")
        .eq("snapshot_date", snapshot_date_yesterday)
        .eq("snapshot_hour", snapshot_hour)
        .execute()
    )

    rows = result.data or []
    indexed = {}

    for row in rows:
        key = (
            row.get("marketplace_code"),
            row.get("data_date"),
        )
        indexed[key] = row

    return indexed


def build_completed_day_alerts(kpi_rows):
    """
    Анализируем вчерашний полный день против предыдущих 7 полных дней.
    """
    alerts = []

    target_date = (today_local() - timedelta(days=1)).isoformat()
    previous_from = (today_local() - timedelta(days=8)).isoformat()
    ozon_completeness = get_ozon_report_completeness(target_date)

    for mp in ["wb", "ozon"]:
        if mp == "ozon" and not ozon_completeness.get("complete"):
            blockers = ", ".join(ozon_completeness.get("blockers") or []) or "unknown"
            alerts.append(
                f"⚠️ <b>Ozon</b>\n"
                f"{target_date}: данные неполные, полный дневной вывод пропущен.\n"
                f"Причины: {blockers}."
            )
            continue

        mp_rows = [
            r for r in kpi_rows
            if r.get("marketplace_code") == mp
            and previous_from <= r.get("kpi_date") <= target_date
        ]

        mp_rows = sorted(mp_rows, key=lambda x: x.get("kpi_date"))

        target_rows = [r for r in mp_rows if r.get("kpi_date") == target_date]
        prev_rows = [r for r in mp_rows if r.get("kpi_date") < target_date]

        if not target_rows or not prev_rows:
            continue

        today = target_rows[0]

        orders = num(today.get("orders_qty"))
        buyouts = num(today.get("buyouts_qty"))

        avg_orders_7d = avg([r.get("orders_qty") for r in prev_rows])
        avg_buyouts_7d = avg([r.get("buyouts_qty") for r in prev_rows])

        orders_delta = pct_change(orders, avg_orders_7d)
        buyouts_delta = pct_change(buyouts, avg_buyouts_7d)

        mp_name = "WB" if mp == "wb" else "Ozon"

        if orders_delta is not None and orders_delta <= -0.25:
            alerts.append(
                f"🟠 <b>{mp_name}: вчера падение заказов</b>\n"
                f"{target_date}: заказов {orders:.0f}, среднее 7 дней {avg_orders_7d:.0f}, отклонение {fmt_pct(orders_delta)}."
            )

        if buyouts_delta is not None and buyouts_delta <= -0.25:
            alerts.append(
                f"🟠 <b>{mp_name}: вчера падение выкупов</b>\n"
                f"{target_date}: выкупов {buyouts:.0f}, среднее 7 дней {avg_buyouts_7d:.0f}, отклонение {fmt_pct(buyouts_delta)}."
            )

    return alerts


def build_intraday_alerts(current_snapshots):
    """
    Сегодня на текущий час против вчера на этот же час.
    Первый день после внедрения сравнения может не быть.
    """
    alerts = []

    yesterday_index = get_yesterday_same_hour_snapshots(current_snapshots)

    for row in current_snapshots:
        mp = row.get("marketplace_code")
        data_date = row.get("data_date")
        snapshot_hour = row.get("snapshot_hour")

        try:
            prev_data_date = (datetime.fromisoformat(data_date).date() - timedelta(days=1)).isoformat()
        except Exception:
            continue

        prev = yesterday_index.get((mp, prev_data_date))

        mp_name = "WB" if mp == "wb" else "Ozon"

        if not prev:
            alerts.append(
                f"ℹ️ <b>{mp_name}: срез на {snapshot_hour}:00 сохранен</b>\n"
                f"{data_date}: заказов {num(row.get('orders_qty')):.0f}, сумма {fmt_money(row.get('orders_amount_seller'))}.\n"
                f"Сравнение с вчера на {snapshot_hour}:00 появится после накопления вчерашнего среза."
            )
            continue

        orders_now = num(row.get("orders_qty"))
        orders_prev = num(prev.get("orders_qty"))

        amount_now = num(row.get("orders_amount_seller"))
        amount_prev = num(prev.get("orders_amount_seller"))

        buyouts_now = num(row.get("buyouts_qty"))
        buyouts_prev = num(prev.get("buyouts_qty"))

        orders_delta = pct_change(orders_now, orders_prev)
        amount_delta = pct_change(amount_now, amount_prev)
        buyouts_delta = pct_change(buyouts_now, buyouts_prev)

        if orders_delta is not None and orders_delta <= -0.25:
            alerts.append(
                f"🟡 <b>{mp_name}: сегодня на {snapshot_hour}:00 заказы ниже вчера</b>\n"
                f"{data_date}: {orders_now:.0f} заказов против {orders_prev:.0f} вчера на это же время, отклонение {fmt_pct(orders_delta)}.\n"
                f"Сумма заказов: {fmt_money(amount_now)} против {fmt_money(amount_prev)}, отклонение {fmt_pct(amount_delta)}."
            )
        else:
            alerts.append(
                f"✅ <b>{mp_name}: сегодня на {snapshot_hour}:00 без критичного падения</b>\n"
                f"{data_date}: {orders_now:.0f} заказов против {orders_prev:.0f} вчера на это же время, отклонение {fmt_pct(orders_delta)}.\n"
                f"Сумма заказов: {fmt_money(amount_now)} против {fmt_money(amount_prev)}, отклонение {fmt_pct(amount_delta)}.\n"
                f"Выкупы: {buyouts_now:.0f} против {buyouts_prev:.0f}, отклонение {fmt_pct(buyouts_delta)}."
            )

    return alerts




def overlay_wb_orders_from_sales_funnel(kpi_rows):
    """
    Подменяет WB-заказы в KPI на данные WB Sales Funnel.
    Выкупы не трогаем: они уже совпадают с ЛК.
    """
    try:
        result = (
            supabase
            .table("marketplace_orders_analytics")
            .select("order_date,orders_qty,orders_amount,source")
            .eq("marketplace_code", "wb")
            .eq("source", "wb_sales_funnel")
            .order("order_date", desc=True)
            .limit(60)
            .execute()
        )

        analytics_rows = result.data or []
    except Exception as e:
        print(f"Не удалось загрузить WB Sales Funnel orders для overlay: {e}")
        return kpi_rows

    analytics_by_date = {
        str(r.get("order_date")): r
        for r in analytics_rows
    }

    for row in kpi_rows or []:
        if row.get("marketplace_code") != "wb":
            continue

        kpi_date = str(row.get("kpi_date"))

        if kpi_date in analytics_by_date:
            analytics = analytics_by_date[kpi_date]

            row["orders_qty"] = float(analytics.get("orders_qty") or 0)
            row["orders_amount_seller"] = float(analytics.get("orders_amount") or 0)
            row["orders_amount_buyer"] = float(analytics.get("orders_amount") or 0)
            row["orders_source"] = "wb_sales_funnel"

    return kpi_rows


def build_executive_summary(kpi_rows):
    """
    Короткая управленческая сводка по вчерашнему полному дню.
    """
    target_date = (today_local() - timedelta(days=1)).isoformat()
    previous_from = (today_local() - timedelta(days=8)).isoformat()
    ozon_completeness = get_ozon_report_completeness(target_date)

    lines = [
        "<b>0. Короткая управленческая сводка</b>"
    ]

    risks = []
    actions = []

    for mp in ["wb", "ozon"]:
        mp_name = "WB" if mp == "wb" else "Ozon"

        mp_rows = [
            r for r in kpi_rows
            if r.get("marketplace_code") == mp
            and previous_from <= r.get("kpi_date") <= target_date
        ]

        mp_rows = sorted(mp_rows, key=lambda x: x.get("kpi_date"))

        target_rows = [r for r in mp_rows if r.get("kpi_date") == target_date]
        prev_rows = [r for r in mp_rows if r.get("kpi_date") < target_date]

        if not target_rows:
            lines.append(f"ℹ️ <b>{mp_name}</b>: нет данных за {target_date}.")
            continue

        row = target_rows[0]

        orders = num(row.get("orders_qty"))
        orders_amount = num(row.get("orders_amount_seller"))
        buyouts = num(row.get("buyouts_qty"))
        buyouts_amount = num(row.get("buyouts_amount_seller"))
        commission = num(row.get("commission_amount"))
        logistics = num(row.get("logistics_amount"))
        other = num(row.get("other_expenses_amount"))
        ad_spend = num(row.get("ad_spend"))
        ad_orders_revenue = num(row.get("ad_orders_revenue"))
        organic_orders_revenue = num(row.get("organic_orders_revenue"))
        ad_share_revenue = row.get("ad_share_revenue")

        total_expenses = commission + logistics + other + ad_spend
        net_after_expenses = buyouts_amount - total_expenses

        avg_orders_7d = avg([r.get("orders_qty") for r in prev_rows]) if prev_rows else 0
        avg_buyouts_7d = avg([r.get("buyouts_qty") for r in prev_rows]) if prev_rows else 0

        orders_delta = pct_change(orders, avg_orders_7d)
        buyouts_delta = pct_change(buyouts, avg_buyouts_7d)

        if mp == "wb":
            lines.append(
                f"🟣 <b>WB вчера</b>\n"
                f"Заказы: {orders:.0f} / {fmt_money(orders_amount)} руб.\n"
                f"Выкупы: {buyouts:.0f} / {fmt_money(buyouts_amount)} руб.\n"
                f"Отклонение заказов к 7дн: {fmt_pct(orders_delta)}."
            )
        else:
            if not ozon_completeness.get("complete"):
                blockers = ", ".join(ozon_completeness.get("blockers") or []) or "unknown"
                lines.append(
                    f"🔵 <b>Ozon вчера</b>\n"
                    f"⚠️ Ozon вчера: данные неполные, управленческий вывод не строим.\n"
                    f"Причины: {blockers}."
                )
                continue

            ads = get_ozon_ads_breakdown(target_date)
            organic_reconciliation = get_ozon_organic_reconciliation(target_date)
            ads_unknown = (
                ads["advertising_order_other"]
                + ads["advertising_order_unknown"]
                + ads["advertising_other"]
            )
            ads_line = (
                f"Реклама: клики {fmt_money(ads['advertising_clicks'])}, "
                f"заказ 10% {fmt_money(ads['advertising_order_10'])}, "
                f"заказ 5% {fmt_money(ads['advertising_order_5'])}"
            )
            if ads_unknown > 0:
                ads_line += f", не распознано/прочее {fmt_money(ads_unknown)}"
            ads_line += "."
            attribution_line = format_ozon_attribution_summary(
                ad_orders_revenue,
                organic_orders_revenue,
                ad_share_revenue,
                organic_reconciliation,
            )

            lines.append(
                f"🔵 <b>Ozon вчера</b>\n"
                f"Заказы: {orders:.0f} / {fmt_money(orders_amount)} руб.\n"
                f"Реализация: {buyouts:.0f} шт / {fmt_money(buyouts_amount)} руб.\n"
                f"{attribution_line}\n"
                f"{ads_line}\n"
                f"Комиссии: {fmt_money(commission)}, логистика: {fmt_money(logistics)}, прочие: {fmt_money(other)}.\n"
                f"После расходов Ozon: {fmt_money(net_after_expenses)} руб.\n"
                f"Отклонение заказов к 7дн: {fmt_pct(orders_delta)}."
            )

        if orders_delta is not None and orders_delta <= -0.25:
            risks.append(f"{mp_name}: заказы вчера ниже среднего 7 дней на {fmt_pct(orders_delta)}")
            actions.append(f"Проверить {mp_name}: остатки, рекламу, акции, цены и видимость топ-SKU.")

        if buyouts_delta is not None and buyouts_delta <= -0.25:
            risks.append(f"{mp_name}: выкупы вчера ниже среднего 7 дней на {fmt_pct(buyouts_delta)}")
            actions.append(f"Проверить {mp_name}: причины снижения выкупов, отмены, возвраты и проблемные SKU.")

        if mp == "ozon" and buyouts_amount > 0:
            expense_share = total_expenses / buyouts_amount
            if expense_share >= 0.45:
                risks.append(f"Ozon: расходы составили {fmt_pct(expense_share)} от реализации")
                actions.append("Проверить Ozon: комиссии, эквайринг, логистику и рекламные списания.")

    lines.append("")
    if risks:
        lines.append("⚠️ <b>Главный риск</b>\n" + risks[0])
    else:
        lines.append("✅ <b>Главный риск</b>\nКритичного риска по вчерашнему дню не выявлено.")

    if actions:
        unique_actions = []
        for action in actions:
            if action not in unique_actions:
                unique_actions.append(action)

        lines.append("")
        lines.append("🎯 <b>Что проверить сегодня</b>\n" + "\n".join([f"— {a}" for a in unique_actions[:3]]))
    else:
        lines.append("")
        lines.append("🎯 <b>Что проверить сегодня</b>\n— Контроль топ-SKU по остаткам, рекламе и просадкам заказов.")

    return lines


def build_short_snapshot(intraday_rows):
    """
    Короткий executive snapshot:
    сегодня на текущий час vs вчера на тот же час.

    Важно:
    Не сравниваем 9:00 с 22:00 — это искажает вывод.
    Если вчерашнего среза на тот же час нет, пишем, что сопоставимого среза нет.
    """
    if not intraday_rows:
        return ["ℹ️ Короткий срез: данных пока нет."]

    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    moscow_now = datetime.now(ZoneInfo("Europe/Moscow"))
    today = moscow_now.date().isoformat()
    yesterday = (moscow_now.date() - timedelta(days=1)).isoformat()

    today_rows = [
        r for r in intraday_rows
        if str(r.get("snapshot_date")) == today
    ]

    if not today_rows:
        return ["ℹ️ Короткий срез: сегодняшнего среза пока нет."]

    latest_hour = max(int(r.get("snapshot_hour") or 0) for r in today_rows)

    def find_row(marketplace, day, hour):
        candidates = [
            r for r in intraday_rows
            if r.get("marketplace_code") == marketplace
            and str(r.get("snapshot_date")) == day
            and int(r.get("snapshot_hour") or 0) == hour
            and str(r.get("data_date")) == day
        ]
        return candidates[0] if candidates else None

    def fmt_money(value):
        return f"{float(value or 0):,.0f}".replace(",", " ")

    def marketplace_lines(title, marketplace):
        today_row = find_row(marketplace, today, latest_hour)
        yesterday_row = find_row(marketplace, yesterday, latest_hour)

        if not today_row:
            return [f"ℹ️ {title}: данных за сегодня на {latest_hour}:00 нет."]

        today_qty = float(today_row.get("orders_qty") or 0)
        today_amount = float(today_row.get("orders_amount") or today_row.get("orders_amount_seller") or 0)

        if yesterday_row:
            y_qty = float(yesterday_row.get("orders_qty") or 0)
            y_amount = float(yesterday_row.get("orders_amount") or yesterday_row.get("orders_amount_seller") or 0)

            qty_delta = ((today_qty / y_qty - 1) * 100) if y_qty else 0
            amount_delta = ((today_amount / y_amount - 1) * 100) if y_amount else 0

            sign_qty = "+" if qty_delta > 0 else ""
            sign_amount = "+" if amount_delta > 0 else ""

            return [
                f"<b>{title}</b>",
                f"Сегодня на {latest_hour}:00: {today_qty:.0f} заказов / {fmt_money(today_amount)} ₽",
                f"Вчера на {latest_hour}:00: {y_qty:.0f} заказов / {fmt_money(y_amount)} ₽",
                f"Отклонение: {sign_qty}{qty_delta:.0f}% по заказам / {sign_amount}{amount_delta:.0f}% по сумме",
            ]

        return [
            f"<b>{title}</b>",
            f"Сегодня на {latest_hour}:00: {today_qty:.0f} заказов / {fmt_money(today_amount)} ₽",
            f"Вчерашнего сопоставимого среза на {latest_hour}:00 пока нет.",
            "Сравнение появится после накопления среза за этот же час вчера.",
        ]

    lines = [
        "<b>2. Сегодня на текущий час против вчера на этот же час</b>",
        "",
    ]

    lines.extend(marketplace_lines("WB", "wb"))
    lines.append("")
    lines.extend(marketplace_lines("Ozon", "ozon"))

    return lines



def build_message():
    kpi_rows = get_kpi_rows(days_back=30)
    kpi_rows = overlay_wb_orders_from_sales_funnel(kpi_rows)
    current_snapshots = save_today_snapshot(kpi_rows)

    completed_day_alerts = build_completed_day_alerts(kpi_rows)
    intraday_alerts = build_intraday_alerts(current_snapshots)

    lines = [
        "📊 <b>MP Analytics Alerts</b>",
        f"Дата: {today_local().isoformat()}",
        "",
    ]

    lines.extend(build_executive_summary(kpi_rows))

    lines.extend([
        "",
        "<b>1. Полный вчерашний день</b>",
    ])

    if completed_day_alerts:
        lines.extend(completed_day_alerts)
    else:
        lines.append("✅ Критичных отклонений по полному вчерашнему дню нет.")

    intraday_rows = (
        supabase
        .table("intraday_snapshots")
        .select("*")
        .order("snapshot_date", desc=True)
        .order("snapshot_hour", desc=True)
        .limit(100)
        .execute()
        .data
        or []
    )

    lines.append("")
    lines.extend(build_short_snapshot(intraday_rows))

    return "\n\n".join(lines)


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("Не заполнен TELEGRAM_BOT_TOKEN")

    if not TELEGRAM_CHAT_ID:
        raise ValueError("Не заполнен TELEGRAM_CHAT_ID")

    message = build_message()
    send_telegram(message)
