"""render.yaml для четырёх cron-сервисов РБХ — источник правды здесь, файл генерируется (§5.5 первой задачи RBH, план 09-30, поправка советника 2:
сервисы KARATOV в Blueprint не входят — их конфигурация живёт в панели Render).

    venv/bin/python3 scripts/render_blueprint.py --write     перегенерировать render.yaml
    venv/bin/python3 scripts/render_blueprint.py --check     файл на диске = генератору (тест делает то же)

Четыре сервиса из одного репозитория и ветки main: ночь и утренний алерт на кабинет, расписание со сдвигом 5 минут от KARATOV (00:15 / 07:30 UTC),
чтобы три ночи не стартовали в одну секунду и утренние сообщения шли по порядку. Команда ночи = команда KARATOV (сверена с Render 09-28 и 10-06),
`MP_CABINET` — в переменных сервиса; секреты — в группе окружения кабинета (`rbh1` / `rbh2`, `sync: false` → владелец вводит в Dashboard при
создании Blueprint, в git значений нет); настройки сборщика рекламы — те же, что у KARATOV (.env.example). Регион — frankfurt (ближе к eu-central-1
Supabase; у KARATOV сверить по панели перед созданием), план — starter (сверить там же). PyYAML в окружении нет, поэтому YAML пишется строками
генератора, а тесты проверяют структуру по BLUEPRINT и равенство файла генератору.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "render.yaml")

NIGHT_COMMAND = ("python3 run_daily_pipeline.py --skip-recovery --skip-organic --skip-telegram --skip-excel --skip-decision "
                 "--ozon-campaign-selection smart_recent_active --ozon-recent-activity-days 7 --ozon-dormant-probe-size 100 "
                 "--ozon-max-daily-cpc-units 1200 --ozon-allow-staged-cpc-partial")
ALERT_COMMAND = "python3 alerts_telegram.py"
BUILD_COMMAND = "pip install -r requirements.txt"
REGION = "frankfurt"
PLAN = "starter"

# Секреты кабинета — владелец вводит в Dashboard (sync: false); значения не попадают ни в git, ни в отчёты.
SECRET_KEYS = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "OZON_CLIENT_ID", "OZON_API_KEY", "OZON_PERFORMANCE_CLIENT_ID",
               "OZON_PERFORMANCE_CLIENT_SECRET", "WB_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
# Несекретные настройки — те же, что у KARATOV (.env.example); флаги Selected CPO у нового кабинета выключены до отдельного решения
# (ENABLE без APPROVE жгло бы квоту Performance впустую). PYTHON_VERSION — как у KARATOV (снять с панели), поэтому тоже sync: false.
SETTING_VALUES = (
    ("APP_TIMEZONE", "Europe/Moscow"),
    ("OZON_PERFORMANCE_STATE_BACKEND", "db"),
    ("OZON_PERFORMANCE_DAILY_TARGET_MODE", "yesterday"),
    ("OZON_PERFORMANCE_DAILY_CPC_SELECTION_MODE", "complete"),
    ("OZON_PERFORMANCE_CAMPAIGN_BATCH_SIZE", "10"),
    ("OZON_PERFORMANCE_MAX_CPC_BATCHES_PER_RUN", "5"),
    ("OZON_PERFORMANCE_STATS_DAILY_CAMPAIGN_LIMIT", "2000"),
    ("OZON_PERFORMANCE_STATS_DAILY_CAMPAIGN_RESERVE", "200"),
    ("OZON_PERFORMANCE_MAX_STATS_CAMPAIGNS_PER_DAILY_RUN", "1800"),
    ("OZON_PERFORMANCE_CPC_BACKFILL_START_HHMM", "03:05"),
    ("ENABLE_OZON_SELECTED_CPO_DAILY", "false"),
    ("APPROVE_OZON_SELECTED_CPO_DAILY_WRITE", "false"),
)
UNSYNCED_SETTINGS = ("PYTHON_VERSION",)

CABINETS = (
    {"code": "rbh1", "display": "РБХ-1", "night": "20 0 * * *", "alert": "35 7 * * *"},
    {"code": "rbh2", "display": "РБХ-2", "night": "25 0 * * *", "alert": "40 7 * * *"},
)


def services():
    out = []
    for c in CABINETS:
        for kind, name, schedule, cmd in (("night", f"mp-analytics-{c['code']}", c["night"], NIGHT_COMMAND),
                                          ("alert", f"mp-analytics-{c['code']}-telegram-report", c["alert"], ALERT_COMMAND)):
            out.append({"name": name, "kind": kind, "cabinet": c["code"], "schedule": schedule, "startCommand": cmd, "group": c["code"]})
    return out


def groups():
    return [{"name": c["code"], "secrets": SECRET_KEYS, "settings": SETTING_VALUES, "unsynced": UNSYNCED_SETTINGS} for c in CABINETS]


def render():
    lines = ["# render.yaml — четыре cron-сервиса РБХ (RBH §5.5). Файл генерируется scripts/render_blueprint.py — править там, затем --write.",
             "# Сервисы основного кабинета (mp-analytics, mp-analytics-telegram-report) в Blueprint не входят: их конфигурация — в панели Render.",
             "# Секреты (sync: false) владелец вводит в Dashboard при создании Blueprint; в git значений нет.",
             "services:"]
    for s in services():
        c = next(x for x in CABINETS if x["code"] == s["cabinet"])
        what = "ночной прогон" if s["kind"] == "night" else "утренний алерт в Telegram (+ книга месяца)"
        lines += [f"  - type: cron",
                  f"    name: {s['name']}",
                  f"    # {c['display']}: {what}; расписание со сдвигом +5 / +10 мин от основного кабинета ({'00:15' if s['kind'] == 'night' else '07:30'} UTC)",
                  f"    runtime: python",
                  f"    region: {REGION}",
                  f"    plan: {PLAN}",
                  f"    branch: main",
                  f"    autoDeployTrigger: commit",
                  f"    buildCommand: {BUILD_COMMAND}",
                  f"    schedule: \"{s['schedule']}\"",
                  f"    startCommand: {s['startCommand']}",
                  f"    envVars:",
                  f"      - key: MP_CABINET",
                  f"        value: {s['cabinet']}",
                  f"      - fromGroup: {s['group']}"]
    lines.append("envVarGroups:")
    for g in groups():
        lines += [f"  - name: {g['name']}",
                  f"    envVars:"]
        for k in g["secrets"]:
            lines += [f"      - key: {k}", f"        sync: false"]
        for k in g["unsynced"]:
            lines += [f"      - key: {k}", f"        sync: false   # как у основного кабинета (снять с панели Render)"]
        for k, v in g["settings"]:
            lines += [f"      - key: {k}", f"        value: \"{v}\""]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    text = render()
    if args.write:
        open(OUT, "w", encoding="utf-8").write(text)
        print(f"render.yaml записан: {len(text.splitlines())} строк, сервисов {len(services())}, групп {len(groups())}")
        return 0
    if args.check:
        on_disk = open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
        print("render.yaml = генератору" if on_disk == text else "render.yaml ОТЛИЧАЕТСЯ от генератора — scripts/render_blueprint.py --write")
        return 0 if on_disk == text else 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
