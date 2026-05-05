import argparse
import html
import os
import subprocess
import sys
from collections import deque
from datetime import datetime

import requests
from dotenv import load_dotenv


load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STEPS = [
    ("WB: загрузка заказов", "python3 loaders/wb_orders_loader.py"),
    ("WB: загрузка заказов Analytics Sales Funnel", "python3 loaders/wb_sales_funnel_orders_loader.py"),
    ("WB: загрузка продаж/выкупов", "python3 loaders/wb_sales_loader.py"),
    ("WB: загрузка остатков", "python3 loaders/wb_stocks_loader.py"),

    ("Ozon: загрузка FBS заказов", "python3 loaders/ozon_fbs_orders_loader.py"),
    ("Ozon: загрузка FBO заказов", "python3 loaders/ozon_fbo_orders_loader.py"),
    ("Ozon: дневные финоперации", "python3 loaders/ozon_finance_transactions_loader.py"),
    ("Ozon: расходы и комиссии", "python3 loaders/ozon_expenses_loader.py"),
    ("Ozon: реклама Performance API", "python3 loaders/ozon_performance_ads_loader.py"),
    ("Ozon: расчет organic sales по SKU", "python3 reports_ozon_sku_organic.py --from-db-only"),
    ("Ozon: загрузка остатков", "python3 loaders/ozon_stocks_loader.py"),

    ("KPI: расчет SKU", "python3 reports_daily_sku_kpi.py"),
    ("KPI: расчет маркетплейсов", "python3 reports_daily_marketplace_kpi.py"),

    ("Excel: выгрузка управленческого отчета", "python3 export_management_excel.py"),
    ("Telegram: отправка сигналов", "python3 alerts_telegram.py"),
]


def parse_args():
    parser = argparse.ArgumentParser(description="Run MP Analytics daily pipeline.")
    parser.add_argument(
        "--skip-telegram",
        action="store_true",
        help="Skip Telegram executive report step. Useful when report is scheduled by a separate cron job.",
    )
    return parser.parse_args()


def send_failure_alert(title, returncode, tail_lines):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    tail_text = "\n".join(line for line in tail_lines if line).strip()
    if len(tail_text) > 3500:
        tail_text = tail_text[-3500:]

    message = (
        "❌ <b>Пайплайн MP Analytics упал</b>\n"
        f"Шаг: {html.escape(title)}\n"
        f"Код ошибки: {returncode}\n"
    )

    if tail_text:
        message += f"\n<pre>{html.escape(tail_text)}</pre>"

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
    except Exception as exc:
        print(f"Не удалось отправить Telegram alert о падении пайплайна: {exc}")


def prepare_command(command):
    stripped = command.lstrip()

    if stripped.startswith("python3 "):
        return command.replace("python3 ", "python3 -u ", 1)

    return command


def run_step(title, command):
    prepared_command = prepare_command(command)

    print("\n" + "=" * 80)
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {title}")
    print("=" * 80)
    print(f"Команда: {prepared_command}\n")

    process = subprocess.Popen(
        prepared_command,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    tail_lines = deque(maxlen=20)

    assert process.stdout is not None

    for line in process.stdout:
        tail_lines.append(line.rstrip())
        print(line, end="", flush=True)

    process.stdout.close()
    returncode = process.wait()

    if returncode != 0:
        print(f"❌ Ошибка на шаге: {title}")
        print(f"Код ошибки: {returncode}")
        send_failure_alert(title, returncode, list(tail_lines))
        sys.exit(returncode)

    print(f"✅ Готово: {title}")


def main():
    args = parse_args()
    print("\n🚀 Запуск ежедневного пайплайна MP Analytics")
    print(f"Старт: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    for title, command in STEPS:
        if args.skip_telegram and title.startswith("Telegram:"):
            print(f"⏭️ Пропускаем шаг: {title}")
            continue
        run_step(title, command)

    print("\n✅ Весь пайплайн успешно завершен")
    print(f"Финиш: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
