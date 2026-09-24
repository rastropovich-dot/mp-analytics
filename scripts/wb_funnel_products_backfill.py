#!/usr/bin/env python3
"""История воронки WB по товарам: сырьё в файлы → план → запись по слову (wb_funnel_products_daily + сводки дней).

    python3 scripts/wb_funnel_products_backfill.py --fetch --date-from 2026-09-01 --date-to 2026-09-23   снять дни в файлы (только чтение WB)
    python3 scripts/wb_funnel_products_backfill.py --plan  --date-from 2026-09-01 --date-to 2026-09-23   план из файлов, 0 обращений, db_writes = 0
    python3 scripts/wb_funnel_products_backfill.py --apply --approve-wb-funnel-write --date-from … --date-to …   запись по слову владельца

Сырьё — data/wb_funnel_raw/funnel_<день>.json: все страницы дня (loaders.wb_sales_funnel_orders_loader
.fetch_wb_sales_funnel_day), момент съёма, число страниц, полный ли день. Файл есть — в API не идёт;
между днями 21 с (лимит 3 запроса/мин); ночное окно прогона — стоп (loaders/pipeline_window.py).
≤ 2 обращения на день при ~1 000–1 100 карточках.

План (из файлов): по дням — карточек, заказов шт и orderSum, buyoutSum, orderSum по букве артикула
(`t` — Дискаунтер, `f` — Standard, прочее — вслух); против сводки дня в marketplace_orders_analytics
(что записала ночь D+1 — пересмотр воронки задним числом виден как разница); против снимка
первой страницы 2026-09-23 (logs/wb_funnel_recheck_20260923/, WB-6 §2) по тем же nmId.

Запись: строки по товарам upsert по (day, nm_id), сводка дня upsert (source wb_sales_funnel), затем
чистка застрявших ключей дня по правилу loaders/stale_keys.py (только у полных дней). Без
--approve-wb-funnel-write не пишет. Снимок затрагиваемых строк не делается: таблица по товарам
новая (строк нет), сводки дней — по одной строке на день, старые значения печатаются в плане.
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

from loaders import stale_keys  # noqa: E402
from loaders.pipeline_window import in_nightly_run_window, window_text  # noqa: E402
import loaders.wb_sales_funnel_orders_loader as funnel  # noqa: E402

RAW_DIR = os.path.join(ROOT, "data", "wb_funnel_raw")
CALLS_PATH = os.path.join(RAW_DIR, "calls.json")
RECHECK_DIR = os.path.join(ROOT, "logs", "wb_funnel_recheck_20260923")
DISCOUNTER_LETTER = "t"
Z = Decimal(0)


def days_between(d1, d2):
    d, end = date.fromisoformat(d1), date.fromisoformat(d2)
    while d <= end:
        yield d.isoformat()
        d += timedelta(days=1)


def raw_path(day):
    return os.path.join(RAW_DIR, f"funnel_{day}.json")


def fetch(d1, d2, sleep_fn=None):
    """Снять дни в файлы. Есть файл — пропуск."""
    sleep_fn = sleep_fn or time.sleep
    os.makedirs(RAW_DIR, exist_ok=True)
    ledger = json.load(open(CALLS_PATH, encoding="utf-8")) if os.path.exists(CALLS_PATH) else []
    counters = Counter()
    first = True
    for day in days_between(d1, d2):
        path = raw_path(day)
        if os.path.exists(path):
            print(f"{day}: есть, пропуск", flush=True)
            continue
        if in_nightly_run_window(datetime.now(timezone.utc)):
            print(f"{day}: ночное окно {window_text()} — стоп; повторный запуск продолжит", flush=True)
            return 3
        if not first:
            sleep_fn(funnel.PAGE_SLEEP_SECONDS)
        first = False
        before = dict(counters)
        started = datetime.now(timezone.utc)
        result = funnel.fetch_wb_sales_funnel_day(day, counters, sleep_fn)
        payload = {"day": day, "fetched_at_utc": started.isoformat(), "pages": result["pages"], "complete": result["complete"],
                   "dup_nm_ids": result["dup_nm_ids"], "orders_qty": result["orders_qty"], "orders_amount": result["orders_amount"],
                   "products": result["products"]}
        with open(path, "w", encoding="utf-8") as h:
            json.dump(payload, h, ensure_ascii=False)
        ledger.append({"day": day, "at_utc": started.isoformat(), "requests": counters["requests"] - before.get("requests", 0),
                       "pages": result["pages"], "cards": len(result["products"]), "429": counters["429"] - before.get("429", 0),
                       "transient": counters["transient"] - before.get("transient", 0), "complete": result["complete"]})
        json.dump(ledger, open(CALLS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"{day}: карточек {len(result['products'])}, страниц {result['pages']}, заказов {result['orders_qty']:.0f} / "
              f"{result['orders_amount']:.0f}, файл {os.path.relpath(path, ROOT)}", flush=True)
    print(f"Обращений: {counters['requests']}, 429 — {counters['429']}, сетевых отказов {counters['transient']}", flush=True)
    return 0


def load_files(d1, d2):
    """{день: payload} из файлов сырья за период."""
    out = {}
    for path in sorted(glob.glob(os.path.join(RAW_DIR, "funnel_*.json"))):
        day = os.path.basename(path)[len("funnel_"):-len(".json")]
        if d1 <= day <= d2:
            out[day] = json.load(open(path, encoding="utf-8"))
    return out


def letter_of(vendor_code):
    return (str(vendor_code or "").strip()[:1] or "?").lower()


def day_summary(products):
    """Сводка дня по карточкам: заказов шт / orderSum / buyoutSum всего и по букве артикула."""
    s = {"cards": len(products), "qty": Z, "sum": Z, "buyout_sum": Z, "by_letter": defaultdict(lambda: {"qty": Z, "sum": Z, "cards": 0})}
    for p in products:
        st = (p.get("statistic") or {}).get("selected") or {}
        letter = letter_of((p.get("product") or {}).get("vendorCode"))
        qty, total = Decimal(str(st.get("orderCount") or 0)), Decimal(str(st.get("orderSum") or 0))
        s["qty"] += qty; s["sum"] += total; s["buyout_sum"] += Decimal(str(st.get("buyoutSum") or 0))
        b = s["by_letter"][letter]
        b["qty"] += qty; b["sum"] += total; b["cards"] += 1
    return s


def read_summaries(sb, d1, d2):
    rows = stale_keys.read_window_rows(sb, funnel.SUMMARY_TABLE, "order_date,orders_qty,orders_amount,loaded_at",
                                       [("eq", "marketplace_code", "wb"), ("eq", "source", funnel.SOURCE),
                                        ("gte", "order_date", d1), ("lte", "order_date", d2)], ["order_date"])
    return {str(r["order_date"]): r for r in rows}


def recheck_snapshot(day):
    """Первая страница дня из снимка 2026-09-23 (WB-6 §2): {nmId: orderSum}, или None."""
    path = os.path.join(RECHECK_DIR, f"funnel_{day}.json")
    if not os.path.exists(path):
        return None
    d = json.load(open(path, encoding="utf-8"))
    return {(p.get("product") or {}).get("nmId"): Decimal(str(((p.get("statistic") or {}).get("selected") or {}).get("orderSum") or 0))
            for p in (d.get("data") or {}).get("products") or []}


def plan(sb, files):
    if not files:
        print("файлов сырья нет — сначала --fetch")
        return None
    days = sorted(files)
    summaries = read_summaries(sb, days[0], days[-1]) if sb is not None else {}
    print(f"Сырьё: дней {len(days)} ({days[0]} … {days[-1]}), обращений за съём — "
          f"{sum(e['requests'] for e in json.load(open(CALLS_PATH))) if os.path.exists(CALLS_PATH) else '—'}")
    print(f"\n{'день':11}{'карт.':>6}{'стр.':>5}{'полн.':>6}{'заказов':>8}{'orderSum':>13}{'t (Дискаунтер)':>15}{'не f/t':>8}{'buyoutSum':>12}"
          f"{'сводка в базе':>14}{'разница':>11}{'снимок 09-23 (стр. 1)':>24}")
    tot = Counter()
    letters_seen = Counter()
    for day in days:
        f = files[day]
        s = day_summary(f["products"])
        other = sum((v["sum"] for k, v in s["by_letter"].items() if k not in ("f", DISCOUNTER_LETTER)), Z)
        for k, v in s["by_letter"].items():
            if k not in ("f", DISCOUNTER_LETTER):
                letters_seen[k] += v["cards"]
        base = summaries.get(day)
        base_sum = Decimal(str(base["orders_amount"])) if base else None
        diff = (s["sum"] - base_sum) if base_sum is not None else None
        snap = recheck_snapshot(day)
        snap_text = ""
        if snap is not None:
            now_by_nm = {(p.get("product") or {}).get("nmId"): Decimal(str(((p.get("statistic") or {}).get("selected") or {}).get("orderSum") or 0)) for p in f["products"]}
            snap_sum = sum(snap.values(), Z)
            now_same = sum((now_by_nm.get(k, Z) for k in snap), Z)
            snap_text = f"{snap_sum:,.0f} → {now_same:,.0f} ({now_same - snap_sum:+,.0f})"
        print(f"{day:11}{s['cards']:>6}{f['pages']:>5}{'да' if f['complete'] else 'НЕТ':>6}{s['qty']:>8,.0f}{s['sum']:>13,.0f}"
              f"{s['by_letter'][DISCOUNTER_LETTER]['sum']:>15,.0f}{other:>8,.0f}{s['buyout_sum']:>12,.0f}"
              f"{(f'{base_sum:,.0f}' if base_sum is not None else '—'):>14}{(f'{diff:+,.0f}' if diff is not None else '—'):>11}{snap_text:>24}")
        tot["cards"] += s["cards"]; tot["qty"] += s["qty"]; tot["sum"] += s["sum"]; tot["t"] += s["by_letter"][DISCOUNTER_LETTER]["sum"]
        tot["base"] += base_sum or Z
        tot["days_equal"] += 1 if diff == 0 else 0
        tot["incomplete"] += 0 if f["complete"] else 1
    print(f"{'итого':11}{tot['cards']:>6}{'':>5}{'':>6}{tot['qty']:>8,.0f}{tot['sum']:>13,.0f}{tot['t']:>15,.0f}{'':>8}{'':>12}{tot['base']:>14,.0f}"
          f"{tot['sum'] - tot['base']:>+11,.0f}")
    print(f"дней, где сводка в базе = сырью: {tot['days_equal']} из {len(days)}; неполных дней (повтор nmId между страницами): {tot['incomplete']}; "
          f"карточек с буквой не f/t: {dict(letters_seen) or 0}")
    print(f"К записи: строк по товарам {tot['cards']}, сводок дней {len(days)}; db_writes = 0")
    return days


def apply(sb, files):
    observed_at = datetime.now(timezone.utc).isoformat()
    written = deleted = 0
    for day in sorted(files):
        f = files[day]
        rows = [r for r in (funnel.build_product_row(day, p, observed_at) for p in f["products"]) if r is not None]
        funnel.save_day({"order_date": day, "marketplace_code": "wb", "orders_qty": float(f["orders_qty"]),
                         "orders_amount": float(f["orders_amount"]), "source": funnel.SOURCE, "products_count": len(f["products"])}, sb)
        w, d = funnel.save_products(sb, day, rows, f["complete"], apply=True)
        written += w; deleted += d
    print(f"✅ {funnel.TABLE}: upsert {written} строк за {len(files)} дней, застрявших удалено {deleted}; сводок дней {len(files)} ({observed_at})")
    return written, deleted


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--approve-wb-funnel-write", action="store_true")
    ap.add_argument("--date-from", required=True)
    ap.add_argument("--date-to", required=True)
    args = ap.parse_args(argv)
    if args.fetch:
        return fetch(args.date_from, args.date_to)
    if not (args.plan or args.apply):
        ap.error("нужен --fetch, --plan или --apply")
    sb = funnel.supabase
    files = load_files(args.date_from, args.date_to)
    days = plan(sb, files)
    if args.apply:
        if not args.approve_wb_funnel_write:
            print("--apply без --approve-wb-funnel-write: не пишу")
            return 2
        if not days:
            return 2
        if in_nightly_run_window(datetime.now(timezone.utc)):
            raise SystemExit(f"ночное окно ({window_text()}): запись отложить")
        apply(sb, files)
    return 0


if __name__ == "__main__":
    sys.exit(main())
