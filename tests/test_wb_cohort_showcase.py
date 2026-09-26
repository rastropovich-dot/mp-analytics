"""WB-11 §3: правило showcase_rate для строк WB витрин и проба колонки buyout_rate_source."""
import unittest

from loaders import wb_buyout_cohort as cohort

DAILY = {"2026-09-01": {"mature": True, "rate": 0.2978, "forecast": None}, "2026-09-24": {"mature": False, "rate": None, "forecast": 0.4151}}
SKU = {("2026-09-01", "218330479"): 0.5, ("2026-09-01", "7"): None}


class ShowcaseRuleTests(unittest.TestCase):
    def test_marketplace_level(self):
        self.assertEqual(cohort.showcase_rate("2026-09-01", DAILY, 1.27), (0.2978, "cohort"))
        self.assertEqual(cohort.showcase_rate("2026-09-24", DAILY, 1.27), (0.4151, "cohort_forecast"))
        self.assertEqual(cohort.showcase_rate("2026-03-01", DAILY, 1.27), (1.27, "calendar"))
        self.assertEqual(cohort.showcase_rate("2026-09-01", {}, 1.27), (1.27, "calendar"))

    def test_sku_level(self):
        self.assertEqual(cohort.showcase_rate("2026-09-01", DAILY, 2.0, sku=SKU, nm=218330479), (0.5, "cohort"))
        self.assertEqual(cohort.showcase_rate("2026-09-01", DAILY, 2.0, sku=SKU, nm=7), (0, "cohort"))              # ключ без заказов в этот день
        self.assertEqual(cohort.showcase_rate("2026-09-01", DAILY, 2.0, sku=SKU, nm=99), (0, "cohort"))
        self.assertEqual(cohort.showcase_rate("2026-09-24", DAILY, 2.0, sku=SKU, nm=99), (0.4151, "cohort_forecast"))

    def test_table_has_column_probe(self):
        class Sb:
            def __init__(self, error=None): self.error = error
            def table(self, _n):
                sb = self
                class Q:
                    def select(self, *_a): return self
                    def limit(self, *_a): return self
                    def execute(self):
                        if sb.error: raise Exception(sb.error)
                        return type("R", (), {"data": []})()
                return Q()
        self.assertTrue(cohort.table_has_column(Sb(), "daily_marketplace_kpi", "buyout_rate_source"))
        self.assertFalse(cohort.table_has_column(Sb("{'code': 'PGRST204', 'message': \"Could not find the 'buyout_rate_source' column\"}"), "daily_marketplace_kpi", "buyout_rate_source"))
        self.assertFalse(cohort.table_has_column(Sb("column daily_marketplace_kpi.buyout_rate_source does not exist (42703)"), "daily_marketplace_kpi", "buyout_rate_source"))
        with self.assertRaises(Exception):
            cohort.table_has_column(Sb("statement timeout"), "daily_marketplace_kpi", "buyout_rate_source")


if __name__ == "__main__":
    unittest.main()
