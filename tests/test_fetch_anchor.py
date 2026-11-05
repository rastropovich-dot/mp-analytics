"""Шестидесятая §1: постраничное чтение книги без глубокого OFFSET — окно с якорного дня, OFFSET только внутри дня.

Книга «Фин рез» KARATOV падала 01–05.11 на чтении daily_sku_kpi: OFFSET 250 000 — 15,2 с при statement_timeout 8 с
(EXPLAIN ANALYZE 05.11). Тесты: строки и их порядок = прежнему OFFSET от начала окна на любых размерах дней (меньше
страницы, ровно страница, больше страницы, кратно 1 000), число обращений то же, OFFSET не больше строк одного дня,
без gte по первой колонке сортировки — прежний путь. Сети нет: таблица в памяти исполняет фильтры, сортировку и range.
"""
import os
import random
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import report_ozon_month as rep  # noqa: E402

OPS = {"eq": lambda a, b: a == b, "gte": lambda a, b: a >= b, "lte": lambda a, b: a <= b}


class Res:
    def __init__(self, data):
        self.data = data


class Query:
    def __init__(self, table):
        self.table, self.filters, self.orders, self.rng = table, [], [], None

    def select(self, cols):
        self.cols = [c.strip() for c in cols.split(",")]
        return self

    def __getattr__(self, op):
        if op in OPS:
            return lambda col, val: (self.filters.append((op, col, val)), self)[1]
        raise AttributeError(op)

    def order(self, col):
        self.orders.append(col)
        return self

    def range(self, a, b):
        self.rng = (a, b)
        return self

    def execute(self):
        self.table.calls.append(self)
        rows = [r for r in self.table.rows if all(OPS[op](r[c], v) for op, c, v in self.filters)]
        rows.sort(key=lambda r: tuple(r[c] for c in self.orders))
        a, b = self.rng
        return Res([{c: r[c] for c in self.cols} for r in rows[a:b + 1]])


class FakeSb:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def table(self, name):
        return Query(self)


def make_rows(sizes, other_market=True):
    rows, rid = [], 0
    for i, n in enumerate(sizes):
        day = f"2026-10-{i + 1:02d}"
        for k in range(n):
            rid += 1
            rows.append({"id": rid, "kpi_date": day, "marketplace_code": "ozon", "marketplace_sku": f"{(k * 7919) % 100000:05d}", "ad_spend": k})
            if other_market:
                rid += 1
                rows.append({"id": rid, "kpi_date": day, "marketplace_code": "wb", "marketplace_sku": f"{k:05d}", "ad_spend": k})
    random.Random(1).shuffle(rows)       # порядок кучи — не порядок ключа
    return rows


def old_fetch(sb, table, select, filters, orders):
    """Прежний rep.fetch до шестидесятой: OFFSET от начала окна."""
    out, page = [], 0
    while True:
        qb = sb.table(table).select(select)
        for f in filters:
            qb = getattr(qb, f[0])(*f[1:])
        for col in orders:
            qb = qb.order(col)
        res = qb.range(page * 1000, page * 1000 + 999).execute()
        out.extend(res.data)
        if len(res.data) < 1000:
            return out
        page += 1


SELECT = "id,kpi_date,marketplace_sku,ad_spend"
FILTERS = [("eq", "marketplace_code", "ozon"), ("gte", "kpi_date", "2026-10-01"), ("lte", "kpi_date", "2026-10-31")]
ORDERS = ["kpi_date", "marketplace_code", "marketplace_sku"]


def run(fetch_fn, sb):
    with mock.patch("builtins.print"):
        return fetch_fn(sb, "daily_sku_kpi", SELECT, FILTERS, ORDERS)


class AnchorFetchTests(unittest.TestCase):
    def check_same(self, sizes):
        sb_old, sb_new = FakeSb(make_rows(sizes)), None
        sb_new = FakeSb(sb_old.rows)
        old = run(old_fetch, sb_old)
        new = run(rep.fetch, sb_new)
        self.assertEqual(new, old, f"строки и порядок как у OFFSET от начала окна, дни {sizes}")
        self.assertEqual(len(new), sum(sizes))
        self.assertEqual(len(sb_new.calls), len(sb_old.calls), "обращений столько же")
        biggest_day = max(sizes)
        for q in sb_new.calls:
            self.assertLessEqual(q.rng[0], biggest_day, "OFFSET не больше строк одного дня")
        return sb_new

    def test_days_smaller_than_page(self):
        self.check_same([300, 450, 120, 999, 1, 640, 333, 700])

    def test_days_bigger_than_page_and_exact_multiples(self):
        sb = self.check_same([2520, 1000, 3000, 7, 2886, 1999, 2001, 0, 1000])
        self.assertEqual(len(sb.calls), 15)
        last = sb.calls[-1]
        self.assertEqual(dict((f[1], f[2]) for f in last.filters if f[0] == "gte"), {"kpi_date": "2026-10-09"},
                         "последняя страница начинается с якорного дня, а не с начала окна")
        self.assertEqual(last.rng, (587, 1586), "OFFSET — 587 строк якорного дня, уже прочитанных, а не 14 000")

    def test_total_is_exact_multiple_of_page(self):
        self.check_same([1000, 1000, 1000])
        self.check_same([500, 500])

    def test_random_sizes(self):
        rnd = random.Random(60)
        for _ in range(25):
            self.check_same([rnd.choice([0, 1, 999, 1000, 1001, rnd.randint(1, 3500)]) for _ in range(rnd.randint(1, 12))])

    def test_empty_window(self):
        sb = FakeSb([])
        self.assertEqual(run(rep.fetch, sb), [])
        self.assertEqual(len(sb.calls), 1)

    def test_lte_bound_is_kept(self):
        sb = FakeSb(make_rows([1500, 1500, 1500]))
        with mock.patch("builtins.print"):
            rows = rep.fetch(sb, "daily_sku_kpi", SELECT, [("eq", "marketplace_code", "ozon"), ("gte", "kpi_date", "2026-10-01"),
                                                           ("lte", "kpi_date", "2026-10-02")], ORDERS)
        self.assertEqual(len(rows), 3000)
        self.assertEqual({r["kpi_date"] for r in rows}, {"2026-10-01", "2026-10-02"})
        for q in sb.calls:
            self.assertIn(("lte", "kpi_date", "2026-10-02"), q.filters)

    def test_anchor_column_rules(self):
        self.assertEqual(rep._anchor_column(SELECT, FILTERS, ORDERS), "kpi_date")
        self.assertIsNone(rep._anchor_column("id,marketplace_sku", FILTERS, ORDERS), "якоря нет в select — прежний OFFSET")
        self.assertIsNone(rep._anchor_column(SELECT, [("eq", "marketplace_code", "ozon")], ORDERS), "нет gte — прежний OFFSET")
        self.assertIsNone(rep._anchor_column("*", FILTERS, ORDERS))
        self.assertIsNone(rep._anchor_column("sku,name", [], ["sku"]))

    def test_without_anchor_offset_path_is_unchanged(self):
        sb_old, sb_new = FakeSb(make_rows([1200, 900, 1500])), None
        sb_new = FakeSb(sb_old.rows)
        flt = [("eq", "marketplace_code", "ozon")]
        with mock.patch("builtins.print"):
            old = old_fetch(sb_old, "daily_sku_kpi", "id,marketplace_sku", flt, ORDERS)
            new = rep.fetch(sb_new, "daily_sku_kpi", "id,marketplace_sku", flt, ORDERS)
        self.assertEqual(new, old)
        self.assertEqual([q.rng for q in sb_new.calls], [(0, 999), (1000, 1999), (2000, 2999), (3000, 3999)])


if __name__ == "__main__":
    unittest.main()
