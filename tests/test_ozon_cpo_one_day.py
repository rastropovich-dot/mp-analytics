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


if __name__ == "__main__":
    unittest.main()
