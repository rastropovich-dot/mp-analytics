"""Добор заказов FBO за 2026-09-09 … 2026-09-14 новым кодом, С ЗАПИСЬЮ в marketplace_orders.

Разрешено владельцем в docs/inbox.md от 2026-09-14 (вторая задача, п. 2): только
FBO, только этот шаг. Окно выгрузки — штатные 30 дней, как в ночном прогоне;
пишутся ТОЛЬКО строки с order_date >= WRITE_FROM — эти даты покрыты окном
целиком, старые даты не трогаются (частичный агрегат по грубому ключу
заместил бы полный, см. how-we-work.md «Пробная запись — это тоже запись»).

Запуск из корня проекта:
    venv/bin/python3 scripts/ozon_fbo_backfill_write_20260914.py /tmp/backfill_fbo_write.json
"""
import json
import sys
import time
from collections import defaultdict
from decimal import Decimal

sys.path.insert(0, ".")
import loaders.ozon_fbo_orders_loader as fbo  # noqa: E402
from loaders import http_retry  # noqa: E402

WRITE_FROM = "2026-09-09"


def main(out_path):
    calls = {"n": 0, "429": 0}
    orig = http_retry.post

    def counted(*a, **kw):
        calls["n"] += 1
        r = orig(*a, **kw)
        if r.status_code == 429:
            calls["429"] += 1
        return r

    http_retry.post = counted

    t0 = time.monotonic()
    postings = fbo.get_fbo_postings(days_back=30)
    t_fetch = time.monotonic() - t0
    rows_all = fbo.build_order_rows(postings)
    rows = [r for r in rows_all if r["order_date"] >= WRITE_FROM]
    by_date = defaultdict(int)
    for r in rows:
        by_date[r["order_date"]] += 1
    print(f"\n=== выгрузка: {len(postings)} отправлений, {calls['n']} обращений, "
          f"429={calls['429']}, {t_fetch:.0f} с")
    print(f"строк всего {len(rows_all)}, к записи (>= {WRITE_FROM}): {len(rows)} "
          f"по датам {dict(sorted(by_date.items()))}")

    t1 = time.monotonic()
    fbo.save_orders(rows)
    t_write = time.monotonic() - t1
    print(f"=== запись: {len(rows)} строк за {t_write:.1f} с, db_writes = {len(rows)}")

    # Сверка записанного с тем, что даёт API: множества ключей и значения.
    want = {(r["order_date"], r["marketplace_sku"]): r for r in rows}
    db = {}
    page = 0
    query = (fbo.supabase.table("marketplace_orders")
             .select("order_date,marketplace_sku,orders_qty,orders_amount_buyer,orders_amount_seller")
             .eq("marketplace_code", "ozon").eq("order_schema", "fbo").gte("order_date", WRITE_FROM))
    while True:
        res = query.range(page * 1000, page * 1000 + 999).execute()
        for r in res.data:
            db[(r["order_date"], str(r["marketplace_sku"]))] = r
        if len(res.data) < 1000:
            break
        page += 1
    only_api = sorted(set(want) - set(db))
    only_db = sorted(set(db) - set(want))
    diff = [k for k in set(want) & set(db)
            if float(want[k]["orders_qty"]) != float(db[k]["orders_qty"])
            or abs(float(want[k]["orders_amount_buyer"]) - float(db[k]["orders_amount_buyer"])) > 0.005]
    print(f"=== сверка после записи: ключей от API {len(want)}, в БД {len(db)}, "
          f"нет в БД {len(only_api)}, лишних в БД {len(only_db)}, расхождений значений {len(diff)}")
    if only_db:
        per = defaultdict(int)
        for k in only_db:
            per[k[0]] += 1
        print("   лишние в БД по датам (остаток отменённых, не чинить):", dict(sorted(per.items())),
              "примеры:", only_db[:5])
    if only_api:
        print("   нет в БД, примеры:", only_api[:5])
    if diff:
        print("   расхождения, примеры:", [(k, want[k]["orders_qty"], db[k]["orders_qty"]) for k in diff[:5]])
    db_by_date = defaultdict(lambda: [0, Decimal(0)])
    for k, r in db.items():
        db_by_date[k[0]][0] += 1
        db_by_date[k[0]][1] += Decimal(str(r["orders_amount_buyer"]))
    print("дата        строк в БД   сумма orders_amount_buyer")
    for d in sorted(db_by_date):
        print(f"{d}  {db_by_date[d][0]:5}   {db_by_date[d][1]:>15,.2f}")
    with open(out_path, "w") as f:
        json.dump({"postings": len(postings), "requests": calls["n"], "r429": calls["429"],
                   "t_fetch": t_fetch, "rows_written": len(rows), "by_date": dict(by_date),
                   "t_write": t_write, "only_api": len(only_api), "only_db": len(only_db),
                   "diff": len(diff)}, f, indent=1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/backfill_fbo_write.json")
