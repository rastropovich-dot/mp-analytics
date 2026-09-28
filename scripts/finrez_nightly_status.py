#!/usr/bin/env python3
"""Итог ночной сборки книги «Фин рез» (scripts/finrez_nightly.sh) — одной строкой в pipeline_runtime_state (finrez_nightly:last),
чтобы утренний алерт 07:30 UTC на Render мог сказать «книга Фин рез: собрана …» (сорок третья §5). Книга живёт на машине владельца,
Render её не видит — видит только эту строку.

    venv/bin/python3 scripts/finrez_nightly_status.py --print --date … --out … --bytes … --sha256 … --rc 0    только напечатать payload, db_writes = 0
    venv/bin/python3 scripts/finrez_nightly_status.py --write …                                              записать (upsert по state_key)

Строку алерта строит alerts_telegram.finrez_book_line(payload). Отказ записи не роняет сборку: код 0 и строка «⚠️ итог не записан» в логе.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

STATE_KEY = "finrez_nightly:last"
STATE_TYPE = "finrez_nightly"


def build_payload(args):
    finished = datetime.now(timezone.utc)
    return {"date": args.date, "started_at": args.started, "finished_at": finished.isoformat(timespec="seconds"), "seconds": int(args.seconds),
            "out": args.out, "bytes": int(args.bytes or 0), "sha256": args.sha256 or None, "rc": int(args.rc), "copied": args.copied or None,
            "error": args.error or None, "log": args.log or None, "host": os.uname().nodename}


def write_payload(payload, client=None):
    if client is None:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
        from loaders.ozon_fbo_orders_loader import supabase as client
    client.table("pipeline_runtime_state").upsert(
        {"state_key": STATE_KEY, "state_type": STATE_TYPE, "account_signature": None, "payload": payload, "updated_at": payload["finished_at"]},
        on_conflict="state_key").execute()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", dest="print_only"); ap.add_argument("--write", action="store_true")
    for k in ("date", "started", "out", "sha256", "copied", "error", "log"):
        ap.add_argument(f"--{k}", default="")
    ap.add_argument("--seconds", default="0"); ap.add_argument("--bytes", default="0"); ap.add_argument("--rc", default="0")
    args = ap.parse_args(argv)
    payload = build_payload(args)
    print("finrez_nightly:last →", json.dumps(payload, ensure_ascii=False))
    if args.write and not args.print_only:
        try:
            write_payload(payload)
            print("итог записан в pipeline_runtime_state (finrez_nightly:last); db_writes = 1")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ итог не записан в pipeline_runtime_state: {type(exc).__name__}: {exc}")
    else:
        print("db_writes = 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
