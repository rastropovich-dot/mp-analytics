#!/usr/bin/env python3
"""Посев subject_name / brand_name в wb_sales_report_rows из сырья отчёта на диске (WB-8 §2, миграция 2026-09-24).

    venv/bin/python3 scripts/seed_wb_sales_report_subjects.py --plan                                   0 обращений, db_writes = 0
    venv/bin/python3 scripts/seed_wb_sales_report_subjects.py --apply --approve-wb-subjects-write      запись по слову

Сырьё — data/wb_sales_report_raw/daily_<от>_<до>.json (бэкфилл 09-23: 02-01 … 09-22, 329 522 строки). У каждой
строки отчёта есть subjectName и brandName (проверено 09-24); пусто — у операций без товара (возмещение ПВЗ,
хранение, удержание), их не трогаем. Строки с 09-23 сырья на диске нет — их доберёт ночной загрузчик
(окно 21 день, поля в FIELDS с 09-24).

ЗАПИСЬ — update по rrd_id пачками с одинаковыми значениями: пар (предмет, бренд) — десятки, поэтому
`update({...}).in_("rrd_id", …)` по CHUNK ключей за запрос, а не upsert целых строк (upsert из старого сырья
затёр бы ночные значения окна). Ничего, кроме двух колонок, не меняется. Отказ в ночном окне.
"""
import argparse
import glob
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from loaders.pipeline_window import in_nightly_run_window, window_text  # noqa: E402
import loaders.wb_sales_report_loader as loader  # noqa: E402

RAW_DIR = os.path.join(ROOT, "data", "wb_sales_report_raw")
CHUNK = 300


def collect(raw_dir=RAW_DIR):
    """{(subject, brand): [rrd_id]} из файлов сырья; строки без предмета и бренда — счётчик по операции."""
    groups = defaultdict(list)
    without = Counter()
    seen = set()
    files = sorted(glob.glob(os.path.join(raw_dir, "daily_*.json")))
    total = 0
    for path in files:
        for x in json.load(open(path, encoding="utf-8")):
            rrd = int(x["rrdId"])
            if rrd in seen:
                raise RuntimeError(f"rrdId {rrd} встречается дважды в сырье")
            seen.add(rrd)
            total += 1
            subject, brand = (x.get("subjectName") or None), (x.get("brandName") or None)
            if subject or brand:
                groups[(subject, brand)].append(rrd)
            else:
                without[x.get("sellerOperName")] += 1
    return groups, without, total, len(files)


def plan(groups, without, total, files):
    with_subject = sum(len(v) for v in groups.values())
    print(f"Сырьё: файлов {files}, строк {total}; с предметом/брендом {with_subject}, без {sum(without.values())} "
          f"(по операциям: " + ", ".join(f"{k} {v}" for k, v in without.most_common(6)) + ")")
    print(f"Пар (предмет, бренд): {len(groups)}; запросов update по {CHUNK} ключей: {sum((len(v) + CHUNK - 1) // CHUNK for v in groups.values())}")
    for (subject, brand), ids in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"    {subject!s:32} {brand!s:12} {len(ids):>8}")
    print("db_writes = 0")


def apply(sb, groups, sleep_fn=None):
    if in_nightly_run_window(datetime.now(timezone.utc)):
        raise SystemExit(f"ночное окно ({window_text()}): запись отложить")
    updated, requests = 0, 0
    started = datetime.now(timezone.utc)
    for (subject, brand), ids in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        n = 0
        for i in range(0, len(ids), CHUNK):
            res = sb.table(loader.TABLE).update({"subject_name": subject, "brand_name": brand}, count="exact", returning="minimal").in_("rrd_id", ids[i:i + CHUNK]).execute()
            requests += 1
            n += res.count if res.count is not None else 0
        updated += n
        print(f"✅ {subject} / {brand}: обновлено {n} из {len(ids)}", flush=True)
    print(f"✅ {loader.TABLE}: subject_name / brand_name обновлены у {updated} строк, запросов {requests}, "
          f"{(datetime.now(timezone.utc) - started).total_seconds():.0f} с ({started.isoformat()})")
    return updated


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true"); ap.add_argument("--apply", action="store_true")
    ap.add_argument("--approve-wb-subjects-write", action="store_true")
    args = ap.parse_args(argv)
    if not (args.plan or args.apply):
        ap.error("нужен --plan или --apply")
    groups, without, total, files = collect()
    plan(groups, without, total, files)
    if args.apply:
        if not args.approve_wb_subjects_write:
            print("--apply без --approve-wb-subjects-write: не пишу"); return 2
        apply(loader._client(), groups)
    return 0


if __name__ == "__main__":
    sys.exit(main())
