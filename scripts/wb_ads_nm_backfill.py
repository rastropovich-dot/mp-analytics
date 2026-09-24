#!/usr/bin/env python3
"""История рекламы WB по номенклатурам (adv/v3/fullstats) → wb_ad_spend_nm_daily: оценка → сырьё → план → запись по слову.

    python3 scripts/wb_ads_nm_backfill.py --estimate --date-from 2026-02-01 --date-to 2026-08-31   обращений и минут, 0 обращений
    python3 scripts/wb_ads_nm_backfill.py --fetch    --date-from 2026-02-01 --date-to 2026-08-31   сырьё в файлы (по слову — WB-8 §3)
    python3 scripts/wb_ads_nm_backfill.py --plan     --date-from 2026-02-01 --date-to 2026-08-31   план из файлов, 0 обращений, db_writes = 0
    python3 scripts/wb_ads_nm_backfill.py --apply --approve-wb-ads-nm-write --date-from … --date-to …   запись по слову

По календарным месяцам (период метода ≤ 31 день): кампании — те, у которых в месяце есть списания в
wb_ad_spend_daily; пачки по 50 id (loaders/wb_ads_nm_loader.batches); один запрос — вся пачка за весь
месяц. Сырьё — data/wb_ads_nm_raw/fullstats_<YYYY-MM>_b<n>.json (ответ + паспорт: месяц, границы,
кампании, момент съёма); файл есть — в API не идёт; между запросами 21 с; ночное окно — стоп.

План (из файлов): по месяцам — строк по номенклатурам, кампаний в ответе / спрошено, Σ nms.sum против
Σ updSum месяца (тождество WB-8 §3: на 1 … 7 июля +1,6 … 1,8 %), nms без nmId, дней вне месяца. Запись —
upsert по (day, advert_id, app_type, nm_id) и чистка застрявших ключей месяца по спрошенным кампаниям
(только если все пачки месяца сняты). Без --approve-wb-ads-nm-write не пишет.
"""
import argparse
import glob
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

from loaders.pipeline_window import in_nightly_run_window, window_text  # noqa: E402
import loaders.wb_ads_nm_loader as nm  # noqa: E402

RAW_DIR = os.path.join(ROOT, "data", "wb_ads_nm_raw")
CALLS_PATH = os.path.join(RAW_DIR, "calls.json")
Z = Decimal(0)


def months_between(d1, d2):
    """[(месяц, первый день, последний день)] внутри d1 … d2."""
    out = []
    cur = date.fromisoformat(d1).replace(day=1)
    end = date.fromisoformat(d2)
    while cur <= end:
        nxt = date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
        last = nxt - timedelta(days=1)
        out.append((cur.strftime("%Y-%m"), max(cur, date.fromisoformat(d1)).isoformat(), min(last, end).isoformat()))
        cur = nxt
    return out


def raw_path(month, n):
    return os.path.join(RAW_DIR, f"fullstats_{month}_b{n}.json")


def estimate(sb, d1, d2):
    """Обращений и минут по месяцам — из wb_ad_spend_daily, к API не ходит."""
    total = 0
    print(f"{'месяц':9}{'кампаний':>10}{'пачек':>7}{'Σ updSum':>16}")
    plan = []
    for month, m1, m2 in months_between(d1, d2):
        by_adv, _by_day = nm.campaigns_with_spend(sb, m1, m2)
        n = len(nm.batches(by_adv))
        total += n
        plan.append((month, m1, m2, sorted(by_adv), n))
        print(f"{month:9}{len(by_adv):>10}{n:>7}{sum(by_adv.values(), Z):>16,.2f}")
    print(f"итого обращений {total} ≈ {total * nm.PAUSE_SECONDS / 60:.0f} мин при паузе {nm.PAUSE_SECONDS} с (лимит 3/мин); ответ ~1,7 МБ на 50 кампаний за неделю")
    return plan


def fetch(sb, d1, d2, sleep_fn=None):
    sleep_fn = sleep_fn or time.sleep
    os.makedirs(RAW_DIR, exist_ok=True)
    ledger = json.load(open(CALLS_PATH, encoding="utf-8")) if os.path.exists(CALLS_PATH) else []
    counters = Counter()
    first = True
    for month, m1, m2, ids, _n in estimate(sb, d1, d2):
        for i, chunk in enumerate(nm.batches(ids), 1):
            path = raw_path(month, i)
            if os.path.exists(path):
                print(f"{month} пачка {i}: есть, пропуск", flush=True)
                continue
            if in_nightly_run_window(datetime.now(timezone.utc)):
                print(f"{month} пачка {i}: ночное окно {window_text()} — стоп; повторный запуск продолжит", flush=True)
                return 3
            if not first:
                sleep_fn(nm.PAUSE_SECONDS)
            first = False
            before = counters["requests"]
            started = datetime.now(timezone.utc)
            answer = nm.request_fullstats(chunk, m1, m2, counters, sleep_fn)
            payload = {"month": month, "date_from": m1, "date_to": m2, "batch": i, "ids": chunk, "fetched_at_utc": started.isoformat(),
                       "campaigns_in_answer": len(answer), "answer": answer}
            with open(path, "w", encoding="utf-8") as h:
                json.dump(payload, h, ensure_ascii=False)
            ledger.append({"month": month, "batch": i, "ids": len(chunk), "at_utc": started.isoformat(), "requests": counters["requests"] - before,
                           "campaigns_in_answer": len(answer), "429": counters["429"], "transient": counters["transient"]})
            json.dump(ledger, open(CALLS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            print(f"{month} пачка {i}: кампаний {len(chunk)} → в ответе {len(answer)}, файл {os.path.relpath(path, ROOT)}", flush=True)
    print(f"Обращений: {counters['requests']}, 429 — {counters['429']}, 5xx — {counters['5xx']}, сетевых отказов {counters['transient']}", flush=True)
    return 0


def load_files(d1, d2):
    """{месяц: [паспорта пачек]} из файлов сырья за период."""
    out = defaultdict(list)
    months = {m for m, _a, _b in months_between(d1, d2)}
    for path in sorted(glob.glob(os.path.join(RAW_DIR, "fullstats_*_b*.json"))):
        payload = json.load(open(path, encoding="utf-8"))
        if payload.get("month") in months:
            out[payload["month"]].append(payload)
    return dict(out)


def plan(sb, files, d1, d2):
    if not files:
        print("файлов сырья нет — сначала --fetch")
        return None
    print(f"\n{'месяц':9}{'пачек':>7}{'спрошено':>10}{'в ответе':>10}{'строк nm':>10}{'Σ nms.sum':>16}{'Σ updSum':>16}{'отношение':>11}{'без nmId':>9}{'вне мес.':>9}")
    plans = {}
    tot = Counter()
    for month, m1, m2 in months_between(d1, d2):
        parts = files.get(month)
        if not parts:
            print(f"{month:9}{'—':>7}   сырья нет")
            continue
        counters = Counter()
        rows = []
        asked, answered = set(), set()
        for p in parts:
            asked.update(p["ids"]); answered.update(a.get("advertId") for a in p["answer"])
            rows.extend(nm.build_rows(p["answer"], "files", m1, m2, counters))
        by_adv, spend_by_day = nm.campaigns_with_spend(sb, m1, m2) if sb is not None else ({}, {})
        expected_batches = len(nm.batches(by_adv)) if by_adv else len(parts)
        complete = len(parts) == expected_batches and set(by_adv) <= asked if by_adv else True
        s_nm = sum((Decimal(r["sum"] or 0) for r in rows), Z); s_upd = sum(spend_by_day.values(), Z)
        print(f"{month:9}{len(parts):>7}{len(asked):>10}{len(answered):>10}{len(rows):>10}{s_nm:>16,.2f}{s_upd:>16,.2f}{(s_nm / s_upd if s_upd else 0):>11.4f}"
              f"{counters['nm_without_id']:>9}{counters['days_outside']:>9}" + ("" if complete else "   пачек меньше, чем кампаний со списаниями — месяц неполный, застрявшие не чищу"))
        plans[month] = {"rows": rows, "asked": sorted(asked), "complete": complete, "d1": m1, "d2": m2}
        tot["rows"] += len(rows); tot["nm"] += s_nm; tot["upd"] += s_upd; tot["batches"] += len(parts)
    print(f"{'итого':9}{tot['batches']:>7}{'':>20}{tot['rows']:>10}{tot['nm']:>16,.2f}{tot['upd']:>16,.2f}{(tot['nm'] / tot['upd'] if tot['upd'] else 0):>11.4f}")
    print(f"К записи: строк {tot['rows']}; db_writes = 0")
    return plans


def apply(sb, plans):
    observed_at = datetime.now(timezone.utc).isoformat()
    written = deleted = 0
    for month, p in sorted(plans.items()):
        rows = [dict(r, observed_at=observed_at) for r in p["rows"]]
        written += nm.upsert_rows(sb, rows) if rows else 0
        print(f"✅ {nm.TABLE} {month}: upsert {len(rows)} строк", flush=True)
        deleted += nm.cleanup_stale(sb, p["d1"], p["d2"], p["asked"], rows, p["complete"], apply=True)
    print(f"✅ {nm.TABLE}: upsert {written} строк, застрявших удалено {deleted} ({observed_at})")
    return written, deleted


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--estimate", action="store_true"); ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--plan", action="store_true"); ap.add_argument("--apply", action="store_true")
    ap.add_argument("--approve-wb-ads-nm-write", action="store_true")
    ap.add_argument("--date-from", required=True); ap.add_argument("--date-to", required=True)
    args = ap.parse_args(argv)
    sb = nm._client()
    if args.estimate:
        estimate(sb, args.date_from, args.date_to); return 0
    if args.fetch:
        return fetch(sb, args.date_from, args.date_to)
    if not (args.plan or args.apply):
        ap.error("нужен --estimate, --fetch, --plan или --apply")
    plans = plan(sb, load_files(args.date_from, args.date_to), args.date_from, args.date_to)
    if args.apply:
        if not args.approve_wb_ads_nm_write:
            print("--apply без --approve-wb-ads-nm-write: не пишу"); return 2
        if not plans:
            return 2
        if in_nightly_run_window(datetime.now(timezone.utc)):
            raise SystemExit(f"ночное окно ({window_text()}): запись отложить")
        apply(sb, plans)
    return 0


if __name__ == "__main__":
    sys.exit(main())
