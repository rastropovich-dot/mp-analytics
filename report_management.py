import os
from datetime import date, timedelta
from dotenv import load_dotenv
from supabase import create_client
from openai import OpenAI

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
client = OpenAI(api_key=OPENAI_API_KEY)


def load_wb_daily(days_back=14):
    """
    WB daily:
    - заказы берем из marketplace_orders_analytics / wb_sales_funnel,
      потому что это ближе к ЛК WB.
    - выкупы/продажи оставляем из daily_marketplace_kpi.
    """
    kpi_result = (
        supabase
        .table("daily_marketplace_kpi")
        .select("*")
        .eq("marketplace_code", "wb")
        .order("kpi_date", desc=True)
        .limit(days_back + 5)
        .execute()
    )

    rows = kpi_result.data or []

    analytics_result = (
        supabase
        .table("marketplace_orders_analytics")
        .select("order_date,orders_qty,orders_amount,source")
        .eq("marketplace_code", "wb")
        .eq("source", "wb_sales_funnel")
        .order("order_date", desc=True)
        .limit(days_back + 5)
        .execute()
    )

    analytics_rows = analytics_result.data or []
    analytics_by_date = {
        str(r.get("order_date")): r
        for r in analytics_rows
    }

    for row in rows:
        kpi_date = str(row.get("kpi_date"))

        if kpi_date in analytics_by_date:
            analytics = analytics_by_date[kpi_date]

            row["orders_qty"] = float(analytics.get("orders_qty") or 0)
            row["orders_amount_seller"] = float(analytics.get("orders_amount") or 0)
            row["orders_amount_buyer"] = float(analytics.get("orders_amount") or 0)
            row["orders_source"] = "wb_sales_funnel"

    rows = sorted(rows, key=lambda r: str(r.get("kpi_date") or ""))
    return rows[-days_back:]



def load_ozon_realization():
    date_from = (date.today() - timedelta(days=14)).isoformat()

    result = (
        supabase
        .table("daily_marketplace_kpi")
        .select("*")
        .eq("marketplace_code", "ozon")
        .gte("kpi_date", date_from)
        .order("kpi_date")
        .execute()
    )

    return result.data or []


def load_ozon_orders_by_schema(days_back=14):
    result = (
        supabase
        .table("v_ozon_orders_daily_by_schema")
        .select("*")
        .order("order_date", desc=True)
        .limit(60)
        .execute()
    )

    return result.data or []


def print_wb_daily(rows):
    print("\n=== WB: дневная динамика за последние 14 дней ===\n")

    if not rows:
        print("Нет данных WB")
        return

    print(
        f"{'Дата':<12} "
        f"{'Заказы':>10} "
        f"{'Сумма заказов':>15} "
        f"{'Выкупы':>10} "
        f"{'Сумма выкупов':>15} "
        f"{'% 7дн':>10}"
    )
    print("-" * 78)

    for idx, row in enumerate(rows):
        window = rows[max(0, idx - 6):idx + 1]

        orders_7d = sum(float(r.get("orders_qty") or 0) for r in window)
        buyouts_7d = sum(float(r.get("buyouts_qty") or 0) for r in window)

        if orders_7d > 0:
            rolling_rate = buyouts_7d / orders_7d * 100
        else:
            rolling_rate = 0

        print(
            f"{row.get('kpi_date'):<12} "
            f"{float(row.get('orders_qty') or 0):>10.0f} "
            f"{float(row.get('orders_amount_seller') or 0):>15,.0f} "
            f"{float(row.get('buyouts_qty') or 0):>10.0f} "
            f"{float(row.get('buyouts_amount_seller') or 0):>15,.0f} "
            f"{rolling_rate:>9.1f}%"
        )

    print("\nПримечание: % 7дн = сумма выкупов за последние 7 дней / сумма заказов за последние 7 дней.")
    print("Это не когортный выкуп, но он лучше дневного %, потому что у выкупов есть лаг.")

def print_ozon_realization(rows):
    print("\n=== Ozon: дневная экономика за последние 14 дней ===\n")

    if not rows:
        print("Нет данных Ozon realization")
        return

    print(
        f"{'Дата отчета':<12} "
        f"{'Выкупы/шт':>12} "
        f"{'Сумма реализации':>20} "
        f"{'Комиссии':>15}"
    )
    print("-" * 68)

    for row in rows:
        print(
            f"{row.get('kpi_date'):<12} "
            f"{float(row.get('buyouts_qty') or 0):>12.0f} "
            f"{float(row.get('buyouts_amount_seller') or 0):>20,.0f} "
            f"{float(row.get('commission_amount') or 0):>15,.0f}"
        )



def print_ozon_orders_by_schema(rows):
    print("\n=== Ozon: дневные заказы FBO + FBS за последние 14 дней ===\n")

    if not rows:
        print("Нет данных Ozon orders")
        return

    from datetime import date, timedelta

    grouped = {}

    for row in rows:
        day = row.get("order_date")
        schema = row.get("order_schema") or "unknown"

        if day not in grouped:
            grouped[day] = {
                "fbo_qty": 0,
                "fbo_amount": 0,
                "fbs_qty": 0,
                "fbs_amount": 0,
            }

        qty = float(row.get("orders_qty") or 0)
        amount = float(row.get("orders_amount_seller") or 0)

        if schema == "fbo":
            grouped[day]["fbo_qty"] += qty
            grouped[day]["fbo_amount"] += amount
        elif schema == "fbs":
            grouped[day]["fbs_qty"] += qty
            grouped[day]["fbs_amount"] += amount

    # Берем последние 14 дат, по которым реально есть данные.
    # Так не печатаем искусственные нули за дни до начала загрузки.
    calendar_days = sorted(grouped.keys())[-14:]

    print(
        f"{'Дата':<12}"
        f"{'FBO шт':>9} "
        f"{'FBO сумма':>14} "
        f"{'FBS шт':>9} "
        f"{'FBS сумма':>14} "
        f"{'Всего шт':>10} "
        f"{'Всего сумма':>15}"
    )
    print("-" * 90)

    for day in calendar_days:
        item = grouped.get(day, {
            "fbo_qty": 0,
            "fbo_amount": 0,
            "fbs_qty": 0,
            "fbs_amount": 0,
        })

        total_qty = item["fbo_qty"] + item["fbs_qty"]
        total_amount = item["fbo_amount"] + item["fbs_amount"]

        print(
            f"{day:<12}"
            f"{item['fbo_qty']:>9.0f} "
            f"{item['fbo_amount']:>14,.0f} "
            f"{item['fbs_qty']:>9.0f} "
            f"{item['fbs_amount']:>14,.0f} "
            f"{total_qty:>10.0f} "
            f"{total_amount:>15,.0f}"
        )


def safe_num(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def pct_change(current, base):
    current = safe_num(current)
    base = safe_num(base)
    if base == 0:
        return None
    return current / base - 1


def avg_safe(values):
    nums = [safe_num(v) for v in values if v is not None]
    return sum(nums) / len(nums) if nums else 0


def detect_anomalies(wb_rows, ozon_realization_rows, ozon_orders_rows):
    """
    Жесткие правила: код решает, когда нужен drill-down.
    AI потом только объясняет.

    Важно: дневной AI-анализ не должен использовать текущий неполный день.
    Сегодня анализируется отдельно в intraday Telegram-блоке.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    current_date = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()

    wb_rows = [
        r for r in (wb_rows or [])
        if str(r.get("kpi_date") or "") < current_date
    ]
    ozon_realization_rows = [
        r for r in (ozon_realization_rows or [])
        if str(r.get("kpi_date") or "") < current_date
    ]
    ozon_orders_rows = [
        r for r in (ozon_orders_rows or [])
        if str(r.get("order_date") or "") < current_date
    ]

    anomalies = []

    def sorted_by_date(rows, date_key):
        return sorted(rows or [], key=lambda r: str(r.get(date_key) or ""))

    # WB: вчера против среднего предыдущих 7 дней
    wb = sorted_by_date(wb_rows, "kpi_date")
    if len(wb) >= 3:
        last = wb[-1]
        prev = wb[-8:-1] if len(wb) >= 8 else wb[:-1]

        orders_delta = pct_change(last.get("orders_qty"), avg_safe([r.get("orders_qty") for r in prev]))
        buyouts_delta = pct_change(last.get("buyouts_qty"), avg_safe([r.get("buyouts_qty") for r in prev]))

        if orders_delta is not None and orders_delta <= -0.25:
            anomalies.append({
                "level": "CRITICAL",
                "code": "WB_ORDERS_DROP",
                "marketplace": "wb",
                "message": f"WB: заказы ниже среднего на {orders_delta:.0%}",
            })
        elif orders_delta is not None and orders_delta <= -0.15:
            anomalies.append({
                "level": "WARNING",
                "code": "WB_ORDERS_DROP",
                "marketplace": "wb",
                "message": f"WB: заказы ниже среднего на {orders_delta:.0%}",
            })

        if buyouts_delta is not None and buyouts_delta <= -0.25:
            anomalies.append({
                "level": "CRITICAL",
                "code": "WB_BUYOUTS_DROP",
                "marketplace": "wb",
                "message": f"WB: выкупы ниже среднего на {buyouts_delta:.0%}",
            })
        elif buyouts_delta is not None and buyouts_delta <= -0.15:
            anomalies.append({
                "level": "WARNING",
                "code": "WB_BUYOUTS_DROP",
                "marketplace": "wb",
                "message": f"WB: выкупы ниже среднего на {buyouts_delta:.0%}",
            })

    # Ozon экономика: расходы как % реализации
    ozon = sorted_by_date(ozon_realization_rows, "kpi_date")
    if ozon:
        last = ozon[-1]
        buyouts_amount = safe_num(last.get("buyouts_amount_seller"))
        expenses = (
            safe_num(last.get("commission_amount"))
            + safe_num(last.get("logistics_amount"))
            + safe_num(last.get("other_expenses_amount"))
        )

        if buyouts_amount > 0:
            expense_share = expenses / buyouts_amount

            if expense_share >= 0.50:
                anomalies.append({
                    "level": "CRITICAL",
                    "code": "OZON_EXPENSE_SHARE_HIGH",
                    "marketplace": "ozon",
                    "message": f"Ozon: расходы {expense_share:.0%} от реализации",
                })
            elif expense_share >= 0.45:
                anomalies.append({
                    "level": "WARNING",
                    "code": "OZON_EXPENSE_SHARE_HIGH",
                    "marketplace": "ozon",
                    "message": f"Ozon: расходы {expense_share:.0%} от реализации",
                })

    # Ozon FBO/FBS: отдельная просадка схемы
    orders = sorted_by_date(ozon_orders_rows, "order_date")
    if orders:
        by_schema = {}
        for row in orders:
            schema = row.get("order_schema") or "unknown"
            by_schema.setdefault(schema, []).append(row)

        for schema, rows in by_schema.items():
            rows = sorted_by_date(rows, "order_date")
            if len(rows) >= 3:
                last = rows[-1]
                prev = rows[-8:-1] if len(rows) >= 8 else rows[:-1]
                delta = pct_change(last.get("orders_qty"), avg_safe([r.get("orders_qty") for r in prev]))

                if delta is not None and delta <= -0.35:
                    anomalies.append({
                        "level": "CRITICAL",
                        "code": "OZON_SCHEMA_DROP",
                        "marketplace": "ozon",
                        "schema": schema,
                        "message": f"Ozon {schema}: заказы ниже среднего на {delta:.0%}",
                    })
                elif delta is not None and delta <= -0.20:
                    anomalies.append({
                        "level": "WARNING",
                        "code": "OZON_SCHEMA_DROP",
                        "marketplace": "ozon",
                        "schema": schema,
                        "message": f"Ozon {schema}: заказы ниже среднего на {delta:.0%}",
                    })

    return anomalies


def ai_mode_from_anomalies(anomalies):
    if any(a.get("level") == "CRITICAL" for a in anomalies):
        return "CRITICAL"
    if any(a.get("level") == "WARNING" for a in anomalies):
        return "WARNING"
    return "NORMAL"


def compact_rows(rows, date_key, fields, limit=14):
    if not rows:
        return "нет данных"

    clean = []
    for row in rows:
        item = {date_key: row.get(date_key)}
        for field in fields:
            item[field] = row.get(field)
        clean.append(item)

    clean = sorted(clean, key=lambda x: str(x.get(date_key) or ""))[-limit:]
    return "\n".join(str(x) for x in clean)


def build_ai_context(wb_rows, ozon_realization_rows, ozon_orders_rows):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    current_date = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()

    # Для дневного AI-вывода убираем текущий неполный день.
    wb_rows = [
        r for r in (wb_rows or [])
        if str(r.get("kpi_date") or "") < current_date
    ]
    ozon_realization_rows = [
        r for r in (ozon_realization_rows or [])
        if str(r.get("kpi_date") or "") < current_date
    ]
    ozon_orders_rows = [
        r for r in (ozon_orders_rows or [])
        if str(r.get("order_date") or "") < current_date
    ]

    anomalies = detect_anomalies(wb_rows, ozon_realization_rows, ozon_orders_rows)
    mode = ai_mode_from_anomalies(anomalies)

    context = []
    context.append(f"AI_MODE: {mode}")

    if anomalies:
        context.append("Сработавшие правила:")
        for item in anomalies[:10]:
            context.append(f"- {item.get('level')}: {item.get('message')} [{item.get('code')}]")
    else:
        context.append("Сработавшие правила: нет сильных отклонений.")

    context.append("\nWB последние дни:")
    context.append(compact_rows(
        wb_rows,
        "kpi_date",
        ["orders_qty", "orders_amount_seller", "buyouts_qty", "buyouts_amount_seller", "buyout_rate"],
        limit=14,
    ))

    context.append("\nOzon экономика последние дни:")
    context.append(compact_rows(
        ozon_realization_rows,
        "kpi_date",
        ["buyouts_qty", "buyouts_amount_seller", "commission_amount", "logistics_amount", "other_expenses_amount"],
        limit=14,
    ))

    context.append("\nOzon orders FBO/FBS последние дни:")
    context.append(compact_rows(
        ozon_orders_rows,
        "order_date",
        ["order_schema", "orders_qty", "orders_amount_seller"],
        limit=28,
    ))

    # Drill-down добавляем только при WARNING/CRITICAL.
    # Пока без SKU-сырья: сначала включим устойчивую версию на агрегатах.
    if mode in ("WARNING", "CRITICAL"):
        context.append("\nDRILL_DOWN:")
        context.append("Добавлены подробности по проблемным блокам на уровне дней и схем FBO/FBS.")
        context.append("Следующий этап: добавить топ-SKU только для сработавшего marketplace/метрики.")

    return mode, anomalies, "\n".join(context)


def ai_summary(wb_rows, ozon_realization_rows, ozon_orders_rows):
    print("\n=== AI-вывод для руководителя ===\n")

    mode, anomalies, ai_context = build_ai_context(
        wb_rows,
        ozon_realization_rows,
        ozon_orders_rows,
    )

    prompt = f"""
Ты финансовый аналитик маркетплейсов.

Твоя задача — дать короткий управленческий вывод для руководителя.
Не пересчитывай цифры самостоятельно, используй только переданные метрики.
Не выдумывай причин, если их нет в данных.
Пиши по-русски, кратко и конкретно.

Режим анализа: {mode}

Данные:
{ai_context}

Сформируй:
1. Общая оценка дня.
2. Главные отклонения, если есть.
3. Что проверить руководителю сегодня.
4. Объясни режим:
- NORMAL: критичных отклонений нет, указать 1–2 зоны контроля.
- WARNING: есть отклонения, нужна проверка, но не писать "критический сбой".
- CRITICAL: есть критичное отклонение, нужна срочная проверка.
"""

    try:
        response = client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            input=prompt,
        )
        print(response.output_text)
    except Exception as e:
        print(f"AI summary не удалось сформировать: {e}")
        print("Fallback-вывод:")
        print(f"Режим: {mode}")

        if anomalies:
            for item in anomalies[:5]:
                print(f"- {item.get('level')}: {item.get('message')}")
        else:
            print("- Критичных отклонений по правилам нет.")


if __name__ == "__main__":
    wb_rows = load_wb_daily(days_back=14)
    ozon_realization_rows = load_ozon_realization()
    ozon_orders_rows = load_ozon_orders_by_schema(days_back=14)

    print("\n======================================")
    print(" УПРАВЛЕНЧЕСКИЙ ОТЧЕТ MARKETPLACES")
    print("======================================")

    print_wb_daily(wb_rows)
    print_ozon_realization(ozon_realization_rows)
    print_ozon_orders_by_schema(ozon_orders_rows)
    ai_summary(wb_rows, ozon_realization_rows, ozon_orders_rows)


def print_ozon_economics_table(rows):
    print()
    print("Дата        Выкупы   Реализация    Комиссии    Логистика    Прочие     Расходы итого   После расходов")
    print("------------------------------------------------------------------------------------------------------")

    for row in rows:
        buyouts = float(row.get("buyouts_qty") or 0)
        revenue = float(row.get("buyouts_amount_seller") or 0)
        commission = float(row.get("commission_amount") or 0)
        logistics = float(row.get("logistics_amount") or 0)
        other = float(row.get("other_expenses_amount") or 0)
        total_expenses = commission + logistics + other
        net_after_expenses = revenue - total_expenses

        print(
            f"{row.get('kpi_date')}  "
            f"{buyouts:>6.0f}  "
            f"{revenue:>11,.0f}  "
            f"{commission:>10,.0f}  "
            f"{logistics:>10,.0f}  "
            f"{other:>8,.0f}  "
            f"{total_expenses:>13,.0f}  "
            f"{net_after_expenses:>14,.0f}"
        )
