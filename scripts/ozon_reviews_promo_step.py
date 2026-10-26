#!/usr/bin/env python3
"""Ночной шаг «Ozon: участники ускоренного сбора отзывов» (слово владельца 2026-10-26, «Отзывы» по SKU до витрины). Нефатальный, идёт после
каталога. Только у кабинета с REVIEWS_96_RULE = "participants" (РБХ); у остальных — строка «пропуск» без обращений.

Все SKU ozon_products → /v3/product/info/list по 1 000 SKU (РБХ ~800 SKU — 1 обращение) → promotions[type = REVIEWS_PROMO].is_enabled →
ozon_reviews_promo_daily (observed_date = сегодня по UTC, upsert по ключу). Витрина (reports_daily_sku_kpi) берёт отсюда, по каким SKU
разносить тип 96 дня; снимок старше дня — последний ≤ дате.

    venv/bin/python3 scripts/ozon_reviews_promo_step.py --dry-run                         SKU и план обращений: в API не ходит, не пишет
    venv/bin/python3 scripts/ozon_reviews_promo_step.py                                   ночной режим: снимок + запись
    venv/bin/python3 scripts/ozon_reviews_promo_step.py --seed-file data/rbh1/ozon_products/catalog_20261021T223822Z.json [--apply]
                                                                                          посев истории из снимка каталога (items с promotions):
                                                                                          без --apply — план, с --apply — запись (по слову)
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
import cabinet  # noqa: E402
CABINET = cabinet.assert_env()  # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента
from loaders import ozon_reviews  # noqa: E402

CHUNK = 500


def write(sb, rows):
    for i in range(0, len(rows), CHUNK):
        sb.table(ozon_reviews.TABLE).upsert(rows[i:i + CHUNK], on_conflict="observed_date,sku").execute()


def summary(rows):
    on = [r for r in rows if r["is_enabled"]]
    return f"SKU {len(rows)}, подключено к «Ускоренному сбору отзывов» {len(on)}" + (f": {', '.join(str(r['sku']) for r in on[:20])}" + (" …" if len(on) > 20 else "") if on else "")


def run(sb, dry_run=False, now=None, catalog=None):
    if CABINET.REVIEWS_96_RULE != "participants":
        print(f"пропуск: у кабинета правило типа 96 «{CABINET.REVIEWS_96_RULE}» — участники услуги не нужны; обращений 0, db_writes 0")
        return 0
    if catalog is None:
        import ozon_product_catalog as catalog
    now = now or datetime.now(timezone.utc)
    skus = sorted(catalog.table_skus(sb), key=int)
    print(f"SKU в {catalog.TABLE}: {len(skus)}; обращений к /v3/product/info/list: {(len(skus) + catalog.BATCH - 1) // catalog.BATCH}", flush=True)
    if dry_run:
        print("--dry-run: в API не хожу, не пишу; db_writes = 0")
        return 0
    stats = {}
    items = catalog.fetch_cards(skus, stats)
    rows = ozon_reviews.promo_rows(items, now.date().isoformat(), now.isoformat(timespec="seconds"))
    write(sb, rows)
    print(f"снимок {now.date().isoformat()}: {summary(rows)}; обращений {stats.get('requests', 0)}, повторов {stats.get('retries', 0)}; "
          f"записано строк {len(rows)} в {ozon_reviews.TABLE}", flush=True)
    return 0


def seed(sb, path, apply=False):
    with open(path, encoding="utf-8") as fh:
        snap = json.load(fh)
    observed_at = snap["observed_at"]
    rows = ozon_reviews.promo_rows(snap.get("items") or [], observed_at[:10], observed_at)
    print(f"посев из {os.path.relpath(path, ROOT)} (снимок {observed_at}): {summary(rows)}")
    if not apply:
        print("план: без --apply ничего не пишу; db_writes = 0")
        return 0
    write(sb, rows)
    print(f"записано строк {len(rows)} в {ozon_reviews.TABLE} (дата {observed_at[:10]})")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="SKU и план обращений: в API не ходить, не писать")
    ap.add_argument("--seed-file", help="посев истории из снимка каталога (data/<кабинет>/ozon_products/catalog_*.json с items)")
    ap.add_argument("--apply", action="store_true", help="с --seed-file: записать (по слову)")
    args = ap.parse_args(argv)
    from loaders.ozon_fbo_orders_loader import supabase as sb   # тот же клиент, что у ночных шагов
    if args.seed_file:
        return seed(sb, args.seed_file, apply=args.apply)
    return run(sb, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
