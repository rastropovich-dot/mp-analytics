"""Типы начислений кабинетов РБХ (сухие прогоны 2026-10-15, классификация советника 2026-10-21): семь типов, которых
у KARATOV не было, лежат в TYPE_TO_EXPENSE явными строками — иначе ночь РБХ писала бы их как unknown_* в «прочее»."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "loaders"))
import ozon_finance_accrual as accrual  # noqa: E402

RBH_TYPES = {12: "logistics", 30: "logistics", 19: "external_promo", 95: "reviews", 50: "other", 84: "other", 116: "other"}


class RbhAccrualTypesTests(unittest.TestCase):
    def test_seven_rbh_types_have_the_advisor_articles(self):
        for type_id, article in RBH_TYPES.items():
            self.assertEqual(accrual.TYPE_TO_EXPENSE.get(type_id), article, type_id)

    def test_no_rbh_type_leaks_into_advertising_by_prefix(self):
        for type_id in RBH_TYPES:
            self.assertFalse(accrual.TYPE_TO_EXPENSE[type_id].startswith("advertising"), type_id)
            self.assertNotIn(type_id, accrual.AD_TYPE_IDS)

    def test_map_grew_by_exactly_seven_over_the_karatov_set(self):
        self.assertEqual(len(accrual.TYPE_TO_EXPENSE), 41 + 7)


if __name__ == "__main__":
    unittest.main()
