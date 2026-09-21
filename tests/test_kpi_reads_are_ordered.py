"""Чтения витрины KPI: каждый постраничный запрос уходит с сортировкой по уникальному ключу.

range() без order() у PostgREST не обещает порядка: страницы приходят с повторами и
пропусками (how-we-work, 15 сентября). Подделка ниже воспроизводит именно это — запрос
без order получает строки в порядке, зависящем от смещения, — поэтому тест падает на
прежнем коде, а не только проверяет слово «order» в запросе.
"""
import unittest
from unittest import mock

import reports_daily_sku_kpi as kpi


class FakeQuery:
    def __init__(self, client, table):
        self.client, self.table = client, table
        self.orders, self.filters, self.window, self.cap = [], [], None, None

    def select(self, *_a, **_kw):
        return self

    def order(self, column, **_kw):
        self.orders.append(column)
        return self

    def gt(self, column, value):
        self.filters.append((column, value))
        return self

    def limit(self, n):
        self.cap = n
        return self

    def range(self, start, end):
        self.window = (start, end)
        return self

    def execute(self):
        self.client.requests.append({"table": self.table, "orders": list(self.orders), "filters": list(self.filters), "window": self.window})
        if self.table in self.client.broken:
            raise Exception("relation does not exist")
        rows = [r for r in self.client.tables[self.table] if all(r[c] > v for c, v in self.filters)]
        if self.orders:
            rows.sort(key=lambda r: tuple(r[c] for c in self.orders))
        elif self.window:
            shift = (self.window[0] // 1000) * 7 % max(len(rows), 1)      # без сортировки порядок «плывёт» от страницы к странице
            rows = rows[shift:] + rows[:shift]
        if self.window:
            rows = rows[self.window[0]:self.window[1] + 1]
        if self.cap is not None:
            rows = rows[:self.cap]
        if self.table in self.client.overlapping and self.client.requests[-1]["filters"]:
            rows = self.client.tables[self.table][:1] + rows                 # сломанный бэкенд: страница с чужой строкой
        return type("R", (), {"data": rows})()


class FakeClient:
    def __init__(self, tables):
        self.tables, self.requests, self.broken, self.overlapping = tables, [], set(), set()

    def table(self, name):
        return FakeQuery(self, name)


def table_with_id(n):
    ids = list(range(1, n + 1))
    ids = ids[::2] + ids[1::2]            # физический порядок не совпадает с порядком id
    return [{"id": i, "value": i} for i in ids]


class OrderedReadsTests(unittest.TestCase):
    def setUp(self):
        self.organic = [{"sale_date": f"2026-05-{d:02d}", "marketplace_code": "ozon", "marketplace_sku": str(s)} for d in range(1, 22) for s in range(120)]
        self.client = FakeClient({"marketplace_orders": table_with_id(2500), "marketplace_buyouts": table_with_id(1000),
                                  "marketplace_expenses": table_with_id(3001), "ozon_daily_sku_organic": self.organic[::-1]})
        patcher = mock.patch.object(kpi, "supabase", self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_request_of_all_four_reads_is_ordered(self):
        kpi.load_orders(); kpi.load_buyouts(); kpi.load_expenses(); kpi.load_ozon_organic()
        self.assertEqual({r["table"] for r in self.client.requests},
                         {"marketplace_orders", "marketplace_buyouts", "marketplace_expenses", "ozon_daily_sku_organic"})
        for r in self.client.requests:
            self.assertTrue(r["orders"], f"запрос без order: {r}")

    def test_tables_with_id_are_read_whole_by_key_not_by_offset(self):
        for loader, table, n in ((kpi.load_orders, "marketplace_orders", 2500), (kpi.load_buyouts, "marketplace_buyouts", 1000),
                                 (kpi.load_expenses, "marketplace_expenses", 3001)):
            rows = loader()
            self.assertEqual(sorted(r["id"] for r in rows), list(range(1, n + 1)), table)
            mine = [r for r in self.client.requests if r["table"] == table]
            self.assertTrue(all(r["orders"] == ["id"] and r["window"] is None for r in mine), table)
            self.assertEqual([r["filters"] for r in mine][1:], [[("id", 1000 * k)] for k in range(1, len(mine))], table)

    def test_exact_page_multiple_makes_one_extra_empty_request_and_stops(self):
        kpi.load_buyouts()
        self.assertEqual(len([r for r in self.client.requests if r["table"] == "marketplace_buyouts"]), 2)

    def test_organic_has_no_id_and_is_ordered_by_its_full_primary_key(self):
        rows = kpi.load_ozon_organic()
        self.assertEqual(len(rows), len(self.organic))
        self.assertEqual(len({(r["sale_date"], r["marketplace_sku"]) for r in rows}), len(self.organic))
        for r in self.client.requests:
            self.assertEqual(r["orders"], ["sale_date", "marketplace_code", "marketplace_sku"])

    def test_missing_organic_table_still_degrades_to_empty(self):
        self.client.broken.add("ozon_daily_sku_organic")
        self.assertEqual(kpi.load_ozon_organic(), [])

    def test_duplicate_rows_fail_loudly_instead_of_feeding_the_showcase(self):
        self.client.overlapping.add("marketplace_orders")
        with self.assertRaises(RuntimeError):
            kpi.load_orders()


if __name__ == "__main__":
    unittest.main()
