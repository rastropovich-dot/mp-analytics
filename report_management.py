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

def load_ozon_fbs_orders(days_back=14):
    date_from = date.today() - timedelta(days=days_back - 1)
    date_to = date.today()

    result = (
        supabase
        .table("marketplace_orders")
        .select("*")
        .eq("marketplace_code", "ozon")
        .gte("order_date", date_from.isoformat())
        .lte("order_date", date_to.isoformat())
        .order("order_date")
        .execute()
    )

    rows = result.data or []

    grouped = {}

    current = date_from
    while current <= date_to:
        d = current.isoformat()
        grouped[d] = {
            "order_date": d,
            "orders_qty": 0,
            "orders_amount_seller": 0,
        }
        current += timedelta(days=1)

    for row in rows:
        d = row.get("order_date")

        if not d:
            continue

        if d not in grouped:
            grouped[d] = {
                "order_date": d,
                "orders_qty": 0,
                "orders_amount_seller": 0,
            }

        grouped[d]["orders_qty"] += float(row.get("orders_qty") or 0)
        grouped[d]["orders_amount_seller"] += float(row.get("orders_amount_seller") or 0)

    return list(grouped.values())

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


def print_ozon_fbs(rows):
    print("\n=== Ozon FBS: дневные заказы, календарь последних 14 дней ===\n")

    if not rows:
        print("Нет данных Ozon FBS orders")
        return

    print(
        f"{'Дата':<12} "
        f"{'Заказы FBS':>12} "
        f"{'Сумма заказов FBS':>20}"
    )
    print("-" * 50)

    for row in rows:
        print(
            f"{row.get('order_date'):<12} "
            f"{float(row.get('orders_qty') or 0):>12.0f} "
            f"{float(row.get('orders_amount_seller') or 0):>20,.0f}"
        )


def ai_summary(wb_rows, ozon_realization_rows, ozon_fbs_rows):
    prompt = f"""
Ты финансовый аналитик маркетплейсов.

У нас есть разные типы данных:
1. WB — дневные данные: заказы, выкупы, процент выкупа.
2. Ozon realization — месячная реализация, НЕ дневной отчет.
3. Ozon FBS orders — дневные FBS-заказы, но это не весь Ozon.

WB daily:
{wb_rows}

Ozon monthly realization:
{ozon_realization_rows}

Ozon FBS daily orders:
{ozon_fbs_rows}

Сделай короткую управленческую выжимку на русском:
1. Что видно по WB.
2. Что видно по Ozon realization.
3. Что видно по Ozon FBS.
4. Какие ограничения данных важно помнить.
5. Какие 3 действия руководителю проверить завтра.

Пиши коротко, без воды. Не делай вывод, что Ozon упал или вырос по дням на основе месячной realization.
"""

    response = client.responses.create(
        model="gpt-4o-mini",
        input=prompt
    )

    print("\n=== AI-вывод для руководителя ===\n")
    print(response.output_text)


if __name__ == "__main__":
    wb_rows = load_wb_daily(days_back=14)
    ozon_realization_rows = load_ozon_realization()
    ozon_fbs_rows = load_ozon_fbs_orders(days_back=14)

    print("\n======================================")
    print(" УПРАВЛЕНЧЕСКИЙ ОТЧЕТ MARKETPLACES")
    print("======================================")

    print_wb_daily(wb_rows)
    print_ozon_realization(ozon_realization_rows)
    print_ozon_fbs(ozon_fbs_rows)
    ai_summary(wb_rows, ozon_realization_rows, ozon_fbs_rows)


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
