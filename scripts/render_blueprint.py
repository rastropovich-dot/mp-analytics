"""render.yaml для четырёх cron-сервисов РБХ — источник правды здесь, файл генерируется (§5.5 первой задачи RBH, план 09-30, поправка советника 2:
сервисы KARATOV в Blueprint не входят — их конфигурация живёт в панели Render).

    venv/bin/python3 scripts/render_blueprint.py --write     перегенерировать render.yaml
    venv/bin/python3 scripts/render_blueprint.py --check     файл на диске = генератору (тест делает то же)

Четыре сервиса из одного репозитория и ветки main: ночь и утренний алерт на кабинет, расписание со сдвигом 5 минут от KARATOV (00:15 / 07:30 UTC),
чтобы три ночи не стартовали в одну секунду и утренние сообщения шли по порядку. Команда ночи = команда KARATOV (сверена с Render 09-28, 10-06 и
21.10 по API), `MP_CABINET` — в переменных сервиса; секреты — в группе окружения кабинета (`rbh1` / `rbh2`, `sync: false` → значения в git не лежат).

Параметры — ровно как у KARATOV, сняты по Render API 2026-10-21 (inbox_rbh, дополнение 21.10 п. 2): region `oregon`, runtime python, plan `standard`
у ночного и `starter` у утреннего, `PYTHON_VERSION` не задан (умолчание Render), переменных у ночного 13, у утреннего 11 — те же имена
(MAIN_NIGHT_ENV_KEYS / MAIN_ALERT_ENV_KEYS); `APP_TIMEZONE` и настроек `OZON_PERFORMANCE_*` (кроме STATE_BACKEND) на Render нет — код берёт умолчания.
Флаги Selected CPO у нового кабинета выключены до отдельного решения (ENABLE без APPROVE жгло бы квоту Performance впустую). PyYAML в окружении нет,
поэтому YAML пишется строками генератора, а тесты проверяют структуру по функциям и равенство файла генератору. Тот же состав читает
scripts/render_services_sync.py, который создаёт сервисы через API вместо Blueprint (дополнение 21.10 п. 3).
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
RUNTIME = "python"
REGION = "oregon"                 # KARATOV по API 21.10 (не frankfurt, как предполагал план 09-30)
PLAN_NIGHT = "standard"           # mp-analytics
PLAN_ALERT = "starter"            # mp-analytics-telegram-report
REPO = "https://github.com/rastropovich-dot/mp-analytics"
BRANCH = "main"

# Имена переменных сервисов основного кабинета (karatov), как их отдаёт GET /v1/services/{id}/env-vars 2026-10-21 (значения не снимались).
MAIN_NIGHT_ENV_KEYS = ("APPROVE_OZON_SELECTED_CPO_DAILY_WRITE", "ENABLE_OZON_SELECTED_CPO_DAILY", "OPENAI_API_KEY", "OZON_API_KEY",
                          "OZON_CLIENT_ID", "OZON_PERFORMANCE_CLIENT_ID", "OZON_PERFORMANCE_CLIENT_SECRET", "OZON_PERFORMANCE_STATE_BACKEND",
                          "SUPABASE_SERVICE_KEY", "SUPABASE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WB_API_KEY")
MAIN_ALERT_ENV_KEYS = tuple(k for k in MAIN_NIGHT_ENV_KEYS if not k.endswith("SELECTED_CPO_DAILY") and not k.endswith("SELECTED_CPO_DAILY_WRITE"))

# Секреты кабинета — из .env.rbh<N> (sync: false в Blueprint; sync-скрипт берёт значения из файла; в git и отчётах значений нет).
SECRET_KEYS = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "OZON_CLIENT_ID", "OZON_API_KEY", "OZON_PERFORMANCE_CLIENT_ID",
               "OZON_PERFORMANCE_CLIENT_SECRET", "WB_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OPENAI_API_KEY")
# Несекретные настройки группы (у обоих сервисов кабинета) — как у KARATOV.
SETTING_VALUES = (("OZON_PERFORMANCE_STATE_BACKEND", "db"),)
# Только у ночного сервиса — как у KARATOV; значения false до отдельного решения владельца.
NIGHT_ONLY_VALUES = (("ENABLE_OZON_SELECTED_CPO_DAILY", "false"), ("APPROVE_OZON_SELECTED_CPO_DAILY_WRITE", "false"))

CABINETS = (
    {"code": "rbh1", "display": "РБХ-1", "night": "20 0 * * *", "alert": "35 7 * * *"},
    {"code": "rbh2", "display": "РБХ-2", "night": "25 0 * * *", "alert": "40 7 * * *"},
)


def services():
    out = []
    for c in CABINETS:
        for kind, name, schedule, cmd, plan in (("night", f"mp-analytics-{c['code']}", c["night"], NIGHT_COMMAND, PLAN_NIGHT),
                                                ("alert", f"mp-analytics-{c['code']}-telegram-report", c["alert"], ALERT_COMMAND, PLAN_ALERT)):
            out.append({"name": name, "kind": kind, "cabinet": c["code"], "display": c["display"], "schedule": schedule, "startCommand": cmd,
                        "buildCommand": BUILD_COMMAND, "plan": plan, "region": REGION, "runtime": RUNTIME, "group": c["code"],
                        "own_values": (("MP_CABINET", c["code"]),) + (NIGHT_ONLY_VALUES if kind == "night" else ())})
    return out


def groups():
    return [{"name": c["code"], "secrets": SECRET_KEYS, "settings": SETTING_VALUES} for c in CABINETS]


def env_keys(service):
    """Полный список имён переменных сервиса (группа + свои), отсортированный — для сверки с KARATOV и для sync-скрипта."""
    grp = next(g for g in groups() if g["name"] == service["group"])
    return tuple(sorted(set(grp["secrets"]) | {k for k, _v in grp["settings"]} | {k for k, _v in service["own_values"]}))


def render():
    lines = ["# render.yaml — четыре cron-сервиса РБХ (RBH §5.5). Файл генерируется scripts/render_blueprint.py — править там, затем --write.",
             "# Сервисы основного кабинета (mp-analytics, mp-analytics-telegram-report) в Blueprint не входят: их конфигурация — в панели Render.",
             "# Параметры (region, plan, runtime, состав переменных) = сервисам основного кабинета по Render API 2026-10-21. Секреты (sync: false) в git не лежат;",
             "# сервисы создаёт scripts/render_services_sync.py через API по этому же составу (Blueprint руками не нужен).",
             "services:"]
    for s in services():
        what = "ночной прогон" if s["kind"] == "night" else "утренний алерт в Telegram (+ книга месяца)"
        lines += [f"  - type: cron",
                  f"    name: {s['name']}",
                  f"    # {s['display']}: {what}; расписание со сдвигом +5 / +10 мин от основного кабинета ({'00:15' if s['kind'] == 'night' else '07:30'} UTC)",
                  f"    runtime: {s['runtime']}",
                  f"    region: {s['region']}",
                  f"    plan: {s['plan']}",
                  f"    branch: {BRANCH}",
                  f"    autoDeployTrigger: commit",
                  f"    buildCommand: {s['buildCommand']}",
                  f"    schedule: \"{s['schedule']}\"",
                  f"    startCommand: {s['startCommand']}",
                  f"    envVars:"]
        for k, v in s["own_values"]:
            lines += [f"      - key: {k}", f"        value: {v}"]
        lines += [f"      - fromGroup: {s['group']}"]
    lines.append("envVarGroups:")
    for g in groups():
        lines += [f"  - name: {g['name']}",
                  f"    envVars:"]
        for k in g["secrets"]:
            lines += [f"      - key: {k}", f"        sync: false"]
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
