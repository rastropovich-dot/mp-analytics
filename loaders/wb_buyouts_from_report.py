#!/usr/bin/env python3
"""Выкупы WB (marketplace_buyouts) из отчёта реализации (wb_sales_report_rows): правило строки и ночная запись окна.

Решение советника 2026-09-24 (WB-7 §2): отчёт за день D лежит в базе уже в ночь D+1 (проверено утром 09-24:
max(rr_date) = 09-23), поэтому выкупы WB ночью пишутся ИЗ ОТЧЁТА целиком — всё окно отчёта, 21 день;
supplier/sales остаётся запасным путём только на ночь, когда отчёт не собрался (loaders/wb_sales_loader.py).
История 02-01 … 09-23 переписана тем же правилом 2026-09-24 09:01 UTC (scripts/wb_buyouts_rebuild_from_report.py).

ПРАВИЛО СТРОКИ (то же зерно, что у старого загрузчика — (дата, nmId)):
    buyout_date            день продажи — saleDt в московском времени (старый загрузчик брал `date`
                           supplier/sales, ту же московскую дату: сентябрь 22 из 22 равны)
    marketplace_sku        nmId
    buyouts_qty            Σ quantity × (+1 Продажа / −1 Возврат)
    buyouts_amount_seller  Σ retailPriceWithDisc × знак   (как priceWithDisc у supplier/sales)
    buyouts_amount_buyer   Σ retailAmount × знак          (как finishedPrice)
    article                как в базе, если ключ есть; иначе vendorCode прописными
    product_name           как в базе; у новых ключей — пусто (в отчёте предмета нет)
    revenue_after_commission_vat, commission_amount, vat_amount — нули, как писал старый загрузчик
    buyouts_units          не трогается (колонка Ozon, у WB null)

НОЧЬ (run): окно days_back дней до вчера по дате продажи МСК; ключи окна строятся из отчёта, ключи
add / rewrite пишутся upsert-ом, same — не трогаются; ключи окна, которых среди построенных нет, снимает
loaders/stale_keys.py (только при полном сборе отчёта, порог ≤ 200 и ≤ 2 % окна, список до удаления,
удаление после записи; день без единой продажи в отчёте не трогается). Обращений к WB — 0: читает базу.
"""
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from loaders import stale_keys
except ImportError:  # пайплайн зовёт загрузчики как скрипты: python3 loaders/<файл>.py
    import stale_keys

TABLE = "marketplace_buyouts"
REPORT = "wb_sales_report_rows"
MSK = timezone(timedelta(hours=3))
LAG_DAYS = 7          # rrDate позже даты продажи: 60 строк из 32 009 на 1…3 дня, 5 на 16…115 (report_wb_month.py)
DEFAULT_DAYS_BACK = 21   # = окно загрузчика отчёта (loaders/wb_sales_report_loader.py)
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


# ---------- запись ----------

def upsert_rows(sb, rows, batch=500):
    written = 0
    for i in range(0, len(rows), batch):
        sb.table(TABLE).upsert(rows[i:i + batch], on_conflict="buyout_date,marketplace_code,marketplace_sku").execute()
        written += len(rows[i:i + batch])
    return written


def _delete_wb_by_id(sb, rows, batch=200):
    ids = [r["id"] for r in rows]
    n = 0
    for i in range(0, len(ids), batch):
        res = sb.table(TABLE).delete().in_("id", ids[i:i + batch]).eq("marketplace_code", "wb").execute()
        n += len(res.data or [])
    return n


def cleanup_stale(sb, d1, d2, db_rows, rows, days_with_sales, complete, apply):
    """Ключи окна в базе, которых нет среди построенных, — снять по правилу stale_keys (день без продаж в отчёте — не трогать)."""
    window = {"day_from": d1, "day_to": d2, "complete": complete, "retries": 0, "failures": 0 if complete else 1}
    return stale_keys.cleanup(
        sb, TABLE, window, db_rows, set(rows), days_with_sales,
        lambda r: (str(r["buyout_date"]), str(r["marketplace_sku"])), lambda r: str(r["buyout_date"]),
        lambda r: f"{r['buyout_date']} sku {r['marketplace_sku']} {r.get('article')} qty {q(r['buyouts_qty'])} ₽ {q(r['buyouts_amount_seller'])} id {r['id']}",
        _delete_wb_by_id, apply)


def run(sb, days_back=DEFAULT_DAYS_BACK, today=None, dry_run=False, complete=True):
    """Ночная запись окна выкупов из отчёта. complete — отчёт за окно собран полностью (иначе застрявшие не чистятся)."""
    today = today or date.today()
    d2 = (today - timedelta(days=1)).isoformat()
    d1 = (today - timedelta(days=days_back)).isoformat()
    sales = read_report_sales(sb, d1, d2)
    db_rows = read_db(sb, d1, d2)
    db_by_key = {(str(r["buyout_date"]), str(r["marketplace_sku"])): r for r in db_rows}
    rows = build_rows(sales, db_by_key)
    days_with_sales = {r["day"] for r in sales}
    cls = classify(rows, db_by_key, days_with_sales)
    qty = sum((Decimal(str(r["buyouts_qty"])) for r in rows.values()), Z)
    amount = sum((Decimal(str(r["buyouts_amount_seller"])) for r in rows.values()), Z)
    print(f"WB выкупы из отчёта: окно {d1} … {d2} по дате продажи МСК — строк отчёта {len(sales)}, дней с продажами {len(days_with_sales)}, "
          f"ключей {len(rows)} (шт {qty:,.0f}, оборот {amount:,.2f}); в базе ключей окна {len(db_rows)}: добавить {len(cls['add'])}, "
          f"переписать {len(cls['rewrite'])}, без изменений {len(cls['same'])}, застрявших {len(cls['delete'])}, "
          f"в днях без продаж (не трогаю) {len(cls['untouched'])}", flush=True)
    if not sales:
        print("в отчёте за окно нет ни одной продажи — не пишу и не чищу (пустое окно — не истина «ноль»)", flush=True)
        return {"source": REPORT, "rows": 0, "written": 0, "deleted": 0, "window": (d1, d2)}
    to_write = [row for _k, row, _o in cls["add"] + cls["rewrite"]]
    if dry_run:
        print(f"dry-run: не пишу {len(to_write)} строк, db_writes = 0", flush=True)
        cleanup_stale(sb, d1, d2, db_rows, rows, days_with_sales, complete, apply=False)
        return {"source": REPORT, "rows": len(rows), "written": 0, "deleted": 0, "window": (d1, d2)}
    written = upsert_rows(sb, to_write) if to_write else 0
    print(f"✅ WB выкупы/продажи записаны в {TABLE} из отчёта реализации: {written} строк (добавить {len(cls['add'])}, "
          f"переписать {len(cls['rewrite'])}); без изменений {len(cls['same'])}", flush=True)
    deleted = cleanup_stale(sb, d1, d2, db_rows, rows, days_with_sales, complete, apply=True)
    return {"source": REPORT, "rows": len(rows), "written": written, "deleted": deleted, "window": (d1, d2)}
