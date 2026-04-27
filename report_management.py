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
    date_from = (date.today() - timedelta(days=days_back)).isoformat()

    result = (
        supabase
        .table("daily_marketplace_kpi")
        .select("*")
        .eq("marketplace_code", "wb")
        .gte("kpi_date", date_from)
        .order("kpi_date")
        .execute()
    )

    return result.data or []


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


def ai_summary(wb_rows, ozon_realization_rows, ozon_orders_rows):
    print("\n=== AI-вывод для руководителя ===\n")
    print("AI summary временно отключен: нужно сократить объем данных для промта.")
    return


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
    print("\n=== AI-вывод для руководителя ===\n")
    print("AI summary временно отключен: нужно сократить объем данных для промта.")


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
