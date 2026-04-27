import subprocess
import sys
from datetime import datetime


STEPS = [
    ("WB: загрузка заказов", "python3 loaders/wb_orders_loader.py"),
    ("WB: загрузка продаж/выкупов", "python3 loaders/wb_sales_loader.py"),
    ("WB: загрузка остатков", "python3 loaders/wb_stocks_loader.py"),

    ("Ozon: загрузка FBS заказов", "python3 loaders/ozon_fbs_orders_loader.py"),
    ("Ozon: дневные финоперации", "python3 loaders/ozon_finance_transactions_loader.py"),
    ("Ozon: расходы и комиссии", "python3 loaders/ozon_expenses_loader.py"),
    ("Ozon: загрузка остатков", "python3 loaders/ozon_stocks_loader.py"),

    ("KPI: расчет SKU", "python3 reports_daily_sku_kpi.py"),
    ("KPI: расчет маркетплейсов", "python3 reports_daily_marketplace_kpi.py"),

    ("Excel: выгрузка управленческого отчета", "python3 export_management_excel.py"),
    ("Telegram: отправка сигналов", "python3 alerts_telegram.py"),
]


def run_step(title, command):
    print("\n" + "=" * 80)
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {title}")
    print("=" * 80)
    print(f"Команда: {command}\n")

    result = subprocess.run(
        command,
        shell=True,
        text=True,
        capture_output=True
    )

    if result.stdout:
        print(result.stdout)

    if result.stderr:
        print("STDERR:")
        print(result.stderr)

    if result.returncode != 0:
        print(f"❌ Ошибка на шаге: {title}")
        print(f"Код ошибки: {result.returncode}")
        sys.exit(result.returncode)

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
