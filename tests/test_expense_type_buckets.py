"""Каждый тип расхода обязан быть прочитан, и прочитан там, где задумано.

Два дефекта одного класса:
  - призрак: тип advertising вычислялся и никем не читался;
  - молчаливое поглощение: else забирал любой незнакомый тип в прочие расходы,
    так туда вошли unknown_*, и витрина считала их, пока мы докладывали обратное.
"""
import unittest

import reports_daily_sku_kpi as kpi


class ExpenseBucketTests(unittest.TestCase):
    def test_every_known_type_has_a_bucket(self):
        for expense_type in ("commission", "logistics", "other", "subscription",
                             "external_promo", "advertising_clicks",
                             "advertising_order_5", "advertising_order_selected_cpo"):
            self.assertIn(expense_type, kpi.EXPENSE_TYPE_BUCKETS, expense_type)

    def test_external_promo_is_not_advertising(self):
        """Внешнее продвижение не должно попадать в рекламный расход."""
        self.assertEqual(kpi.EXPENSE_TYPE_BUCKETS["external_promo"], "other_expenses_amount")
        self.assertFalse("external_promo".startswith("advertising"),
                         "имя с префиксом advertising утянуло бы тип в рекламу и в органику")

    def test_subscription_is_not_advertising_either(self):
        self.assertEqual(kpi.EXPENSE_TYPE_BUCKETS["subscription"], "other_expenses_amount")
        self.assertFalse("subscription".startswith("advertising"))

    def test_advertising_types_go_to_ad_spend(self):
        for expense_type, bucket in kpi.EXPENSE_TYPE_BUCKETS.items():
            if expense_type.startswith("advertising"):
                self.assertEqual(bucket, "ad_spend", expense_type)

    def test_cpo_other_rates_are_advertising(self):
        """56-я: CPO «Все» со ставкой вне 5 % (с 20.10.2026 — 6 %) — трата рекламы, тип 54 её содержит.

        До 56-й advertising_order_other не было в списке, и 618 313,38 ₽ за 20 … 24.10 легли в прочее
        как незнакомый тип. Загрузчик пишет ровно эти три CPO-типа (classify_cpo_rate)."""
        from loaders import ozon_performance_ads_loader as loader
        for rate in ("5,00", "6,00", "10,00", "12,00"):
            expense_type = loader.classify_cpo_rate(rate)
            self.assertEqual(kpi.EXPENSE_TYPE_BUCKETS.get(expense_type), "ad_spend", f"{rate} -> {expense_type}")

    def test_buckets_are_real_columns(self):
        allowed = {"commission_amount", "logistics_amount", "ad_spend", "other_expenses_amount", "reviews_spend"}   # reviews_spend — 26.10
        for expense_type, bucket in kpi.EXPENSE_TYPE_BUCKETS.items():
            self.assertIn(bucket, allowed, f"{expense_type} -> {bucket}")

    def test_unknown_type_is_named_not_swallowed(self):
        """Незнакомый тип учитывается, но обязан быть назван."""
        source = kpi.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("unknown_expense_types", text)
        self.assertIn("НЕЗНАКОМЫЕ ТИПЫ РАСХОДА", text)


if __name__ == "__main__":
    unittest.main()
