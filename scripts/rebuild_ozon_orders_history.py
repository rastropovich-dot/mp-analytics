#!/usr/bin/env python3
"""Пересборка истории заказов Ozon по одному правилу (подтверждённые + отменённые). Запускает владелец.

После починки загрузчиков отменённые появятся в таблице только вперёд; история с
2026-03-28 останется без них, и ряд разорвётся. Этот скрипт собирает историю
теми же методами (/v3/posting/fbo/list, /v4/posting/fbs/list; период до года,
окнами по 30 дней) и переписывает marketplace_orders за окно целиком.

Режимы (по возрастанию необратимости):

    --estimate            обращения, время, что перепишется — без API и без записи
    --fetch               снять сырьё в data/postings_raw/history_<схема>_<from>_<to>.json;
                          есть файл — в API не идёт (перезапуск бесплатен)
    --plan                строки по новому правилу из файлов против таблицы, помесячно
    --apply               снимок → delete за окно по схеме → запись → контроль счётчиков

Без --apply в БД не пишет (db_writes = 0). Витрины НЕ трогает: ночной
reports_daily_sku_kpi строит KPI из всех заказов заново; ключи витрин, у которых
заказов не останется, надо удалить отдельно (в отчёте). В окне ночного прогона
00:15…03:15 UTC не стартует.

    venv/bin/python3 scripts/rebuild_ozon_orders_history.py --estimate
    venv/bin/python3 scripts/rebuild_ozon_orders_history.py --fetch --date-from 2026-03-28
    venv/bin/python3 scripts/rebuild_ozon_orders_history.py --plan  --date-from 2026-03-28
    venv/bin/python3 scripts/rebuild_ozon_orders_history.py --apply --date-from 2026-03-28
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from loaders import http_retry  # noqa: E402
from loaders import ozon_orders_rows as rules  # noqa: E402

RAW_DIR = os.path.join("data", "postings_raw")
SNAP_DIR = os.path.join("data", "snapshots")
CHUNK_DAYS = 30
PAGE = 100
SECONDS_PER_CALL = 2.05       # замер 2026-09-14, FBO /v3, пауза 1,5 с (docs/ozon_postings_migration.md)
NIGHT_WINDOW = ((0, 15), (3, 15))
BATCH = 500
FETCHED_AT = {}               # схема -> момент сбора сырья (ISO, UTC); пишется в observed_at строк
D = lambda v: Decimal(str(v or 0))  # noqa: E731


def in_night_window(now_utc):
    m = now_utc.hour * 60 + now_utc.minute
    return NIGHT_WINDOW[0][0] * 60 + NIGHT_WINDOW[0][1] <= m <= NIGHT_WINDOW[1][0] * 60 + NIGHT_WINDOW[1][1]


def sb():
    import loaders.ozon_fbo_orders_loader as fbo
    return fbo.supabase


def fetch_all(table, filters, select="*", order="id"):
    out, page = [], 0
    while True:
        q = sb().table(table).select(select)
        for f in filters:
            q = getattr(q, f[0])(*f[1:])
        res = q.order(order).range(page * 1000, page * 1000 + 999).execute()
        out.extend(res.data)
        if len(res.data) < 1000:
            break
        page += 1
    return out


def table_stats(date_from, date_to=None):
    filters = [("eq", "marketplace_code", "ozon"), ("gte", "order_date", date_from)]
    if date_to:
        filters.append(("lte", "order_date", date_to))
    rows = fetch_all("marketplace_orders", filters,
                     select="order_date,order_schema,marketplace_sku,orders_qty,orders_amount_seller")
    by = defaultdict(lambda: [0, Decimal(0), Decimal(0)])
    for r in rows:
        a = by[(r["order_schema"], r["order_date"][:7])]
        a[0] += 1; a[1] += D(r["orders_qty"]); a[2] += D(r["orders_amount_seller"])
    return rows, by


utc_window = rules.utc_window    # одно правило границ на ночной сбор и на пересборку (loaders/ozon_orders_rows.py)


def raw_path(scheme, date_from, date_to):
    return os.path.join(RAW_DIR, f"history_{scheme}_{date_from}_{date_to}.json")


def estimate(date_from):
    rows, by = table_stats(date_from)
    days = (date.today() - date.fromisoformat(date_from)).days + 1
    per_scheme = defaultdict(lambda: [0, Decimal(0)])
    for (scheme, _m), a in by.items():
        per_scheme[scheme][0] += a[0]; per_scheme[scheme][1] += a[2]
    print(f"таблица marketplace_orders (ozon, с {date_from}): строк {len(rows)} — " +
          ", ".join(f"{s}: {a[0]} строк на {a[1]:,.2f}" for s, a in sorted(per_scheme.items())))
    # отправлений в день — по последнему полному сбору precheck_*/fbs_*/fbo_* в data/postings_raw
    est = {}
    for scheme in ("fbo", "fbs"):
        files = sorted(f for f in os.listdir(RAW_DIR) if f.endswith(".json") and (f.startswith(f"precheck_{scheme}_") or f.startswith(f"{scheme}_"))) if os.path.isdir(RAW_DIR) else []
        if not files:
            est[scheme] = None
            continue
        data = json.load(open(os.path.join(RAW_DIR, files[-1])))
        postings = data["postings"]
        w = data.get("window")
        span = 30
        if w:
            span = max(1, (datetime.fromisoformat(w["to"].replace("Z", "+00:00")) - datetime.fromisoformat(w["since"].replace("Z", "+00:00"))).days)
        per_day = len(postings) / span
        total = per_day * days
        chunks = -(-days // CHUNK_DAYS)
        calls = int(-(-total // PAGE)) + chunks   # +1 обращение на окно (последняя неполная страница)
        est[scheme] = (files[-1], per_day, total, calls)
    total_calls = sum(e[3] for e in est.values() if e)
    print(f"\nоценка сбора истории {date_from} … сегодня ({days} дней), окна по {CHUNK_DAYS} дней, страница {PAGE}, {SECONDS_PER_CALL} с на обращение:")
    for scheme, e in est.items():
        if e:
            print(f"  {scheme}: ~{e[1]:.0f} отправлений/день по {e[0]} → ~{e[2]:,.0f} отправлений, ~{e[3]} обращений")
        else:
            print(f"  {scheme}: нет свежего сбора в {RAW_DIR}, оценить не по чему")
    print(f"  итого ~{total_calls} обращений ≈ {total_calls * SECONDS_PER_CALL / 60:.0f} мин; 429 — 60 с на каждый")
    print(f"\nчто перепишется при --apply: все {len(rows)} строк ozon с order_date ≥ {date_from} (обе схемы) — delete по схеме и окну, затем запись;"
          f" снимок строк в {SNAP_DIR}/orders_rewrite_<UTC>.json (~{len(rows) * 260 / 1e6:.0f} МБ) до удаления; "
          f"витрины daily_sku_kpi / daily_marketplace_kpi за те же даты — ночной KPI пересоберёт, ключи без заказов удалить отдельно")
    print("db_writes = 0")


def fetch_history(scheme, date_from, date_to):
    path = raw_path(scheme, date_from, date_to)
    if os.path.exists(path):
        data = json.load(open(path))
        FETCHED_AT[scheme] = data.get("fetched_at")
        print(f"{scheme}: сырьё из файла {path}: {len(data['postings'])} отправлений, снято {data.get('fetched_at')}")
        return data["postings"]
    if in_night_window(datetime.now(timezone.utc)):
        raise SystemExit("окно ночного прогона 00:15…03:15 UTC — не стартую")
    calls = {"n": 0, "429": 0}
    orig = http_retry.post

    def counted(*a, **kw):
        calls["n"] += 1
        r = orig(*a, **kw)
        if r.status_code == 429:
            calls["429"] += 1
        return r
    http_retry.post = counted
    if scheme == "fbo":
        import loaders.ozon_fbo_orders_loader as mod
        get = mod.get_fbo_postings
    else:
        import loaders.ozon_fbs_orders_loader as mod
        get = mod.get_ozon_fbs_postings
    start, end = utc_window(date_from, date_to)
    postings, seen, t0 = [], {}, time.monotonic()
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=CHUNK_DAYS), end)
        batch = get(since=cur, to=nxt)
        for p in batch:
            seen[p["posting_number"]] = p   # окна не пересекаются; повтор — последний снимок
        print(f"  {scheme} {cur.date()} … {nxt.date()}: {len(batch)} отправлений, обращений всего {calls['n']}", flush=True)
        cur = nxt
    http_retry.post = orig
    postings = list(seen.values())
    os.makedirs(RAW_DIR, exist_ok=True)
    FETCHED_AT[scheme] = datetime.now(timezone.utc).isoformat()
    json.dump({"schema": scheme, "fetched_at": FETCHED_AT[scheme],
               "window": {"since": date_from, "to": date_to, "since_utc": start.isoformat(), "to_utc": end.isoformat()},
               "postings": postings}, open(path, "w"), ensure_ascii=False)
    print(f"{scheme}: {len(postings)} уникальных отправлений, обращений {calls['n']}, 429 — {calls['429']}, {time.monotonic() - t0:.0f} с → {path}")
    return postings


def month_ranges(date_from, date_to):
    """[(с, по), …] по календарным месяцам внутри date_from … date_to, границы включены."""
    out, cur, end = [], date.fromisoformat(date_from), date.fromisoformat(date_to)
    while cur <= end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        out.append((cur.isoformat(), min(nxt - timedelta(days=1), end).isoformat()))
        cur = nxt
    return out


def plan(date_from, date_to, apply=False, delete_by_month=False):
    rows_by_scheme, counters = {}, {}
    for scheme in ("fbo", "fbs"):
        postings = fetch_history(scheme, date_from, date_to)
        # observed_at — момент сбора, подтвердившего строку, а не момент запуска --apply:
        # между ними могут пройти часы, и строка не должна выглядеть свежее своего сырья.
        rows, c = rules.build_order_rows(postings, scheme, observed_at=FETCHED_AT.get(scheme))
        rules.print_counters(scheme, c)
        # Обе границы: сбор кончается в date_to, и delete ниже обязан кончаться там же.
        # Без верхней границы --date-to в прошлом стёр бы всё до сегодня, а записал до date_to.
        rows_by_scheme[scheme] = [r for r in rows if date_from <= r["order_date"] <= date_to]
        counters[scheme] = c
    table, by = table_stats(date_from, date_to)
    new_by = defaultdict(lambda: [0, Decimal(0), Decimal(0), Decimal(0), Decimal(0)])
    for scheme, rows in rows_by_scheme.items():
        for r in rows:
            a = new_by[(scheme, r["order_date"][:7])]
            a[0] += 1; a[1] += D(r["orders_qty"]); a[2] += D(r["orders_amount_seller"]); a[3] += D(r["cancelled_orders_qty"]); a[4] += D(r["cancelled_orders_amount_seller"])
    print(f"\n{'схема':6}{'месяц':9}{'табл. строк':>12}{'табл. ₽':>16}{'новых строк':>12}{'подтв. ₽':>16}{'отм. ₽':>15}{'создано ₽':>16}{'табл.−создано':>15}")
    for key in sorted(set(by) | set(new_by)):
        t = by.get(key, [0, Decimal(0), Decimal(0)]); n = new_by.get(key, [0] + [Decimal(0)] * 4)
        print(f"{key[0]:6}{key[1]:9}{t[0]:>12}{t[2]:>16,.2f}{n[0]:>12}{n[2]:>16,.2f}{n[4]:>15,.2f}{n[2] + n[4]:>16,.2f}{t[2] - n[2] - n[4]:>15,.2f}")
    print("\nсмысл последней колонки: FBS до починки писал все статусы (≈ создано); FBO — только подтверждённые плюс устаревшие ключи")
    if not apply:
        print("db_writes = 0")
        return
    # --- apply ---
    os.makedirs(SNAP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = os.path.join(SNAP_DIR, f"orders_rewrite_{stamp}.json")
    # Снимок — полные строки: table выше читается пятью колонками для статистики, и
    # восстановить по нему таблицу было бы нельзя (нет article, product_name, *_buyer, cancelled_*, observed_at).
    full = fetch_all("marketplace_orders", [("eq", "marketplace_code", "ozon"), ("gte", "order_date", date_from), ("lte", "order_date", date_to)])
    if len(full) != len(table):
        raise SystemExit(f"снимок: {len(full)} строк против {len(table)} в статистике — таблица меняется под руками, стоп до записи")
    json.dump({"date_from": date_from, "date_to": date_to, "rows": full}, open(snap, "w"), ensure_ascii=False)
    print(f"снимок {len(full)} строк (все колонки) → {snap}")
    for scheme, rows in rows_by_scheme.items():
        before = sb().table("marketplace_orders").select("id", count="exact").eq("marketplace_code", "ozon").eq("order_schema", scheme).gte("order_date", date_from).lte("order_date", date_to).limit(1).execute().count
        t0 = time.monotonic()
        # Один запрос атомарен: не уложился в statement_timeout — откатился целиком, таблица цела.
        # По месяцам — только запасной путь: отказ посередине оставит дыру до перезапуска (снимок есть).
        for d1, d2 in (month_ranges(date_from, date_to) if delete_by_month else [(date_from, date_to)]):
            t1 = time.monotonic()
            sb().table("marketplace_orders").delete().eq("marketplace_code", "ozon").eq("order_schema", scheme).gte("order_date", d1).lte("order_date", d2).execute()
            print(f"{scheme}: delete {d1} … {d2} — {time.monotonic() - t1:.1f} с", flush=True)
        left = sb().table("marketplace_orders").select("id", count="exact").eq("marketplace_code", "ozon").eq("order_schema", scheme).gte("order_date", date_from).lte("order_date", date_to).limit(1).execute().count
        if left:
            raise SystemExit(f"{scheme}: после delete осталось {left} строк — стоп, запись не начата")
        t2 = time.monotonic()
        for i in range(0, len(rows), BATCH):
            sb().table("marketplace_orders").upsert(rows[i:i + BATCH], on_conflict="order_date,marketplace_code,marketplace_sku,order_schema").execute()
        print(f"{scheme}: запись {len(rows)} строк пачками по {BATCH} — {time.monotonic() - t2:.1f} с (delete {t2 - t0:.1f} с)", flush=True)
        after = sb().table("marketplace_orders").select("id", count="exact").eq("marketplace_code", "ozon").eq("order_schema", scheme).gte("order_date", date_from).lte("order_date", date_to).limit(1).execute().count
        print(f"{scheme}: было {before}, удалено, записано {len(rows)}, в таблице {after} {'=' if after == len(rows) else '≠ ОШИБКА'}")
    print("готово; витрины за окно пересоберёт ночной KPI, ключи витрин без заказов — удалить отдельно")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", default="2026-03-28")
    ap.add_argument("--date-to", default=date.today().isoformat())
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--estimate", action="store_true")
    g.add_argument("--fetch", action="store_true")
    g.add_argument("--plan", action="store_true")
    g.add_argument("--apply", action="store_true")
    ap.add_argument("--delete-by-month", action="store_true",
                    help="с --apply: удалять помесячно — запасной путь, если delete одним запросом не уложился в statement_timeout")
    args = ap.parse_args()
    if args.estimate:
        estimate(args.date_from)
    elif args.fetch:
        for scheme in ("fbo", "fbs"):
            fetch_history(scheme, args.date_from, args.date_to)
        print("db_writes = 0")
    else:
        if args.apply and in_night_window(datetime.now(timezone.utc)):
            raise SystemExit("окно ночного прогона — не стартую")
        plan(args.date_from, args.date_to, apply=args.apply, delete_by_month=args.delete_by_month)


if __name__ == "__main__":
    main()
