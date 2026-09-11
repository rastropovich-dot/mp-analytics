"""Две строки услуги одного типа внутри одного SKU должны СУММИРОВАТЬСЯ.

Ozon 2026-09-10, дословно: одинаковых SKU внутри начисления не бывает, а две
строки с одним type_id внутри SKU «теоретически» возможны — «мы сейчас не
отдаём, но явного запрета нет».

Замещение вместо суммирования — тот же дефект, что испортил пробу 2026-08-12:
строка, собранная по части данных, заместила целую, и 6 813,59 ₽ исчезли молча.
"""
import unittest

from loaders import ozon_finance_accrual as accrual


def make_accrual(services, sku=777, accrual_id=1):
    return {
        "accrual_id": accrual_id,
        "date": "2026-09-10",
        "posting": {"products": [{
            "sku": sku,
            "delivery": {"services": [
                {"type_id": t, "accrued": {"amount": str(a), "currency": "RUB"}}
                for t, a in services
            ]},
            "commission": None,
        }]},
    }


class DuplicateServiceLineTests(unittest.TestCase):
    def test_two_lines_of_the_same_type_are_summed(self):
        rows, counters, _ = accrual.build_expense_rows(
            [make_accrual([(1, -100), (1, -50)])]
        )
        amounts = [r["expense_amount"] for r in rows if r["expense_type"] == "other"]
        self.assertEqual(amounts, [150.0], "вторая строка заместила первую вместо суммирования")

    def test_duplicate_is_counted(self):
        _, counters, _ = accrual.build_expense_rows(
            [make_accrual([(1, -100), (1, -50)])]
        )
        self.assertEqual(counters.get("duplicate_service_line"), 1)

    def test_distinct_types_are_not_counted_as_duplicates(self):
        _, counters, _ = accrual.build_expense_rows(
            [make_accrual([(1, -100), (46, -50)])]
        )
        self.assertNotIn("duplicate_service_line", counters)

    def test_unknown_type_also_sums(self):
        rows, _, unknown = accrual.build_expense_rows(
            [make_accrual([(999, -10), (999, -15)])]
        )
        amounts = [r["expense_amount"] for r in rows if r["expense_type"] == "unknown_999"]
        self.assertEqual(amounts, [25.0])
        self.assertEqual(unknown.get(999), 25.0)

    def test_sign_is_kept_for_refunds(self):
        rows, _, _ = accrual.build_expense_rows(
            [make_accrual([(1, -100), (1, 30)])]
        )
        amounts = [r["expense_amount"] for r in rows if r["expense_type"] == "other"]
        self.assertEqual(amounts, [70.0], "возврат обязан уменьшать расход, а не прибавляться")


if __name__ == "__main__":
    unittest.main()
