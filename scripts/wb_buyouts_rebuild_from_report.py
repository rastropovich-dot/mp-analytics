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

ЧТО ДЕЛАЕТ --apply: отказ в ночном окне; снимок затрагиваемых строк базы
(snapshots/marketplace_buyouts_wb_before_rebuild_<ts>.csv.gz + _meta.json с sha256); upsert
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

TABLE = "marketplace_buyouts"
REPORT = "wb_sales_report_rows"
MSK = timezone(timedelta(hours=3))
LAG_DAYS = 7          # rrDate позже даты продажи: 60 строк из 32 009 на 1…3 дня, 5 на 16…115 (report_wb_month.py)
COLLECTION_START = "2026-02-01"   # с этого дня отчёт лежит в базе (бэкфилл 09-23)
Z = Decimal(0)
C = Decimal("0.01")


def q(v):
    return Decimal(str(v if v is not None else 0)).quantize(C)


def sale_day(value):
    ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(MSK).date().isoformat()


# ---------- чтение ----------

def read_report_sales(sb, d1, d2):
    """Строки Продажа/Возврат отчёта с датой продажи (МСК) в d1 … d2; читаем по rrDate с запасом."""
    d2x = (date.fromisoformat(d2) + timedelta(days=LAG_DAYS)).isoformat()
    rows = stale_keys.read_window_rows(
        sb, REPORT, "rrd_id,rr_date,sale_dt,nm_id,vendor_code,seller_oper_name,quantity,retail_price_with_disc,retail_amount",
        [("gte", "rr_date", d1), ("lte", "rr_date", d2x), ("in_", "seller_oper_name", ["Продажа", "Возврат"])], ["rr_date", "rrd_id"])
    out = []
    for r in rows:
        if not r.get("sale_dt"):
            raise RuntimeError(f"строка отчёта без saleDt: rrdId {r['rrd_id']}")
        day = sale_day(r["sale_dt"])
        if d1 <= day <= d2:
            r["day"] = day
            out.append(r)
    return out


def read_db(sb, d1, d2):
    return stale_keys.read_window_rows(
        sb, TABLE, "id,buyout_date,marketplace_sku,article,product_name,buyouts_qty,buyouts_amount_buyer,buyouts_amount_seller,buyouts_units",
        [("eq", "marketplace_code", "wb"), ("gte", "buyout_date", d1), ("lte", "buyout_date", d2)], ["buyout_date", "marketplace_sku"])


# ---------- сборка ----------

def build_rows(sales, db_by_key):
    """Строки таблицы из строк отчёта: ключ (день продажи МСК, nmId)."""
    acc = {}
    for r in sales:
        sign = 1 if r["seller_oper_name"] == "Продажа" else -1
        key = (r["day"], str(r["nm_id"]))
        a = acc.setdefault(key, {"qty": Z, "seller": Z, "buyer": Z, "vendor": str(r.get("vendor_code") or "")})
        a["qty"] += sign * Decimal(str(r.get("quantity") or 0))
        a["seller"] += sign * Decimal(str(r.get("retail_price_with_disc") or 0))
        a["buyer"] += sign * Decimal(str(r.get("retail_amount") or 0))
    rows = {}
    for key, a in acc.items():
        old = db_by_key.get(key)
        rows[key] = {
            "buyout_date": key[0], "marketplace_code": "wb", "marketplace_sku": key[1],
            "article": (old or {}).get("article") or a["vendor"].upper() or None,
            "product_name": (old or {}).get("product_name"),
            "buyouts_qty": float(a["qty"]), "buyouts_amount_buyer": float(q(a["buyer"])), "buyouts_amount_seller": float(q(a["seller"])),
            "revenue_after_commission_vat": 0, "commission_amount": 0, "vat_amount": 0,
        }
    return rows


def classify(rows, db_by_key, days_with_sales):
    """add / rewrite / same / delete по ключам; ключи базы в днях без единой продажи в отчёте — untouched."""
    out = {"add": [], "rewrite": [], "same": [], "delete": [], "untouched": []}
    for key, row in rows.items():
        old = db_by_key.get(key)
        if old is None:
            out["add"].append((key, row, None))
        elif (q(old["buyouts_qty"]), q(old["buyouts_amount_seller"]), q(old["buyouts_amount_buyer"])) != (q(row["buyouts_qty"]), q(row["buyouts_amount_seller"]), q(row["buyouts_amount_buyer"])):
            out["rewrite"].append((key, row, old))
        else:
            out["same"].append((key, row, old))
    for key, old in db_by_key.items():
        if key in rows:
            continue
        (out["delete"] if key[0] in days_with_sales else out["untouched"]).append((key, None, old))
    return out


def month_table(cls):
    by = defaultdict(lambda: defaultdict(Decimal))
    for kind, items in cls.items():
        for key, row, old in items:
            m = key[0][:7]
            by[m][kind + "_n"] += 1
            if row is not None:
                by[m]["after_qty"] += Decimal(str(row["buyouts_qty"])); by[m]["after_amt"] += Decimal(str(row["buyouts_amount_seller"]))
            if old is not None and kind != "untouched":
                by[m]["before_qty"] += Decimal(str(old["buyouts_qty"] or 0)); by[m]["before_amt"] += Decimal(str(old["buyouts_amount_seller"] or 0))
    return by


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
    os.makedirs(os.path.join(ROOT, "snapshots"), exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(ROOT, "snapshots", f"marketplace_buyouts_wb_before_rebuild_{label}_{ts}.csv.gz")
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
