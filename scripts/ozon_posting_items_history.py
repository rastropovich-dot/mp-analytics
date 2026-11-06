#!/usr/bin/env python3
"""История ozon_posting_items из сырья отправлений на диске (шестьдесят первая §3). Ozon не зовёт.

Источник — файлы data/<кабинет>/postings_raw/: history_<схема>_<от>_<до>.json (rebuild_ozon_orders_history.py --fetch) и
ночные снимки <схема>_<UTC>.json (dump_raw шагов заказов). У отправления, которое лежит в нескольких файлах, берётся
наблюдение из самого свежего файла (fetched_at). Строки — то же правило, что ночью (loaders/ozon_posting_items.item_rows),
observed_at = момент сбора файла, source = 'history:<файл>'.

Режимы:
    (без флагов)  план: строки и штуки по месяцам; сверка Σ штук и Σ штуки × цена продавца по (день, SKU, схема) с
                  marketplace_orders (созданные = подтверждённые + отменённые); дни окна без сырья названы; строки, которые
                  в таблице уже есть с более свежим observed_at (ночь), не пишутся и посчитаны отдельно
    --apply       то же и запись upsert-ом по (posting_number, sku) — по слову владельца; в окне ночного прогона не стартует

Подтверждённые / отменённые сверяются справочно: статус в файле — на момент его сбора, а marketplace_orders за старые дни
переписан последней ночью, когда день был в 30-дневном окне; разница — дозревание между двумя сборами, не потеря строк.

    venv/bin/python3 scripts/ozon_posting_items_history.py --date-from 2026-08-23 --date-to 2026-11-05 [--raw-dir …] [--apply]
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from math import ceil
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
import cabinet  # noqa: E402
CABINET = cabinet.assert_env()  # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента
from loaders import ozon_posting_items as items  # noqa: E402
from loaders import ozon_orders_rows as rules  # noqa: E402
from loaders import stale_keys  # noqa: E402
from loaders.pipeline_window import in_nightly_run_window, window_text  # noqa: E402

RAW_DIR = cabinet.data_path("postings_raw", prof=CABINET)
CENT = Decimal("0.01")
APP_TIMEZONE = rules.APP_TIMEZONE
PAGE = 100                 # отправлений на страницу списка FBO /v3 и FBS /v4 (спека: maximum 100)


def raw_files(raw_dir, schema):
    """[(fetched_at, путь)] — history_* и ночные снимки одной схемы; part_* (куски history) и precheck_* не берутся."""
    out = []
    for path in glob.glob(os.path.join(raw_dir, f"*{schema}_*.json")):
        name = os.path.basename(path)
        if not (name.startswith(f"history_{schema}_") or name.startswith(f"{schema}_")):
            continue
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("schema", schema) != schema:
            continue
        out.append((str(data.get("fetched_at") or ""), path, data.get("postings") or []))
    return sorted(out, key=lambda t: t[0])


def latest_postings(files):
    """{posting_number: (posting, fetched_at, файл)} — наблюдение из самого свежего файла."""
    out = {}
    for fetched_at, path, postings in files:   # по возрастанию fetched_at: позднее перезаписывает раннее
        for p in postings:
            number = str(p.get("posting_number") or "")
            if number:
                out[number] = (p, fetched_at, os.path.basename(path))
    return out


def local_day(ts):
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(APP_TIMEZONE)).date().isoformat()


def build_rows(raw_dir, schema, date_from, date_to):
    """(строки окна, счётчики правила, файлы, полные дни). День полон, если его накрывает файл, снятый ПОСЛЕ конца дня
    (по МСК): файл, снятый днём, держит только часть заказов этого дня (KARATOV 22.09 — 23:14 МСК, 24.10 — 14:52 МСК)."""
    files = raw_files(raw_dir, schema)
    by_file = defaultdict(list)
    for number, (p, fetched_at, name) in latest_postings(files).items():
        by_file[(fetched_at, name)].append(p)
    complete, info = set(), []
    for fetched_at, path, postings in files:
        days = [d for d in (rules.order_date_of(p, schema) for p in postings) if d]
        fetched_day = local_day(fetched_at) if fetched_at else None
        if days and fetched_day:
            d, last = date.fromisoformat(min(days)), date.fromisoformat(min(max(days), fetched_day))
            while d <= last:
                if d.isoformat() < fetched_day:
                    complete.add(d.isoformat())
                d += timedelta(days=1)
        info.append((fetched_at, os.path.basename(path), len(postings), min(days) if days else None, max(days) if days else None))
    rows, counters = [], defaultdict(int)
    for (fetched_at, name), postings in sorted(by_file.items()):
        part, c = items.item_rows(postings, schema, fetched_at, source=f"history:{name}")
        for k, v in c.items():
            counters[k] += v
        rows.extend(r for r in part if date_from <= r["order_date"] <= date_to)
    return rows, dict(counters), info, complete


def read_orders(sb, schema, day_from, day_to):
    out, day, end = [], date.fromisoformat(day_from), date.fromisoformat(day_to)
    while day <= end:
        out.extend(stale_keys.read_window_rows(
            sb, "marketplace_orders",
            "order_date,marketplace_sku,order_schema,orders_qty,cancelled_orders_qty,orders_amount_seller,cancelled_orders_amount_seller",
            [("eq", "marketplace_code", "ozon"), ("eq", "order_schema", schema), ("eq", "order_date", day.isoformat())],
            ["marketplace_sku"]))
        day += timedelta(days=1)
    return out


def read_existing(sb, schema, day_from, day_to):
    """{(posting_number, sku): observed_at} таблицы за окно; таблицы нет — None."""
    try:
        rows = []
        day, end = date.fromisoformat(day_from), date.fromisoformat(day_to)
        while day <= end:
            rows.extend(stale_keys.read_window_rows(sb, items.TABLE, "posting_number,sku,observed_at",
                                                    [("eq", "schema", schema), ("eq", "order_date", day.isoformat())],
                                                    ["posting_number", "sku"]))
            day += timedelta(days=1)
    except Exception as exc:  # noqa: BLE001
        if "PGRST205" in str(exc) or "does not exist" in str(exc):
            return None
        raise
    return {(str(r["posting_number"]), str(r["sku"])): str(r["observed_at"]) for r in rows}


def newer_in_table(rows, existing):
    """Строки, у которых в таблице наблюдение свежее файла (ночь уже переписала ключ), — не пишутся."""
    if not existing:
        return rows, []
    keep, skip = [], []
    for r in rows:
        seen = existing.get((r["posting_number"], str(r["sku"])))
        if seen and datetime.fromisoformat(seen.replace("Z", "+00:00")) >= datetime.fromisoformat(str(r["observed_at"]).replace("Z", "+00:00")):
            skip.append(r)
        else:
            keep.append(r)
    return keep, skip


def created_by_day(item_rows_, order_rows):
    """По дням: (ключей, ключей с расхождением по созданным, Σ штук отправлений, Σ штук заказов, Σ ₽ отпр., Σ ₽ заказов)."""
    a, b = items.day_sums(item_rows_), items.order_sums(order_rows)
    out = defaultdict(lambda: [0, 0, Decimal(0), Decimal(0), Decimal(0), Decimal(0)])
    for key in set(a) | set(b):
        x, y = a.get(key, (Decimal(0), Decimal(0))), b.get(key, (Decimal(0), Decimal(0)))
        d = out[key[0]]
        d[0] += 1
        d[1] += int(x[0] != y[0] or abs(x[1] - y[1]) > CENT)
        d[2] += x[0]; d[3] += y[0]; d[4] += x[1]; d[5] += y[1]
    return dict(out)


def cancelled_by_month(item_rows_, order_rows, days):
    """Справочно: отменённые штуки сырья (статус в файле) против cancelled_orders_qty заказов по месяцам."""
    out = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for r in item_rows_:
        if r["order_date"] in days and r["status"] in rules.CANCELLED_STATUSES:
            out[r["order_date"][:7]][0] += r["quantity"]
    for r in order_rows:
        if str(r["order_date"]) in days:
            out[str(r["order_date"])[:7]][1] += Decimal(str(r.get("cancelled_orders_qty") or 0))
    return dict(out)


def plan(sb, schema, date_from, date_to, raw_dir, night_from=None, apply=False, out=print):
    """night_from — первый день окна первой ночи на новом коде: дни ≥ него пишет ночь, история их не берёт."""
    rows_all, counters, files, complete = build_rows(raw_dir, schema, date_from, date_to)
    out(f"=== {CABINET.DISPLAY_NAME} · {schema.upper()} · {date_from} … {date_to}"
        + (f" · дни с {night_from} пишет первая ночь" if night_from else ""))
    for fetched_at, name, n, d1, d2 in files:
        out(f"  файл {name}: отправлений {n}, дни {d1} … {d2}, собран {fetched_at[:19]} UTC")
    all_days, d = [], date.fromisoformat(date_from)
    while d <= date.fromisoformat(date_to):
        all_days.append(d.isoformat()); d += timedelta(days=1)
    hist_days = [x for x in all_days if not night_from or x < night_from]
    rows = [r for r in rows_all if r["order_date"] in complete and r["order_date"] in hist_days]
    with_rows = {r["order_date"] for r in rows_all}
    incomplete = [x for x in hist_days if x in with_rows and x not in complete]
    no_raw = [x for x in hist_days if x not in with_rows]
    out(f"  к записи из сырья: строк {len(rows)}, штук {sum(r['quantity'] for r in rows)} (полных дней {len({r['order_date'] for r in rows})}); "
        f"пропущено правилом {{{', '.join(f'{k}: {v}' for k, v in counters.items() if k not in ('items', 'rows', 'buyer_known', 'buyer_unknown'))}}}; "
        f"цена покупателя неизвестна у {sum(1 for r in rows if r['buyer_price'] is None)}")
    orders = read_orders(sb, schema, date_from, date_to)
    days = created_by_day(rows_all, orders)
    good = sorted({r["order_date"] for r in rows})
    by_month = defaultdict(lambda: [0, 0, Decimal(0), Decimal(0), 0])
    for x in good:
        v = days.get(x, [0, 0, 0, 0, 0, 0])
        m = by_month[x[:7]]
        m[0] += v[2]; m[1] += v[3]; m[2] += v[4]; m[3] += v[5]; m[4] += 1
    canc = cancelled_by_month(rows, orders, set(good))
    for month, (qi, qo, ai, ao, n) in sorted(by_month.items()):
        ci, co = canc.get(month, (0, 0))
        out(f"  {month}: дней {n}, созданные штук {qi} / заказов {int(qo)} (Δ {int(qi - qo)}), ₽ {ai:,.2f} / {ao:,.2f} (Δ {ai - ao:,.2f}); "
            f"отменённые штук {int(ci)} / {int(co)} (Δ {int(ci - co)}, справочно)".replace(",", " "))
    differ = [x for x in good if days.get(x, [0, 0])[1] > 0]
    out(f"  полные дни: тождество по созданным держится {len(good) - len(differ)}, расходится {len(differ)}")
    for x in differ[:15]:
        v = days[x]
        out(f"    {x}: ключей {v[0]}, с расхождением {v[1]}; штук {v[2]} / {v[3]} (Δ {v[2] - v[3]}), ₽ {v[4]} / {v[5]} (Δ {v[4] - v[5]})")
    need = sorted(set(no_raw) | set(incomplete))
    if incomplete:
        out(f"  дни с неполным сырьём (файл снят в тот же день) — из сырья НЕ пишутся: {_ranges(incomplete)}")
    if no_raw:
        out(f"  дни без сырья: {_ranges(no_raw)}")
    if need:
        recent = [x for x in good][-14:]
        per_day = (sum(1 for r in rows if r["order_date"] in set(recent)) / len(recent)) if recent else 0
        postings = ceil(per_day * len(need))
        out(f"  API для них: ~{postings} отправлений (среднее {per_day:.0f} строк/день по 14 полным дням) → ~{ceil(postings / PAGE) + len(_ranges(need).split(', '))} страниц "
            f"по {PAGE} ({len(_ranges(need).split(', '))} окн.), ~{ceil(postings / PAGE) * 2.05 / 60:.1f} мин при паузе 1,5 с")
    else:
        out("  API: 0 обращений — все дни истории есть в полном сырье")
    existing = read_existing(sb, schema, date_from, date_to)
    keep, skip = newer_in_table(rows, existing)
    out(f"  таблица: " + ("нет (миграция не применена) — к записи все строки" if existing is None else
                          f"ключей окна {len(existing)}; к записи {len(keep)}, не писать (в таблице свежее) {len(skip)}"))
    if apply:
        n = items.upsert(sb, keep)
        out(f"  ✅ записано {n} строк")
    else:
        out("  план; без --apply ничего не пишется (db_writes 0)")
    return {"rows": len(rows), "to_write": len(keep), "skip_newer": len(skip), "days_differ": differ, "days_need_api": need}


def _ranges(days):
    out, start, prev = [], None, None
    for x in days:
        d = date.fromisoformat(x)
        if start is None:
            start = prev = d
        elif d == prev + timedelta(days=1):
            prev = d
        else:
            out.append((start, prev)); start = prev = d
    if start:
        out.append((start, prev))
    return ", ".join(a.isoformat() if a == b else f"{a.isoformat()} … {b.isoformat()}" for a, b in out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="История ozon_posting_items из сырья на диске: план со сверкой с marketplace_orders; --apply по слову")
    ap.add_argument("--date-from", required=True)
    ap.add_argument("--date-to", required=True)
    ap.add_argument("--schema", choices=("fbo", "fbs", "both"), default="both")
    ap.add_argument("--raw-dir", default=RAW_DIR)
    ap.add_argument("--night-from", help="первый день окна первой ночи на новом коде (дни с него история не пишет)")
    ap.add_argument("--apply", action="store_true", help="записать (по слову владельца)")
    args = ap.parse_args(argv)
    if args.apply and in_nightly_run_window(datetime.now(timezone.utc)):
        raise SystemExit(f"окно ночного прогона ({window_text()}) — --apply не стартует")
    from loaders.ozon_fbo_orders_loader import supabase as sb
    for schema in (("fbo", "fbs") if args.schema == "both" else (args.schema,)):
        plan(sb, schema, args.date_from, args.date_to, args.raw_dir, night_from=args.night_from, apply=args.apply)
    if not args.apply:
        print("db_writes = 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
