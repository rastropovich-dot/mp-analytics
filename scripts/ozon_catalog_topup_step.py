#!/usr/bin/env python3
"""Ночной шаг «Ozon: каталог, добор недостающих» (сорок третья §4). Нефатальный, идёт после шагов FBS / FBO.

SKU базы (заказы ∪ выкупы ∪ реклама с 2026-03-28, как в ozon_product_catalog.sku_universe) минус ozon_products = SKU без карточки —
обычно новые товары первого дня. Если их > 0: карточки только недостающих (`/v3/product/info/list`, 1 обращение на 1 000 SKU) плюс
дерево категорий (файл data/ozon_products/category_tree.json, если есть; на Render файла нет — 1 обращение), разбор и запись upsert
по sku — ровно то, что делает `ozon_product_catalog.py --collect --only-missing --apply` по слову. Итог — строкой в
pipeline_runtime_state (ozon_catalog_topup:last) для утреннего алерта: «каталог: без карточки N SKU (добрано M)».

    venv/bin/python3 scripts/ozon_catalog_topup_step.py --dry-run     счёт и список недостающих: в API не ходит, не пишет (db_writes = 0)
    venv/bin/python3 scripts/ozon_catalog_topup_step.py               ночной режим: карточки + запись + итог (в окне ночи разрешено)

Отказ (429, сеть, разбор) — код 1, итог с полем error всё равно записывается: алерт покажет «⚠️ каталог … ошибка шага».
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
import cabinet  # noqa: E402
CABINET = cabinet.assert_env()  # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента
import ozon_product_catalog as cat  # noqa: E402

STATE_KEY = "ozon_catalog_topup:last"
STATE_TYPE = "ozon_catalog_topup"
TREE_FILE = os.path.join(cat.OUT_DIR, "category_tree.json")
LIST_LIMIT = 50        # сколько SKU без карточки хранить в итоге


def missing_skus(sb, catalog=cat):
    """(info, отсортированный список SKU без строки в ozon_products, число строк таблицы)."""
    info, _orders, _buyouts = catalog.sku_universe(sb)
    in_table = catalog.table_skus(sb) if catalog.table_exists(sb) else set()
    return info, sorted(s for s in info if s not in in_table), len(in_table)


def write_state(sb, payload):
    sb.table("pipeline_runtime_state").upsert(
        {"state_key": STATE_KEY, "state_type": STATE_TYPE, "account_signature": None, "payload": payload, "updated_at": payload["finished_at"]},
        on_conflict="state_key").execute()


def run(sb, dry_run=False, tree_file=TREE_FILE, catalog=cat, now=None):
    t0 = time.monotonic()
    now = now or datetime.now(timezone.utc)
    info, missing, n_table = missing_skus(sb, catalog)
    print(f"Каталог Ozon: SKU в базе {len(info)}, в {catalog.TABLE} {n_table}, без карточки {len(missing)}"
          + (f": {', '.join(missing[:LIST_LIMIT])}" + (" …" if len(missing) > LIST_LIMIT else "") if missing else ""), flush=True)
    payload = {"date": now.date().isoformat(), "started_at": now.isoformat(timespec="seconds"), "skus_in_base": len(info), "in_table": n_table,
               "missing_before": len(missing), "missing": missing[:LIST_LIMIT], "collected": 0, "written": 0, "requests": 0,
               "missing_after": len(missing), "dry_run": bool(dry_run), "error": None}
    if dry_run:
        print(f"--dry-run: в API не хожу, не пишу; обращений было бы {(len(missing) + catalog.BATCH - 1) // catalog.BATCH}"
              f"{' + дерево 1' if missing and not os.path.exists(tree_file) else ''}; db_writes = 0")
        payload["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return payload, 0
    code = 0
    try:
        if missing:
            stats = {}
            observed_at = now.isoformat()
            items = catalog.fetch_cards(missing, stats)
            if tree_file and os.path.exists(tree_file):
                with open(tree_file, encoding="utf-8") as fh:
                    tree = json.load(fh)
            else:
                tree = catalog.fetch_tree(stats)
            rows, counters, other_types = catalog.build_rows({s: info[s] for s in missing}, items, tree, observed_at)
            print(f"карточек получено {len(items)} на {len(missing)} SKU, строк к записи {len(rows)}; обращений к Seller API {stats.get('requests', 0)}, "
                  f"повторов {stats.get('retries', 0)}, отказов {stats.get('failures', 0)}; категории: {dict(counters) if counters else {}}"
                  + (f"; типы вне словаря: {other_types}" if other_types else ""), flush=True)
            catalog.apply(sb, rows, allow_window=True)
            after = missing_skus(sb, catalog)[1]
            payload.update(collected=len(items), written=len(rows), requests=int(stats.get("requests", 0)), missing_after=len(after))
        else:
            print("добирать нечего; db_writes = 0")
    except SystemExit as exc:
        payload["error"] = str(exc); code = 1
    except Exception as exc:  # noqa: BLE001
        payload["error"] = f"{type(exc).__name__}: {exc}"; code = 1
    payload["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload["seconds"] = round(time.monotonic() - t0, 1)
    try:
        write_state(sb, payload)
        print(f"итог записан в pipeline_runtime_state ({STATE_KEY}): без карточки {payload['missing_after']} (добрано {payload['written']})"
              + (f", ошибка: {payload['error']}" if payload["error"] else ""), flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ итог не записан в pipeline_runtime_state: {type(exc).__name__}: {exc}", flush=True)
    return payload, code


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="только счёт и список недостающих: в API не ходить, не писать")
    ap.add_argument("--tree-file", default=TREE_FILE, help="файл дерева категорий (нет файла — одно обращение к API)")
    args = ap.parse_args(argv)
    from loaders.ozon_fbo_orders_loader import supabase as sb   # тот же клиент, что у ночных шагов
    _payload, code = run(sb, dry_run=args.dry_run, tree_file=args.tree_file)
    return code


if __name__ == "__main__":
    sys.exit(main())
