import subprocess
import sys
from datetime import datetime


STEPS = [
    ("WB: загрузка заказов", "python3 loaders/wb_orders_loader.py"),
    ("WB: загрузка заказов Analytics Sales Funnel", "python3 loaders/wb_sales_funnel_orders_loader.py"),
    ("WB: загрузка продаж/выкупов", "python3 loaders/wb_sales_loader.py"),
    ("WB: загрузка остатков", "python3 loaders/wb_stocks_loader.py"),

    ("Ozon: загрузка FBS заказов", "python3 loaders/ozon_fbs_orders_loader.py"),
    ("Ozon: загрузка FBO заказов", "python3 loaders/ozon_fbo_orders_loader.py"),
    ("Ozon: дневные финоперации", "python3 loaders/ozon_finance_transactions_loader.py"),
    ("Ozon: расходы и комиссии", "python3 loaders/ozon_expenses_loader.py"),
    ("Ozon: загрузка остатков", "python3 loaders/ozon_stocks_loader.py"),

    ("KPI: расчет SKU", "python3 reports_daily_sku_kpi.py"),
    ("KPI: расчет маркетплейсов", "python3 reports_daily_marketplace_kpi.py"),

    ("Excel: выгрузка управленческого отчета", "python3 export_management_excel.py"),
    ("Telegram: отправка сигналов", "python3 alerts_telegram.py"),
]


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

    assert process.stdout is not None

    for line in process.stdout:
        print(line, end="", flush=True)

    process.stdout.close()
    returncode = process.wait()

    if returncode != 0:
        print(f"❌ Ошибка на шаге: {title}")
        print(f"Код ошибки: {returncode}")
        sys.exit(returncode)

    print(f"✅ Готово: {title}")


def main():
    print("\n🚀 Запуск ежедневного пайплайна MP Analytics")
    print(f"Старт: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    for title, command in STEPS:
        run_step(title, command)

    print("\n✅ Весь пайплайн успешно завершен")
    print(f"Финиш: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
