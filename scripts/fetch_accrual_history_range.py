#!/usr/bin/env python3
"""Снять accrual/by-day за окно дат в data/accrual_history/<день>.json (формат измерителя; файл есть — день пропускается).

    venv/bin/python3 scripts/fetch_accrual_history_range.py --date-from 2025-11-01 --date-to 2025-11-30

Только чтение: в БД не пишет, файлы кладёт в gitignored data/accrual_history/. Глубина метода проверена 2026-09-23:
ноябрь 2025 отдаётся (30 дней, 56 обращений, 429 — 0). Не стартует в окно ночного прогона (loaders/pipeline_window).
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from loaders import ozon_finance_accrual as accrual  # noqa: E402
from loaders.pipeline_window import in_nightly_run_window, window_text  # noqa: E402

OUT = os.path.join(ROOT, "data", "accrual_history")
PAUSE_SECONDS = 1.5


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", required=True)
    ap.add_argument("--date-to", required=True)
    args = ap.parse_args(argv)
    os.makedirs(OUT, exist_ok=True)
    total = {"pages": 0, "requests": 0, "retries": 0, "failures": 0}
    days = skipped = 0
    d = date.fromisoformat(args.date_from)
    while d <= date.fromisoformat(args.date_to):
        if in_nightly_run_window(datetime.now(timezone.utc)):
            print(f"окно ночного прогона {window_text()} — стоп на {d}")
            return 1
        path = os.path.join(OUT, f"{d.isoformat()}.json")
        if os.path.exists(path):
            skipped += 1
            d += timedelta(days=1)
            continue
        stats = {}
        accruals = accrual.fetch_day(d.isoformat(), stats)
        json.dump({"date": d.isoformat(), "fetched_at": datetime.now(timezone.utc).isoformat(), "accruals": accruals}, open(path, "w"), ensure_ascii=False)
        for k in total:
            total[k] += stats.get(k, 0)
        days += 1
        print(f"{d} начислений {len(accruals)}, страниц {stats.get('pages')}, повторов {stats.get('retries', 0)}", flush=True)
        time.sleep(PAUSE_SECONDS)
        d += timedelta(days=1)
    print(f"снято дней {days}, пропущено (файл есть) {skipped}; обращений {total['requests']}, повторов {total['retries']}, отказов {total['failures']}; db_writes = 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
