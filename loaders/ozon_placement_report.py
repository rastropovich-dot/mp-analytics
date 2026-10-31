#!/usr/bin/env python3
"""Хранение Ozon по товарам: отчёт ЛК «FBO → Стоимость размещения» через /v1/report/placement/by-products (пятьдесят вторая §2).

    python3 loaders/ozon_placement_report.py --apply                       ночной шаг: отчёт за вчера (МСК) → таблица; нефатален
    python3 loaders/ozon_placement_report.py --dry-run                     отчёт за вчера, разбор, план — без записи (Seller API: 3 обращения)
    python3 loaders/ozon_placement_report.py --file data/…/2026-10-20.xlsx --apply     разобрать сохранённый файл, API не зовётся
    python3 loaders/ozon_placement_report.py --date-from 2026-04-01 --date-to 2026-04-30 --apply   месяц одним отчётом (§3.2, по слову)

Путь (разведка §1, 2026-10-21): POST create {date_from, date_to} (период ≤ 31 дня, **не больше 5 отчётов в сутки на кабинет**) → ответ
{"code": …} на верхнем уровне → POST /v1/report/info {code} раз в POLL_SECONDS, не дольше MAX_WAIT_SECONDS (status «success»; «failed» /
«error» — отказ с текстом) → GET по ссылке `file` (ir-*.ozone.ru, живёт 3 ч; application/octet-stream; пустое тело — повтор один раз) →
xlsx: лист один, заголовок в первой строке, 12 колонок (COLUMNS, проверяются как есть — другой заголовок = стоп с перечнем), строка =
(дата, SKU, склад). Сырой файл — data/ozon_placement_raw/<date_from>[_<date_to>].xlsx. Запись: upsert по ключу (day, sku, warehouse),
amount со знаком леджера (−); повтор за тот же день даёт те же строки. Счётчик отчётов за сутки (UTC) — pipeline_runtime_state
`ozon_placement:quota`; итог шага — `ozon_placement:last`. Сверка: Σ файла за день против типа 46 леджера за тот же день (печать).
"""
import argparse
import io
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import requests

try:
    import cabinet
except ImportError:  # python3 loaders/<file>.py — корня проекта нет в sys.path
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import cabinet
try:
    from loaders import http_retry
except ImportError:
    import http_retry  # noqa: F401

BASE = "https://api-seller.ozon.ru"
POLL_SECONDS = 5
MAX_WAIT_SECONDS = 600
DAILY_LIMIT = 5
TABLE = "ozon_placement_by_product_daily"
LEDGER_TABLE = "ozon_accrual_daily_types"
PLACEMENT_TYPE_ID = 46
STATE_KEY = "ozon_placement:last"
QUOTA_KEY = "ozon_placement:quota"
MSK = ZoneInfo("Europe/Moscow")
COLUMNS = ["Дата", "SKU", "Артикул", "Категория товара", "Описательный тип", "Склад", "Признак товара", "Суммарный объем в миллилитрах",
           "Кол-во экземпляров", "Платный объем в миллилитрах", "Кол-во платных экземпляров", "Начисленная стоимость размещения"]
FIELDS = ["day", "sku", "offer_id", "category", "product_type", "warehouse", "product_flag", "volume_ml", "units", "paid_volume_ml", "paid_units", "amount"]
OUT_DIR = cabinet.data_path("ozon_placement_raw", prof=cabinet.profile())
BATCH = 500


def headers():
    return {"Client-Id": os.getenv("OZON_CLIENT_ID", ""), "Api-Key": os.getenv("OZON_API_KEY", ""), "Content-Type": "application/json"}


class ReportError(RuntimeError):
    """Именованный отказ пути отчёта (лимит, failed, таймаут, пустое тело, чужой заголовок)."""


# ---------------------------------------------------------------- API

def create_report(date_from, date_to, post):
    body = {"date_from": str(date_from), "date_to": str(date_to)}
    r = post("/v1/report/placement/by-products/create", body)
    if r.status_code != 200:
        raise ReportError(f"placement/by-products/create {body}: HTTP {r.status_code} {r.text[:200]}")
    data = r.json() or {}
    code = data.get("code") or (data.get("result") or {}).get("code")
    if not code:
        raise ReportError(f"placement/by-products/create: ответ без code: {json.dumps(data, ensure_ascii=False)[:200]}")
    return code, data


def wait_report(code, post, sleep_fn=time.sleep, clock=time.monotonic, max_wait=MAX_WAIT_SECONDS, poll=POLL_SECONDS):
    """→ (info, опросов, секунд). «success» / «done» — готов; «failed» / «error» — ReportError; дольше max_wait — ReportError."""
    started, polls, status, info = clock(), 0, None, {}
    while clock() - started < max_wait:
        polls += 1
        ri = post("/v1/report/info", {"code": code})
        if ri.status_code != 200:
            raise ReportError(f"report/info: HTTP {ri.status_code} {ri.text[:200]}")
        data = ri.json() or {}
        info = data.get("result", data) or {}
        status = str(info.get("status") or "")
        if status in ("success", "done"):
            return info, polls, round(clock() - started, 1)
        if status in ("failed", "error"):
            raise ReportError(f"report/info: status={status} error={info.get('error')!r}")
        sleep_fn(poll)
    raise ReportError(f"report/info: отчёт не готов за {max_wait} с (status={status}, опросов {polls})")


def download(url, get, sleep_fn=time.sleep):
    """GET по ссылке; тело пустое или HTTP ≠ 200 — одна повторная попытка (чат разработчиков, id 58320). → (bytes, попыток)."""
    last = None
    for attempt in (1, 2):
        r = get(url)
        if r.status_code == 200 and r.content:
            return r.content, attempt
        last = f"HTTP {r.status_code}, байт {len(r.content or b'')}"
        sleep_fn(3)
    raise ReportError(f"скачивание отчёта: {last} после 2 попыток")


# ---------------------------------------------------------------- файл

def _cell_date(v):
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v or "").strip()
    if len(s) >= 10 and s[4] == "-":
        return s[:10]
    if len(s) == 10 and s[2] == "." and s[5] == ".":
        return f"{s[6:10]}-{s[3:5]}-{s[0:2]}"
    raise ReportError(f"дата в файле не разобрана: {v!r}")


def parse_xlsx(content):
    """bytes xlsx → строки dict по FIELDS (amount — Decimal, положительный, как в файле); заголовок сверяется с COLUMNS как есть."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = [str(c).strip() if c is not None else "" for c in next(it, ())]
    header = header[:len(COLUMNS)] + [""] * (len(COLUMNS) - len(header))
    if header != COLUMNS:
        raise ReportError(f"заголовок файла не тот: {header} ≠ {COLUMNS}")
    rows = []
    for r in it:
        if not r or all(c is None or str(c).strip() == "" for c in r):
            continue
        r = list(r) + [None] * (len(COLUMNS) - len(r))
        rows.append({"day": _cell_date(r[0]), "sku": int(r[1]), "offer_id": str(r[2] or "").strip(), "category": str(r[3] or "").strip(),
                     "product_type": str(r[4] or "").strip(), "warehouse": str(r[5] or "").strip(), "product_flag": str(r[6] or "").strip(),
                     "volume_ml": int(r[7] or 0), "units": int(r[8] or 0), "paid_volume_ml": int(r[9] or 0), "paid_units": int(r[10] or 0),
                     "amount": Decimal(str(r[11] if r[11] not in (None, "") else 0)).quantize(Decimal("0.01"))})
    return rows


def to_db_rows(rows, report_code, loaded_at=None):
    """Строки таблицы: amount со знаком леджера (расход < 0); ключ (day, sku, warehouse) — дубли внутри файла складываются."""
    loaded_at = loaded_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = {}
    for r in rows:
        key = (r["day"], r["sku"], r["warehouse"])
        if key in out:
            for f in ("volume_ml", "units", "paid_volume_ml", "paid_units"):
                out[key][f] += r[f]
            out[key]["amount"] = str((Decimal(out[key]["amount"]) - r["amount"]).quantize(Decimal("0.01")))
            continue
        out[key] = {"day": r["day"], "sku": r["sku"], "warehouse": r["warehouse"], "offer_id": r["offer_id"], "category": r["category"],
                    "product_type": r["product_type"], "product_flag": r["product_flag"], "volume_ml": r["volume_ml"], "units": r["units"],
                    "paid_volume_ml": r["paid_volume_ml"], "paid_units": r["paid_units"], "amount": str((-r["amount"]).quantize(Decimal("0.01"))),
                    "report_code": report_code, "loaded_at": loaded_at}
    return list(out.values())


def summary(rows):
    """Сводка файла: по дням Σ начислений (положительно, как в файле), строк, SKU, складов, платных строк."""
    by_day = defaultdict(lambda: {"rows": 0, "paid_rows": 0, "amount": Decimal(0), "skus": set(), "warehouses": set()})
    for r in rows:
        d = by_day[r["day"]]
        d["rows"] += 1; d["amount"] += r["amount"]; d["skus"].add(r["sku"]); d["warehouses"].add(r["warehouse"])
        if r["amount"]:
            d["paid_rows"] += 1
    return {k: {"rows": v["rows"], "paid_rows": v["paid_rows"], "amount": v["amount"], "skus": len(v["skus"]), "warehouses": len(v["warehouses"])} for k, v in sorted(by_day.items())}


# ---------------------------------------------------------------- весь путь

def fetch_report(date_from, date_to, post=None, get=None, sleep_fn=time.sleep, clock=time.monotonic, save_dir=OUT_DIR, log=print,
                 create_post=None, create_once=False):
    """create → info → скачивание → разбор. → (rows, stats, content). Обращений: 1 + опросы + скачивания.

    create_once (56-я §4): create уходит ОДНОЙ попыткой, без повторов http_retry (1 / 10 / 10 с) — 24.10 повторы обёртки
    сами нарушили паузу между create и получили 8 отказов 429 «rate limit per second». Отказ → ReportError, следующий
    create — не раньше паузы вызывающего (история — ≥ 65 с). info и скачивание повторяются как прежде.
    """
    post = post or (lambda path, body: http_retry.post(f"{BASE}{path}", label=f"report{path}", headers=headers(), json=body, timeout=120))
    if create_post is None:
        create_post = post if not create_once else (lambda path, body: http_retry.post(
            f"{BASE}{path}", label=f"report{path}", headers=headers(), json=body, timeout=120, max_attempts=1))
    get = get or (lambda url: requests.get(url, timeout=300, allow_redirects=True))
    started = clock()
    stats = {"create": 1, "info": 0, "download": 0, "seconds": 0.0, "bytes": 0}
    code, _data = create_report(date_from, date_to, create_post)
    log(f"placement/by-products: create {date_from} … {date_to} → code …{str(code)[-12:]}")
    info, polls, waited = wait_report(code, post, sleep_fn=sleep_fn, clock=clock)
    stats["info"] = polls
    url = info.get("file")
    if not url:
        raise ReportError("report/info: success без ссылки file")
    content, attempts = download(url, get, sleep_fn=sleep_fn)
    stats["download"] = attempts
    stats["bytes"] = len(content)
    log(f"placement/by-products: info {polls} опрос(ов) за {waited} с, файл {len(content)} байт (попыток скачивания {attempts})")
    if save_dir:
        try:
            os.makedirs(save_dir, exist_ok=True)
            name = f"{date_from}.xlsx" if str(date_from) == str(date_to) else f"{date_from}_{date_to}.xlsx"
            with open(os.path.join(save_dir, name), "wb") as fh:
                fh.write(content)
            stats["saved"] = os.path.join(save_dir, name)
        except OSError as exc:
            log(f"сырой файл не сохранён: {exc}")
    rows = parse_xlsx(content)
    stats["rows"] = len(rows)
    stats["seconds"] = round(clock() - started, 1)
    stats["code"] = code
    return rows, stats, content


# ---------------------------------------------------------------- база

def upsert(sb, db_rows, tries=None, pause=None, sleep_fn=None):
    """upsert пачками по BATCH; пачка — через net_retry (транзиентный отказ прокси / обрыв — повтор той же пачки, а не всего файла:
    26.10 запись истории rbh2 трижды падала на 503 / сбросе потока посреди 168 тыс. строк). Ключ (day, sku, warehouse) — повтор безопасен."""
    try:
        from loaders import net_retry
    except ImportError:
        import net_retry
    kw = {k: v for k, v in (("tries", tries), ("pause", pause), ("sleep_fn", sleep_fn)) if v is not None}
    n = 0
    for i in range(0, len(db_rows), BATCH):
        chunk = db_rows[i:i + BATCH]
        net_retry.call(lambda c=chunk: sb.table(TABLE).upsert(c, on_conflict="day,sku,warehouse").execute(), f"{TABLE}: пачка {i // BATCH + 1}", **kw)
        n += len(chunk)
    return n


def ledger_46(sb, days):
    """{день: сумма типа 46 (знак Ozon)} за перечисленные дни; дня нет в леджере — нет ключа."""
    if not days:
        return {}
    data = sb.table(LEDGER_TABLE).select("accrual_date,amount").eq("type_id", PLACEMENT_TYPE_ID).in_("accrual_date", sorted(days)).execute().data or []
    return {str(r["accrual_date"])[:10]: Decimal(str(r["amount"])) for r in data}


def read_quota(sb):
    data = sb.table("pipeline_runtime_state").select("payload").eq("state_key", QUOTA_KEY).limit(1).execute().data or []
    return (data[0].get("payload") or {}) if data else {}


def bump_quota(payload, day_utc):
    """Счётчик create за сутки UTC: другой день — сброс. → новый payload."""
    payload = dict(payload or {})
    if payload.get("day") != day_utc:
        payload = {"day": day_utc, "creates": 0}
    payload["creates"] = int(payload.get("creates") or 0) + 1
    return payload


def write_state(sb, key, state_type, payload):
    sb.table("pipeline_runtime_state").upsert(
        {"state_key": key, "state_type": state_type, "account_signature": None, "payload": payload, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        on_conflict="state_key").execute()


def compare_text(file_by_day, ledger):
    lines = []
    for d, s in sorted(file_by_day.items()):
        led = ledger.get(d)
        if led is None:
            lines.append(f"  {d}: файл {s['amount']:,.2f}, тип 46 леджера за день ещё нет")
        else:
            diff = s["amount"] + led   # файл положительный, леджер отрицательный
            lines.append(f"  {d}: файл {s['amount']:,.2f}, тип 46 леджера {led:,.2f}, файл + леджер = {diff:+,.2f}" + (" — сошлось" if diff == 0 else " — РАЗНИЦА"))
    return lines


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="день отчёта YYYY-MM-DD (по умолчанию вчера по МСК)")
    ap.add_argument("--date-from"); ap.add_argument("--date-to")
    ap.add_argument("--file", help="сохранённый xlsx вместо запроса к API")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="писать таблицу, счётчик и итог")
    g.add_argument("--dry-run", action="store_true", help="ничего не писать (по умолчанию)")
    ap.add_argument("--no-state", action="store_true", help="не писать ozon_placement:last / quota")
    ap.add_argument("--create-once", action="store_true",
                    help="create одной попыткой, без повторов http_retry (история, 56-я §4); отказ — стоп")
    args = ap.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    prof = cabinet.assert_env()
    print(cabinet.banner(prof))
    if args.date_from or args.date_to:
        if not (args.date_from and args.date_to):
            raise SystemExit("--date-from и --date-to задаются вместе")
        d1, d2 = args.date_from, args.date_to
        if (date.fromisoformat(d2) - date.fromisoformat(d1)).days > 30 or d2 < d1:
            raise SystemExit("период отчёта — не больше 31 дня и date_to ≥ date_from")
    else:
        d1 = d2 = args.date or (datetime.now(MSK).date() - timedelta(days=1)).isoformat()
    apply = bool(args.apply)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sb = None
    if apply or not args.no_state:
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    rc, error, stats, rows = 0, None, {}, []
    quota_note = ""
    try:
        if args.file:
            with open(args.file, "rb") as fh:
                content = fh.read()
            rows = parse_xlsx(content)
            stats = {"create": 0, "info": 0, "download": 0, "bytes": len(content), "rows": len(rows), "code": None, "file": args.file}
            file_days = sorted({r["day"] for r in rows})
            if file_days:
                d1, d2 = file_days[0], file_days[-1]     # окно — по дням файла, не по «вчера»
            print(f"файл {args.file}: {len(content)} байт, строк {len(rows)}, дни {d1} … {d2}; Seller API — 0")
        else:
            quota = read_quota(sb) if sb is not None else {}
            today_utc = datetime.now(timezone.utc).date().isoformat()
            used = int(quota.get("creates") or 0) if quota.get("day") == today_utc else 0
            if used >= DAILY_LIMIT:
                raise ReportError(f"лимит отчётов за сутки исчерпан: {used} из {DAILY_LIMIT} (UTC {today_utc}) — create не отправлен")
            rows, stats, _content = fetch_report(d1, d2, create_once=args.create_once)
            if sb is not None and not args.no_state:
                new_quota = bump_quota(quota, today_utc)
                write_state(sb, QUOTA_KEY, "ozon_placement_quota", new_quota)
                quota_note = f"отчётов за сутки (UTC {today_utc}): {new_quota['creates']} из {DAILY_LIMIT}"
                print(quota_note)
        by_day = summary(rows)
        for d, s in by_day.items():
            print(f"  {d}: строк {s['rows']}, SKU {s['skus']}, складов {s['warehouses']}, платных строк {s['paid_rows']}, Σ начислений {s['amount']:,.2f}")
        db_rows = to_db_rows(rows, stats.get("code"))
        if sb is not None:
            for line in compare_text(by_day, ledger_46(sb, list(by_day))):
                print(line)
        if apply:
            n = upsert(sb, db_rows)
            print(f"✅ {TABLE}: upsert {n} строк за {d1} … {d2}")
        else:
            # счётчик ведёт настоящие create (лимит 5 в сутки на кабинет), поэтому dry-run с create его пишет — вслух (59-я, слово 28.10)
            print(f"dry-run: строк к upsert {len(db_rows)}; " + ("db_writes = 0 (кроме счётчика квоты)" if quota_note else "db_writes = 0"))
    except ReportError as exc:
        rc, error = 1, str(exc)
        print(f"❌ хранение по товарам: {error}")
    except Exception as exc:  # noqa: BLE001
        rc, error = 1, f"{type(exc).__name__}: {exc}"
        print(f"❌ хранение по товарам: {error}")
    payload = {"date_from": d1, "date_to": d2, "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "rows": len(rows), "amount": str(sum((r["amount"] for r in rows), Decimal(0))), "stats": {k: v for k, v in stats.items() if k != "code"},
               "quota": quota_note or None, "rc": rc, "error": error, "mode": "apply" if apply else "dry-run"}
    if apply and sb is not None and not args.no_state:
        try:
            write_state(sb, STATE_KEY, "ozon_placement", payload)
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ итог не записан в pipeline_runtime_state: {type(exc).__name__}: {exc}")
    print(f"{STATE_KEY} → {json.dumps(payload, ensure_ascii=False)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
