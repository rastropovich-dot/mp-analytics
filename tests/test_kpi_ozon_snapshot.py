"""Сравнение снимков витрины: даты до начала окна ночи обязаны совпасть, даты окна — печатаются, но не считаются расхождением."""
import importlib.util
import os
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("kpi_ozon_snapshot", os.path.join(ROOT, "scripts", "kpi_ozon_snapshot.py"))
snap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(snap)


class Compare(unittest.TestCase):
    def test_split_by_window_start(self):
        old = {"by_date": {"2026-08-01": {"rows": "2", "buyouts_amount_seller": "10"}, "2026-09-01": {"rows": "1", "buyouts_amount_seller": "5"}}}
        new = {"by_date": {"2026-08-01": {"rows": "2", "buyouts_amount_seller": "11"}, "2026-09-01": {"rows": "1", "buyouts_amount_seller": "7"}}}
        diffs, night = snap.compare(old, new, "2026-08-24")
        self.assertEqual(diffs, [("2026-08-01", "buyouts_amount_seller", Decimal(10), Decimal(11))])
        self.assertEqual(night, [("2026-09-01", "buyouts_amount_seller", Decimal(5), Decimal(7))])

    def test_missing_date_counts_as_zero(self):
        diffs, _ = snap.compare({"by_date": {"2026-08-01": {"rows": "2"}}}, {"by_date": {}}, "2026-08-24")
        self.assertEqual(diffs, [("2026-08-01", "rows", Decimal(2), Decimal(0))])


if __name__ == "__main__":
    unittest.main()
