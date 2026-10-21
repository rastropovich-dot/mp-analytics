"""Посев sku_catalog (Ozon) из разбора карточек каталога — для кабинета, у которого таблица пуста (РБХ §5.4, 2026-10-21).

    MP_CABINET=rbh1 venv/bin/python3 scripts/seed_sku_catalog_from_products.py            план: сколько строк, примеры; db_writes 0
    MP_CABINET=rbh1 venv/bin/python3 scripts/seed_sku_catalog_from_products.py --apply    upsert по (marketplace_code, marketplace_sku)

Зачем. loaders/ozon_stocks_loader.py строит вселенную товаров из sku_catalog (marketplace_sku там — product_id Ozon, не SKU),
а loaders/ozon_product_details_loader.py обновляет детали только у строк, которые в sku_catalog уже лежат: у нового кабинета
таблица пуста, и остатки Ozon не пишутся («sku_catalog_rows_loaded: 0», найдено на первом прогоне РБХ-1). Источник — разбор
scripts/ozon_product_catalog.py (data/<кабинет>/ozon_products/catalog_latest.json: product_id, offer_id, name, brand, category) и
штрихкод из снимка карточек того же каталога (catalog_<момент>.json → items[].barcodes). В API не ходит. Строки — в форме
ozon_product_details_loader.save_details (те же колонки), так что ночной загрузчик деталей дальше ведёт их сам.
"""
import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
import cabinet  # noqa: E402

CABINET = cabinet.assert_env()   # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента
CATALOG_DIR = cabinet.data_path("ozon_products", prof=CABINET)


def load_rows(catalog_dir=CATALOG_DIR):
    latest = os.path.join(catalog_dir, "catalog_latest.json")
    parsed = json.load(open(latest, encoding="utf-8"))
    if isinstance(parsed, dict):
        parsed = parsed.get("rows") or parsed.get("items") or next(v for v in parsed.values() if isinstance(v, list))
    barcodes = {}
    snaps = sorted(glob.glob(os.path.join(catalog_dir, "catalog_2*.json")))
    if snaps:
        snap = json.load(open(snaps[-1], encoding="utf-8"))
        for item in snap.get("items") or []:
            pid = item.get("id") or item.get("product_id")
            if pid and item.get("barcodes"):
                barcodes[str(pid)] = item["barcodes"][0]
    rows, skipped = [], 0
    for r in parsed:
        pid = r.get("product_id")
        if not pid:
            skipped += 1
            continue
        rows.append({"marketplace_code": "ozon", "marketplace_sku": str(pid), "article": str(r.get("offer_id") or ""),
                     "barcode": barcodes.get(str(pid)), "product_name": r.get("name"), "brand": r.get("brand"),
                     "category": r.get("category") or r.get("type_name")})
    return rows, skipped, latest, snaps[-1] if snaps else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    rows, skipped, latest, snap = load_rows()
    print(f"кабинет: {CABINET.DISPLAY_NAME if hasattr(CABINET, 'DISPLAY_NAME') else CABINET.CODE}; разбор {os.path.basename(latest)}, снимок {os.path.basename(snap) if snap else '—'}")
    print(f"строк к посеву {len(rows)} (без product_id пропущено {skipped}); со штрихкодом {sum(1 for r in rows if r['barcode'])}, "
          f"с брендом {sum(1 for r in rows if r['brand'])}, с артикулом {sum(1 for r in rows if r['article'])}")
    for r in rows[:3]:
        print("  ", {k: (v[:40] if isinstance(v, str) else v) for k, v in r.items()})
    from supabase import create_client
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    before = sb.table("sku_catalog").select("marketplace_sku", count="exact").eq("marketplace_code", "ozon").limit(1).execute().count
    print(f"в sku_catalog (ozon) сейчас: {before}")
    if not args.apply:
        print("db_writes = 0")
        return 0
    written = 0
    for i in range(0, len(rows), 500):
        sb.table("sku_catalog").upsert(rows[i:i + 500], on_conflict="marketplace_code,marketplace_sku").execute()
        written += len(rows[i:i + 500])
    after = sb.table("sku_catalog").select("marketplace_sku", count="exact").eq("marketplace_code", "ozon").limit(1).execute().count
    print(f"✅ sku_catalog: upsert {written}; в таблице (ozon) было {before}, стало {after} {'=' if after == len(rows) else '≠ ОЖИДАНИЮ ' + str(len(rows))}")
    print(f"db_writes = {written}")
    return 0 if after == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
