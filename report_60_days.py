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


def load_kpi(days_back=60):
    date_from = (date.today() - timedelta(days=days_back)).isoformat()

    result = (
        supabase
        .table("daily_marketplace_kpi")
        .select("*")
        .gte("kpi_date", date_from)
        .order("kpi_date")
        .execute()
    )

    return result.data


def print_table(rows):
    print("\n=== Управленческий отчет за последние 60 дней ===\n")

    if not rows:
        print("Нет данных для отчета")
        return

    print(
        f"{'Дата':<12} "
        f"{'MP':<6} "
        f"{'Заказы':>10} "
        f"{'Сумма заказов':>15} "
        f"{'Выкупы':>10} "
        f"{'Сумма выкупов':>15} "
        f"{'% выкупа':>10}"
    )

    print("-" * 85)

    for row in rows:
        buyout_rate = float(row.get("buyout_rate") or 0) * 100

        print(
            f"{row.get('kpi_date'):<12} "
            f"{row.get('marketplace_code'):<6} "
            f"{float(row.get('orders_qty') or 0):>10.0f} "
            f"{float(row.get('orders_amount_seller') or 0):>15,.0f} "
            f"{float(row.get('buyouts_qty') or 0):>10.0f} "
            f"{float(row.get('buyouts_amount_seller') or 0):>15,.0f} "
            f"{buyout_rate:>9.1f}%"
        )


def ai_summary(rows):
    if not rows:
        return

    prompt = f"""
Ты финансовый аналитик маркетплейсов Ozon и WB.

Вот KPI за последние 60 дней:
{rows}

Сделай краткую управленческую выжимку на русском языке:
1. Что происходит с заказами.
2. Что происходит с выкупами.
3. Где есть риск.
4. Какие 3 действия нужно проверить руководителю.
Пиши коротко, по делу, без воды.
"""

    response = client.responses.create(
        model="gpt-4o-mini",
        input=prompt
    )

    print("\n=== AI-вывод для руководителя ===\n")
    print(response.output_text)


if __name__ == "__main__":
    rows = load_kpi(days_back=60)
    print_table(rows)
    ai_summary(rows)
