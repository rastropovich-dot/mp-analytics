#!/usr/bin/env python3
"""Реклама WB — история списаний advert-api GET /adv/v1/upd: съём сырья и план (без записи).

    venv/bin/python3 scripts/wb_ads_backfill.py --fetch [--date-from 2026-02-01] [--date-to <вчера>]
    venv/bin/python3 scripts/wb_ads_backfill.py --plan
    venv/bin/python3 scripts/wb_ads_backfill.py --apply --approve-wb-ads-write     ← только по слову владельца, после миграции

СЪЁМ: интервал метода ≤ 31 дня (спека 08-promotion.yaml, `to` — «максимальный 31»), лимит
1 запрос/с; куски по 31 дню с --date-from → data/wb_ads_raw/upd_<from>_<to>.json, леджер
calls.json (обращения, статусы, 429). Файл есть — в API не идёт. В ночном окне — стоп.
429 / таймаут / сеть — 3 попытки по 65 с, затем отказ с именем куска.

ПЛАН (из файлов, обращений 0, записи нет): строк, Σ updSum по месяцам (день = updTime в
московском времени; без updTime — «без даты»), по paymentType и advertType, кампаний;
проверка зерна — сколько строк на updNum и на (advertId, updTime), нули updNum; Σ / 1,22 по
месяцам против листа владельца (июль 1 694 178 ₽ без НДС, август 1…17 — 835 968).

ЗАПИСЬ (--apply --approve-wb-ads-write, по слову владельца после миграции
sql/20260923_create_wb_ad_spend_daily.sql): строки через loaders.wb_ads_loader.build_row /
upsert_rows, ключ (advert_id, upd_time), обращений к API — 0. Без одобрения не пишет.
"""
import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from loaders import wb_ads_loader as loader  # noqa: E402
from loaders.pipeline_window import in_nightly_run_window, window_text  # noqa: E402

RAW_DIR = os.path.join(ROOT, "data", "wb_ads_raw")
CALLS_PATH = os.path.join(RAW_DIR, "calls.json")
CHUNK_DAYS = loader.MAX_INTERVAL_DAYS
PAUSE_SECONDS = 5
VAT = Decimal("1.22")
OWNER_NO_VAT = {"2026-07": Decimal("1694178"), "2026-08 (1…17)": Decimal("835968")}


def chunks(d1, d2):
    a = date.fromisoformat(d1)
    end = date.fromisoformat(d2)
    while a <= end:
        b = min(a + timedelta(days=CHUNK_DAYS - 1), end)
        yield a.isoformat(), b.isoformat()
        a = b + timedelta(days=1)


def request_chunk(d1, d2, sleep_fn=time.sleep):
    """(status, items, attempts) — через loaders.wb_ads_loader.request_upd (те же повторы 429 / сеть)."""
    counters = Counter()
    items = loader.request_upd(d1, d2, counters, sleep_fn)
    return 200, items, counters["requests"]


def fetch(d1, d2):
    os.makedirs(RAW_DIR, exist_ok=True)
    calls = json.load(open(CALLS_PATH)) if os.path.exists(CALLS_PATH) else []
    made = 0
    for a, b in chunks(d1, d2):
        path = os.path.join(RAW_DIR, f"upd_{a}_{b}.json")
        if os.path.exists(path):
            print(f"{a}…{b}: файл есть, в API не иду")
            continue
        if in_nightly_run_window(datetime.now(timezone.utc)):
            print(f"ночное окно ({window_text()}): стоп перед {a}…{b}")
            break
        if made:
            time.sleep(PAUSE_SECONDS)
        status, items, attempts = request_chunk(a, b)
        made += 1
        json.dump(items, open(path, "w", encoding="utf-8"), ensure_ascii=False)
        calls.append({"chunk": f"{a}_{b}", "at_utc": datetime.now(timezone.utc).isoformat(), "status": status, "attempts": attempts, "rows": len(items),
                      "sum": str(sum(Decimal(str(x.get("updSum") or 0)) for x in items))})
        json.dump(calls, open(CALLS_PATH, "w"), indent=1)
        print(f"{a}…{b}: HTTP {status}, строк {len(items)}, Σ updSum {calls[-1]['sum']}, попыток {attempts}")
    print(f"обращений за этот запуск: {made}; всего в леджере: {len(calls)}, 429/повторов: {sum(c['attempts'] - 1 for c in calls)}")
    return 0


def load_files():
    items, files = [], []
    for name in sorted(os.listdir(RAW_DIR)) if os.path.isdir(RAW_DIR) else []:
        if name.startswith("upd_") and name.endswith(".json"):
            files.append(name)
            items += json.load(open(os.path.join(RAW_DIR, name), encoding="utf-8"))
    return items, files


parse_ts = loader.parse_ts


def upd_day(item):
    ts = item.get("updTime")
    return loader.upd_day(ts) if ts else None


def plan():
    items, files = load_files()
    if not items and not files:
        print("сырья нет — сначала --fetch"); return 2
    print(f"Сырьё: файлов {len(files)}, строк {len(items)}; поля: {sorted({k for x in items for k in x}) if items else '—'}")
    if not items:
        print("во всех файлах пусто: списаний за период нет"); return 0
    by_month, by_pay, by_type, camps = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal), set()
    n_month = Counter()
    for x in items:
        d = upd_day(x)
        m = d[:7] if d else "без даты"
        s = Decimal(str(x.get("updSum") or 0))
        by_month[m] += s; n_month[m] += 1; by_pay[x.get("paymentType")] += s; by_type[x.get("advertType")] += s; camps.add(x.get("advertId"))
    print(f"\n{'месяц':10}{'строк':>7}{'Σ updSum':>16}{'/ 1,22':>16}   лист владельца (без НДС)")
    for m in sorted(by_month):
        own = OWNER_NO_VAT.get(m, None)
        print(f"{m:10}{n_month[m]:>7}{by_month[m]:>16,.2f}{(by_month[m] / VAT):>16,.2f}   {own if own is None else f'{own:,.2f}'}")
    print(f"{'итого':10}{len(items):>7}{sum(by_month.values()):>16,.2f}{(sum(by_month.values()) / VAT):>16,.2f}")
    aug17 = sum(Decimal(str(x.get("updSum") or 0)) for x in items if (upd_day(x) or "")[:10] and "2026-08-01" <= upd_day(x) <= "2026-08-17")
    print(f"август 1…17: Σ {aug17:,.2f}, / 1,22 = {aug17 / VAT:,.2f} против листа {OWNER_NO_VAT['2026-08 (1…17)']:,.2f}")
    print("\nпо paymentType:", {k: f"{v:,.2f}" for k, v in by_pay.items()})
    print("по advertType:", {k: f"{v:,.2f}" for k, v in by_type.items()})
    print(f"кампаний: {len(camps)}")
    upd_nums = Counter(x.get("updNum") for x in items)
    pairs = Counter((x.get("advertId"), x.get("updTime")) for x in items)
    print(f"\nзерно: строк {len(items)}; updNum различных {len(upd_nums)}, нулевых {upd_nums.get(0, 0)}, повторов updNum {sum(c - 1 for c in upd_nums.values() if c > 1)}; "
          f"(advertId, updTime) различных {len(pairs)}, повторов {sum(c - 1 for c in pairs.values() if c > 1)}; без updTime {sum(1 for x in items if not x.get('updTime'))}")
    try:
        loader.check_items(items)
        print("дублей (advertId, updTime) нет — ключ таблицы держит")
    except RuntimeError as error:
        print(f"ВНИМАНИЕ: {error}")
    print("db_writes = 0")
    return items


def apply(items):
    if in_nightly_run_window(datetime.now(timezone.utc)):
        raise SystemExit(f"ночное окно ({window_text()}): запись отложить")
    loader.check_items(items)
    observed_at = datetime.now(timezone.utc).isoformat()
    rows = [loader.build_row(x, observed_at) for x in items]
    written = loader.upsert_rows(loader._client(), rows)
    print(f"✅ {loader.TABLE}: upsert {written} строк ({observed_at}); обращений к API — 0")
    return written


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--approve-wb-ads-write", action="store_true")
    ap.add_argument("--date-from", default="2026-02-01")
    ap.add_argument("--date-to", default=(date.today() - timedelta(days=1)).isoformat())
    args = ap.parse_args(argv)
    if args.fetch:
        return fetch(args.date_from, args.date_to)
    if args.plan or args.apply:
        items = plan()
        if not isinstance(items, list):
            return items
        if args.apply:
            if not args.approve_wb_ads_write:
                print("--apply без --approve-wb-ads-write: не пишу")
                return 2
            apply(items)
        return 0
    ap.error("нужен --fetch, --plan или --apply")


if __name__ == "__main__":
    sys.exit(main())
