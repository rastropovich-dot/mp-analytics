#!/usr/bin/env python3
"""Ручная чистка строк marketplace_buyouts, которых механизм (loaders/stale_keys.py) не достанет: ключи вне 30-дневного окна.

    venv/bin/python3 scripts/delete_stale_buyout_keys.py --key 2026-08-21:4453045714            план: db_writes = 0
    venv/bin/python3 scripts/delete_stale_buyout_keys.py --key 2026-08-21:4453045714 --apply    по слову владельца

Список — не приказ. Каждый ключ перепроверяется по живому by-day за его дату (1–3 обращения на дату): строится строка
выкупов тем же сборщиком, что и ночью (accrual.build_buyout_rows); ключ удаляется только если свежий полный сбор дня
его НЕ построил. Ключ, который сбор построил, не трогается и называется вслух вместе с тем, что построилось.
С --apply: снимок удаляемых строк (все колонки) → data/snapshots/buyouts_stale_keys_<UTC>.json → delete по id →
контроль «осталось 0». Витрину не трогает — ночной KPI перестроит ключ сам (у него не останется источника-выкупа).
Не в окне ночного прогона и утреннего алерта (loaders/pipeline_window.py).
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from loaders import ozon_finance_accrual as accrual  # noqa: E402
from loaders.pipeline_window import in_morning_alert_window, in_nightly_run_window  # noqa: E402

SNAP_DIR = os.path.join(ROOT, "data", "snapshots")
D = lambda v: Decimal(str(v or 0))  # noqa: E731


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", action="append", required=True, help="ДАТА:SKU, можно несколько")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    if in_nightly_run_window(now) or in_morning_alert_window(now):
        raise SystemExit("окно ночного прогона или утреннего алерта — не стартую")
    import loaders.ozon_fbo_orders_loader as fbo
    sb = fbo.supabase
    keys = sorted({tuple(k.split(":", 1)) for k in args.key})
    stats = {}
    to_delete, kept = [], []
    for day, sku in keys:
        res = sb.table("marketplace_buyouts").select("*").eq("marketplace_code", "ozon").eq("buyout_date", day).eq("marketplace_sku", sku).order("id").execute()
        rows = res.data or []
        if not rows:
            print(f"{day} sku {sku}: в таблице нет — нечего удалять")
            continue
        fresh = accrual.build_buyout_rows(accrual.fetch_day(day, stats))[0]
        built = [r for r in fresh if str(r["marketplace_sku"]) == sku]
        for r in rows:
            line = f"{day} sku {sku}: в таблице {D(r['buyouts_amount_seller']):,.2f} ₽, комиссия {D(r['commission_amount']):,.2f}, позиций {D(r['buyouts_qty'])}, штук {r.get('buyouts_units')}, создана {str(r.get('created_at'))[:19]} (id {r['id']})"
            if built:
                kept.append(r)
                print(f"  ОСТАВЛЯЮ — свежий сбор дня строит ключ: {built[0]['buyouts_amount_seller']:,.2f} ₽ | {line}")
            else:
                to_delete.append(r)
                print(f"  К УДАЛЕНИЮ — свежий сбор дня ключ не строит | {line}")
    print(f"обращений к Seller API {stats.get('requests', 0)}, повторов {stats.get('retries', 0)}, отказов {stats.get('failures', 0)}; "
          f"к удалению {len(to_delete)} на {sum((D(r['buyouts_amount_seller']) for r in to_delete), Decimal(0)):,.2f} ₽, оставлено {len(kept)}")
    if not args.apply or not to_delete:
        print("db_writes = 0")
        return
    if stats.get("retries") or stats.get("failures"):
        raise SystemExit("сбор дня был не гладким (повторы/отказы) — не удаляю")
    os.makedirs(SNAP_DIR, exist_ok=True)
    snap = os.path.join(SNAP_DIR, f"buyouts_stale_keys_{now.strftime('%Y%m%dT%H%M%SZ')}.json")
    json.dump({"table": "marketplace_buyouts", "rows": to_delete}, open(snap, "w"), ensure_ascii=False)
    print(f"снимок {len(to_delete)} строк → {snap}")
    ids = [r["id"] for r in to_delete]
    deleted = len(sb.table("marketplace_buyouts").delete().in_("id", ids).execute().data or [])
    left = len(sb.table("marketplace_buyouts").select("id").in_("id", ids).execute().data or [])
    print(f"удалено {deleted} из {len(ids)}, осталось {left} {'=' if left == 0 and deleted == len(ids) else '≠ ОШИБКА'}")
    print(f"db_writes = {deleted}")


if __name__ == "__main__":
    main()
