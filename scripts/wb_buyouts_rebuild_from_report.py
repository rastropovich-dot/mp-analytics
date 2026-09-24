#!/usr/bin/env python3
"""Выкупы WB (marketplace_buyouts) из отчёта реализации (wb_sales_report_rows): план и запись.

    venv/bin/python3 scripts/wb_buyouts_rebuild_from_report.py --plan                       план 02-01 … вчера, ничего не пишет
    venv/bin/python3 scripts/wb_buyouts_rebuild_from_report.py --plan --date-from 2026-03-26 --date-to 2026-09-22
    venv/bin/python3 scripts/wb_buyouts_rebuild_from_report.py --apply --approve-wb-buyouts-write   ← только по слову владельца

ЗАЧЕМ. Старый загрузчик supplier/sales (flag=0, окно 30 дней) недобирал: апрель–август
marketplace_buyouts ниже отчёта реализации на 8,66 млн, февраль–март в базе нет вовсе
(сбор начат 03-26), сентябрь равен (WB-5, WB-6 §3). Отчёт реализации — полный источник
с 2024-01-29, лежит в базе с 02-01 (329 522 строки, бэкфилл 09-23).

ПРАВИЛО СТРОКИ (то же зерно, что у старого загрузчика — (дата, nmId)):
    buyout_date            день продажи — saleDt в московском времени (старый загрузчик брал `date`
                           supplier/sales, то есть ту же московскую дату: сентябрь 22 из 22 равны)
    marketplace_sku        nmId
    buyouts_qty            Σ quantity × (+1 Продажа / −1 Возврат)
    buyouts_amount_seller  Σ retailPriceWithDisc × знак   (как priceWithDisc у supplier/sales)
    buyouts_amount_buyer   Σ retailAmount × знак          (как finishedPrice)
    article                как в базе, если ключ есть; иначе vendorCode в верхнем регистре (в
                           отчёте WB отдаёт его строчными, supplier/sales — прописными)
    product_name           как в базе; у новых ключей — пусто (в отчёте предмета нет)
    revenue_after_commission_vat, commission_amount, vat_amount — нули, как писал старый загрузчик
    buyouts_units          не трогается (колонка Ozon, у WB null)

ЧТО ДЕЛАЕТ --plan (обращений к API — 0; чтение только базы):
    по месяцам: ключей добавить / переписать / без изменений / удалить, штуки и ₽ до и после;
    дни, где база выше отчёта; список ключей на удаление целиком (в отчёте ключа нет — те же
    «застрявшие», что чинили у Ozon); дни, где в отчёте нет ни одной продажи, — НЕ трогаются
    (правило loaders/stale_keys.py: пустой день — не истина «ноль»).

Правило строки и сборка — loaders/wb_buyouts_from_report.py (ночной путь шага «WB: загрузка
продаж/выкупов» с 2026-09-24 строит окно теми же функциями).

ЧТО ДЕЛАЕТ --apply: отказ в ночном окне; снимок затрагиваемых строк базы
(data/snapshots/marketplace_buyouts_wb_before_rebuild_<ts>.csv.gz + _meta.json с sha256, вне git); upsert
построенных строк по 500; удаление ключей списка по id — после записи. Без
--approve-wb-buyouts-write не пишет.
"""
import argparse
import csv
import gzip
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from loaders import stale_keys  # noqa: E402
from loaders.pipeline_window import in_nightly_run_window, window_text  # noqa: E402
import loaders.wb_sales_report_loader as loader  # noqa: E402

from loaders.wb_buyouts_from_report import (  # noqa: E402  — правило строки, чтение, сборка и классификация живут в загрузчике (ночной путь тот же)
    TABLE, REPORT, MSK, LAG_DAYS, Z, C, q, sale_day, read_report_sales, read_db, build_rows, classify, month_table,
)

COLLECTION_START = "2026-02-01"   # с этого дня отчёт лежит в базе (бэкфилл 09-23)


def print_plan(cls, d1, d2, days_with_sales, db_rows):
    by = month_table(cls)
    print(f"\nПлан {d1} … {d2}: ключей в отчёте {sum(len(cls[k]) for k in ('add', 'rewrite', 'same'))}, в базе {len(db_rows)}; "
          f"дней с продажами в отчёте {len(days_with_sales)}")
    print(f"{'месяц':9}{'добавить':>10}{'переписать':>12}{'без изм.':>10}{'удалить':>9}{'не трогать':>11}{'шт до':>8}{'шт после':>10}{'₽ до':>18}{'₽ после':>18}{'разница ₽':>16}")
    tq = defaultdict(Decimal)
    for m in sorted(by):
        r = by[m]
        diff = r["after_amt"] - r["before_amt"]
        for k in ("before_qty", "after_qty", "before_amt", "after_amt"):
            tq[k] += r[k]
        print(f"{m:9}{int(r['add_n']):>10}{int(r['rewrite_n']):>12}{int(r['same_n']):>10}{int(r['delete_n']):>9}{int(r['untouched_n']):>11}"
              f"{r['before_qty']:>8,.0f}{r['after_qty']:>10,.0f}{r['before_amt']:>18,.2f}{r['after_amt']:>18,.2f}{diff:>16,.2f}")
    print(f"{'итого':9}{len(cls['add']):>10}{len(cls['rewrite']):>12}{len(cls['same']):>10}{len(cls['delete']):>9}{len(cls['untouched']):>11}"
          f"{tq['before_qty']:>8,.0f}{tq['after_qty']:>10,.0f}{tq['before_amt']:>18,.2f}{tq['after_amt']:>18,.2f}{tq['after_amt'] - tq['before_amt']:>16,.2f}")
    # дни, где база выше отчёта
    day_before, day_after = defaultdict(Decimal), defaultdict(Decimal)
    for kind in ("add", "rewrite", "same"):
        for key, row, old in cls[kind]:
            day_after[key[0]] += Decimal(str(row["buyouts_amount_seller"]))
            if old is not None:
                day_before[key[0]] += Decimal(str(old["buyouts_amount_seller"] or 0))
    for key, _row, old in cls["delete"]:
        day_before[key[0]] += Decimal(str(old["buyouts_amount_seller"] or 0))
    higher = [(d, day_before[d], day_after[d]) for d in sorted(set(day_before) | set(day_after)) if day_before[d] > day_after[d]]
    print(f"\nДни, где база ВЫШЕ отчёта: {len(higher)}" + (" — " + "; ".join(f"{d} {b:,.2f} → {a:,.2f} ({a - b:+,.2f})" for d, b, a in higher) if higher else ""))
    dels = cls["delete"]
    print(f"\nКлючи на удаление (в базе есть, в отчёте за этот день нет): {len(dels)}, "
          f"штук {sum(Decimal(str(o['buyouts_qty'] or 0)) for _k, _r, o in dels):,.0f}, ₽ {sum(Decimal(str(o['buyouts_amount_seller'] or 0)) for _k, _r, o in dels):,.2f}")
    for key, _row, old in dels:
        print(f"    {key[0]} sku {key[1]} article {old.get('article')} qty {q(old['buyouts_qty'])} ₽ {q(old['buyouts_amount_seller'])} id {old['id']}")
    unt = cls["untouched"]
    if unt:
        days = sorted({k[0] for k, _r, _o in unt})
        print(f"\nНе трогаются (в отчёте за день нет ни одной продажи, а в базе строки есть): {len(unt)} ключей на {len(days)} днях: {', '.join(days[:20])}"
              + (" …" if len(days) > 20 else ""))
    # крупнейшие переписывания
    big = sorted(cls["rewrite"], key=lambda t: abs(Decimal(str(t[1]["buyouts_amount_seller"])) - Decimal(str(t[2]["buyouts_amount_seller"] or 0))), reverse=True)[:10]
    if big:
        print("\nКрупнейшие переписывания (день, sku: было → станет ₽ / шт):")
        for key, row, old in big:
            print(f"    {key[0]} sku {key[1]}: {q(old['buyouts_amount_seller']):,.2f} → {q(row['buyouts_amount_seller']):,.2f} / {q(old['buyouts_qty'])} → {q(row['buyouts_qty'])}")


# ---------- запись ----------

def snapshot(db_rows, label):
    os.makedirs(os.path.join(ROOT, "data", "snapshots"), exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(ROOT, "data", "snapshots", f"marketplace_buyouts_wb_before_rebuild_{label}_{ts}.csv.gz")
    cols = ["id", "buyout_date", "marketplace_sku", "article", "product_name", "buyouts_qty", "buyouts_amount_buyer", "buyouts_amount_seller", "buyouts_units"]
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(cols)
        for r in db_rows:
            w.writerow([r.get(c) for c in cols])
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    json.dump({"rows": len(db_rows), "sha256": digest, "at_utc": datetime.now(timezone.utc).isoformat()}, open(path[:-len(".csv.gz")] + "_meta.json", "w"), indent=1)
    print(f"снимок: {path} — строк {len(db_rows)}, sha256 {digest[:12]}…")
    return path


def apply(sb, cls, db_rows, label):
    if in_nightly_run_window(datetime.now(timezone.utc)):
        raise SystemExit(f"ночное окно ({window_text()}): запись отложить")
    snapshot(db_rows, label)
    to_write = [row for _k, row, _o in cls["add"] + cls["rewrite"]]
    written = 0
    for i in range(0, len(to_write), 500):
        sb.table(TABLE).upsert(to_write[i:i + 500], on_conflict="buyout_date,marketplace_code,marketplace_sku").execute()
        written += len(to_write[i:i + 500])
    print(f"✅ {TABLE}: upsert {written} строк (добавить {len(cls['add'])}, переписать {len(cls['rewrite'])}); без изменений {len(cls['same'])} не трогались")
    ids = [old["id"] for _k, _r, old in cls["delete"]]
    deleted = 0
    for i in range(0, len(ids), 200):
        sb.table(TABLE).delete().in_("id", ids[i:i + 200]).eq("marketplace_code", "wb").execute()
        deleted += len(ids[i:i + 200])
    print(f"✅ {TABLE}: удалено {deleted} ключей по id (список печатался выше)")
    return written, deleted


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--approve-wb-buyouts-write", action="store_true")
    ap.add_argument("--date-from", default=COLLECTION_START)
    ap.add_argument("--date-to", default=(date.today() - timedelta(days=1)).isoformat())
    args = ap.parse_args(argv)
    if not (args.plan or args.apply):
        ap.error("нужен --plan или --apply")
    sb = loader._client()
    d1, d2 = args.date_from, args.date_to
    sales = read_report_sales(sb, d1, d2)
    db_rows = read_db(sb, d1, d2)
    print(f"отчёт: строк Продажа/Возврат с датой продажи {d1} … {d2} — {len(sales)}; база: строк WB — {len(db_rows)}")
    if not sales:
        raise SystemExit("в отчёте нет продаж за период — нечего строить")
    db_by_key = {(str(r["buyout_date"]), str(r["marketplace_sku"])): r for r in db_rows}
    rows = build_rows(sales, db_by_key)
    days_with_sales = {r["day"] for r in sales}
    cls = classify(rows, db_by_key, days_with_sales)
    print_plan(cls, d1, d2, days_with_sales, db_rows)
    if args.apply:
        if not args.approve_wb_buyouts_write:
            print("\n--apply без --approve-wb-buyouts-write: не пишу; db_writes = 0")
            return 2
        apply(sb, cls, db_rows, f"{d1}_{d2}")
        return 0
    print("\ndb_writes = 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
