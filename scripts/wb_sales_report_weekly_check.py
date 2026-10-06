#!/usr/bin/env python3
"""Еженедельные отчёты реализации WB против ежедневных (пятнадцатая задача WB, §2). Только чтение.

    venv/bin/python3 scripts/wb_sales_report_weekly_check.py --fetch     снять weekly-сырьё в data/wb_sales_report_raw/weekly_<от>_<до>.json
    venv/bin/python3 scripts/wb_sales_report_weekly_check.py --compare   weekly-файлы против wb_sales_report_rows (daily) за те же даты

--fetch: тот же метод и тот же сборщик, что у ежедневных (loaders/wb_sales_report_loader.fetch_period), period=weekly,
куски по CHUNK_WEEKS недель Пн…Вс от WEEK_START до WEEK_END (неделя, которая ещё не закрыта, недельного отчёта не имеет);
файл есть — в API не идёт; пауза 65 с между обращениями (лимит метода 1/мин); ночное окно прогона — стоп;
обращения — в calls.json рядом с ежедневными. Ответ проверяется мягко: повторы rrdId и строки вне куска печатаются,
файл сохраняется в любом случае — это тоже знание.

--compare: daily — таблица wb_sales_report_rows (чтение по ключу rrd_id > последнего, страницы по 1 000, кэш в файле),
weekly — файлы. По неделям (понедельник rr_date) Σ всех числовых полей; множества rrd_id — только в weekly / только в
daily — с суммами; общие rrd_id — поле за полем; отчёт REPORT_OF_INTEREST — даты, строки, «Корректировка» и сумма
AMOUNT_OF_INTEREST со знаком по всем числовым полям. db_writes = 0. Деньги — Decimal.
"""
import argparse
import glob
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")

import loaders.wb_sales_report_loader as loader  # noqa: E402
from loaders.pipeline_window import in_nightly_run_window, window_text  # noqa: E402

RAW_DIR = os.path.join("data", "wb_sales_report_raw")
CALLS_PATH = os.path.join(RAW_DIR, "calls.json")
WEEK_START = "2026-03-30"      # понедельник недели, в которую входит 01.04
WEEK_END = "2026-09-27"        # воскресенье последней закрытой недели на 2026-09-30
CHUNK_WEEKS = 4
REPORT_OF_INTEREST = 63466988
AMOUNT_OF_INTEREST = Decimal("7119571.15")
Z = Decimal(0)

NUM_COLS = [dst for _src, dst, kind in loader.FIELDS if kind in ("n", "i") and dst not in ("report_id", "nm_id", "shk_id")]
TEXT_COLS = ["rr_date", "sale_dt", "order_dt", "seller_oper_name", "bonus_type_name", "doc_type", "nm_id", "srid",
             "office_name", "tech_size", "vendor_code"]
KEY_COLS = ["quantity", "retail_price_with_disc", "retail_amount", "for_pay", "ppvz_sales_commission", "ppvz_reward",
            "delivery_service", "rebill_logistic_cost", "paid_storage", "deduction", "penalty", "additional_payment",
            "acquiring_fee", "paid_acceptance"]


def D(value):
    return Decimal(str(value)) if value not in (None, "") else Z


def monday(day_text):
    d = date.fromisoformat(str(day_text)[:10])
    return (d - timedelta(days=d.weekday())).isoformat()


def chunks(start=WEEK_START, end=WEEK_END, weeks=CHUNK_WEEKS):
    d, stop = date.fromisoformat(start), date.fromisoformat(end)
    assert d.weekday() == 0 and stop.weekday() == 6, "куски только Пн…Вс"
    while d <= stop:
        last = min(d + timedelta(days=7 * weeks - 1), stop)
        yield d.isoformat(), last.isoformat()
        d = last + timedelta(days=1)


def weekly_path(d1, d2):
    return os.path.join(RAW_DIR, f"weekly_{d1}_{d2}.json")


def fetch(sleep_fn=time.sleep):
    if in_nightly_run_window():
        print(f"ночное окно прогона {window_text()} — не ходим в WB, стоп", flush=True)
        return 2
    os.makedirs(RAW_DIR, exist_ok=True)
    ledger = json.load(open(CALLS_PATH, encoding="utf-8")) if os.path.exists(CALLS_PATH) else []
    counters = {"requests": 0, "429": 0, "transient": 0}
    for d1, d2 in chunks():
        path = weekly_path(d1, d2)
        if os.path.exists(path):
            print(f"{path}: файл есть — в API не идём", flush=True)
            continue
        before = dict(counters)
        items = loader.fetch_period(d1, d2, counters, period="weekly", sleep_fn=sleep_fn)
        ids = [int(x["rrdId"]) for x in items]
        dups = len(ids) - len(set(ids))
        outside = sorted({str(x.get("rrDate"))[:10] for x in items if not (d1 <= str(x.get("rrDate"))[:10] <= d2)})
        reports = sorted({(int(x.get("reportId")), str(x.get("dateFrom")), str(x.get("dateTo")), str(x.get("createDate")))
                          for x in items})
        print(f"weekly {d1}…{d2}: строк {len(items)}, отчётов {len(reports)}, повторов rrdId {dups}, "
              f"rrDate вне куска: {outside[:6] if outside else 'нет'}", flush=True)
        for rep in reports:
            print(f"    отчёт {rep[0]}: {rep[1]} … {rep[2]}, создан {rep[3]}", flush=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, default=str)
        ledger.append({"chunk": f"weekly_{d1}_{d2}", "at_utc": datetime.now(timezone.utc).isoformat(),
                       "requests": counters["requests"] - before["requests"], "rows": len(items),
                       "429": counters["429"] - before["429"], "transient": counters["transient"] - before["transient"]})
        with open(CALLS_PATH, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
    print(f"итого обращений {counters['requests']}, 429 — {counters['429']}, сеть — {counters['transient']}", flush=True)
    return 0


def load_weekly():
    rows, seen = [], set()
    for path in sorted(glob.glob(os.path.join(RAW_DIR, "weekly_*.json"))):
        items = json.load(open(path, encoding="utf-8"))
        for item in items:
            row = loader.build_row(item, "files")
            row["report_period"] = "weekly"
            if row["rrd_id"] in seen:
                raise RuntimeError(f"rrdId {row['rrd_id']} встречается дважды в weekly-файлах")
            seen.add(row["rrd_id"])
            rows.append(row)
        print(f"{path}: строк {len(items)}", flush=True)
    return rows


def load_daily_db(sb, d1, d2, cache_path, page=1000):
    if cache_path and os.path.exists(cache_path):
        rows = json.load(open(cache_path, encoding="utf-8"))
        print(f"daily из кэша {cache_path}: строк {len(rows)}", flush=True)
        return rows
    cols = ",".join(["rrd_id", "report_id", "report_period", "report_date_from", "report_date_to", "report_create_date"]
                    + TEXT_COLS + NUM_COLS)
    rows, last, pages, t0 = [], -1, 0, time.time()
    while True:
        res = (sb.table(loader.TABLE).select(cols).gte("rr_date", d1).lte("rr_date", d2)
               .gt("rrd_id", last).order("rrd_id").limit(page).execute())
        data = res.data or []
        rows.extend(data)
        pages += 1
        if len(data) < page:
            break
        last = data[-1]["rrd_id"]
    print(f"daily из {loader.TABLE} {d1}…{d2}: строк {len(rows)} за {pages} страниц, {time.time() - t0:.1f} с", flush=True)
    if cache_path:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
    return rows


def fmt(value):
    return f"{value:,.2f}".replace(",", " ")


def sums_by(rows, key_fn, cols):
    out = defaultdict(lambda: defaultdict(Decimal))
    for r in rows:
        k = key_fn(r)
        out[k]["rows"] += 1
        for c in cols:
            out[k][c] += D(r.get(c))
    return out


def describe_rows(rows, title, limit=40):
    print(f"\n{title}: строк {len(rows)}")
    if not rows:
        return
    if len(rows) <= limit:
        for r in sorted(rows, key=lambda x: (str(x.get('rr_date')), x['rrd_id'])):
            print(f"    rrd_id {r['rrd_id']} | {r.get('rr_date')} | отчёт {r.get('report_id')} {r.get('report_date_from')}…{r.get('report_date_to')} | "
                  f"{r.get('seller_oper_name')} | {str(r.get('bonus_type_name') or '')[:60]} | nm {r.get('nm_id')} | "
                  f"цена {fmt(D(r.get('retail_price_with_disc')))} | к перечисл. {fmt(D(r.get('for_pay')))} | "
                  f"удерж. {fmt(D(r.get('deduction')))} | доплата {fmt(D(r.get('additional_payment')))} | штраф {fmt(D(r.get('penalty')))}")
    grouped = sums_by(rows, lambda r: (monday(r["rr_date"]), r.get("seller_oper_name"), str(r.get("bonus_type_name") or "")[:50]),
                      ["retail_price_with_disc", "for_pay", "deduction", "additional_payment", "penalty", "paid_storage",
                       "delivery_service", "acquiring_fee"])
    print("    по (неделя, операция, вид): строк | цена | к перечислению | удержания | доплаты | штрафы | хранение | доставка | эквайринг")
    for k in sorted(grouped):
        g = grouped[k]
        print(f"    {k[0]} | {k[1]} | {k[2]} | {int(g['rows'])} | {fmt(g['retail_price_with_disc'])} | {fmt(g['for_pay'])} | "
              f"{fmt(g['deduction'])} | {fmt(g['additional_payment'])} | {fmt(g['penalty'])} | {fmt(g['paid_storage'])} | "
              f"{fmt(g['delivery_service'])} | {fmt(g['acquiring_fee'])}")


def compare(cache_path):
    weekly = load_weekly()
    if not weekly:
        print("weekly-файлов нет — сначала --fetch")
        return 1
    d1 = min(r["rr_date"] for r in weekly)
    d2 = max(r["rr_date"] for r in weekly)
    print(f"weekly: строк {len(weekly)}, rr_date {d1} … {d2}, отчётов {len({r['report_id'] for r in weekly})}")
    sb = loader._client()
    daily = load_daily_db(sb, d1, d2, cache_path)
    periods = defaultdict(int)
    for r in daily:
        periods[r.get("report_period")] += 1
    print(f"daily: строк {len(daily)}, report_period {dict(periods)}, отчётов {len({r['report_id'] for r in daily})}")

    w_by = {r["rrd_id"]: r for r in weekly}
    d_by = {r["rrd_id"]: r for r in daily}
    only_w = [w_by[k] for k in w_by.keys() - d_by.keys()]
    only_d = [d_by[k] for k in d_by.keys() - w_by.keys()]
    common = w_by.keys() & d_by.keys()
    print(f"\nrrd_id: общих {len(common)}, только в weekly {len(only_w)}, только в daily {len(only_d)}")

    # Недельные отчёты как они есть
    reps = sums_by(weekly, lambda r: (r["report_id"], r["report_date_from"], r["report_date_to"], r["report_create_date"]),
                   ["retail_price_with_disc", "for_pay"])
    print("\nнедельные отчёты: id | период | создан | строк | цена | к перечислению")
    for k in sorted(reps, key=lambda x: (x[1], x[0])):
        g = reps[k]
        print(f"    {k[0]} | {k[1]} … {k[2]} | {k[3]} | {int(g['rows'])} | {fmt(g['retail_price_with_disc'])} | {fmt(g['for_pay'])}")

    # По неделям: Σ полей weekly против daily
    ws = sums_by(weekly, lambda r: monday(r["rr_date"]), NUM_COLS)
    ds = sums_by(daily, lambda r: monday(r["rr_date"]), NUM_COLS)
    weeks = sorted(set(ws) | set(ds))
    print("\nпо неделям (понедельник rr_date): weekly − daily по ключевым полям; «=» — 0,00 по всем числовым полям")
    head = " | ".join(KEY_COLS)
    print(f"    неделя | строк w/d | {head}")
    total_diff = defaultdict(Decimal)
    for wk in weeks:
        diffs = {c: ws[wk][c] - ds[wk][c] for c in NUM_COLS}
        for c in NUM_COLS:
            total_diff[c] += diffs[c]
        if all(v == 0 for v in diffs.values()) and ws[wk]["rows"] == ds[wk]["rows"]:
            print(f"    {wk} | {int(ws[wk]['rows'])}/{int(ds[wk]['rows'])} | =")
        else:
            cells = " | ".join(fmt(diffs[c]) for c in KEY_COLS)
            other = {c: fmt(diffs[c]) for c in NUM_COLS if c not in KEY_COLS and diffs[c] != 0}
            print(f"    {wk} | {int(ws[wk]['rows'])}/{int(ds[wk]['rows'])} | {cells}" + (f" | прочие поля: {other}" if other else ""))
    print("    Σ weekly по ключевым полям: " + " | ".join(fmt(sum((ws[w][c] for w in weeks), Z)) for c in KEY_COLS))
    print("    Σ daily  по ключевым полям: " + " | ".join(fmt(sum((ds[w][c] for w in weeks), Z)) for c in KEY_COLS))
    print("    Σ разницы по всем числовым полям: " + ", ".join(f"{c} {fmt(v)}" for c, v in total_diff.items() if v != 0) or "    разниц нет")

    # Общие rrd_id — поле за полем
    diff_count, diff_sum, text_diff = defaultdict(int), defaultdict(Decimal), defaultdict(int)
    examples = defaultdict(list)
    for k in common:
        w, d = w_by[k], d_by[k]
        for c in NUM_COLS:
            if D(w.get(c)) != D(d.get(c)):
                diff_count[c] += 1
                diff_sum[c] += D(w.get(c)) - D(d.get(c))
                if len(examples[c]) < 3:
                    examples[c].append((k, w.get("rr_date"), w.get(c), d.get(c)))
        for c in TEXT_COLS:
            if str(w.get(c) if w.get(c) is not None else "") != str(d.get(c) if d.get(c) is not None else ""):
                text_diff[c] += 1
                if len(examples[c]) < 3:
                    examples[c].append((k, w.get("rr_date"), w.get(c), d.get(c)))
    print(f"\nобщие rrd_id ({len(common)}): числовые поля с разницей — "
          + (", ".join(f"{c}: строк {diff_count[c]}, Σ(w−d) {fmt(diff_sum[c])}" for c in NUM_COLS if diff_count[c]) or "нет"))
    print("    текстовые поля с разницей — " + (", ".join(f"{c}: {n}" for c, n in text_diff.items()) or "нет"))
    for c, ex in examples.items():
        print(f"    примеры {c}: {ex}")

    describe_rows(only_w, "только в weekly (нет в daily)")
    describe_rows(only_d, "только в daily (нет в weekly)")

    # Отчёт REPORT_OF_INTEREST
    rows_i = [r for r in weekly if r["report_id"] == REPORT_OF_INTEREST]
    print(f"\nотчёт {REPORT_OF_INTEREST}: строк {len(rows_i)}")
    if rows_i:
        r0 = rows_i[0]
        print(f"    период {r0['report_date_from']} … {r0['report_date_to']}, создан {r0['report_create_date']}, "
              f"rr_date {min(r['rr_date'] for r in rows_i)} … {max(r['rr_date'] for r in rows_i)}")
        s = sums_by(rows_i, lambda r: "все", KEY_COLS)["все"]
        print("    Σ: " + ", ".join(f"{c} {fmt(s[c])}" for c in KEY_COLS))
        by_oper = sums_by(rows_i, lambda r: (r.get("seller_oper_name"), str(r.get("bonus_type_name") or "")[:70]),
                          ["retail_price_with_disc", "for_pay", "deduction", "additional_payment", "penalty"])
        print("    по (операция, вид): строк | цена | к перечислению | удержания | доплаты | штрафы")
        for k in sorted(by_oper, key=lambda x: str(x)):
            g = by_oper[k]
            print(f"    {k[0]} | {k[1]} | {int(g['rows'])} | {fmt(g['retail_price_with_disc'])} | {fmt(g['for_pay'])} | "
                  f"{fmt(g['deduction'])} | {fmt(g['additional_payment'])} | {fmt(g['penalty'])}")
        corr = [r for r in rows_i if (r.get("seller_oper_name") or "").lower().startswith("корректировк") or D(r.get("additional_payment")) != 0]
        describe_rows(corr, f"    в отчёте {REPORT_OF_INTEREST}: «Корректировка» или доплата ≠ 0")
    hits = [(r["rrd_id"], r["rr_date"], r["report_id"], r.get("seller_oper_name"), str(r.get("bonus_type_name") or "")[:60], c, r.get(c))
            for r in weekly for c in NUM_COLS if abs(D(r.get(c))) == AMOUNT_OF_INTEREST]
    print(f"\nсумма {fmt(AMOUNT_OF_INTEREST)} (по модулю) в weekly-строках: {len(hits)}")
    for h in hits:
        print(f"    rrd_id {h[0]} | {h[1]} | отчёт {h[2]} | {h[3]} | {h[4]} | поле {h[5]} = {h[6]}")
    hits_d = [(r["rrd_id"], r["rr_date"], r["report_id"], r.get("seller_oper_name"), c, r.get(c))
              for r in daily for c in NUM_COLS if abs(D(r.get(c))) == AMOUNT_OF_INTEREST]
    print(f"та же сумма в daily-строках окна: {len(hits_d)} {hits_d[:5]}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--daily-cache", default=os.path.join("logs", f"wb15_daily_db_{WEEK_START}_{WEEK_END}.json"),
                    help="кэш чтения daily из базы (json); пусто — без кэша")
    args = ap.parse_args(argv)
    if args.fetch:
        code = fetch()
        if code:
            return code
    if args.compare:
        return compare(args.daily_cache or None)
    if not (args.fetch or args.compare):
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
