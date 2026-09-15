"""Шаг «Ozon: лог статусов отправлений» — переходы из сырого ответа сбора в ozon_posting_status_log.

Без --apply ничего не пишет: показывает, сколько строк даст (db_writes = 0).
Читает последний файл data/postings_raw/<schema>_*.json (их кладут шаги заказов —
scripts/ozon_fbo_orders_step.py и scripts/ozon_fbs_orders_step.py — из того же
ответа, что уже записан в marketplace_orders; к API не ходит). Пишет только
изменения статусов: первое наблюдение и смену статуса.

    venv/bin/python3 scripts/ozon_posting_status_log.py                       # план по обеим схемам
    venv/bin/python3 scripts/ozon_posting_status_log.py --apply               # ночной шаг
    venv/bin/python3 scripts/ozon_posting_status_log.py --raw data/postings_raw/fbo_20260914T115900Z.json --apply   # посев из снимка

В ночном прогоне — нефатальный шаг после загрузки заказов: его отказ не
должен ронять расходы, рекламу и KPI. Таблицы нет — печатает и выходит с кодом 1.
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from loaders import ozon_posting_status_log as log  # noqa: E402


def table_exists(sb):
    try:
        sb.table(log.TABLE).select("posting_number").limit(1).execute()
        return True
    except Exception as exc:
        if "PGRST205" in str(exc) or log.TABLE in str(exc):
            return False
        raise


def process(sb, path, apply, source_override=None):
    data = json.load(open(path))
    schema, fetched_at, postings = data["schema"], data["fetched_at"], data["postings"]
    observed_at = datetime.fromisoformat(fetched_at.replace("Z", "+00:00")).isoformat()
    source = source_override or ("nightly" if os.path.basename(path).startswith(schema + "_") else f"snapshot:{os.path.basename(path)}")
    numbers = [str(p.get("posting_number") or "") for p in postings]
    last = log.load_last_status(sb, numbers) if sb is not None else {}
    rows, counters = log.observation_rows(postings, schema, observed_at, source, last)
    print(f"{schema} {os.path.basename(path)}: отправлений {counters['seen']}, впервые {counters['new']}, сменили статус {counters['changed']}, "
          f"без изменений {counters['unchanged']}, без даты {counters['no_date']}, дублей {counters['duplicates']} → строк {len(rows)}")
    if apply and rows:
        written = log.write_rows(sb, rows)
        print(f"  записано {written}, db_writes = {written}")
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw", action="append", help="конкретный файл сырья; можно несколько; по умолчанию — последние fbo_ и fbs_")
    parser.add_argument("--schema", choices=["fbo", "fbs", "both"], default="both")
    parser.add_argument("--source", help="пометка источника, например snapshot:2026-09-14")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    paths = args.raw or [p for s in (["fbo", "fbs"] if args.schema == "both" else [args.schema]) if (p := log.latest_raw(s))]
    if not paths:
        print("нет сырья в", log.RAW_DIR); return 1

    sb = None
    from supabase import create_client
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))
    if not table_exists(sb):
        print(f"таблицы {log.TABLE} нет — применить sql/20260916_create_ozon_posting_status_log.sql; шаг пропущен")
        return 1
    total = 0
    for path in paths:
        total += process(sb, path, args.apply, args.source)
    print(f"итого строк {'записано' if args.apply else 'к записи'}: {total}" + ("" if args.apply else "; db_writes = 0"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
