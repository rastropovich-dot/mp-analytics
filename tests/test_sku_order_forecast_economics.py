import unittest
from unittest import mock

import reports_sku_order_forecast_economics as forecast


class SkuOrderForecastEconomicsTests(unittest.TestCase):
    def test_selected_cpo_not_loaded_unknown_when_no_source_or_downstream(self):
        coverage = forecast.classify_selected_cpo_coverage("2026-05-12", "1300079194", [], [], [])
        self.assertEqual(coverage["selected_cpo_status"], "not_loaded_unknown")
        self.assertEqual(coverage["selected_cpo_downstream_spend"], 0.0)
        self.assertEqual(coverage["selected_cpo_warning"], "selected_cpo_not_loaded_may_understate_ad_spend")

    def test_selected_cpo_confirmed_present_when_source_and_downstream_match(self):
        coverage = forecast.classify_selected_cpo_coverage(
            "2026-05-16",
            "1300079194",
            [
                {"sale_date": "2026-05-16", "ordered_sku": "1300079194", "promoted_sku": "1300079194", "spend": 22047.30},
                {"sale_date": "2026-05-16", "ordered_sku": "1300079194", "promoted_sku": "1300079194", "spend": 0.0},
            ],
            [{"expense_date": "2026-05-16", "expense_amount": 22047.30}],
            [{"sale_date": "2026-05-16", "ad_source": "cpo_selected_products", "ad_spend": 22047.30}],
        )
        self.assertEqual(coverage["selected_cpo_status"], "confirmed_present")
        self.assertEqual(coverage["selected_cpo_ordered_sku_source_spend"], 22047.30)
        self.assertEqual(coverage["selected_cpo_downstream_spend"], 22047.30)
        self.assertIsNone(coverage["selected_cpo_warning"])

    def test_selected_cpo_confirmed_zero_when_source_date_exists_but_sku_absent(self):
        coverage = forecast.classify_selected_cpo_coverage(
            "2026-05-15",
            "1300079194",
            [{"sale_date": "2026-05-15", "ordered_sku": "999", "promoted_sku": "999", "spend": 1000}],
            [],
            [],
        )
        self.assertEqual(coverage["selected_cpo_status"], "confirmed_zero")
        self.assertEqual(coverage["selected_cpo_ordered_sku_source_spend"], 0.0)

    def test_selected_cpo_inconsistent_when_source_and_downstream_differ(self):
        coverage = forecast.classify_selected_cpo_coverage(
            "2026-05-16",
            "1300079194",
            [{"sale_date": "2026-05-16", "ordered_sku": "1300079194", "promoted_sku": "1300079194", "spend": 22047.30}],
            [{"expense_date": "2026-05-16", "expense_amount": 100.0}],
            [{"sale_date": "2026-05-16", "ad_source": "cpo_selected_products", "ad_spend": 100.0}],
        )
        self.assertEqual(coverage["selected_cpo_status"], "inconsistent")
        self.assertEqual(coverage["selected_cpo_warning"], "selected_cpo_source_downstream_mismatch")

    def test_expected_fin_result_subtracts_full_ad_spend(self):
        row = forecast.build_forecast_row(
            {
                "date": "2026-05-16",
                "marketplace_code": "ozon",
                "marketplace_sku": "1300079194",
                "orders_qty": 2,
                "orders_revenue": 200000,
                "ad_orders_revenue": 100000,
                "organic_orders_revenue": 100000,
                "cpc_spend": 3000,
                "cpo_all_spend": 0,
                "selected_cpo_spend": 7000,
                "total_ad_spend": 10000,
            },
            selected_rate={"rate_qty": 0.5, "rate_amount": 0.5, "source": "sku_7d", "sample_orders_qty": 10, "confidence": "high"},
            cost_assumptions={
                "commission_rate": 0.1,
                "commission_rate_source": "x",
                "acquiring_rate": 0.0,
                "acquiring_rate_source": "missing",
                "logistics_per_unit": 100,
                "logistics_rate_source": "x",
                "other_rate": 0.05,
                "other_rate_source": "x",
                "assumption_flags": ["acquiring_rate_assumed_zero"],
            },
            unit_cost=10000,
            cogs_source="manual",
        )
        self.assertAlmostEqual(row["expected_buyouts_revenue"], 100000)
        self.assertAlmostEqual(row["expected_gross_margin"], 74900)
        self.assertAlmostEqual(row["expected_fin_result"], 64900)

    def test_expected_buyout_rate_applies_to_orders_revenue_and_qty(self):
        row = forecast.build_forecast_row(
            {
                "date": "2026-05-16",
                "marketplace_code": "ozon",
                "marketplace_sku": "1",
                "orders_qty": 4,
                "orders_revenue": 400000,
                "ad_orders_revenue": 100000,
                "organic_orders_revenue": 300000,
                "cpc_spend": 1000,
                "cpo_all_spend": 0,
                "selected_cpo_spend": 0,
                "total_ad_spend": 1000,
            },
            selected_rate={"rate_qty": 0.25, "rate_amount": 0.6, "source": "sku_7d", "sample_orders_qty": 10, "confidence": "high"},
            cost_assumptions={"commission_rate": 0, "commission_rate_source": "x", "acquiring_rate": 0, "acquiring_rate_source": "x", "logistics_per_unit": 0, "logistics_rate_source": "x", "other_rate": 0, "other_rate_source": "x", "assumption_flags": []},
            unit_cost=10000,
            cogs_source="manual",
        )
        self.assertAlmostEqual(row["expected_buyouts_qty"], 1.0)
        self.assertAlmostEqual(row["expected_buyouts_revenue"], 240000.0)

    def test_cpc_acos_is_cpc_spend_over_ad_revenue(self):
        row = forecast.build_forecast_row(
            {
                "date": "2026-05-16",
                "marketplace_code": "ozon",
                "marketplace_sku": "1",
                "orders_qty": 1,
                "orders_revenue": 100000,
                "ad_orders_revenue": 50000,
                "organic_orders_revenue": 50000,
                "cpc_spend": 5000,
                "cpo_all_spend": 0,
                "selected_cpo_spend": 0,
                "total_ad_spend": 5000,
            },
            selected_rate={"rate_qty": 1, "rate_amount": 1, "source": "sku_7d", "sample_orders_qty": 10, "confidence": "high"},
            cost_assumptions={"commission_rate": 0, "commission_rate_source": "x", "acquiring_rate": 0, "acquiring_rate_source": "x", "logistics_per_unit": 0, "logistics_rate_source": "x", "other_rate": 0, "other_rate_source": "x", "assumption_flags": []},
            unit_cost=0,
            cogs_source="manual",
        )
        self.assertAlmostEqual(row["cpc_acos"], 0.1)

    def test_total_order_tacos_is_total_ad_spend_over_all_orders_revenue(self):
        row = forecast.build_forecast_row(
            {
                "date": "2026-05-16",
                "marketplace_code": "ozon",
                "marketplace_sku": "1",
                "orders_qty": 1,
                "orders_revenue": 100000,
                "ad_orders_revenue": 40000,
                "organic_orders_revenue": 60000,
                "cpc_spend": 2000,
                "cpo_all_spend": 1000,
                "selected_cpo_spend": 3000,
                "total_ad_spend": 6000,
            },
            selected_rate={"rate_qty": 1, "rate_amount": 1, "source": "sku_7d", "sample_orders_qty": 10, "confidence": "high"},
            cost_assumptions={"commission_rate": 0, "commission_rate_source": "x", "acquiring_rate": 0, "acquiring_rate_source": "x", "logistics_per_unit": 0, "logistics_rate_source": "x", "other_rate": 0, "other_rate_source": "x", "assumption_flags": []},
            unit_cost=0,
            cogs_source="manual",
        )
        self.assertAlmostEqual(row["total_order_tacos"], 0.06)

    def test_headroom_respects_target_profit_amount(self):
        row = forecast.build_forecast_row(
            {
                "date": "2026-05-16",
                "marketplace_code": "ozon",
                "marketplace_sku": "1",
                "orders_qty": 1,
                "orders_revenue": 100000,
                "ad_orders_revenue": 50000,
                "organic_orders_revenue": 50000,
                "cpc_spend": 1000,
                "cpo_all_spend": 0,
                "selected_cpo_spend": 0,
                "total_ad_spend": 1000,
            },
            selected_rate={"rate_qty": 1, "rate_amount": 1, "source": "sku_7d", "sample_orders_qty": 10, "confidence": "high"},
            cost_assumptions={"commission_rate": 0, "commission_rate_source": "x", "acquiring_rate": 0, "acquiring_rate_source": "x", "logistics_per_unit": 0, "logistics_rate_source": "x", "other_rate": 0, "other_rate_source": "x", "assumption_flags": []},
            unit_cost=0,
            cogs_source="manual",
            target_profit_amount=5000,
        )
        self.assertAlmostEqual(row["max_affordable_ad_spend"], 95000)
        self.assertAlmostEqual(row["ad_spend_headroom"], 94000)

    def test_dry_run_report_declares_zero_writes(self):
        with mock.patch.object(forecast, "load_article_unit_costs", return_value=({}, "article_unit_costs_table_missing")):
            report = forecast.build_report(
                marketplace_code="ozon",
                sku="1300079194",
                date_from="2026-05-16",
                date_to="2026-05-16",
                unit_cost_override=32963,
                kpi_rows=[
                    {
                        "report_date": "2026-05-16",
                        "marketplace_code": "ozon",
                        "marketplace_sku": "1300079194",
                        "article": "F000283615",
                        "product_name": "Item",
                        "orders_qty": 2,
                        "orders_amount_seller": 221646,
                        "buyouts_qty": 2,
                        "buyouts_amount_seller": 223513,
                        "commission_amount": 93875.46,
                        "logistics_amount": 0,
                        "other_expenses_amount": 2132.69,
                        "ad_orders_revenue": 110823,
                        "organic_orders_revenue": 221646,
                    }
                ],
                organic_rows=[
                    {
                        "sale_date": "2026-05-16",
                        "ad_orders_qty": 1,
                        "organic_orders_qty": 2,
                    }
                ],
                attribution_rows=[],
                decision_rows=[],
            )
        self.assertEqual(report["db_writes"], 0)
        self.assertFalse(report["migration_applied"])

    def test_missing_acquiring_rate_is_surfaced_as_assumption(self):
        row = forecast.build_forecast_row(
            {
                "date": "2026-05-16",
                "marketplace_code": "ozon",
                "marketplace_sku": "1",
                "orders_qty": 1,
                "orders_revenue": 100000,
                "ad_orders_revenue": 50000,
                "organic_orders_revenue": 50000,
                "cpc_spend": 1000,
                "cpo_all_spend": 0,
                "selected_cpo_spend": 0,
                "total_ad_spend": 1000,
            },
            selected_rate={"rate_qty": 1, "rate_amount": 1, "source": "sku_7d", "sample_orders_qty": 10, "confidence": "high"},
            cost_assumptions={
                "commission_rate": 0.1,
                "commission_rate_source": "x",
                "acquiring_rate": 0.0,
                "acquiring_rate_source": "assumed_zero_missing_explicit_layer",
                "logistics_per_unit": 0,
                "logistics_rate_source": "x",
                "other_rate": 0,
                "other_rate_source": "x",
                "assumption_flags": ["acquiring_rate_assumed_zero"],
            },
            unit_cost=0,
            cogs_source="manual",
        )
        self.assertIn("acquiring_rate_assumed_zero", row["assumption_flags"])
        self.assertIn("acquiring_rate_zero_assumption", row["assumption_flags"])

    def test_forecast_report_surfaces_selected_cpo_unknown_warning(self):
        with mock.patch.object(forecast, "load_article_unit_costs", return_value=({}, "article_unit_costs_table_missing")):
            report = forecast.build_report(
                marketplace_code="ozon",
                sku="1300079194",
                date_from="2026-05-16",
                date_to="2026-05-16",
                unit_cost_override=32963,
                kpi_rows=[
                    {
                        "report_date": "2026-05-16",
                        "marketplace_code": "ozon",
                        "marketplace_sku": "1300079194",
                        "article": "F000283615",
                        "product_name": "Item",
                        "orders_qty": 2,
                        "orders_amount_seller": 221646,
                        "buyouts_qty": 2,
                        "buyouts_amount_seller": 223513,
                        "commission_amount": 93875.46,
                        "logistics_amount": 0,
                        "other_expenses_amount": 2132.69,
                        "ad_orders_revenue": 110823,
                        "organic_orders_revenue": 221646,
                    }
                ],
                organic_rows=[
                    {
                        "sale_date": "2026-05-16",
                        "ad_orders_qty": 1,
                        "organic_orders_qty": 2,
                    }
                ],
                attribution_rows=[],
                decision_rows=[],
                selected_cpo_source_rows=[],
                selected_cpo_expense_rows=[],
            )
        self.assertEqual(report["rows"][0]["selected_cpo_status"], "not_loaded_unknown")
        self.assertEqual(report["rows"][0]["selected_cpo_warning"], "selected_cpo_not_loaded_may_understate_ad_spend")
        self.assertEqual(report["rows"][0]["expected_fin_result_confidence"], "lower")


if __name__ == "__main__":
    unittest.main()
