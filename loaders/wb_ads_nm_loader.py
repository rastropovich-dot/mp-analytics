#!/usr/bin/env python3
"""Реклама WB по номенклатурам: advert-api GET /adv/v3/fullstats → wb_ad_spend_nm_daily.

    python3 loaders/wb_ads_nm_loader.py             окно 31 день до вчера, только дни со списаниями, запись
    python3 loaders/wb_ads_nm_loader.py --dry-run   то же без записи (db_writes = 0)

Зовётся из шага «WB: реклама» после загрузчика списаний (loaders/wb_ads_loader.py; строка шага не менялась).

МЕТОД (spec/wb/08-promotion.yaml): ids ≤ 50 кампаний за запрос, период ≤ 31 день, лимит 3 запроса/мин
(пауза PAUSE_SECONDS = 21 с между запросами), статусы кампаний 7 / 9 / 11. Ответ: кампания → days[] →
apps[] (appType) → nms[] (nmId, name, views, clicks, ctr, cpc, sum, atbs, orders, cr, shks, sum_price,
canceled). Глубина истории спекой не названа; проба 09-24 — июль (85 дней назад) отдаёт; февраль — только
пробой бэкфилла.

КОГО СПРАШИВАТЬ. Только кампании, у которых в окне есть списания в wb_ad_spend_daily (adv/v1/upd):
нет списаний — нет обращений (сентябрь 2026: реклама остановлена 18.08 → 0 запросов в ночь). Кампаний с
списаниями в месяц — 78 … 313 (WB-6 §3а) → 2 … 7 пачек по 50 за месяц.

ЗЕРНО — (день, кампания, appType, nmId), как в ответе; строки без nmId не пишутся (считаются вслух).
Застрявшие ключи: для запрошенных кампаний и дней окна ключи таблицы, которых в свежем полном ответе нет,
снимает loaders/stale_keys.py (только при полном сборе, порог, список до удаления, удаление после записи).

ТОЖДЕСТВО (измерено на 1 … 7 июля): Σ nms.sum по дню = Σ days.sum до копейки; против Σ updSum того же дня
+1,6 … 1,8 % — статистика кампаний против списаний, не объяснено (WB-8 §3); печатается в логе шага.
"""
import argparse
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

try:
    from loaders import stale_keys
except ImportError:  # пайплайн зовёт как скрипт
    import stale_keys

URL = "https://advert-api.wildberries.ru/adv/v3/fullstats"
TABLE = "wb_ad_spend_nm_daily"
SPEND_TABLE = "wb_ad_spend_daily"
DEFAULT_DAYS_BACK = 31
MAX_INTERVAL_DAYS = 31
IDS_PER_REQUEST = 50
PAUSE_SECONDS = 21
MAX_ATTEMPTS = 3
RETRY_SECONDS = 65
TIMEOUT = 180
WB_API_KEY = os.getenv("WB_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
TRANSIENT = (requests.Timeout, requests.ConnectionError)

NM_FIELDS = (("name", "nm_name", "s"), ("views", "views", "i"), ("clicks", "clicks", "i"), ("ctr", "ctr", "n"), ("cpc", "cpc", "n"), ("sum", "sum", "n"),
             ("atbs", "atbs", "i"), ("orders", "orders", "i"), ("cr", "cr", "n"), ("shks", "shks", "i"), ("sum_price", "sum_price", "n"), ("canceled", "canceled", "i"))


def _client():
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def batches(ids, size=IDS_PER_REQUEST):
    ids = sorted({int(i) for i in ids})
    return [ids[i:i + size] for i in range(0, len(ids), size)]


def request_fullstats(ids, d1, d2, counters=None, sleep_fn=None):
    """Одна пачка кампаний за период ≤ 31 день. 429 / 5xx / таймаут / сеть — пауза и повтор, MAX_ATTEMPTS раз."""
    sleep_fn = sleep_fn or time.sleep
    counters = counters if counters is not None else Counter()
    if (date.fromisoformat(d2) - date.fromisoformat(d1)).days + 1 > MAX_INTERVAL_DAYS:
        raise RuntimeError(f"WB fullstats: интервал {d1}…{d2} длиннее {MAX_INTERVAL_DAYS} дней")
    if len(ids) > IDS_PER_REQUEST:
        raise RuntimeError(f"WB fullstats: {len(ids)} кампаний в запросе — больше {IDS_PER_REQUEST}")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        counters["requests"] += 1
        try:
            resp = requests.get(URL, headers={"Authorization": WB_API_KEY}, params={"ids": ",".join(str(i) for i in ids), "beginDate": d1, "endDate": d2}, timeout=TIMEOUT)
        except TRANSIENT as error:
            counters["transient"] += 1
            print(f"WB fullstats {d1}…{d2} ({len(ids)} id): сеть — {type(error).__name__}, попытка {attempt}/{MAX_ATTEMPTS}", flush=True)
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"WB fullstats {d1}…{d2}: сетевой отказ не изжит за {MAX_ATTEMPTS} попыток") from error
            sleep_fn(RETRY_SECONDS)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            counters["429" if resp.status_code == 429 else "5xx"] += 1
            print(f"WB fullstats {d1}…{d2} ({len(ids)} id): HTTP {resp.status_code}, попытка {attempt}/{MAX_ATTEMPTS}", flush=True)
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"WB fullstats {d1}…{d2}: HTTP {resp.status_code} не изжит за {MAX_ATTEMPTS} попыток")
            sleep_fn(RETRY_SECONDS)
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"WB fullstats {d1}…{d2}: HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if body is None:
            body = []
        if not isinstance(body, list):
            raise RuntimeError(f"WB fullstats {d1}…{d2}: ответ не список: {str(body)[:200]}")
        print(f"WB fullstats {d1}…{d2} ({len(ids)} id): HTTP 200, кампаний в ответе {len(body)}, {len(resp.content)} байт", flush=True)
        return body
    raise RuntimeError("unreachable")


def _num(value, field):
    if value in (None, ""):
        return None
    try:
        return str(Decimal(str(value)))
    except InvalidOperation:
        raise RuntimeError(f"WB fullstats: поле {field} не число: {value!r}")


def build_rows(answer, observed_at, d1=None, d2=None, counters=None):
    """Строки таблицы из ответа (список кампаний). Дни вне d1 … d2 пропускаются; nms без nmId считаются."""
    counters = counters if counters is not None else Counter()
    rows = {}
    for adv in answer or []:
        advert_id = adv.get("advertId")
        if advert_id in (None, ""):
            raise RuntimeError("WB fullstats: кампания без advertId в ответе")
        for day in adv.get("days") or []:
            d = str(day.get("date"))[:10]
            if (d1 and d < d1) or (d2 and d > d2):
                counters["days_outside"] += 1
                continue
            for app in day.get("apps") or []:
                app_type = app.get("appType")
                if app_type in (None, ""):
                    raise RuntimeError(f"WB fullstats: app без appType у кампании {advert_id} за {d}")
                for nm in app.get("nms") or []:
                    if nm.get("nmId") in (None, ""):
                        counters["nm_without_id"] += 1
                        continue
                    key = (d, int(advert_id), int(app_type), int(nm["nmId"]))
                    if key in rows:
                        raise RuntimeError(f"WB fullstats: повтор ключа {key} в ответе")
                    row = {"day": d, "advert_id": key[1], "app_type": key[2], "nm_id": key[3], "observed_at": observed_at}
                    for src, dst, kind in NM_FIELDS:
                        v = nm.get(src)
                        row[dst] = None if v in (None, "") else (int(v) if kind == "i" else (_num(v, src) if kind == "n" else str(v)))
                    rows[key] = row
    return list(rows.values())


def campaigns_with_spend(sb, d1, d2):
    """{advert_id: Σ updSum} по wb_ad_spend_daily за d1 … d2 — кого спрашивать; и Σ по дням для тождества."""
    rows = stale_keys.read_window_rows(sb, SPEND_TABLE, "advert_id,upd_day,upd_sum", [("gte", "upd_day", d1), ("lte", "upd_day", d2)], ["upd_day", "advert_id", "upd_time"])
    by_adv, by_day = defaultdict(Decimal), defaultdict(Decimal)
    for r in rows:
        by_adv[int(r["advert_id"])] += Decimal(str(r["upd_sum"])); by_day[str(r["upd_day"])] += Decimal(str(r["upd_sum"]))
    return dict(by_adv), dict(by_day)


def upsert_rows(sb, rows, batch=500):
    n = 0
    for i in range(0, len(rows), batch):
        sb.table(TABLE).upsert(rows[i:i + batch], on_conflict="day,advert_id,app_type,nm_id").execute()
        n += len(rows[i:i + batch])
    return n


def _delete(sb, rows, batch=200):
    n = 0
    by_day = defaultdict(list)
    for r in rows:
        by_day[str(r["day"])].append(r)
    for d, rs in sorted(by_day.items()):
        for adv in sorted({int(r["advert_id"]) for r in rs}):
            nms = sorted({int(r["nm_id"]) for r in rs if int(r["advert_id"]) == adv})
            for i in range(0, len(nms), batch):
                res = sb.table(TABLE).delete().eq("day", d).eq("advert_id", adv).in_("nm_id", nms[i:i + batch]).execute()
                n += len(res.data or [])
    return n


def cleanup_stale(sb, d1, d2, advert_ids, rows, complete, apply):
    """Ключи окна по запрошенным кампаниям, которых в свежем ответе нет, — снять по правилу stale_keys."""
    existing = []
    for adv_batch in batches(advert_ids, 100):
        existing.extend(stale_keys.read_window_rows(sb, TABLE, "day,advert_id,app_type,nm_id,sum",
                                                    [("gte", "day", d1), ("lte", "day", d2), ("in_", "advert_id", adv_batch)],
                                                    ["day", "advert_id", "app_type", "nm_id"]))
    built_keys = {(str(r["day"]), r["advert_id"], r["app_type"], r["nm_id"]) for r in rows}
    built_days = {str(r["day"]) for r in rows}
    window = {"day_from": d1, "day_to": d2, "complete": complete, "retries": 0, "failures": 0 if complete else 1}
    return stale_keys.cleanup(sb, TABLE, window, existing, built_keys, built_days,
                              lambda r: (str(r["day"]), int(r["advert_id"]), int(r["app_type"]), int(r["nm_id"])), lambda r: str(r["day"]),
                              lambda r: f"{r['day']} кампания {r['advert_id']} app {r['app_type']} nmId {r['nm_id']} sum {r.get('sum')}",
                              lambda sb_, stale: _delete(sb_, stale), apply)


def identity_line(rows, spend_by_day):
    """Σ nms.sum по дням против Σ updSum по дням — одной строкой в лог."""
    by_day = defaultdict(Decimal)
    for r in rows:
        by_day[str(r["day"])] += Decimal(r["sum"] or 0)
    a, u = sum(by_day.values(), Decimal(0)), sum(spend_by_day.values(), Decimal(0))
    return f"тождество: Σ nms.sum {a:,.2f} против Σ updSum {u:,.2f} за дни окна ({'x' if not u else f'{a / u:.4f}'}), дней с nm-строками {len(by_day)} из {len(spend_by_day)} со списаниями"


def run(sb, days_back=DEFAULT_DAYS_BACK, today=None, dry_run=False, sleep_fn=None):
    sleep_fn = sleep_fn or time.sleep
    today = today or date.today()
    d1 = (today - timedelta(days=days_back)).isoformat()
    d2 = (today - timedelta(days=1)).isoformat()
    print(f"WB реклама по номенклатурам: окно {d1} … {d2} (adv/v3/fullstats), {'dry-run, db_writes = 0' if dry_run else 'запись'}", flush=True)
    by_adv, spend_by_day = campaigns_with_spend(sb, d1, d2)
    counters = Counter()
    if not by_adv:
        print(f"списаний в окне нет ({SPEND_TABLE} пуст за {d1} … {d2}) — кампаний не спрашиваю; обращений 0", flush=True)
        return {"rows": 0, "written": 0, "deleted": 0, "requests": 0, "429": 0, "campaigns": 0}
    observed_at = datetime.now(timezone.utc).isoformat()
    rows, complete = [], True
    chunks = batches(by_adv)
    print(f"кампаний со списаниями {len(by_adv)} → пачек по {IDS_PER_REQUEST}: {len(chunks)}", flush=True)
    for n, chunk in enumerate(chunks):
        if n:
            sleep_fn(PAUSE_SECONDS)
        answer = request_fullstats(chunk, d1, d2, counters, sleep_fn)
        rows.extend(build_rows(answer, observed_at, d1, d2, counters))
    print(f"строк по номенклатурам {len(rows)}, кампаний в ответах {len({r['advert_id'] for r in rows})} из {len(by_adv)}; "
          f"обращений {counters['requests']}, 429 — {counters['429']}, 5xx — {counters['5xx']}, сетевых отказов {counters['transient']}; "
          f"nms без nmId {counters['nm_without_id']}, дней вне окна {counters['days_outside']}", flush=True)
    print(identity_line(rows, spend_by_day), flush=True)
    if dry_run:
        print("dry-run: ничего не пишу, db_writes = 0", flush=True)
        cleanup_stale(sb, d1, d2, list(by_adv), rows, complete, apply=False)
        return {"rows": len(rows), "written": 0, "deleted": 0, "requests": counters["requests"], "429": counters["429"], "campaigns": len(by_adv)}
    written = upsert_rows(sb, rows) if rows else 0
    print(f"✅ {TABLE}: upsert {written} строк", flush=True)
    deleted = cleanup_stale(sb, d1, d2, list(by_adv), rows, complete, apply=True)
    return {"rows": len(rows), "written": written, "deleted": deleted, "requests": counters["requests"], "429": counters["429"], "campaigns": len(by_adv)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.days_back > MAX_INTERVAL_DAYS:
        ap.error(f"--days-back не больше {MAX_INTERVAL_DAYS}: период метода")
    run(_client(), days_back=args.days_back, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
