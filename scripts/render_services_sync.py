"""Сервисы РБХ на Render через API — вместо Blueprint руками (первая задача RBH, дополнение советника 2026-10-21, п. 3).

    venv/bin/python3 scripts/render_services_sync.py --plan            состав без единого обращения: имена, расписания, команды,
                                                                        план / регион, список ключей и откуда значение; db_writes 0
    venv/bin/python3 scripts/render_services_sync.py --check           + GET списка сервисов владельца: какие уже есть (только чтение)
    venv/bin/python3 scripts/render_services_sync.py --apply           по слову владельца: создать недостающие сервисы ПРИОСТАНОВЛЕННЫМИ
    venv/bin/python3 scripts/render_services_sync.py --apply --only mp-analytics-rbh1   один сервис

Состав — ровно из scripts/render_blueprint.py (тот же, что в render.yaml): четыре cron-сервиса, тот же репозиторий и ветка main,
autoDeployTrigger commit, команды, расписания, plan / region / runtime как у основного кабинета (сняты по API 21.10), переменные —
имена из блюпринта, значения: MP_CABINET и флаги Selected CPO — из блюпринта, остальное — из .env.rbh<N> (service_role, ключи Ozon /
WB, Telegram). Секрет пустой или отсутствует → отказ до первого обращения с именами ключей. Значения никуда не печатаются: в плане —
имя ключа, источник и длина.

Render API (api-docs.render.com/reference/create-service, прочитано 21.10): POST /v1/services с type cron_job, serviceDetails
{runtime, schedule, plan, region, envSpecificDetails{buildCommand, startCommand}}, envVars [{key, value}]; создание сервиса само по
себе не ставит паузу, поэтому сразу после 201 — POST /v1/services/{id}/suspend (Render ничего не запустит до «resume»). Снятие паузы —
отдельным словом после §5.4 и мержа (resume не делает ни один режим этого скрипта). Существующий сервис с таким именем не трогается
(ни PATCH, ни переменных) — печатается «уже есть» с его id. RENDER_API_KEY — из общего .env (как у чтения логов); ownerId — рабочее
пространство владельца (OWNER_ID, у основного кабинета тот же; --owner-id переопределяет). Обращений к API: --plan 0, --check 1,
--apply 1 + 2 на создаваемый сервис.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import cabinet  # noqa: E402
import render_blueprint as rb  # noqa: E402

API = "https://api.render.com/v1"
OWNER_ID = "tea-d7n5qs1f9bms738bfvug"      # рабочее пространство владельца: ownerId сервисов основного кабинета (GET /v1/services, 21.10)
SERVICE_TYPE = "cron_job"
# Без значения не блокируют создание: переменная не создаётся, а называется в плане. OPENAI_API_KEY читает только decision layer,
# выключенный в команде ночи (--skip-decision); у основного кабинета она на Render есть, у РБХ владелец добавит в панели при включении.
OPTIONAL_KEYS = ("OPENAI_API_KEY",)


class SyncRefused(RuntimeError):
    """Отказ до обращений: нет ключа Render, нет значений секретов, нет файла кабинета."""


def read_env_file(path):
    """Пары ключ → значение из .env-файла; пустые значения — как отсутствие (overlay кабинета их тоже не берёт)."""
    if not os.path.isfile(path):
        return None
    from dotenv import dotenv_values
    return {k: v for k, v in dotenv_values(path).items() if v not in (None, "")}


def service_values(service, cabinet_env, common_env=None):
    """Значения переменных сервиса: свои (MP_CABINET, флаги) из блюпринта, остальное из .env.rbh<N>, а чего там нет или пусто —
    из общего .env (то же правило, что у overlay кабинета: TELEGRAM_BOT_TOKEN общий). Возвращает (список {key, value} в порядке
    имён, источники {key: 'blueprint' | '.env.rbh<N>' | '.env'}, отсутствующие ключи)."""
    own = dict(service["own_values"])
    out, sources, missing = [], {}, []
    for key in rb.env_keys(service):
        if key in own:
            out.append({"key": key, "value": own[key]}); sources[key] = "blueprint"
        elif cabinet_env and cabinet_env.get(key):
            out.append({"key": key, "value": cabinet_env[key]}); sources[key] = f".env.{service['cabinet']}"
        elif common_env and common_env.get(key):
            out.append({"key": key, "value": common_env[key]}); sources[key] = ".env"
        elif key in OPTIONAL_KEYS:
            sources[key] = "пусто — не создаётся (необязательная)"
        else:
            missing.append(key)
    return out, sources, missing


def create_body(service, env_vars, owner_id=OWNER_ID):
    return {
        "type": SERVICE_TYPE,
        "name": service["name"],
        "ownerId": owner_id,
        "repo": rb.REPO,
        "branch": rb.BRANCH,
        "autoDeployTrigger": "commit",
        "rootDir": "",
        "envVars": env_vars,
        "serviceDetails": {
            "runtime": service["runtime"],
            "schedule": service["schedule"],
            "plan": service["plan"],
            "region": service["region"],
            "envSpecificDetails": {"buildCommand": service["buildCommand"], "startCommand": service["startCommand"]},
        },
    }


def masked(body):
    """Тело запроса для печати: значения переменных заменены на длину."""
    shown = json.loads(json.dumps(body))
    shown["envVars"] = [{"key": e["key"], "value": f"<{len(str(e['value']))} символов>"} for e in body["envVars"]]
    return shown


def plan(root=ROOT, only=None):
    """Состав и источники значений для каждого сервиса, без обращений. Возвращает список записей; missing — чего нет в .env."""
    rows = []
    common_env = read_env_file(os.path.join(root, ".env")) or {}
    for s in rb.services():
        if only and s["name"] not in only:
            continue
        env_path = cabinet.overlay_path(s["cabinet"], root)
        cab_env = read_env_file(env_path)
        env_vars, sources, missing = service_values(s, cab_env, common_env)
        rows.append({"service": s, "env_vars": env_vars, "sources": sources, "missing": missing,
                     "env_file": os.path.basename(env_path), "env_file_exists": cab_env is not None})
    return rows


def print_plan(rows):
    print(f"Render, сервисы РБХ: {len(rows)} ({SERVICE_TYPE}), репозиторий {rb.REPO}, ветка {rb.BRANCH}, autoDeployTrigger commit, ownerId {OWNER_ID}")
    for r in rows:
        s = r["service"]
        print(f"\n- {s['name']} ({s['display']}, {s['kind']}): schedule «{s['schedule']}», plan {s['plan']}, region {s['region']}, runtime {s['runtime']}")
        print(f"    build: {s['buildCommand']}")
        print(f"    start: {s['startCommand']}")
        print(f"    переменных {len(r['env_vars'])} из {len(rb.env_keys(s))}: " + ", ".join(
            f"{e['key']} ← {r['sources'][e['key']]} ({len(str(e['value']))})" for e in r["env_vars"]))
        optional_empty = [k for k, src in r["sources"].items() if src.startswith("пусто")]
        if optional_empty:
            print(f"    · без значения, не создаются: {', '.join(optional_empty)} (см. OPTIONAL_KEYS)")
        if not r["env_file_exists"]:
            print(f"    ❌ файла {r['env_file']} нет")
        if r["missing"]:
            print(f"    ❌ нет значения в {r['env_file']}: {', '.join(r['missing'])}")
    bad = [r for r in rows if r["missing"] or not r["env_file_exists"]]
    print("\n" + ("✅ значения всех переменных на месте; создание — --apply по слову (сервисы встанут на паузу сразу после создания)"
                  if not bad else f"⛔ --apply откажет: у {len(bad)} сервисов нет значений"))
    return not bad


def api_key(root=ROOT):
    key = os.getenv("RENDER_API_KEY") or (read_env_file(os.path.join(root, ".env")) or {}).get("RENDER_API_KEY")
    if not key:
        raise SyncRefused("RENDER_API_KEY не задан (общий .env) — к Render не иду")
    return key


def list_services(session, key, owner_id=OWNER_ID):
    r = session.get(f"{API}/services", params={"type": SERVICE_TYPE, "ownerId": owner_id, "limit": 100},
                    headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"GET /v1/services: HTTP {r.status_code} {r.text[:200]}")
    return {item["service"]["name"]: item["service"] for item in r.json()}


def apply(rows, session, key, owner_id=OWNER_ID, existing=None):
    """Создать недостающие сервисы и сразу приостановить. Возвращает список исходов (name, action, id, suspended)."""
    bad = [r["service"]["name"] for r in rows if r["missing"] or not r["env_file_exists"]]
    if bad:
        raise SyncRefused("нет значений переменных у: " + ", ".join(bad) + " — ни одного обращения не сделано")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json", "Content-Type": "application/json"}
    existing = list_services(session, key, owner_id) if existing is None else existing
    outcomes = []
    for r in rows:
        s = r["service"]
        if s["name"] in existing:
            e = existing[s["name"]]
            outcomes.append({"name": s["name"], "action": "exists", "id": e.get("id"), "suspended": e.get("suspended")})
            continue
        body = create_body(s, r["env_vars"], owner_id)
        created = session.post(f"{API}/services", json=body, headers=headers, timeout=120)
        if created.status_code != 201:
            outcomes.append({"name": s["name"], "action": "create_failed", "status": created.status_code, "text": created.text[:300]})
            break   # дальше не идём: разбирать причину, повтор создаст дубль имени только после починки
        sid = created.json()["service"]["id"]
        paused = session.post(f"{API}/services/{sid}/suspend", headers=headers, timeout=60)
        outcomes.append({"name": s["name"], "action": "created", "id": sid,
                         "suspended": "suspended" if paused.status_code in (200, 202, 204) else f"suspend_failed HTTP {paused.status_code}"})
    return outcomes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true", help="состав и источники значений, 0 обращений")
    g.add_argument("--check", action="store_true", help="план + какие сервисы уже есть у владельца (1 GET)")
    g.add_argument("--apply", action="store_true", help="создать недостающие сервисы приостановленными (по слову)")
    ap.add_argument("--only", action="append", help="имя сервиса; можно повторять")
    ap.add_argument("--owner-id", default=OWNER_ID)
    args = ap.parse_args(argv)
    rows = plan(only=set(args.only) if args.only else None)
    ok = print_plan(rows)
    if args.plan:
        print("обращений к Render: 0; db_writes = 0")
        return 0
    import requests
    session = requests.Session()
    try:
        key = api_key()
        existing = list_services(session, key, args.owner_id)
    except (SyncRefused, RuntimeError) as exc:
        print(f"⛔ {exc}"); return 2
    print(f"\nУ владельца cron-сервисов: {len(existing)} — " + ", ".join(sorted(existing)))
    for r in rows:
        name = r["service"]["name"]
        print(f"  {name}: {'уже есть, id ' + existing[name]['id'] + ', ' + str(existing[name].get('suspended')) if name in existing else 'нет — будет создан'}")
    if args.check:
        print("обращений к Render: 1 (чтение); db_writes = 0")
        return 0
    if not ok:
        print("⛔ --apply: значений не хватает, обращений на запись 0"); return 2
    print("\nСоздание (по слову владельца): тела запросов без значений —")
    for r in rows:
        if r["service"]["name"] not in existing:
            print(json.dumps(masked(create_body(r["service"], r["env_vars"], args.owner_id)), ensure_ascii=False, indent=2))
    try:
        outcomes = apply(rows, session, key, args.owner_id, existing=existing)
    except SyncRefused as exc:
        print(f"⛔ {exc}"); return 2
    for o in outcomes:
        print(f"  {o['name']}: {o['action']}" + (f", id {o.get('id')}, {o.get('suspended')}" if o.get("id") else f" — {o.get('status')} {o.get('text', '')}"))
    created = [o for o in outcomes if o["action"] == "created"]
    print(f"обращений к Render: {1 + 2 * len(created) + (1 if any(o['action'] == 'create_failed' for o in outcomes) else 0)}; создано {len(created)}, "
          f"на паузе {sum(1 for o in created if o['suspended'] == 'suspended')}; снятие паузы — отдельным словом")
    return 0 if all(o["action"] in ("created", "exists") and o.get("suspended") in ("suspended", None) or o["action"] == "exists" for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
