#!/usr/bin/env python3
"""FBS: /v3/posting/fbs/list (старый, offset) против /v4/posting/fbs/list (новый, cursor) на ОДНОМ окне. db_writes = 0.

Та же процедура, что при переводе FBO на /v3 (docs/ozon_postings_migration.md):
множества posting_number в обе стороны, значения полей, которые читает
загрузчик (status, in_process_at, shipment_date; products: sku, offer_id,
quantity, price с приведением строки и объекта к Decimal), и строки таблицы
по новому правилу (loaders/ozon_orders_rows.py) — подтверждённые и
отменённые. Сырьё обоих методов кладётся в data/postings_raw/precheck_*.json
(префикс precheck_ лог статусов не читает).

    venv/bin/python3 scripts/ozon_fbs_v4_parity.py --days-back 30
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from loaders import http_retry  # noqa: E402
from loaders import ozon_orders_rows as rules  # noqa: E402
import loaders.ozon_fbs_orders_loader as fbs  # noqa: E402

RAW_DIR = os.path.join("data", "postings_raw")
V3_URL = "https://api-seller.ozon.ru/v3/posting/fbs/list"


class Counting:
    def __init__(self):
        self.n = 0
        self.r429 = 0
        self.orig = http_retry.post

    def __call__(self, *a, **kw):
        self.n += 1
        r = self.orig(*a, **kw)
        if r.status_code == 429:
            self.r429 += 1
        return r


def fetch_v3(since, to, counter):
    payload = {"dir": "ASC", "filter": {"since": since.isoformat(), "to": to.isoformat()},
               "limit": 1000, "offset": 0, "with": {"analytics_data": True, "financial_data": True}}
    out, pages = [], 0
    while True:
        pages += 1
        if pages > 100:
            raise RuntimeError("v3: больше 100 страниц по 1000 — цикл не сходится")
        for attempt in range(1, 4):
            time.sleep(1.5)
            r = counter(V3_URL, label="Ozon FBS v3 parity", headers=fbs.ozon_headers(), json=payload, timeout=120)
            if r.status_code == 429:
                print(f"  v3: 429, пауза 60 с, попытка {attempt}/3", flush=True)
                time.sleep(60)
                continue
            break
        if r.status_code != 200:
            raise RuntimeError(f"v3: HTTP {r.status_code} {r.text[:200]}")
        batch = ((r.json() or {}).get("result") or {}).get("postings") or []
        out.extend(batch)
        if len(batch) < payload["limit"]:
            return out, pages
        payload["offset"] += payload["limit"]


def dump(schema_tag, postings, window, fetched_at):
    os.makedirs(RAW_DIR, exist_ok=True)
    path = os.path.join(RAW_DIR, f"precheck_{schema_tag}_{fetched_at.strftime('%Y%m%dT%H%M%SZ')}.json")
    json.dump({"schema": "fbs", "fetched_at": fetched_at.isoformat(), "window": window, "postings": postings},
              open(path, "w"), ensure_ascii=False)
    return path


def price_dec(product):
    return rules.product_price(product, "fbs")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=30)
    args = ap.parse_args()
    to = datetime.now(timezone.utc).replace(microsecond=0)
    since = to - timedelta(days=args.days_back)
    window = {"since": since.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "to": to.strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    print(f"окно {window['since']} … {window['to']}; db_writes = 0")

    c4 = Counting(); http_retry.post = c4
    t0 = time.monotonic()
    v4 = fbs.get_ozon_fbs_postings(since=since, to=to)
    t4 = time.monotonic() - t0
    http_retry.post = c4.orig
    p4 = dump("fbs", v4, window, to)
    print(f"v4: {len(v4)} отправлений, обращений {c4.n}, 429 — {c4.r429}, {t4:.0f} с → {p4}")

    c3 = Counting()
    t0 = time.monotonic()
    v3, pages3 = fetch_v3(since, to, c3)
    t3 = time.monotonic() - t0
    p3 = dump("fbs_v3", v3, window, to)
    print(f"v3: {len(v3)} отправлений, обращений {c3.n} ({pages3} страниц), 429 — {c3.r429}, {t3:.0f} с → {p3}")

    s3 = {p["posting_number"] for p in v3}
    s4 = {p["posting_number"] for p in v4}
    print(f"\nмножества posting_number: уник v3 {len(s3)}, уник v4 {len(s4)}, в v4 нет {len(s3 - s4)}, в v3 нет {len(s4 - s3)}, общих {len(s3 & s4)}")
    if s3 - s4:
        print("  только в v3:", sorted(s3 - s4)[:8])
    if s4 - s3:
        print("  только в v4:", sorted(s4 - s3)[:8])

    by3 = {p["posting_number"]: p for p in v3}
    by4 = {p["posting_number"]: p for p in v4}
    diffs = defaultdict(int); examples = []
    price_forms = defaultdict(int)
    for pn in sorted(s3 & s4):
        a, b = by3[pn], by4[pn]
        for f in ("status", "in_process_at", "shipment_date"):
            if (a.get(f) or "") != (b.get(f) or ""):
                diffs[f] += 1
                if len(examples) < 12:
                    examples.append((pn, f, a.get(f), b.get(f)))
        pa = {str(x.get("sku")): x for x in a.get("products") or []}
        pb = {str(x.get("sku")): x for x in b.get("products") or []}
        for sku in set(pa) | set(pb):
            if sku not in pa or sku not in pb:
                diffs["products.presence"] += 1
                if len(examples) < 12:
                    examples.append((pn, f"products[{sku}]", sku in pa, sku in pb))
                continue
            x, y = pa[sku], pb[sku]
            price_forms[(type(x.get("price")).__name__, type(y.get("price")).__name__)] += 1
            if int(x.get("quantity") or 0) != int(y.get("quantity") or 0):
                diffs["products.quantity"] += 1
            if price_dec(x) != price_dec(y):
                diffs["products.price"] += 1
                if len(examples) < 12:
                    examples.append((pn, f"price[{sku}]", x.get("price"), y.get("price")))
            if str(x.get("offer_id") or "") != str(y.get("offer_id") or ""):
                diffs["products.offer_id"] += 1
    print(f"значения по общим отправлениям: расхождений {dict(diffs) or 'нет'}; формы цены (v3, v4): {dict(price_forms)}")
    for e in examples:
        print("   ", e)
    missing_created = sum(1 for p in v4 if "created_at" in p)
    print(f"created_at в v4: у {missing_created} из {len(v4)} (ожидается 0 — поля нет); in_process_at пуст у {sum(1 for p in v4 if not p.get('in_process_at'))}")

    r3, c3r = rules.build_order_rows(v3, "fbs", observed_at="parity")
    r4, c4r = rules.build_order_rows(v4, "fbs", observed_at="parity")
    key = lambda r: (r["order_date"], r["marketplace_sku"])  # noqa: E731
    k3 = {key(r): r for r in r3}; k4 = {key(r): r for r in r4}
    fields = ("orders_qty", "orders_amount_seller", "cancelled_orders_qty", "cancelled_orders_amount_seller")
    bad = sum(1 for k in set(k3) & set(k4) if any(Decimal(str(k3[k][f])) != Decimal(str(k4[k][f])) for f in fields))
    print(f"\nстроки по новому правилу: v3 {len(k3)}, v4 {len(k4)}, только v3 {len(set(k3) - set(k4))}, только v4 {len(set(k4) - set(k3))}, "
          f"общих с разными значениями {bad}")
    print(f"счётчики v3: {c3r}\nсчётчики v4: {c4r}")
    print("итоги (подтв. qty, подтв. ₽, отм. qty, отм. ₽): v3 %s | v4 %s" % (tuple(str(x) for x in rules.sums(r3)), tuple(str(x) for x in rules.sums(r4))))
    per_date = defaultdict(lambda: [0, 0])
    for p in v3:
        per_date[rules.order_date_of(p, "fbs")][0] += 1
    for p in v4:
        per_date[rules.order_date_of(p, "fbs")][1] += 1
    print("\nотправлений по дате (v3 / v4):", ", ".join(f"{d}: {a}/{b}{'' if a == b else ' ≠'}" for d, (a, b) in sorted(per_date.items())))
    print("db_writes = 0")


if __name__ == "__main__":
    main()
