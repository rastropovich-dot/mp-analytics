#!/usr/bin/env python3
"""Переклассификация advertising_other -> advertising_clicks (дефект A).

Дефект A (исправлен на источнике в 6967f4e): classify_expense_type читала текст
метаданных кампании, при бэкфилле старой даты метаданные пустые, и расход CPC
уезжал в catch-all advertising_other. Исторически так лежит 1194 строки на
123 413,92 ₽ за 90 дат. На источнике это больше не происходит, но старые строки
надо перенести.

Почему это не UPDATE одного поля. Уникальный индекс
    marketplace_expenses_unique_idx (expense_date, marketplace_code,
                                     marketplace_sku, expense_type)
включает expense_type, поэтому смена типа — это смена ключа. Там, где на ту же
дату и SKU уже есть строка advertising_clicks, простая смена типа упрётся в
конфликт, и расходы надо СКЛАДЫВАТЬ, а не заменять.

Три класса строк:
    simple      смена типа на месте, конфликта нет
    merge       прибавить сумму к существующей advertising_clicks, старую удалить
    suspicious  НЕ ТРОГАЕМ (см. ниже)

Инвариант проверки: для каждой даты
    sum(clicks) + sum(other)  ДО  ==  sum(clicks)  ПОСЛЕ
минус сумма suspicious-строк, которые остаются в advertising_other.

По умолчанию dry-run: db_writes = 0. Запись только с --apply вместе с
--approve-reclassification.
"""

import argparse
import csv
import os
import sys
from collections import defaultdict

sys.path.insert(0, ".")

from loaders.ozon_performance_ads_loader import supabase  # noqa: E402

MARKETPLACE = "ozon"
SOURCE_TYPE = "advertising_other"
TARGET_TYPE = "advertising_clicks"
SNAPSHOT_DIR = "snapshots"


def fetch_all(table, columns, filters=None, page_size=1000):
    rows, start = [], 0
    while True:
        query = supabase.table(table).select(columns)
        for column, value in (filters or {}).items():
            query = query.eq(column, value)
        batch = query.range(start, start + page_size - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < page_size:
            return rows
        start += page_size


def load_rows():
    other = fetch_all("marketplace_expenses",
                      "id,expense_date,marketplace_sku,expense_type,expense_amount",
                      {"marketplace_code": MARKETPLACE, "expense_type": SOURCE_TYPE})
    clicks = fetch_all("marketplace_expenses",
                       "id,expense_date,marketplace_sku,expense_type,expense_amount",
                       {"marketplace_code": MARKETPLACE, "expense_type": TARGET_TYPE})
    return other, clicks


def classify_rows(other_rows, clicks_rows):
    """simple | merge | suspicious.

    suspicious — строки, где SKU НИ РАЗУ не встречается как advertising_clicks.
    Для такого SKU нет основания считать расход кликовым: возможно, это законный
    другой тип. Все они лежат на 2026-08-11, внутри окна пересборки кампаний
    10-16 августа, что делает их ещё подозрительнее. Не трогаем.
    """
    clicks_by_key = {}
    clicks_skus = set()
    for row in clicks_rows:
        key = (str(row["expense_date"]), str(row["marketplace_sku"]))
        clicks_by_key[key] = row
        clicks_skus.add(str(row["marketplace_sku"]))

    buckets = {"simple": [], "merge": [], "suspicious": []}
    for row in other_rows:
        sku = str(row["marketplace_sku"])
        key = (str(row["expense_date"]), sku)
        if sku not in clicks_skus:
            buckets["suspicious"].append(row)
        elif key in clicks_by_key:
            buckets["merge"].append({**row, "target": clicks_by_key[key]})
        else:
            buckets["simple"].append(row)
    return buckets


def build_snapshot(other_rows, clicks_rows):
    """Расход по каждой затронутой дате в разрезе expense_type."""
    affected = {str(r["expense_date"]) for r in other_rows}
    totals = defaultdict(lambda: defaultdict(lambda: {"rows": 0, "spend": 0.0}))
    for row in other_rows + clicks_rows:
        day = str(row["expense_date"])
        if day not in affected:
            continue
        bucket = totals[day][str(row["expense_type"])]
        bucket["rows"] += 1
        bucket["spend"] += float(row["expense_amount"] or 0)
    return totals


def write_snapshot(totals, other_rows):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)

    by_date = os.path.join(SNAPSHOT_DIR, "advertising_other_before_by_date.csv")
    with open(by_date, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["expense_date", "expense_type", "rows", "spend"])
        for day in sorted(totals):
            for expense_type in sorted(totals[day]):
                cell = totals[day][expense_type]
                writer.writerow([day, expense_type, cell["rows"], round(cell["spend"], 2)])

    # Построчный снимок — единственное, к чему можно вернуться: версионирования нет.
    by_row = os.path.join(SNAPSHOT_DIR, "advertising_other_before_rows.csv")
    with open(by_row, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "expense_date", "marketplace_sku", "expense_type", "expense_amount"])
        for row in sorted(other_rows, key=lambda r: (str(r["expense_date"]), str(r["marketplace_sku"]))):
            writer.writerow([row["id"], row["expense_date"], row["marketplace_sku"],
                             row["expense_type"], row["expense_amount"]])
    return by_date, by_row


def summarize(buckets):
    out = {}
    for name, rows in buckets.items():
        out[name] = {
            "rows": len(rows),
            "spend": round(sum(float(r["expense_amount"] or 0) for r in rows), 2),
            "dates": len({str(r["expense_date"]) for r in rows}),
        }
    return out


def apply_changes(buckets, approved):
    """Запись. Без approved не вызывается."""
    if not approved:
        raise RuntimeError("apply_changes требует --approve-reclassification")

    writes = 0
    for row in buckets["simple"]:
        supabase.table("marketplace_expenses").update(
            {"expense_type": TARGET_TYPE}).eq("id", row["id"]).execute()
        writes += 1

    for row in buckets["merge"]:
        target = row["target"]
        merged = round(float(target["expense_amount"] or 0) + float(row["expense_amount"] or 0), 2)
        supabase.table("marketplace_expenses").update(
            {"expense_amount": merged}).eq("id", target["id"]).execute()
        supabase.table("marketplace_expenses").delete().eq("id", row["id"]).execute()
        writes += 2

    return writes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="выполнить запись (по умолчанию dry-run)")
    parser.add_argument("--approve-reclassification", action="store_true",
                        help="обязательное подтверждение вместе с --apply")
    parser.add_argument("--snapshot", action="store_true", help="записать снимок в snapshots/")
    args = parser.parse_args()

    other_rows, clicks_rows = load_rows()
    buckets = classify_rows(other_rows, clicks_rows)
    stats = summarize(buckets)

    print(f"advertising_other всего: {len(other_rows)} строк, "
          f"{sum(float(r['expense_amount'] or 0) for r in other_rows):,.2f} ₽, "
          f"{len({str(r['expense_date']) for r in other_rows})} дат".replace(",", " "))
    print()
    for name, label in (("simple", "простая смена типа"),
                        ("merge", "слияние с существующей clicks"),
                        ("suspicious", "СОМНИТЕЛЬНЫЕ — не трогаем")):
        cell = stats[name]
        print(f"  {label:<34} {cell['rows']:>5} строк  {cell['spend']:>12,.2f} ₽  "
              f"{cell['dates']:>3} дат".replace(",", " "))

    if args.snapshot:
        by_date, by_row = write_snapshot(build_snapshot(other_rows, clicks_rows), other_rows)
        print(f"\nСнимок записан: {by_date}, {by_row}")

    if not args.apply:
        print("\nDRY-RUN: db_writes = 0. Для записи нужны --apply --approve-reclassification.")
        return

    if not args.approve_reclassification:
        raise SystemExit("--apply требует --approve-reclassification")

    writes = apply_changes(buckets, approved=True)
    print(f"\nПрименено. db_writes = {writes}")


if __name__ == "__main__":
    main()
