#!/usr/bin/env python3
"""Логистика 1–9 июля 2026 по дате реализации отправления — пересчёт из сырья.

Проверка гипотезы «ручной отчёт относит логистику к дате реализации
отправления, а мы — к дате начисления» (четырнадцатая задача, 2026-09-16).
Читает только файлы, к Ozon и к БД не ходит:

  data/accrual_history/<день>.json           ответы accrual/by-day за 1–9 июля
                                             (снял measure_buyouts_history_vs_accrual.py)
  data/accrual_postings/2026-07-01_2026-07-09.json
                                             ответы accrual/postings по 2 420
                                             отправлениям с продажей в эти дни
                                             (13 обращений 2026-09-16, по 200 номеров)

«Продажа отправления в день D» — начисление категории POSTING за день D, у
которого хотя бы у одного товара заполнена commission. 46 отправлений
продаются в двух днях (частичные выдачи); в таблице отчёта такие отнесены к
ПОСЛЕДНЕМУ дню реализации, вторая колонка показывает отнесение к ПЕРВОМУ —
итог одинаковый, различается только разбивка по дням.

Типы логистики — TYPE_TO_EXPENSE из loaders/ozon_finance_accrual.py.
Числа ручного отчёта — из data/manual_report_july.xlsx (вне git), лист
за 1–9 июля, строка «Логистика».
"""
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from loaders.ozon_finance_accrual import TYPE_TO_EXPENSE  # noqa: E402

DAYS = [f"2026-07-0{i}" for i in range(1, 10)]
BYDAY_DIR = os.path.join("data", "accrual_history")
POSTINGS_FILE = os.path.join("data", "accrual_postings", "2026-07-01_2026-07-09.json")
LOGISTICS_TYPES = {t for t, article in TYPE_TO_EXPENSE.items() if article == "logistics"}

# Ручной отчёт, строка «Логистика», по дням (сумма дней 714 443,98; итог в
# листе 714 443,99 — копейка округления дневных ячеек).
MANUAL = {
    "2026-07-01": "77022.20", "2026-07-02": "63746.85", "2026-07-03": "82728.35",
    "2026-07-04": "69832.01", "2026-07-05": "74526.90", "2026-07-06": "89283.65",
    "2026-07-07": "91640.93", "2026-07-08": "96240.89", "2026-07-09": "69422.20",
}
MANUAL_TOTAL = Decimal("714443.99")


def fees(node):
    """Все узлы {type_id, accrued} в ответе by-day, где бы они ни лежали."""
    if isinstance(node, dict):
        if "type_id" in node and "accrued" in node:
            yield node
        for v in node.values():
            yield from fees(v)
    elif isinstance(node, list):
        for it in node:
            yield from fees(it)


def load_byday(day):
    with open(os.path.join(BYDAY_DIR, f"{day}.json")) as f:
        data = json.load(f)
    return data["accruals"] if isinstance(data, dict) else data


def main():
    byday = {d: load_byday(d) for d in DAYS}
    with open(POSTINGS_FILE) as f:
        postings = json.load(f)

    # 1. Продажи по дням из by-day.
    sold = {}
    for d in DAYS:
        s = set()
        for rec in byday[d]:
            if rec.get("accrued_category") != "POSTING" or not rec.get("posting"):
                continue
            if any(p.get("commission") for p in rec["posting"].get("products") or []):
                s.add(rec["unit_number"])
        sold[d] = s
    all_sold = set().union(*sold.values())
    first_day, last_day = {}, {}
    for d in DAYS:
        for pn in sold[d]:
            first_day.setdefault(pn, d)
            last_day[pn] = d
    multi = [pn for pn in all_sold if first_day[pn] != last_day[pn]]

    # 2. Логистика по отправлению из accrual/postings, все даты начисления.
    log_by_pn, rows, rows_out, n_accr = {}, 0, 0, 0
    for p in postings:
        s = Decimal(0)
        for a in p["accruals"]:
            n_accr += 1
            if a["type_id"] in LOGISTICS_TYPES:
                rows += 1
                if not (DAYS[0] <= a["accrual_date"] <= DAYS[-1]):
                    rows_out += 1
                s += Decimal(a["accrued"]["amount"])
        log_by_pn[p["posting_number"]] = s
    missing = all_sold - set(log_by_pn)

    print(f"отправлений с продажей 1–9 июля: {len(all_sold)}, в двух днях: {len(multi)}, "
          f"нет в accrual/postings: {len(missing)}")
    print(f"начислений в accrual/postings: {n_accr}, логистических: {rows}, "
          f"из них вне окна 1–9 июля: {rows_out}")
    print(f"типов логистики: {len(LOGISTICS_TYPES)}")

    # 3. По дате реализации (последний / первый день) против ручного отчёта.
    print(f"\n{'день':12}{'посл. день':>14}{'первый день':>14}{'ручной':>14}{'разн. (посл.)':>16}")
    t_last = t_first = t_man = Decimal(0)
    for d in DAYS:
        v_last = -sum((log_by_pn[pn] for pn in all_sold if last_day[pn] == d), Decimal(0))
        v_first = -sum((log_by_pn[pn] for pn in all_sold if first_day[pn] == d), Decimal(0))
        man = Decimal(MANUAL[d])
        t_last += v_last; t_first += v_first; t_man += man
        print(f"{d:12}{v_last:>14,.2f}{v_first:>14,.2f}{man:>14,.2f}{v_last - man:>16,.2f}")
    print(f"{'итого':12}{t_last:>14,.2f}{t_first:>14,.2f}{t_man:>14,.2f}{t_last - t_man:>16,.2f}")
    print(f"итог листа {MANUAL_TOTAL:,.2f}; сумма дней {t_man:,.2f}; разница {MANUAL_TOTAL - t_man:,.2f}")
    print(f"без НДС 22 %: {t_last / Decimal('1.22'):,.2f}")

    # 4. По дате начисления из by-day — как в marketplace_expenses.
    print("\nпо дате начисления (by-day):")
    t_acc = Decimal(0)
    for d in DAYS:
        s = -sum((Decimal(f["accrued"]["amount"]) for rec in byday[d] for f in fees(rec)
                  if f["type_id"] in LOGISTICS_TYPES), Decimal(0))
        t_acc += s
        print(f"  {d} {s:>14,.2f}")
    print(f"  итого {t_acc:>14,.2f}")


if __name__ == "__main__":
    main()
