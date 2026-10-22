#!/usr/bin/env python3
"""Одна переменная окружения Render — из локального .env в cron-сервисы (WB-18, дополнение советника 22.10).

Зачем: `WB_API_KEY` на обоих сервисах Render протух 20.10 (exp в JWT), ночные шаги WB получают 401; владелец обновил
`~/mp-analytics/.env`. Скрипт переносит значение ключа из файла в переменные сервисов Render API
(`PUT /v1/services/{id}/env-vars/{key}`), `RENDER_API_KEY` берёт из `.env` текущего каталога (или --env).

Режимы: без --apply — план: сервис, ключ, длина и `exp` из JWT старого (из Render) и нового (из файла) значения —
**значений не печатает никогда**; `--apply` — запись (CLAUDE.md §2: Render не менять без подтверждения — только по слову
владельца), затем `GET env-vars` и `exp` после записи; `--wait-live N` — ждать до N секунд деплой, созданный после записи,
и его статус live (Render деплоит сервис при смене переменной). Выход: 0 — план / записано и подтверждено; 2 — расхождение
после записи или деплой не дождался.

    venv/bin/python3 scripts/render_env_sync.py --key WB_API_KEY --from ~/mp-analytics/.env            # план
    venv/bin/python3 scripts/render_env_sync.py --key WB_API_KEY --from ~/mp-analytics/.env --apply    # по слову
"""
import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

RENDER_API = "https://api.render.com/v1"
SERVICES = {"crn-d7n7nan7f7vs73fk70kg": "mp-analytics", "crn-d7t5ed1j2pic73aiqmog": "mp-analytics-telegram-report"}
TIMEOUT = 30


def read_env_value(path, key):
    """Значение KEY=… из .env (первое совпадение; кавычки по краям снимаются). Нет ключа — None."""
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(f"{key}="):
                value = line[len(key) + 1:].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                return value
    return None


def jwt_exp(token):
    """exp из payload JWT как ISO-дата UTC; не JWT или без exp — None. Значение токена наружу не отдаётся."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        exp = data.get("exp")
        return datetime.fromtimestamp(int(exp), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if exp else None
    except Exception:  # noqa: BLE001 — любое «не JWT»
        return None


def describe(value):
    """Безопасное описание значения: длина и exp (если JWT); самого значения в выводе нет."""
    if value is None:
        return "нет"
    return f"len {len(value)}, exp {jwt_exp(value) or '—'}"


RETRIES, RETRY_PAUSE = 4, 5   # сеть машины мигает (VPN): TLS EOF / обрыв соединения — повтор, HTTP-коды не повторяются


def _call(fn, *a, **kw):
    """Повтор транзиентных сетевых отказов (SSL EOF, обрыв, таймаут) — RETRIES попыток с паузой; ответы HTTP не повторяются."""
    for attempt in range(1, RETRIES + 1):
        try:
            return fn(*a, **kw)
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt == RETRIES:
                raise
            print(f"  сеть: {type(exc).__name__} — повтор {attempt} из {RETRIES - 1} через {RETRY_PAUSE} с", flush=True)
            time.sleep(RETRY_PAUSE)


def _headers(api_key):
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json", "Content-Type": "application/json"}


def get_env_vars(service_id, api_key, http=None):
    http = http or requests
    """{key: value} переменных сервиса (страницы по cursor)."""
    out, cursor = {}, None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = _call(http.get, f"{RENDER_API}/services/{service_id}/env-vars", headers=_headers(api_key), params=params, timeout=TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"GET env-vars {service_id}: HTTP {r.status_code}")
        page = r.json() or []
        for item in page:
            ev = item.get("envVar") or {}
            if ev.get("key"):
                out[ev["key"]] = ev.get("value")
        if len(page) < 100:
            return out
        cursor = page[-1].get("cursor")
        if not cursor:
            return out


def put_env_var(service_id, key, value, api_key, http=None):
    http = http or requests
    r = _call(http.put, f"{RENDER_API}/services/{service_id}/env-vars/{key}", headers=_headers(api_key), data=json.dumps({"value": value}), timeout=TIMEOUT)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"PUT env-var {key} {service_id}: HTTP {r.status_code}")
    return True


def latest_deploys(service_id, api_key, http=None, limit=3):
    http = http or requests
    r = _call(http.get, f"{RENDER_API}/services/{service_id}/deploys", headers=_headers(api_key), params={"limit": limit}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"GET deploys {service_id}: HTTP {r.status_code}")
    return [d.get("deploy") or {} for d in (r.json() or [])]


def plan(services, key, new_value, api_key, http=None):
    """[(service_id, name, old_value, new_value)] — старое из Render, новое из файла; печать — только через describe."""
    rows = []
    for sid in services:
        current = get_env_vars(sid, api_key, http).get(key)
        rows.append((sid, SERVICES.get(sid, sid), current, new_value))
    return rows


def wait_live(service_id, api_key, started_at, seconds, http=None, sleep_fn=time.sleep, poll=15):
    """Ждать деплой, созданный не раньше started_at (ISO UTC), до статуса live. Возвращает (status, created_at, finished_at) или None."""
    deadline = time.time() + seconds
    while True:
        for d in latest_deploys(service_id, api_key, http):
            if (d.get("createdAt") or "") >= started_at:
                if d.get("status") == "live":
                    return ("live", d.get("createdAt"), d.get("finishedAt"))
                if d.get("status") in ("build_failed", "update_failed", "canceled", "deactivated"):
                    return (d.get("status"), d.get("createdAt"), d.get("finishedAt"))
        if time.time() >= deadline:
            return None
        sleep_fn(poll)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--key", required=True, help="имя переменной, например WB_API_KEY")
    ap.add_argument("--from", dest="src", required=True, help=".env с новым значением (например ~/mp-analytics/.env)")
    ap.add_argument("--services", default=",".join(SERVICES), help="id сервисов Render через запятую (по умолчанию оба cron-сервиса)")
    ap.add_argument("--env", default=".env", help=".env с RENDER_API_KEY (по умолчанию ./.env)")
    ap.add_argument("--apply", action="store_true", help="записать (по слову владельца); без флага — план")
    ap.add_argument("--wait-live", type=int, default=120, help="с --apply: ждать деплой, созданный после записи, до N секунд (0 — не ждать); "
                    "у cron-сервисов смена переменной деплоя не создаёт (проверено 22.10) — отсутствие деплоя не отказ, переменная берётся следующим запуском")
    args = ap.parse_args(argv)
    api_key = read_env_value(args.env, "RENDER_API_KEY")
    if not api_key:
        print(f"RENDER_API_KEY не найден в {args.env} — стоп"); return 2
    new_value = read_env_value(args.src, args.key)
    if not new_value:
        print(f"{args.key} не найден в {args.src} — стоп"); return 2
    services = [s.strip() for s in args.services.split(",") if s.strip()]
    print(f"Render env sync: ключ {args.key}, новое значение из {args.src}: {describe(new_value)}; сервисов {len(services)}; "
          f"{'ЗАПИСЬ' if args.apply else 'план, ничего не пишется'}", flush=True)
    rows = plan(services, args.key, new_value, api_key)
    same = 0
    for sid, name, old, new in rows:
        state = "совпадает с файлом" if old == new else ("в Render нет" if old is None else "отличается")
        print(f"  {name} ({sid}): сейчас {describe(old)} — {state}", flush=True)
        same += old == new
    if not args.apply:
        print("план: без --apply ничего не пишется"); return 0
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    written = []
    for sid, name, old, new in rows:
        if old == new:
            print(f"  {name}: уже совпадает — не пишу"); continue
        put_env_var(sid, args.key, new, api_key)
        written.append(sid)
        print(f"  {name}: записано {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}", flush=True)
    bad = 0
    for sid, name, _old, new in rows:
        after = get_env_vars(sid, api_key).get(args.key)
        ok = after == new
        bad += not ok
        print(f"  {name}: после записи {describe(after)} — {'= файлу' if ok else 'НЕ совпадает с файлом'}", flush=True)
    if args.wait_live and written:
        for sid in written:
            res = wait_live(sid, api_key, started_at, args.wait_live)
            if res is None:
                print(f"  {SERVICES.get(sid, sid)}: деплоя после записи нет за {args.wait_live} с — у cron-сервиса переменная применится следующим запуском (прежний деплой остаётся live)")
            elif res[0] != "live":
                print(f"  {SERVICES.get(sid, sid)}: деплой {res[0]}, создан {res[1]} — НЕ live"); bad += 1
            else:
                print(f"  {SERVICES.get(sid, sid)}: деплой {res[0]}, создан {res[1]}, завершён {res[2]}", flush=True)
    print(f"записано сервисов: {len(written)}; расхождений после записи: {bad}")
    return 2 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
