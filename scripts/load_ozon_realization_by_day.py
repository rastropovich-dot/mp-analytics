"""Сбор /v1/finance/realization/by-day в ozon_realization_by_day.

Запускает владелец руками. Без --apply ничего не пишет в БД (db_writes = 0):
снимает даты, кладёт сырые ответы в data/ozon_realization_by_day/<дата>.json
(папка в .gitignore) и печатает, что запишет. С --apply читает те же файлы и
пишет; перевыгружает только если файла нет или дан --refetch. Так один сбор
стоит одну выгрузку, а сырьё остаётся.

    venv/bin/python3 scripts/load_ozon_realization_by_day.py                        # авто-окно: все доступные даты, без записи
    venv/bin/python3 scripts/load_ozon_realization_by_day.py --apply                # записать из файлов (или снять и записать)
    venv/bin/python3 scripts/load_ozon_realization_by_day.py --date-from 2026-09-01 --date-to 2026-09-13
    venv/bin/python3 scripts/load_ozon_realization_by_day.py --check-only            # сверка таблицы с выкупами, без API

Границы у Ozon (опыт 2026-09-14): не раньше 32 дней до текущего дня — 400 code 3
«the requested date must be no earlier than 32 days before the current day»;
текущий день — 404 code 5 «Report for the requested date not found». Авто-окно:
от (сегодня − 32 + 1) до вчера; отказы по границе — не ошибка, а край.

Лимиты: в спеке ни слова; в ответе есть заголовок ratelimit-remaining
(наблюдалось 35 … 46, окно и потолок неизвестны, счётчик меняется и без наших
вызовов — общий на аккаунт). Пауза между датами 1,5 с; на 429 — 60 с и до трёх
попыток, затем стоп с явной причиной. Число пауз печатается.

Единица хранения — одна сторона (sale / return) одной строки отчёта, ключ
(realization_date, row_number, side); дата при записи ПЕРЕЗАПИСЫВАЕТСЯ целиком.
Схема: sql/20260915_create_ozon_realization_by_day.sql.
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

URL = "https://api-seller.ozon.ru/v1/finance/realization/by-day"
TABLE = "ozon_realization_by_day"
RAW_DIR = os.path.join("data", "ozon_realization_by_day")
RETENTION_DAYS = 32
PAGE_PAUSE_SECONDS = 1.5
ANTISPAM_PAUSE_SECONDS = 60
ANTISPAM_MAX_ATTEMPTS = 3
BATCH = 500
MONEY_FIELDS = ("price_per_instance", "amount", "bonus", "commission", "compensation", "standard_fee",
                "bank_coinvestment", "stars", "pick_up_point_coinvestment", "total")
SIDES = {"sale": "delivery_commission", "return": "return_commission"}


def money(value):
    return Decimal(str(value if value is not None else 0)).quantize(Decimal("0.01"))


def ozon_headers():
    return {"Client-Id": os.getenv("OZON_CLIENT_ID"), "Api-Key": os.getenv("OZON_API_KEY"), "Content-Type": "application/json"}


def flatten_rows(rows, realization_date, fetched_at, marketplace_code="ozon"):
    """Строки ответа -> записи таблицы: по одной на каждую непустую сторону.

    Пустая сторона (null) — записи нет. Сторона с quantity = 0 — тоже нет: в
    отчёте такого не встречалось, а check в схеме не пропустит.
    """
    out = []
    for row in rows:
        item = row.get("item") or {}
        for side, block_name in SIDES.items():
            block = row.get(block_name)
            if not block:
                continue
            quantity = int(block.get("quantity") or 0)
            if quantity <= 0:
                raise RuntimeError(f"{realization_date} строка {row.get('rowNumber')}: {block_name} с quantity={quantity}")
            offer_id = str(item.get("offer_id") or "").strip()
            if not offer_id or not item.get("sku"):
                raise RuntimeError(f"{realization_date} строка {row.get('rowNumber')}: нет sku/offer_id: {item}")
            rec = {
                "marketplace_code": marketplace_code,
                "realization_date": realization_date,
                "row_number": int(row["rowNumber"]),
                "side": side,
                "sku": int(item["sku"]),
                "offer_id": offer_id,
                "offer_id_norm": offer_id.lower(),
                "barcode": item.get("barcode"),
                "product_name": item.get("name"),
                "seller_price_per_instance": str(money(row.get("seller_price_per_instance"))),
                "commission_ratio": str(Decimal(str(row.get("commission_ratio") or 0)).quantize(Decimal("0.0001"))),
                "quantity": quantity,
                "raw_row": row,
                "source": "/v1/finance/realization/by-day",
                "fetched_at": fetched_at,
            }
            for f in MONEY_FIELDS:
                rec[f] = str(money(block.get(f)))
            out.append(rec)
    return out


def check_identities(recs):
    """Тождества, снятые 2026-09-14 на 31.08 (0 расхождений из 210 блоков):
    amount = price × qty; total = amount + bonus + bank + stars + apvz + compensation − standard_fee − commission."""
    bad = []
    for r in recs:
        d = {f: Decimal(r[f]) for f in MONEY_FIELDS}
        if abs(d["price_per_instance"] * r["quantity"] - d["amount"]) > Decimal("0.01"):
            bad.append((r["realization_date"], r["row_number"], r["side"], "amount≠price×qty"))
        calc = d["amount"] + d["bonus"] + d["bank_coinvestment"] + d["stars"] + d["pick_up_point_coinvestment"] + d["compensation"] - d["standard_fee"] - d["commission"]
        if abs(calc - d["total"]) > Decimal("0.01"):
            bad.append((r["realization_date"], r["row_number"], r["side"], f"total≠формула ({calc} vs {d['total']})"))
    return bad


def summarize(recs):
    agg = defaultdict(lambda: defaultdict(Decimal))
    for r in recs:
        a = agg[r["side"]]
        a["rows"] += 1
        a["quantity"] += r["quantity"]
        for f in ("amount", "bonus", "bank_coinvestment", "standard_fee", "total"):
            a[f] += Decimal(r[f])
    return agg


def fetch_day(day, counters):
    """Один день. Возвращает (status, rows, fetched_at). 400/404 по границе — не ошибка."""
    for attempt in range(1, ANTISPAM_MAX_ATTEMPTS + 1):
        time.sleep(PAGE_PAUSE_SECONDS)
        counters["requests"] += 1
        fetched_at = datetime.now(timezone.utc).isoformat()
        r = requests.post(URL, headers=ozon_headers(), json={"year": day.year, "month": day.month, "day": day.day}, timeout=120)
        counters["last_ratelimit_remaining"] = r.headers.get("ratelimit-remaining")
        if r.status_code == 429:
            counters["429"] += 1
            print(f"  {day}: 429, пауза {ANTISPAM_PAUSE_SECONDS} с, попытка {attempt}/{ANTISPAM_MAX_ATTEMPTS} ({r.text[:120]})", flush=True)
            time.sleep(ANTISPAM_PAUSE_SECONDS)
            continue
        if r.status_code == 200:
            return "ok", r.json().get("rows") or [], fetched_at
        if r.status_code in (400, 404):
            return f"boundary:{r.status_code}:{r.text[:100]}", None, fetched_at
        raise RuntimeError(f"{day}: HTTP {r.status_code} {r.text[:300]}")
    raise RuntimeError(f"{day}: 429 не прошёл за {ANTISPAM_MAX_ATTEMPTS} попыток")


def raw_path(day):
    return os.path.join(RAW_DIR, f"{day.isoformat()}.json")


def load_or_fetch(day, counters, refetch):
    path = raw_path(day)
    if not refetch and os.path.exists(path):
        saved = json.load(open(path))
        return saved["status"], saved["rows"], saved["fetched_at"], "file"
    status, rows, fetched_at = fetch_day(day, counters)
    os.makedirs(RAW_DIR, exist_ok=True)
    json.dump({"date": day.isoformat(), "status": status, "rows": rows, "fetched_at": fetched_at}, open(path, "w"), ensure_ascii=False)
    return status, rows, fetched_at, "api"


def supabase_client():
    from supabase import create_client
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


def table_exists(sb):
    try:
        sb.table(TABLE).select("row_number").limit(1).execute()
        return True
    except Exception as exc:
        if "PGRST205" in str(exc) or TABLE in str(exc):
            return False
        raise


def count_day(sb, day):
    return sb.table(TABLE).select("row_number", count="exact").eq("realization_date", day).limit(1).execute().count or 0


def write_day(sb, day, recs):
    """Дата целиком: удалить старое, вставить новое. row_number стабилен только внутри одного ответа."""
    existing = count_day(sb, day)
    if existing:
        sb.table(TABLE).delete().eq("realization_date", day).execute()
    for i in range(0, len(recs), BATCH):
        sb.table(TABLE).insert(recs[i:i + BATCH]).execute()
    after = count_day(sb, day)
    if after != len(recs):
        raise RuntimeError(f"{day}: записано {len(recs)}, в таблице {after} — не совпадает")
    return existing, after


def coverage_check(sb, days):
    """Сверка с marketplace_buyouts по датам: множества SKU в обе стороны и тождество
    amount + bonus + bank_coinvestment (нетто) = выручка выкупов accrual (31.08: сошлось до копейки)."""
    print("\n=== сверка с marketplace_buyouts")
    print("дата        sku отч./выкуп  общих  только отч.  только выкуп   amount+bonus+bank нетто   выручка выкупов   разница")
    for day in days:
        rows = sb.table(TABLE).select("side,sku,amount,bonus,bank_coinvestment").eq("realization_date", day).execute().data
        if not rows:
            print(f"{day}  — в таблице нет строк")
            continue
        net = Decimal(0)
        for r in rows:
            sign = 1 if r["side"] == "sale" else -1
            net += sign * (Decimal(str(r["amount"])) + Decimal(str(r["bonus"])) + Decimal(str(r["bank_coinvestment"])))
        buy = sb.table("marketplace_buyouts").select("marketplace_sku,buyouts_amount_seller").eq("marketplace_code", "ozon").eq("buyout_date", day).execute().data
        s1 = {int(r["sku"]) for r in rows}
        s2 = {int(b["marketplace_sku"]) for b in buy}
        rev = sum(Decimal(str(b["buyouts_amount_seller"])) for b in buy)
        print(f"{day}  {len(s1):4} / {len(s2):4}   {len(s1 & s2):5}  {len(s1 - s2):11}  {len(s2 - s1):12}   {net:>22,.2f}   {rev:>15,.2f}   {net - rev:>10,.2f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--apply", action="store_true", help="писать в БД (иначе db_writes = 0)")
    parser.add_argument("--refetch", action="store_true", help="снимать заново даже при наличии файла")
    parser.add_argument("--check-only", action="store_true", help="только сверка таблицы с выкупами, без API")
    args = parser.parse_args()

    today = date.today()
    date_from = date.fromisoformat(args.date_from) if args.date_from else today - timedelta(days=RETENTION_DAYS - 1)
    date_to = date.fromisoformat(args.date_to) if args.date_to else today - timedelta(days=1)
    days = [date_from + timedelta(days=i) for i in range((date_to - date_from).days + 1)]
    print(f"окно: {date_from} … {date_to}, дат {len(days)}; режим: {'ЗАПИСЬ' if args.apply else 'план, db_writes = 0'}")

    if args.check_only:
        sb = supabase_client()
        if not table_exists(sb):
            raise SystemExit(f"таблицы {TABLE} нет — применить sql/20260915_create_ozon_realization_by_day.sql")
        coverage_check(sb, [d.isoformat() for d in days])
        print("db_writes = 0")
        return

    counters = {"requests": 0, "429": 0, "last_ratelimit_remaining": None}
    t0 = time.monotonic()
    per_day = {}
    print("дата        источник  статус       строк  записей  продажи шт   продажи amount     bonus продаж   возвраты шт")
    for day in days:
        status, rows, fetched_at, origin = load_or_fetch(day, counters, args.refetch)
        if status != "ok":
            per_day[day.isoformat()] = None
            print(f"{day}  {origin:8}  {status[:40]}")
            continue
        recs = flatten_rows(rows, day.isoformat(), fetched_at)
        bad = check_identities(recs)
        if bad:
            raise RuntimeError(f"{day}: тождества нарушены в {len(bad)} записях, первые: {bad[:3]}")
        per_day[day.isoformat()] = recs
        s = summarize(recs)
        print(f"{day}  {origin:8}  ok          {len(rows):5}  {len(recs):7}  {s['sale']['quantity']:10.0f}  {s['sale']['amount']:>16,.2f}  {s['sale']['bonus']:>14,.2f}  {s['return']['quantity']:11.0f}")
    elapsed = time.monotonic() - t0
    ok_days = [d for d, r in per_day.items() if r]
    total_recs = sum(len(r) for r in per_day.values() if r)
    print(f"\nитого: дат с данными {len(ok_days)} из {len(days)}, записей {total_recs}, обращений {counters['requests']}, "
          f"429 — {counters['429']}, время {elapsed:.0f} с, ratelimit-remaining после последнего вызова {counters['last_ratelimit_remaining']}")

    if not args.apply:
        print("режим плана: db_writes = 0. Сырьё в", RAW_DIR)
        return

    sb = supabase_client()
    if not table_exists(sb):
        raise SystemExit(f"таблицы {TABLE} нет — применить sql/20260915_create_ozon_realization_by_day.sql")
    written = 0
    for d in ok_days:
        before, after = write_day(sb, d, per_day[d])
        written += after
        print(f"  {d}: было {before}, записано {after}")
    print(f"строк записано: {written}, db_writes = {written}")
    coverage_check(sb, ok_days)


if __name__ == "__main__":
    main()
