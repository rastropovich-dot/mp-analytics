"""CPO за один день: пишутся только строки целевой даты, чужие даты отбрасываются тем же правилом, что ночью; сводка по типам. Сети нет."""
import os
import sys
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ozon_cpo_one_day as cpo  # noqa: E402


def row(day, sku, kind, amount):
    return {"expense_date": day, "marketplace_code": "ozon", "marketplace_sku": sku, "expense_type": kind, "expense_amount": amount}


class DayFilter(unittest.TestCase):
    def test_only_target_day_survives(self):
        rows = [row("2026-09-14", "1", "advertising_order_5", 10.0), row("2026-09-13", "2", "advertising_order_5", 5.0)]
        attr = [{"sale_date": "2026-09-14", "marketplace_sku": "1"}, {"sale_date": "2026-09-15", "marketplace_sku": "3"}]
        kept, kept_attr, dropped, dropped_attr = cpo.select_day_rows(rows, attr, "2026-09-14")
        self.assertEqual([r["marketplace_sku"] for r in kept], ["1"])
        self.assertEqual([r["marketplace_sku"] for r in kept_attr], ["1"])
        self.assertEqual((dropped, dropped_attr), (1, 1))

    def test_by_type_sums(self):
        t = cpo.by_type([row("d", "1", "advertising_order_5", 10.5), row("d", "2", "advertising_order_5", 4.5), row("d", "3", "advertising_order_selected_cpo", 1)])
        self.assertEqual(t["advertising_order_5"], [2, Decimal("15.0")])
        self.assertEqual(t["advertising_order_selected_cpo"], [1, Decimal("1")])


class Window(unittest.TestCase):
    def test_window_days_and_limit(self):
        self.assertEqual(cpo.window_days("2026-10-21", "2026-10-22"), ["2026-10-21", "2026-10-22"])
        self.assertEqual(len(cpo.window_days("2026-08-23", "2026-10-22")), 61)
        with self.assertRaises(SystemExit):
            cpo.window_days("2026-08-01", "2026-10-22")
        with self.assertRaises(SystemExit):
            cpo.window_days("2026-10-22", "2026-10-21")

    def test_only_window_days_survive(self):
        rows = [row("2026-10-22", "1", "advertising_order_5", 1.0), row("2026-10-23", "2", "advertising_order_5", 2.0)]
        kept, _attr, dropped, _ = cpo.select_window_rows(rows, [], ["2026-10-21", "2026-10-22"])
        self.assertEqual([r["marketplace_sku"] for r in kept], ["1"])
        self.assertEqual(dropped, 1)

    def test_by_day_type_to_cent(self):
        t = cpo.by_day_type([row("d", "1", "advertising_order_5", 9674.919999999993), row("d", "2", "advertising_order_5", 0.1)])
        self.assertEqual(t[("d", "advertising_order_5")], Decimal("9675.02"))

    def test_stale_keys_are_those_missing_from_file(self):
        existing = {("2026-09-01", "1", "advertising_order_5"), ("2026-09-01", "9", "advertising_order_5")}
        self.assertEqual(cpo.stale_cpo_keys(existing, [row("2026-09-01", 1, "advertising_order_5", 1)]), [("2026-09-01", "9", "advertising_order_5")])

    def test_window_args(self):
        with self.assertRaises(SystemExit):
            cpo.main(["--date-from", "2026-10-01"])
        with self.assertRaises(SystemExit):
            cpo.main(["--date-from", "2026-10-01", "--date-to", "2026-10-02", "--fetch"])
        with self.assertRaises(SystemExit):
            cpo.main(["--date-from", "2026-10-01", "--date-to", "2026-10-02", "--from-file", "x.csv", "--write"])


class AggregateCents(unittest.TestCase):
    def test_aggregate_rounds_to_cent(self):
        from loaders import ozon_performance_ads_loader as ads
        out = ads.aggregate_rows([row("d", "1", "advertising_order_5", 9674.9), row("d", "1", "advertising_order_5", 0.019999999993)])
        self.assertEqual(out[0]["expense_amount"], 9674.92)
        self.assertEqual(repr(out[0]["expense_amount"]), "9674.92")


if __name__ == "__main__":
    unittest.main()
