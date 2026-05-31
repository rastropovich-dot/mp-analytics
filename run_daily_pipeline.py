import argparse
import html
import json
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

def build_ozon_performance_daily_command(args=None):
    command_parts = [
        "python3",
        "loaders/ozon_performance_ads_loader.py",
        "--mode",
        "daily-yesterday",
    ]

    if args:
        if getattr(args, "ozon_campaign_selection", None):
            command_parts.extend(["--campaign-selection", args.ozon_campaign_selection])
        if getattr(args, "ozon_recent_activity_days", None) is not None:
            command_parts.extend(["--recent-activity-days", str(args.ozon_recent_activity_days)])
        if getattr(args, "ozon_dormant_probe_size", None) is not None:
            command_parts.extend(["--dormant-probe-size", str(args.ozon_dormant_probe_size)])
        if getattr(args, "ozon_max_daily_cpc_units", None) is not None:
            command_parts.extend(["--max-daily-cpc-units", str(args.ozon_max_daily_cpc_units)])
        if getattr(args, "ozon_allow_staged_cpc_partial", False):
            command_parts.append("--allow-staged-cpc-partial")

    return " ".join(command_parts)


def build_steps(args=None):
    return [
        (
            "Ozon Performance: CPC recovery before daily",
            "python3 scripts/ozon_performance_recovery_worker.py --write --approve-recovery-worker-write --phase pre --max-batches-per-run 1",
        ),
        ("WB: загрузка заказов", "python3 loaders/wb_orders_loader.py"),
        ("WB: загрузка заказов Analytics Sales Funnel", "python3 loaders/wb_sales_funnel_orders_loader.py"),
        ("WB: загрузка продаж/выкупов", "python3 loaders/wb_sales_loader.py"),
        ("WB: загрузка остатков", "python3 loaders/wb_stocks_loader.py"),
        ("Ozon: загрузка FBS заказов", "python3 loaders/ozon_fbs_orders_loader.py"),
        ("Ozon: загрузка FBO заказов", "python3 loaders/ozon_fbo_orders_loader.py"),
        ("Ozon: дневные финоперации", "python3 loaders/ozon_finance_transactions_loader.py"),
        ("Ozon: расходы и комиссии", "python3 loaders/ozon_expenses_loader.py"),
        ("Ozon: реклама Performance API", build_ozon_performance_daily_command(args)),
        (
            "Ozon Performance: CPC recovery after daily",
            "python3 scripts/ozon_performance_recovery_worker.py --write --approve-recovery-worker-write --phase post --wait-for-minutes 240 --timezone Europe/Moscow --max-attempts 10 --max-batches-per-run 1 --stop-when-complete",
        ),
        ("Ozon: total orders analytics по SKU", "python3 loaders/ozon_sku_total_analytics_loader.py --mode daily-yesterday"),
        ("Ozon: расчет organic sales по SKU", "python3 reports_ozon_sku_organic.py --mode daily-yesterday --from-db-only"),
        ("Ozon: загрузка остатков", "python3 loaders/ozon_stocks_loader.py"),
        ("KPI: расчет SKU", "python3 reports_daily_sku_kpi.py"),
        ("KPI: расчет маркетплейсов", "python3 reports_daily_marketplace_kpi.py"),
        ("Decision: SKU daily input", "python3 reports_sku_decision_daily_input.py --mode daily-yesterday"),
        ("Excel: выгрузка управленческого отчета", "python3 export_management_excel.py"),
        ("Telegram: отправка сигналов", "python3 alerts_telegram.py"),
    ]


STEPS = build_steps()


def parse_args():
    parser = argparse.ArgumentParser(description="Run MP Analytics daily pipeline.")
    parser.add_argument(
        "--skip-telegram",
        action="store_true",
        help="Skip Telegram executive report step. Useful when report is scheduled by a separate cron job.",
    )
    parser.add_argument(
        "--skip-excel",
        action="store_true",
        help="Skip Excel export step. Useful as a temporary mitigation if export causes memory pressure.",
    )
    parser.add_argument(
        "--skip-decision",
        action="store_true",
        help="Skip Decision: SKU daily input step. Useful as an emergency mitigation if decision rebuild causes memory pressure.",
    )
    parser.add_argument(
        "--skip-recovery",
        action="store_true",
        help="Skip Ozon Performance CPC recovery step. Useful as an emergency mitigation if recovery should be temporarily disabled.",
    )
    parser.add_argument(
        "--ozon-campaign-selection",
        choices=("complete", "smart_recent_active"),
        default=None,
        help="Pass Ozon Performance daily campaign selection mode to the daily-yesterday loader only.",
    )
    parser.add_argument(
        "--ozon-recent-activity-days",
        type=int,
        default=None,
        help="Recent activity window for smart Ozon campaign selection.",
    )
    parser.add_argument(
        "--ozon-dormant-probe-size",
        type=int,
        default=None,
        help="Deterministic dormant probe size for smart Ozon campaign selection.",
    )
    parser.add_argument(
        "--ozon-max-daily-cpc-units",
        type=int,
        default=1200,
        help="Optional cap for the initial Ozon daily CPC campaign units before post-recovery continues the tail.",
    )
    parser.add_argument(
        "--ozon-allow-staged-cpc-partial",
        action="store_true",
        help="Allow Ozon daily-yesterday CPC stage to stop intentionally before full completion and leave pending_backfill for post-recovery.",
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


def parse_recovery_worker_result(output_text):
    return parse_json_after_marker(output_text, "Ozon Performance recovery worker result:")


def parse_json_after_marker(output_text, marker):
    if marker not in output_text:
        return None

    marker_index = output_text.rfind(marker)
    after_marker = output_text[marker_index + len(marker):].lstrip()
    if not after_marker.startswith("{"):
        return None

    decoder = json.JSONDecoder()
    try:
        parsed, _ = decoder.raw_decode(after_marker)
    except json.JSONDecodeError:
        return None
    return parsed


def parse_ozon_performance_run_summary(output_text):
    return parse_json_after_marker(output_text, "Ozon Performance run summary:")


def is_recovery_step(title):
    return title.startswith("Ozon Performance: CPC recovery")


def is_ozon_organic_step(title):
    return title == "Ozon: расчет organic sales по SKU"


def recovery_result_allows_ozon_downstream(recovery_result):
    if not recovery_result:
        return False
    return recovery_result.get("status") == "complete"


def ozon_run_summary_is_complete(run_summary):
    if not run_summary:
        return False
    return run_summary.get("overall_status") == "success"


def should_skip_pipeline_step(title, args, ozon_downstream_allowed):
    if args.skip_recovery and is_recovery_step(title):
        return True, f"⏭️ Пропускаем шаг: {title}"
    if args.skip_excel and title.startswith("Excel:"):
        return True, f"⏭️ Пропускаем шаг: {title}"
    if args.skip_decision and title == "Decision: SKU daily input":
        return True, f"⏭️ Пропускаем шаг: {title}"
    if args.skip_telegram and title.startswith("Telegram:"):
        return True, f"⏭️ Пропускаем шаг: {title}"
    if ozon_downstream_allowed is False and is_ozon_organic_step(title):
        return True, f"⏭️ Пропускаем шаг: {title} (Ozon Performance still partial/incomplete)"
    return False, None


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
    full_output_lines = []

    assert process.stdout is not None

    for line in process.stdout:
        tail_lines.append(line.rstrip())
        full_output_lines.append(line)
        print(line, end="", flush=True)

    process.stdout.close()
    returncode = process.wait()

    if returncode != 0:
        print(f"❌ Ошибка на шаге: {title}")
        print(f"Код ошибки: {returncode}")
        send_failure_alert(title, returncode, list(tail_lines))
        sys.exit(returncode)

    recovery_result = None
    if is_recovery_step(title):
        recovery_result = parse_recovery_worker_result("".join(full_output_lines))
        if recovery_result and recovery_result.get("status") == "failed":
            print(f"❌ Ошибка на шаге: {title}")
            print("Recovery worker returned status=failed")
            send_failure_alert(title, 1, list(tail_lines))
            sys.exit(1)

    print(f"✅ Готово: {title}")
    return {
        "output_text": "".join(full_output_lines),
        "recovery_result": recovery_result,
        "ozon_run_summary": parse_ozon_performance_run_summary("".join(full_output_lines))
        if title == "Ozon: реклама Performance API"
        else None,
    }


def main():
    args = parse_args()
    steps = build_steps(args)
    print("\n🚀 Запуск ежедневного пайплайна MP Analytics")
    print(f"Старт: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    ozon_downstream_allowed = None

    for title, command in steps:
        should_skip, skip_message = should_skip_pipeline_step(title, args, ozon_downstream_allowed)
        if should_skip:
            print(skip_message)
            continue
        step_result = run_step(title, command)

        if title == "Ozon: реклама Performance API":
            ozon_downstream_allowed = ozon_run_summary_is_complete(step_result.get("ozon_run_summary"))
            summary = step_result.get("ozon_run_summary") or {}
            print(
                "Ozon Performance daily status after main load: "
                f"{summary.get('overall_status') or 'unknown'}"
            )
        elif title == "Ozon Performance: CPC recovery after daily":
            recovery_result = step_result.get("recovery_result") or {}
            if recovery_result_allows_ozon_downstream(recovery_result):
                ozon_downstream_allowed = True
            else:
                ozon_downstream_allowed = False
            print(
                "Ozon Performance status after post-recovery: "
                f"{recovery_result.get('status') or 'unknown'}"
            )

    print("\n✅ Весь пайплайн успешно завершен")
    print(f"Финиш: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
