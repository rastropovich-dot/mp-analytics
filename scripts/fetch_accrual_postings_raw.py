#!/usr/bin/env python3
"""Сырьё accrual/by-day и accrual/postings за диапазон дат. Только чтение, db_writes = 0.

Зачем: у by-day нет штук, у accrual/postings они есть (quantity, seller_price
в строке типа 69 SaleCommission). Чтобы посчитать штуки за период, нужны
оба: by-day даёт список отправлений с продажей в день, postings — их
начисления с количеством.

  data/accrual_history/<день>.json           формат измерителя
                                             (measure_buyouts_history_vs_accrual.py);
                                             есть файл — в API не идём
  data/accrual_postings/<from>_<to>.json      список {posting_number, accruals}
                                             по всем отправлениям с продажей
                                             в окне, без повторов

    venv/bin/python3 scripts/fetch_accrual_postings_raw.py --date-from 2026-08-31 --date-to 2026-09-15

Обращения: by-day 1–3 на дату, postings — по 200 номеров за вызов. Пауза
1,5 с, на 429 — 60 с и до трёх попыток, число пауз печатается. Больше
--max-requests (по умолчанию 300) — стоп с явной причиной. В окне ночного
прогона 00:15…03:15 UTC не стартует.
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
import requests  # noqa: E402
from loaders import ozon_finance_accrual as accrual  # noqa: E402

BYDAY_DIR = os.path.join("data", "accrual_history")
POSTINGS_DIR = os.path.join("data", "accrual_postings")
PAUSE_SECONDS = 1.5
ANTISPAM_PAUSE_SECONDS = 60
ANTISPAM_MAX_ATTEMPTS = 3
CHUNK = 200
NIGHT_WINDOW = ((0, 15), (3, 15))  # UTC


def in_night_window(now_utc):
    minutes = now_utc.hour * 60 + now_utc.minute
    return NIGHT_WINDOW[0][0] * 60 + NIGHT_WINDOW[0][1] <= minutes <= NIGHT_WINDOW[1][0] * 60 + NIGHT_WINDOW[1][1]


def post(path, body, label, counters, max_requests):
    for attempt in range(1, ANTISPAM_MAX_ATTEMPTS + 1):
        time.sleep(PAUSE_SECONDS)
        counters["requests"] += 1
        if counters["requests"] > max_requests:
            raise SystemExit(f"обращений уже {counters['requests']} — больше {max_requests}, стоп")
        r = requests.post(f"{accrual.BASE}{path}", headers=accrual.headers(), json=body, timeout=180)
        if r.status_code == 429:
            counters["429"] += 1
            print(f"  {label}: 429, пауза {ANTISPAM_PAUSE_SECONDS} с, попытка {attempt}/{ANTISPAM_MAX_ATTEMPTS} ({r.text[:100]})", flush=True)
            time.sleep(ANTISPAM_PAUSE_SECONDS)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"{label}: HTTP {r.status_code} {r.text[:200]}")
        return r.json() or {}
    raise RuntimeError(f"{label}: 429 не прошёл за {ANTISPAM_MAX_ATTEMPTS} попыток")


def fetch_day(day, counters, max_requests):
    out, last_id = [], None
    while True:
        body = {"date": day}
        if last_id:
            body["last_id"] = last_id
        data = post("/v1/finance/accrual/by-day", body, f"by-day {day}", counters, max_requests)
        batch = data.get("accruals") or []
        out.extend(batch)
        last_id = data.get("last_id")
        if not batch or not last_id:
            return out


def load_or_fetch_day(day, counters, max_requests):
    path = os.path.join(BYDAY_DIR, f"{day}.json")
    if os.path.exists(path):
        return json.load(open(path))["accruals"], "file"
    accruals = fetch_day(day, counters, max_requests)
    os.makedirs(BYDAY_DIR, exist_ok=True)
    json.dump({"date": day, "fetched_at": datetime.now(timezone.utc).isoformat(), "accruals": accruals},
              open(path, "w"), ensure_ascii=False)
    return accruals, "api"


def sold_postings(accruals):
    """Отправления с продажей или возвратом в этот день — те же строки, что берёт build_buyout_rows."""
    out = []
    for a in accruals:
        if a.get("accrued_category") != "POSTING" or not a.get("posting"):
            continue
        for product in a["posting"].get("products") or []:
            commission = product.get("commission") or {}
            if not commission:
                continue
            if accrual.money(commission.get("sale_amount")) == 0 and accrual.money(commission.get("sale_commission")) == 0:
                continue
            out.append(a.get("unit_number"))
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", required=True)
    ap.add_argument("--date-to", required=True)
    ap.add_argument("--max-requests", type=int, default=300)
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    if in_night_window(now):
        raise SystemExit("окно ночного прогона 00:15…03:15 UTC — не стартую")
    counters = {"requests": 0, "429": 0}
    t0 = time.monotonic()
    d, d_to = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
    numbers, seen, per_day = [], set(), {}
    src = {"file": 0, "api": 0}
    while d <= d_to:
        day = d.isoformat()
        accruals, where = load_or_fetch_day(day, counters, args.max_requests)
        src[where] += 1
        sold = sold_postings(accruals)
        per_day[day] = len(sold)
        for pn in sold:
            if pn not in seen:
                seen.add(pn)
                numbers.append(pn)
        print(f"  {day}: начислений {len(accruals)}, отправлений с продажей {len(sold)} ({where})", flush=True)
        d += timedelta(days=1)
    print(f"by-day: дат {len(per_day)}, из файлов {src['file']}, из API {src['api']}; уникальных отправлений {len(numbers)}", flush=True)
    result, rows = [], 0
    for i in range(0, len(numbers), CHUNK):
        chunk = numbers[i:i + CHUNK]
        data = post("/v1/finance/accrual/postings", {"posting_numbers": chunk},
                    f"postings {i // CHUNK + 1}/{(len(numbers) + CHUNK - 1) // CHUNK}", counters, args.max_requests)
        items = data.get("posting_accruals") or []
        result.extend(items)
        rows += sum(len(it.get("accruals") or []) for it in items)
    got = {it.get("posting_number") for it in result}
    missing = [pn for pn in numbers if pn not in got]
    os.makedirs(POSTINGS_DIR, exist_ok=True)
    out_path = os.path.join(POSTINGS_DIR, f"{args.date_from}_{args.date_to}.json")
    json.dump(result, open(out_path, "w"), ensure_ascii=False)
    print(f"accrual/postings: спросили {len(numbers)}, в ответе {len(got)}, нет в ответе {len(missing)}, строк начислений {rows}")
    if missing:
        print("  нет в ответе:", missing[:10], "…" if len(missing) > 10 else "")
    print(f"обращений {counters['requests']}, 429 — {counters['429']}, {time.monotonic() - t0:.0f} с; записано {out_path}")
    print("db_writes = 0")


if __name__ == "__main__":
    main()
