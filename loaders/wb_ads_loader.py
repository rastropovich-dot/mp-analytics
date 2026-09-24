#!/usr/bin/env python3
"""Реклама WB: фактические списания advert-api GET /adv/v1/upd → wb_ad_spend_daily.

    python3 loaders/wb_ads_loader.py             окно 31 день до вчера, одно обращение, upsert
    python3 loaders/wb_ads_loader.py --dry-run   то же без записи (db_writes = 0)

ЗЕРНО — одно списание: (advertId, updTime). Проверено на сырье 02-01…09-22 (33 781 строка,
WB-6 §3а): пара (advertId, updTime) уникальна (повторов 0), а updNum — нет (37 различных на
33 781 строку, 5 610 нулей), поэтому «номер документа» ключом быть не может. День списания
(upd_day) — updTime в московском времени: так Σ / 1,22 сходится с листом владельца до рубля
(июль 1 694 177,87 против 1 694 178; август 1…17 — 835 968,03 против 835 968). updSum — с НДС.

МЕТОД: интервал ≤ 31 дня, лимит 1 запрос/с (спека 08-promotion.yaml). Окно ночи — 31 день
до вчера: одно обращение; 429 / 5xx / таймаут / сеть — 3 попытки по 65 с, затем именованный
отказ. Списание задним числом не исчезает (спека этого не обещает, но и удаления в сырье
не наблюдалось) — застрявшие ключи здесь не чистятся; если появятся — отдельным решением.
Дубль пары (advertId, updTime) в ответе или строка без advertId / updTime / updSum — отказ
до записи, не молчаливый пропуск.

Шаг «WB: реклама» (с 09-23, по слову). С WB-8 §3 тем же шагом после списаний идёт статистика по
номенклатурам — loaders/wb_ads_nm_loader.py (fullstats по кампаниям со списаниями окна; нет списаний — 0 обращений).
"""
import argparse
import os
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

URL = "https://advert-api.wildberries.ru/adv/v1/upd"
TABLE = "wb_ad_spend_daily"
DEFAULT_DAYS_BACK = 31
MAX_INTERVAL_DAYS = 31
MAX_ATTEMPTS = 3
RETRY_SECONDS = 65
TIMEOUT = 120
MSK = timezone(timedelta(hours=3))
WB_API_KEY = os.getenv("WB_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
TRANSIENT = (requests.Timeout, requests.ConnectionError)

FIELDS = (
    ("updNum", "upd_num", "i"), ("updSum", "upd_sum", "n"), ("campName", "camp_name", "s"), ("advertType", "advert_type", "i"),
    ("paymentType", "payment_type", "s"), ("advertStatus", "advert_status", "i"), ("currency", "currency", "s"),
)
REQUIRED = ("advertId", "updTime", "updSum")


def _client():
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def parse_ts(value):
    """updTime вида 2026-03-03T20:49:33.54322+03:00: дробная часть бывает 5 знаков, fromisoformat 3.9 такое не берёт."""
    s = str(value).replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac, tz = rest, ""
        for sep in ("+", "-"):
            if sep in rest:
                frac, tz = rest.split(sep, 1)
                tz = sep + tz
                break
        s = f"{head}.{frac[:6].ljust(6, '0')}{tz}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def upd_day(value):
    """День списания — updTime в московском времени."""
    return parse_ts(value).astimezone(MSK).date().isoformat()


def build_row(item, observed_at):
    for field in REQUIRED:
        if item.get(field) in (None, ""):
            raise RuntimeError(f"WB ads: в строке нет {field}: {item}")
    ts = parse_ts(item["updTime"])
    row = {"advert_id": int(item["advertId"]), "upd_time": ts.isoformat(), "upd_day": ts.astimezone(MSK).date().isoformat(), "observed_at": observed_at}
    for src, dst, kind in FIELDS:
        value = item.get(src)
        if value in (None, ""):
            row[dst] = None
        elif kind == "i":
            row[dst] = int(value)
        elif kind == "n":
            try:
                row[dst] = str(Decimal(str(value)))
            except InvalidOperation:
                raise RuntimeError(f"WB ads: поле {src} не число: {value!r}")
        else:
            row[dst] = str(value)
    return row


def check_items(items):
    """Дубль (advertId, updTime) — отказ: upsert молча оставил бы одно из двух списаний."""
    pairs = Counter((x.get("advertId"), x.get("updTime")) for x in items)
    dups = [k for k, n in pairs.items() if n > 1]
    if dups:
        raise RuntimeError(f"WB ads: повторы (advertId, updTime) в ответе — {len(dups)}: {dups[:5]}")


def request_upd(d1, d2, counters=None, sleep_fn=time.sleep):
    """Список списаний за d1 … d2 (≤ 31 дня). 429 / 5xx / таймаут / сеть — пауза и повтор."""
    counters = counters if counters is not None else Counter()
    if (date.fromisoformat(d2) - date.fromisoformat(d1)).days + 1 > MAX_INTERVAL_DAYS:
        raise RuntimeError(f"WB ads: интервал {d1}…{d2} длиннее {MAX_INTERVAL_DAYS} дней")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        counters["requests"] += 1
        try:
            resp = requests.get(URL, headers={"Authorization": WB_API_KEY}, params={"from": d1, "to": d2}, timeout=TIMEOUT)
        except TRANSIENT as error:
            counters["transient"] += 1
            print(f"WB ads {d1}…{d2}: сеть — {type(error).__name__}, попытка {attempt}/{MAX_ATTEMPTS}")
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"WB ads {d1}…{d2}: сетевой отказ не изжит за {MAX_ATTEMPTS} попыток") from error
            sleep_fn(RETRY_SECONDS)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            counters["429" if resp.status_code == 429 else "5xx"] += 1
            print(f"WB ads {d1}…{d2}: HTTP {resp.status_code}, попытка {attempt}/{MAX_ATTEMPTS}")
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"WB ads {d1}…{d2}: HTTP {resp.status_code} не изжит за {MAX_ATTEMPTS} попыток")
            sleep_fn(RETRY_SECONDS)
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"WB ads {d1}…{d2}: HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if body is None:
            body = []
        if not isinstance(body, list):
            raise RuntimeError(f"WB ads {d1}…{d2}: ответ не список: {str(body)[:200]}")
        print(f"WB ads {d1}…{d2}: HTTP 200, строк {len(body)}")
        return body
    raise RuntimeError("unreachable")


def upsert_rows(sb, rows, batch=500):
    written = 0
    for i in range(0, len(rows), batch):
        sb.table(TABLE).upsert(rows[i:i + batch], on_conflict="advert_id,upd_time").execute()
        written += len(rows[i:i + batch])
    return written


def summarize(rows):
    by_day = Counter()
    total = Decimal(0)
    for r in rows:
        s = Decimal(r["upd_sum"])
        by_day[r["upd_day"]] += 1
        total += s
    days = sorted(by_day)
    return (f"строк {len(rows)}, дней {len(days)}" + (f" ({days[0]} … {days[-1]})" if days else "") + f", Σ updSum {total:,.2f} (с НДС), "
            f"кампаний {len({r['advert_id'] for r in rows})}")


def run(sb, days_back=DEFAULT_DAYS_BACK, today=None, dry_run=False, sleep_fn=time.sleep):
    today = today or date.today()
    d1 = (today - timedelta(days=days_back)).isoformat()
    d2 = (today - timedelta(days=1)).isoformat()
    print(f"WB реклама: окно {d1} … {d2} (adv/v1/upd), {'dry-run, ' if dry_run else ''}db_writes = {'0' if dry_run else '…'}")
    counters = Counter()
    items = request_upd(d1, d2, counters, sleep_fn)
    check_items(items)
    observed_at = datetime.now(timezone.utc).isoformat()
    rows = [build_row(x, observed_at) for x in items]
    print(summarize(rows))
    print(f"обращений {counters['requests']}, 429 — {counters['429']}, 5xx — {counters['5xx']}, сетевых отказов {counters['transient']}")
    if dry_run:
        print("dry-run: ничего не пишу, db_writes = 0")
        return {"rows": len(rows), "written": 0, "counters": dict(counters)}
    if not rows:
        print("списаний в окне нет — писать нечего (это нормально: реклама WB остановлена 18.08)")
        return {"rows": 0, "written": 0, "counters": dict(counters)}
    written = upsert_rows(sb, rows)
    print(f"✅ {TABLE}: upsert {written} строк")
    return {"rows": len(rows), "written": written, "counters": dict(counters)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.days_back > MAX_INTERVAL_DAYS:
        ap.error(f"--days-back не больше {MAX_INTERVAL_DAYS}: интервал метода")
    sb = _client()   # и в dry-run: статистике по номенклатурам нужен список кампаний со списаниями из базы
    run(sb, days_back=args.days_back, dry_run=args.dry_run)
    # WB-8 §3: статистика по номенклатурам (adv/v3/fullstats) — тем же шагом «WB: реклама», после списаний;
    # обращений столько, сколько пачек кампаний со списаниями в окне (нет списаний — 0). Отказ — именованный,
    # шаг нефатален по правилу FATAL_STEPS; списания к этому моменту уже записаны.
    try:
        from loaders import wb_ads_nm_loader as nm_loader
    except ImportError:  # пайплайн зовёт как скрипт
        import wb_ads_nm_loader as nm_loader
    nm_loader.run(sb, days_back=min(args.days_back, nm_loader.MAX_INTERVAL_DAYS), dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
