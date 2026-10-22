#!/usr/bin/env python3
"""Платное хранение WB по артикулам и дням (WB-18 §2): отчёт seller-analytics-api `/api/v1/paid_storage` → wb_paid_storage_daily.

Отчёт — задание на генерацию (одно задание ≤ 8 дней, лимит 1 запрос в минуту) → статус (1 запрос в 5 с; new / processing / done /
purged / canceled) → скачивание (1 запрос в минуту): строки «дата × nmId × chrtId × склад × поставка × способ расчёта × баркод» с суммой
хранения за день (warehousePrice). Разведка 2026-10-15 (окно 10-07 … 10-14, 35 021 строка): Σ warehousePrice по дням = paid_storage строки
«Хранение» ежедневного отчёта реализации за ту же дату до копейки (расхождения ±0,01 на 3 днях из 8 — округление), без НДС-сдвига;
естественного уникального ключа у строк НЕТ — на зерне (date, nmId, chrtId, officeId, giId, calcType, barcode) 823 ключа с повторами
(строки одного ключа отличаются warehousePrice / barcodesCount — тарифные части), 261 строка — полные дубли (все поля равны), и Σ дня
сходится с отчётом реализации ТОЛЬКО вместе с ними. Поэтому таблица хранит СВЁРТКУ по ключу (Σ warehouse_price, Σ barcodes_count,
Σ loyalty_discount, rows_count) — повтор окна даёт те же строки (идемпотентно), а сырьё — файлом.

Окно ночи: вчера − (days_back − 1) … вчера (8 дней; повтор дней — upsert, это и есть досбор). Сырьё — data/wb_paid_storage_raw/<от>_<до>.json
(ретенция RAW_RETENTION_DAYS; на Render диска нет — живёт внутри прогона). 429 — пауза SLEEP_SECONDS, до MAX_ATTEMPTS попыток, дальше отказ
с причиной; задание отклонено / не готово за STATUS_MAX_POLLS опросов — отказ. 204 «нет данных» — не отказ: строк 0, сказано вслух.

    python3 loaders/wb_paid_storage_loader.py                     ночной шаг: окно 8 дней, запись
    python3 loaders/wb_paid_storage_loader.py --dry-run            то же без записи (к API ходит; db_writes = 0)
    python3 loaders/wb_paid_storage_loader.py --from-file F.json   строки из сохранённого сырья, к API не ходит (--dry-run — и без записи)
    python3 loaders/wb_paid_storage_loader.py --date-from … --date-to …   явное окно (≤ 8 дней), для истории по слову
"""
import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(ROOT, ".env"))
import requests  # noqa: E402
import cabinet  # noqa: E402

CABINET = cabinet.assert_env()  # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента
WB_API_KEY = os.getenv("WB_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

HOST = "https://seller-analytics-api.wildberries.ru"
CREATE_URL = f"{HOST}/api/v1/paid_storage"
STATUS_URL = f"{HOST}/api/v1/paid_storage/tasks/{{task_id}}/status"
DOWNLOAD_URL = f"{HOST}/api/v1/paid_storage/tasks/{{task_id}}/download"
TABLE = "wb_paid_storage_daily"
KEY = ("day", "nm_id", "chrt_id", "office_id", "gi_id", "calc_type", "barcode")
DEFAULT_DAYS_BACK = 8          # максимум метода — 8 дней на отчёт
MAX_WINDOW_DAYS = 8
SLEEP_SECONDS = 65             # лимит создания и скачивания — 1 запрос в минуту
STATUS_POLL_SECONDS = 20       # лимит статуса — 1 запрос в 5 с; опрашиваем реже
STATUS_MAX_POLLS = 45          # ≈ 15 мин ожидания готовности
MAX_ATTEMPTS = 3               # 429 / таймаут / сеть на одном обращении
TIMEOUT = 300
RAW_DIR = cabinet.data_path("wb_paid_storage_raw", prof=CABINET)
RAW_RETENTION_DAYS = 30
TRANSIENT = (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError)   # обрыв тела ответа (IncompleteRead) — история 22.10: 6 окон из 21
REQUIRED = ("date", "nmId", "chrtId")
DONE, WAITING, FAILED = "done", ("new", "processing"), ("purged", "canceled")
RAW_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json$")


def _client():
    from supabase import create_client
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("нет SUPABASE_URL / SUPABASE_SERVICE_KEY в окружении")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _decimal_text(value, field, places=5):
    """Число → текст Decimal с places знаками; None / '' → None; не число — RuntimeError с именем поля (how-we-work: без молчаливого нуля)."""
    if value is None or value == "":
        return None
    try:
        return str(Decimal(str(value)).quantize(Decimal(1).scaleb(-places)))
    except InvalidOperation:
        raise RuntimeError(f"поле {field}: не число {value!r}")


def _date_text(value):
    """'YYYY-MM-DD' (первые 10 знаков значения), если это настоящая дата; иначе None."""
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _int(value, field, default=None):
    if value is None or value == "":
        if default is None:
            raise RuntimeError(f"поле {field}: пусто")
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RuntimeError(f"поле {field}: не целое {value!r}")


# ---------- API ----------

def _get(name, url, params, counters, sleep_fn=time.sleep, sleep_seconds=SLEEP_SECONDS):
    """Одно обращение с повторами: 429 / таймаут / сеть — пауза sleep_seconds, до MAX_ATTEMPTS раз; возвращает Response."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        counters["requests"] += 1
        try:
            resp = requests.get(url, headers={"Authorization": WB_API_KEY}, params=params, timeout=TIMEOUT)
        except TRANSIENT as error:
            counters["transient"] += 1
            print(f"WB paid_storage {name}: сеть/таймаут ({type(error).__name__}), попытка {attempt}/{MAX_ATTEMPTS}, пауза {sleep_seconds} с", flush=True)
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"WB paid_storage {name}: сеть не ожила за {MAX_ATTEMPTS} попыток ({type(error).__name__})")
            sleep_fn(sleep_seconds)
            continue
        if resp.status_code == 429:
            counters["429"] += 1
            print(f"WB paid_storage {name}: 429 (попытка {attempt}/{MAX_ATTEMPTS}), пауза {sleep_seconds} с", flush=True)
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"WB paid_storage {name}: 429 не изжит за {MAX_ATTEMPTS} попыток")
            sleep_fn(sleep_seconds)
            continue
        return resp
    raise RuntimeError(f"WB paid_storage {name}: без ответа")   # pragma: no cover


def create_task(date_from, date_to, counters, sleep_fn=time.sleep):
    resp = _get("create", CREATE_URL, {"dateFrom": date_from, "dateTo": date_to}, counters, sleep_fn)
    if resp.status_code != 200:
        raise RuntimeError(f"WB paid_storage create: HTTP {resp.status_code} {resp.text[:200]}")
    task_id = (resp.json().get("data") or {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"WB paid_storage create: в ответе нет taskId: {resp.text[:200]}")
    print(f"WB paid_storage: задание {task_id} за {date_from} … {date_to}", flush=True)
    return task_id


def wait_done(task_id, counters, sleep_fn=time.sleep, poll_seconds=STATUS_POLL_SECONDS, max_polls=STATUS_MAX_POLLS):
    """Ждём статус done; purged / canceled — отказ; max_polls опросов без done — отказ с последним статусом."""
    status = None
    for i in range(max_polls):
        sleep_fn(5 if i == 0 else poll_seconds)
        resp = _get(f"status#{i + 1}", STATUS_URL.format(task_id=task_id), None, counters, sleep_fn)
        if resp.status_code != 200:
            raise RuntimeError(f"WB paid_storage status: HTTP {resp.status_code} {resp.text[:200]}")
        status = (resp.json().get("data") or {}).get("status")
        if status == DONE:
            print(f"WB paid_storage: отчёт готов после {i + 1} опрос(ов)", flush=True)
            return status
        if status in FAILED:
            raise RuntimeError(f"WB paid_storage: задание {task_id} — статус {status}")
        if status not in WAITING:
            raise RuntimeError(f"WB paid_storage: незнакомый статус {status!r} у задания {task_id}")
    raise RuntimeError(f"WB paid_storage: отчёт не готов за {max_polls} опросов (последний статус {status})")


def download(task_id, counters, sleep_fn=time.sleep):
    """Строки отчёта; 204 — пустой список (нет данных за окно)."""
    resp = _get("download", DOWNLOAD_URL.format(task_id=task_id), None, counters, sleep_fn)
    if resp.status_code == 204:
        print("WB paid_storage: 204 — нет данных за окно", flush=True)
        return []
    if resp.status_code != 200:
        raise RuntimeError(f"WB paid_storage download: HTTP {resp.status_code} {resp.text[:200]}")
    items = resp.json()
    if not isinstance(items, list):
        raise RuntimeError(f"WB paid_storage download: ожидался список, пришло {type(items).__name__}")
    return items


def fetch_window(date_from, date_to, counters, sleep_fn=time.sleep):
    task_id = create_task(date_from, date_to, counters, sleep_fn)
    wait_done(task_id, counters, sleep_fn)
    return task_id, download(task_id, counters, sleep_fn)


# ---------- строки ----------

def build_rows(items, observed_at, task_id=None, date_from=None, date_to=None):
    """Свёртка строк отчёта по KEY: Σ warehouse_price / barcodes_count / loyalty_discount, rows_count; атрибуты — от первой строки ключа.
    Нет обязательного поля или строка вне окна — RuntimeError (вслух, не молча)."""
    acc = {}
    for i, it in enumerate(items):
        for f in REQUIRED:
            if it.get(f) in (None, ""):
                raise RuntimeError(f"строка {i}: нет поля {f}: {json.dumps(it, ensure_ascii=False)[:200]}")
        day = _date_text(it["date"])
        if day is None:
            raise RuntimeError(f"строка {i}: дата не разобрана {it['date']!r}")
        if (date_from and day < date_from) or (date_to and day > date_to):
            raise RuntimeError(f"строка {i}: дата {day} вне окна {date_from} … {date_to}")
        key = (day, _int(it["nmId"], "nmId"), _int(it["chrtId"], "chrtId"), _int(it.get("officeId"), "officeId", 0), _int(it.get("giId"), "giId", 0),
               str(it.get("calcType") or ""), str(it.get("barcode") or ""))
        price = Decimal(_decimal_text(it.get("warehousePrice"), "warehousePrice") or "0")
        loyalty = Decimal(_decimal_text(it.get("loyaltyDiscount"), "loyaltyDiscount") or "0")
        cnt = _int(it.get("barcodesCount"), "barcodesCount", 0)
        a = acc.get(key)
        if a is None:
            a = acc[key] = {"day": key[0], "nm_id": key[1], "chrt_id": key[2], "office_id": key[3], "gi_id": key[4], "calc_type": key[5], "barcode": key[6],
                            "size": str(it.get("size") or "") or None, "vendor_code": str(it.get("vendorCode") or "") or None,
                            "subject": str(it.get("subject") or "") or None, "brand": str(it.get("brand") or "") or None,
                            "warehouse": str(it.get("warehouse") or "") or None, "volume": _decimal_text(it.get("volume"), "volume", 3),
                            "warehouse_coef": _decimal_text(it.get("warehouseCoef"), "warehouseCoef", 4),
                            "log_warehouse_coef": _decimal_text(it.get("logWarehouseCoef"), "logWarehouseCoef", 4),
                            "warehouse_price": Decimal(0), "barcodes_count": 0, "loyalty_discount": Decimal(0), "rows_count": 0,
                            "original_date": _date_text(it.get("originalDate")), "tariff_fix_date": _date_text(it.get("tariffFixDate")),
                            "tariff_lower_date": _date_text(it.get("tariffLowerDate")), "task_id": task_id, "observed_at": observed_at}
        a["warehouse_price"] += price
        a["loyalty_discount"] += loyalty
        a["barcodes_count"] += cnt
        a["rows_count"] += 1
    rows = []
    for a in acc.values():
        r = dict(a)
        r["warehouse_price"] = str(r["warehouse_price"].quantize(Decimal("0.00001")))
        r["loyalty_discount"] = str(r["loyalty_discount"].quantize(Decimal("0.00001")))
        rows.append(r)
    rows.sort(key=lambda r: tuple(r[k] for k in KEY))
    return rows


def summarize(rows):
    """{день: (Σ хранения, nmId, строк)} и общие итоги."""
    by_day = defaultdict(lambda: [Decimal(0), set(), 0])
    for r in rows:
        s = by_day[r["day"]]
        s[0] += Decimal(r["warehouse_price"]); s[1].add(r["nm_id"]); s[2] += 1
    return {d: (v[0], len(v[1]), v[2]) for d, v in sorted(by_day.items())}


def print_summary(rows, items_count):
    by_day = summarize(rows)
    total = sum((v[0] for v in by_day.values()), Decimal(0))
    nm = len({r["nm_id"] for r in rows})
    print(f"WB paid_storage: строк сырья {items_count}, свёрнуто в {len(rows)} строк по ключу {'×'.join(KEY)}; nmId {nm}; Σ хранения {total:,.2f}", flush=True)
    for d, (s, n, k) in by_day.items():
        print(f"  {d}: {s:,.2f} ₽, nmId {n}, строк {k}", flush=True)
    return total


# ---------- сырьё ----------

def raw_path(date_from, date_to, raw_dir=RAW_DIR):
    return os.path.join(raw_dir, f"{date_from}_{date_to}.json")


def save_raw(date_from, date_to, task_id, items, raw_dir=RAW_DIR, downloaded_at=None):
    os.makedirs(raw_dir, exist_ok=True)
    path = raw_path(date_from, date_to, raw_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"taskId": task_id, "dateFrom": date_from, "dateTo": date_to,
                   "downloaded_at": downloaded_at or datetime.now(timezone.utc).isoformat(timespec="seconds"), "rows": items}, f, ensure_ascii=False)
    return path


def load_raw(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {"taskId": None, "dateFrom": None, "dateTo": None, "rows": data}
    return data


def cleanup_raw(today, raw_dir=RAW_DIR, keep_days=RAW_RETENTION_DAYS, apply=True):
    """Файлы <от>_<до>.json, у которых «до» старше keep_days дней, — убрать; список печатается до удаления."""
    if not os.path.isdir(raw_dir):
        return []
    border = (today - timedelta(days=keep_days)).isoformat()
    victims = []
    for name in sorted(os.listdir(raw_dir)):
        m = RAW_NAME_RE.match(name)
        if m and m.group(2) < border:
            victims.append(name)
    if victims:
        print(f"WB paid_storage: сырьё старше {keep_days} дн. ({'удаляю' if apply else 'план'}): {', '.join(victims)}", flush=True)
        if apply:
            for name in victims:
                os.remove(os.path.join(raw_dir, name))
    return victims


# ---------- база ----------

def upsert_rows(sb, rows, batch=500):
    written = 0
    for i in range(0, len(rows), batch):
        sb.table(TABLE).upsert(rows[i:i + batch], on_conflict=",".join(KEY)).execute()
        written += len(rows[i:i + batch])
    return written


def window(today, days_back=DEFAULT_DAYS_BACK):
    date_to = today - timedelta(days=1)
    return (date_to - timedelta(days=days_back - 1)).isoformat(), date_to.isoformat()


def run(sb, days_back=DEFAULT_DAYS_BACK, today=None, dry_run=False, sleep_fn=time.sleep, raw_dir=RAW_DIR, from_file=None,
        date_from=None, date_to=None, keep_raw=False):
    """Сбор окна. Паспорт: window, task_id, items, rows, written, requests, 429, transient, by_day, total, raw."""
    today = today or date.today()
    if date_from and date_to:
        d1, d2 = date_from, date_to
    else:
        d1, d2 = window(today, days_back)
    if (date.fromisoformat(d2) - date.fromisoformat(d1)).days + 1 > MAX_WINDOW_DAYS:
        raise RuntimeError(f"окно {d1} … {d2} длиннее {MAX_WINDOW_DAYS} дней — лимит метода")
    counters = {"requests": 0, "429": 0, "transient": 0}
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if from_file:
        raw = load_raw(from_file)
        task_id, items = raw.get("taskId"), raw["rows"]
        d1, d2 = raw.get("dateFrom") or d1, raw.get("dateTo") or d2   # окно — из файла, не из календаря
        raw_file = from_file
    print(f"WB платное хранение: окно {d1} … {d2} ({(date.fromisoformat(d2) - date.fromisoformat(d1)).days + 1} дн.), "
          f"{'из файла ' + from_file if from_file else 'задание → статус → скачивание'}, {'dry-run' if dry_run else 'запись'}", flush=True)
    if not from_file:
        task_id, items = fetch_window(d1, d2, counters, sleep_fn)
        raw_file = save_raw(d1, d2, task_id, items, raw_dir)
        print(f"сырьё → {raw_file} ({os.path.getsize(raw_file):,} байт)", flush=True)
        if not keep_raw:
            cleanup_raw(today, raw_dir)   # история по слову (--keep-raw): окна старше ретенции не стирать сразу после сохранения
    print(f"обращений {counters['requests']}, 429 — {counters['429']}, сетевых отказов {counters['transient']}", flush=True)
    rows = build_rows(items, observed_at, task_id, d1, d2)
    total = print_summary(rows, len(items))
    written = 0
    if rows and not dry_run:
        written = upsert_rows(sb, rows)
        print(f"✅ {TABLE}: upsert {written} строк", flush=True)
    elif not rows:
        print(f"{TABLE}: строк нет — писать нечего", flush=True)
    print(f"db_writes = {written}", flush=True)
    return {"window": (d1, d2), "task_id": task_id, "items": len(items), "rows": len(rows), "written": written, "requests": counters["requests"],
            "429": counters["429"], "transient": counters["transient"], "by_day": summarize(rows), "total": total, "raw": raw_file}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK, help="окно: вчера − (N − 1) … вчера; ≤ 8")
    ap.add_argument("--date-from"); ap.add_argument("--date-to", help="явное окно ≤ 8 дней (история по слову)")
    ap.add_argument("--from-file", help="строки из сохранённого сырья, к API не ходить")
    ap.add_argument("--dry-run", action="store_true", help="не писать в базу (db_writes = 0)")
    ap.add_argument("--keep-raw", action="store_true", help="не чистить сырьё по ретенции (история по слову: окна старше 30 дней иначе стёрлись бы сразу)")
    args = ap.parse_args(argv)
    if not args.from_file and not WB_API_KEY:
        raise SystemExit("нет WB_API_KEY в окружении")
    sb = None if args.dry_run else _client()
    passport = run(sb, days_back=args.days_back, dry_run=args.dry_run, from_file=args.from_file, date_from=args.date_from, date_to=args.date_to, keep_raw=args.keep_raw)
    return 0 if passport["items"] >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
