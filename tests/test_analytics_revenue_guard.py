"""Сторож шага «Ozon: total orders analytics по SKU»: revenue аналитики против созданных заказов за те же даты — только печать.

2026-09-24: на 21 дне из 26 отношение ровно 1,0000, а за 09-22 аналитика Ozon отдала 11 279 201 против 8 074 089 созданных
(отчёты ЛК подтвердили наши 8 074 089 до рубля) — без строки в логе это было невидимо. Порог тревоги — решение советника.
"""
import unittest
from decimal import Decimal
from unittest import mock

import loaders.ozon_sku_total_analytics_loader as seller


class FakeQuery:
    def __init__(self, rows):
        self.rows, self.rng = rows, (0, 999)

    def __getattr__(self, name):
        return lambda *a, **k: self

    def range(self, a, b):
        self.rng = (a, b); return self

    def execute(self):
        a, b = self.rng
        return mock.Mock(data=self.rows[a:b + 1])


class FakeClient:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        assert name == "marketplace_orders"
        return FakeQuery(self.rows)


class CreatedOrdersTotals(unittest.TestCase):
    def test_confirmed_and_cancelled_are_summed_and_pages_are_walked(self):
        rows = [{"orders_qty": 1, "orders_amount_seller": "1000", "cancelled_orders_qty": 1, "cancelled_orders_amount_seller": "500"}] * 1000 \
             + [{"orders_qty": 2, "orders_amount_seller": "300", "cancelled_orders_qty": None, "cancelled_orders_amount_seller": None}]
        amount, qty, n = seller.created_orders_totals("2026-09-22", "2026-09-22", client=FakeClient(rows))
        self.assertEqual((amount, qty, n), (Decimal("1500300"), Decimal("2002"), 1001))

    def test_empty_table_gives_zero_rows(self):
        self.assertEqual(seller.created_orders_totals("2026-09-22", "2026-09-22", client=FakeClient([])), (Decimal(0), Decimal(0), 0))


class GuardLine(unittest.TestCase):
    def test_ratio_is_printed_for_money_and_units(self):
        line = seller.revenue_guard_line(11279201.0, 544.0, Decimal("8074089"), Decimal("410"), 331)
        self.assertIn("отношение по деньгам 1.3970", line)
        self.assertIn("по штукам 1.3268", line)
        self.assertIn("11,279,201.00", line)

    def test_exact_day_reads_one(self):
        line = seller.revenue_guard_line(6966967.0, 352.0, Decimal("6966967"), Decimal("352"), 308)
        self.assertIn("отношение по деньгам 1.0000, по штукам 1.0000", line)

    def test_no_orders_is_said_not_divided(self):
        line = seller.revenue_guard_line(100.0, 1.0, Decimal(0), Decimal(0), 0)
        self.assertIn("сравнить не с чем", line)
        self.assertNotIn("отношение по деньгам", line)


if __name__ == "__main__":
    unittest.main()
