"""Selected CPO за один день: строки чужих дат отбрасываются и считаются; сводка по типам на Decimal; --write без --fetch и слова отвергается. Сети нет."""
import os
import sys
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ozon_selected_cpo_one_day as sel  # noqa: E402


class DayFilter(unittest.TestCase):
    def test_only_target_day_survives_and_foreign_counted(self):
        rows = [{"sale_date": "2026-09-14", "spend": 1}, {"sale_date": "2026-09-13", "spend": 2}, {"sale_date": "2026-09-14T00:00:00", "spend": 3}]
        kept, foreign = sel.split_by_day(rows, "2026-09-14", "sale_date")
        self.assertEqual([r["spend"] for r in kept], [1, 3])
        self.assertEqual(foreign, 1)

    def test_by_type_sums_decimal(self):
        t = sel.by_type([{"expense_type": "a", "expense_amount": 10.5}, {"expense_type": "a", "expense_amount": "4.5"}, {"expense_type": "b", "expense_amount": None}])
        self.assertEqual(t["a"], [2, Decimal("15.0")])
        self.assertEqual(t["b"], [1, Decimal("0")])

    def test_write_requires_fetch_and_word(self):
        with self.assertRaises(SystemExit):
            sel.main(["--date", "2026-09-14", "--write"])
        with self.assertRaises(SystemExit):
            sel.main(["--date", "2026-09-14", "--write", "--fetch"])


if __name__ == "__main__":
    unittest.main()
