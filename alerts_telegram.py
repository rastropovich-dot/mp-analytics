import os
from datetime import date, timedelta
import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

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


def get_wb_rows(days_back=14):
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


def get_ozon_fbs_rows(days_back=14):
    date_from = (date.today() - timedelta(days=days_back)).isoformat()

    result = (
        supabase
        .table("marketplace_orders")
        .select("*")
        .eq("marketplace_code", "ozon")
        .gte("order_date", date_from)
        .order("order_date")
        .execute()
    )

    rows = result.data or []
    grouped = {}

    for row in rows:
        d = row.get("order_date")
        if not d:
            continue

        if d not in grouped:
            grouped[d] = {"date": d, "orders_qty": 0, "orders_amount": 0}

        grouped[d]["orders_qty"] += float(row.get("orders_qty") or 0)
        grouped[d]["orders_amount"] += float(row.get("orders_amount_seller") or 0)

    return [grouped[k] for k in sorted(grouped.keys())]


def avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else 0


def build_alerts():
    alerts = []

    wb = get_wb_rows(14)
    if wb:
        last = wb[-1]
        prev = wb[-8:-1] if len(wb) >= 8 else wb[:-1]

        last_orders = float(last.get("orders_qty") or 0)
        last_buyouts = float(last.get("buyouts_qty") or 0)

        avg_orders = avg([float(r.get("orders_qty") or 0) for r in prev])
        avg_buyouts = avg([float(r.get("buyouts_qty") or 0) for r in prev])

        if avg_orders > 0 and last_orders < avg_orders * 0.75:
            alerts.append(
                f"🟠 <b>WB: падение заказов</b>\n"
                f"{last.get('kpi_date')}: заказов {last_orders:.0f}, среднее 7 дней {avg_orders:.0f}."
            )

        if avg_buyouts > 0 and last_buyouts < avg_buyouts * 0.75:
            alerts.append(
                f"🟠 <b>WB: падение выкупов</b>\n"
                f"{last.get('kpi_date')}: выкупов {last_buyouts:.0f}, среднее 7 дней {avg_buyouts:.0f}."
            )

        if last_orders <= 5 and last_buyouts >= 50:
            alerts.append(
                f"🟡 <b>WB: выкупы без заказов</b>\n"
                f"{last.get('kpi_date')}: заказов {last_orders:.0f}, выкупов {last_buyouts:.0f}. Вероятен лаг WB sales."
            )

    ozon = get_ozon_fbs_rows(14)
    if ozon:
        last = ozon[-1]
        prev = ozon[-8:-1] if len(ozon) >= 8 else ozon[:-1]

        last_orders = float(last.get("orders_qty") or 0)
        avg_orders = avg([float(r.get("orders_qty") or 0) for r in prev])

        if avg_orders > 0 and last_orders < avg_orders * 0.75:
            alerts.append(
                f"🟠 <b>Ozon FBS: падение заказов</b>\n"
                f"{last.get('date')}: заказов {last_orders:.0f}, среднее 7 дней {avg_orders:.0f}."
            )

    if not alerts:
        alerts.append("✅ Критичных сигналов нет.")

    message = "📊 <b>MP Analytics Alerts</b>\n"
    message += f"Дата: {date.today().isoformat()}\n\n"
    message += "\n\n".join(alerts)

    return message


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("Не заполнен TELEGRAM_BOT_TOKEN в .env")

    if not TELEGRAM_CHAT_ID:
        raise ValueError("Не заполнен TELEGRAM_CHAT_ID в .env")

    msg = build_alerts()
    send_telegram(msg)
