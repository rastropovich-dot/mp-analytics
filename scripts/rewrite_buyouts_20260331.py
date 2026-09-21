"""Перезапись 2026-03-31 в marketplace_buyouts из accrual/by-day и пересборка витрин за эту дату.

Решение владельца 2026-09-14 (docs/inbox.md, восьмая задача, п. 2). Почему: 940
строк за 03-31 записал 2026-04-25 разовый loaders/ozon_realization_loader.py —
месячный отчёт о реализации за март одной датой (46 842 023,46, 2 523 позиции).
Источник за день даёт 1 892 788,00: 60 SKU, 65 позиций нетто. Разбор:
docs/ozon_finance_migration.md, раздел «31 марта».

Запускает владелец руками. Без --apply ничего не пишет и не удаляет
(db_writes = 0): показывает, что удалит и что запишет. Порядок с --apply:

  1. снимок: текущие строки 03-31 из marketplace_buyouts, daily_sku_kpi и
     daily_marketplace_kpi — в data/snapshots/ (до любого удаления);
  2. marketplace_buyouts: delete 03-31 (ozon) → запись строк, собранных штатным
     build_buyout_rows из accrual/by-day за 03-31;
  3. витрины ТОЛЬКО за 03-31: delete daily_sku_kpi / daily_marketplace_kpi (ozon,
     kpi_date = 03-31) → запись строк из штатных build_kpi / build_marketplace_kpi,
     отфильтрованных по дате (сами сборщики читают всю историю, но пишем одну дату);
  4. контроль: строки, позиции, суммы до и после; ожидание — 60 SKU,
     65 позиций нетто, 1 892 788,00.

    venv/bin/python3 scripts/rewrite_buyouts_20260331.py
    venv/bin/python3 scripts/rewrite_buyouts_20260331.py --apply
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from loaders import ozon_finance_accrual as accrual  # noqa: E402

DAY = "2026-03-31"
MP = "ozon"
SNAP_DIR = os.path.join("data", "snapshots")
# Ожидание — по штатному сборщику (план 2026-09-14): 60 SKU, 65 позиций нетто
# (69 продаж − 4 возврата), сумма 1 892 788,00. В источнике 121 товарная строка,
# 48 из них с нулевой суммой и комиссией сборщик пропускает — так задумано.
EXPECT = {"skus": 60, "positions": 65, "amount": Decimal("1892788.00")}
BATCH = 500


def D(v):
    return Decimal(str(v or 0)).quantize(Decimal("0.01"))


ORDER_BY = {"marketplace_buyouts": "marketplace_sku", "daily_sku_kpi": "marketplace_sku", "daily_marketplace_kpi": "kpi_date"}


def fetch_all(sb, table, filters, select="*"):
    """Постранично и с ORDER BY: range без сортировки у PostgREST отдаёт повторы (урок 2026-09-15)."""
    out, page = [], 0
    while True:
        q = sb.table(table).select(select)
        for op, field, value in filters:
            q = getattr(q, op)(field, value)
        q = q.order(ORDER_BY[table])
        res = q.range(page * 1000, page * 1000 + 999).execute()
        out.extend(res.data)
        if len(res.data) < 1000:
            break
        page += 1
    return out


def sums(rows, qty="buyouts_qty", amount="buyouts_amount_seller"):
    return (len(rows), sum(D(r.get(qty)) for r in rows), sum(D(r.get(amount)) for r in rows),
            len({str(r.get("marketplace_sku")) for r in rows}))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="удалять и писать; без флага — только план")
    args = parser.parse_args()

    from supabase import create_client
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

    # --- что есть сейчас
    cur = fetch_all(sb, "marketplace_buyouts", [("eq", "marketplace_code", MP), ("eq", "buyout_date", DAY)])
    sku_kpi = fetch_all(sb, "daily_sku_kpi", [("eq", "marketplace_code", MP), ("eq", "kpi_date", DAY)])
    mp_kpi = fetch_all(sb, "daily_marketplace_kpi", [("eq", "marketplace_code", MP), ("eq", "kpi_date", DAY)])
    n, q, a, s = sums(cur)
    print(f"=== сейчас в marketplace_buyouts за {DAY}: строк {n}, SKU {s}, позиций {q:.0f}, "
          f"buyouts_amount_seller {a:,.2f}, commission_amount {sum(D(r.get('commission_amount')) for r in cur):,.2f}")
    print(f"    daily_sku_kpi за {DAY}: строк {len(sku_kpi)}, Σbuyouts {sum(D(r.get('buyouts_amount_seller')) for r in sku_kpi):,.2f}; "
          f"daily_marketplace_kpi: {[(r.get('buyouts_qty'), r.get('buyouts_amount_seller')) for r in mp_kpi]}")

    # --- что даст источник
    accruals = accrual.fetch_day(DAY)
    rows, counters = accrual.build_buyout_rows(accruals)
    rows = [r for r in rows if r["buyout_date"] == DAY]
    n2, q2, a2, s2 = sums(rows)
    print(f"=== источник accrual/by-day за {DAY}: начислений {len(accruals)}, строк к записи {n2}, SKU {s2}, "
          f"позиций {q2:.0f}, buyouts_amount_seller {a2:,.2f}; счётчики {dict(counters)}")
    print(f"    ожидание: SKU {EXPECT['skus']}, позиций {EXPECT['positions']}, сумма {EXPECT['amount']:,.2f} — "
          f"{'сходится' if (s2, int(q2), a2) == (EXPECT['skus'], EXPECT['positions'], EXPECT['amount']) else 'НЕ СХОДИТСЯ, остановись и разберись'}")
    print(f"=== удалится: {n} строк выкупов ({a:,.2f}), {len(sku_kpi)} строк daily_sku_kpi, {len(mp_kpi)} строк daily_marketplace_kpi; "
          f"запишется: {n2} строк выкупов ({a2:,.2f}) и витрины за {DAY}")

    if not args.apply:
        print("режим плана: db_writes = 0")
        return
    if (s2, int(q2), a2) != (EXPECT["skus"], EXPECT["positions"], EXPECT["amount"]):
        raise SystemExit("источник не совпал с ожиданием — не пишу")

    # --- 1. снимок
    os.makedirs(SNAP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = os.path.join(SNAP_DIR, f"rewrite_{DAY}_{stamp}.json")
    json.dump({"day": DAY, "marketplace_buyouts": cur, "daily_sku_kpi": sku_kpi, "daily_marketplace_kpi": mp_kpi,
               "accruals": accruals}, open(snap, "w"), ensure_ascii=False)
    print(f"снимок записан: {snap} ({os.path.getsize(snap)} байт)")

    # --- 2. выкупы
    sb.table("marketplace_buyouts").delete().eq("marketplace_code", MP).eq("buyout_date", DAY).execute()
    left = fetch_all(sb, "marketplace_buyouts", [("eq", "marketplace_code", MP), ("eq", "buyout_date", DAY)], "marketplace_sku")
    if left:
        raise SystemExit(f"после удаления осталось {len(left)} строк — стоп")
    for i in range(0, len(rows), BATCH):
        sb.table("marketplace_buyouts").upsert(rows[i:i + BATCH], on_conflict="buyout_date,marketplace_code,marketplace_sku").execute()
    after = fetch_all(sb, "marketplace_buyouts", [("eq", "marketplace_code", MP), ("eq", "buyout_date", DAY)])
    n3, q3, a3, s3 = sums(after)
    print(f"выкупы после: строк {n3}, SKU {s3}, позиций {q3:.0f}, сумма {a3:,.2f} (db_writes: удалено {n}, записано {n3})")
    if (n3, a3) != (n2, a2):
        raise SystemExit("записано не то, что собрано — стоп")

    # --- 3. витрины только за DAY
    import reports_daily_sku_kpi as sku_kpi_mod
    import reports_daily_marketplace_kpi as mp_kpi_mod
    kpi_rows = [r for r in sku_kpi_mod.build_kpi() if r["kpi_date"] == DAY and r["marketplace_code"] == MP]
    sb.table("daily_sku_kpi").delete().eq("marketplace_code", MP).eq("kpi_date", DAY).execute()
    for i in range(0, len(kpi_rows), BATCH):
        sb.table("daily_sku_kpi").upsert(kpi_rows[i:i + BATCH], on_conflict="kpi_date,marketplace_code,marketplace_sku").execute()
    mp_rows = [r for r in mp_kpi_mod.build_marketplace_kpi() if r["kpi_date"] == DAY and r["marketplace_code"] == MP]
    sb.table("daily_marketplace_kpi").delete().eq("marketplace_code", MP).eq("kpi_date", DAY).execute()
    sb.table("daily_marketplace_kpi").upsert(mp_rows, on_conflict="kpi_date,marketplace_code").execute()
    k2 = fetch_all(sb, "daily_sku_kpi", [("eq", "marketplace_code", MP), ("eq", "kpi_date", DAY)])
    m2 = fetch_all(sb, "daily_marketplace_kpi", [("eq", "marketplace_code", MP), ("eq", "kpi_date", DAY)])
    print(f"витрины после: daily_sku_kpi {len(k2)} строк, Σbuyouts {sum(D(r.get('buyouts_amount_seller')) for r in k2):,.2f}; "
          f"daily_marketplace_kpi {[(r.get('buyouts_qty'), r.get('buyouts_amount_seller'), r.get('commission_amount')) for r in m2]}")
    print(f"db_writes: выкупы −{n} +{n3}, daily_sku_kpi −{len(sku_kpi)} +{len(kpi_rows)}, daily_marketplace_kpi −{len(mp_kpi)} +{len(mp_rows)}")


if __name__ == "__main__":
    main()
