"""Тип 127 AnalyticsPlus — прочее явной строкой (решение владельца 2026-10-06), не подписка и не unknown."""
import unittest

from loaders import ozon_finance_accrual as accrual


class AccrualType127(unittest.TestCase):
    def test_127_is_other_not_subscription(self):
        self.assertEqual(accrual.TYPE_TO_EXPENSE[127], "other")
        self.assertNotEqual(accrual.TYPE_TO_EXPENSE[127], "subscription")

    def test_127_row_goes_to_other_without_sku_and_is_not_unknown(self):
        accruals = [{
            "accrual_id": "2026-10-01-non-item-127", "date": "2026-10-01T00:00:00Z", "accrued_category": "NON_ITEM",
            "non_item_fee": {"fees": [{"type_id": 127, "accrued": {"amount": "-28000.00", "currency": "RUB"}}]},
        }]
        rows, counters, unknown = accrual.build_expense_rows(accruals, {127: "AnalyticsPlus"})
        self.assertEqual(unknown, {})
        self.assertNotIn("unknown_127", counters)
        other = [r for r in rows if r["expense_type"] == "other"]
        self.assertEqual(len(other), 1)
        self.assertEqual(other[0]["marketplace_sku"], "")
        self.assertEqual(round(other[0]["expense_amount"], 2), 28000.0)


if __name__ == "__main__":
    unittest.main()
