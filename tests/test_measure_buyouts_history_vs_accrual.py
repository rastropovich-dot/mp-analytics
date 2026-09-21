"""Чистые функции измерителя истории выкупов: сравнение дня и запрет ночного окна."""
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from scripts.measure_buyouts_history_vs_accrual import compare_day, in_night_window


def _accrual(day, sku, sale_amount, sale_commission):
    return {"date": day, "posting": {"products": [{"sku": sku, "commission": {
        "sale_amount": {"amount": sale_amount, "currency": "RUB"},
        "sale_commission": {"amount": sale_commission, "currency": "RUB"}}, "delivery": {"services": []}}]}}


class CompareDayTests(unittest.TestCase):
    def test_equal_day_has_no_difference(self):
        accruals = [_accrual("2026-07-01", 111, "1000.00", "-420.00"), _accrual("2026-07-01", 222, "2000.00", "-840.00")]
        table = {"111": (Decimal(1), Decimal("1000.00")), "222": (Decimal(1), Decimal("2000.00"))}
        r = compare_day("2026-07-01", table, accruals)
        self.assertEqual(r["diff"], Decimal("0.00"))
        self.assertEqual((r["only_table"], r["only_src"], r["diff_amount"]), ([], [], []))

    def test_old_api_row_shows_as_amount_difference(self):
        """07-08: в таблице 5 944 937,00, в accrual 5 935 375,00 — одна строка с другой суммой."""
        accruals = [_accrual("2026-07-08", 111, "5935375.00", "-2542767.06")]
        table = {"111": (Decimal(1), Decimal("5944937.00"))}
        r = compare_day("2026-07-08", table, accruals)
        self.assertEqual(r["diff"], Decimal("9562.00"))
        self.assertEqual(r["diff_amount"], ["111"])

    def test_sku_only_on_one_side_is_listed(self):
        accruals = [_accrual("2026-07-01", 111, "1000.00", "-420.00")]
        table = {"111": (Decimal(1), Decimal("1000.00")), "999": (Decimal(1), Decimal("50.00"))}
        r = compare_day("2026-07-01", table, accruals)
        self.assertEqual(r["only_table"], ["999"])
        self.assertEqual(r["diff"], Decimal("50.00"))

    def test_zero_lines_are_skipped_like_the_nightly_builder(self):
        accruals = [_accrual("2026-07-01", 111, "0", "0"), _accrual("2026-07-01", 222, "10.00", "-4.00")]
        r = compare_day("2026-07-01", {"222": (Decimal(1), Decimal("10.00"))}, accruals)
        self.assertEqual(r["src_rows"], 1)
        self.assertEqual(r["diff"], Decimal("0.00"))


class ToleranceTests(unittest.TestCase):
    def test_float_rounding_is_not_a_difference(self):
        accruals = [_accrual("2026-07-01", 111, "5373634.00", "-1.00")]
        r = compare_day("2026-07-01", {"111": (Decimal(1), Decimal("5373633.99"))}, accruals)
        self.assertFalse(r["material"])
        self.assertEqual(r["diff_amount"], [])

    def test_zero_row_in_table_is_counted_separately(self):
        accruals = [_accrual("2026-07-01", 111, "10.00", "-4.00")]
        r = compare_day("2026-07-01", {"111": (Decimal(1), Decimal("10.00")), "999": (Decimal(0), Decimal("0.00"))}, accruals)
        self.assertFalse(r["material"])
        self.assertEqual(r["zero_table"], ["999"])


class NightWindowTests(unittest.TestCase):
    def test_inside_window_refused(self):
        self.assertTrue(in_night_window(datetime(2026, 9, 16, 0, 15, tzinfo=timezone.utc)))
        self.assertTrue(in_night_window(datetime(2026, 9, 16, 2, 0, tzinfo=timezone.utc)))
        self.assertTrue(in_night_window(datetime(2026, 9, 16, 3, 15, tzinfo=timezone.utc)))

    def test_outside_window_allowed(self):
        self.assertFalse(in_night_window(datetime(2026, 9, 16, 0, 14, tzinfo=timezone.utc)))
        self.assertFalse(in_night_window(datetime(2026, 9, 16, 3, 16, tzinfo=timezone.utc)))
        self.assertFalse(in_night_window(datetime(2026, 9, 15, 19, 0, tzinfo=timezone.utc)))


if __name__ == "__main__":
    unittest.main()
