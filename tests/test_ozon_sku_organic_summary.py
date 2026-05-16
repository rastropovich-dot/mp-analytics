import unittest
from unittest import mock

import reports_ozon_sku_organic as organic


class OzonSkuOrganicSummaryTests(unittest.TestCase):
    def test_reconciliation_breakdown_explains_gap(self):
        rows = [
            {
                "sale_date": "2026-05-12",
                "marketplace_sku": "sku-missing-1",
                "total_orders_qty": 0,
                "total_orders_revenue": 0,
                "ad_orders_qty": 20,
                "ad_orders_revenue": 200000,
                "organic_orders_qty": 0,
                "organic_orders_revenue": 0,
                "calculation_status": "missing_total",
                "warning": "ad_attribution_without_total",
            },
            {
                "sale_date": "2026-05-12",
                "marketplace_sku": "sku-missing-2",
                "total_orders_qty": 0,
                "total_orders_revenue": 0,
                "ad_orders_qty": 10,
                "ad_orders_revenue": 111705,
                "organic_orders_qty": 0,
                "organic_orders_revenue": 0,
                "calculation_status": "missing_total",
                "warning": "ad_attribution_without_total",
            },
            {
                "sale_date": "2026-05-12",
                "marketplace_sku": "sku-zero-total-1",
                "total_orders_qty": 0,
                "total_orders_revenue": 0,
                "ad_orders_qty": 1,
                "ad_orders_revenue": 10566,
                "organic_orders_qty": 0,
                "organic_orders_revenue": 0,
                "calculation_status": "missing_total",
                "warning": "zero_total_with_ad_attribution",
            },
            {
                "sale_date": "2026-05-12",
                "marketplace_sku": "sku-zero-total-2",
                "total_orders_qty": 0,
                "total_orders_revenue": 0,
                "ad_orders_qty": 2,
                "ad_orders_revenue": 15530,
                "organic_orders_qty": 0,
                "organic_orders_revenue": 0,
                "calculation_status": "missing_total",
                "warning": "zero_total_with_ad_attribution",
            },
            {
                "sale_date": "2026-05-12",
                "marketplace_sku": "sku-excess-1",
                "total_orders_qty": 1,
                "total_orders_revenue": 100000,
                "ad_orders_qty": 4,
                "ad_orders_revenue": 126536,
                "organic_orders_qty": 0,
                "organic_orders_revenue": 0,
                "calculation_status": "ok",
                "warning": "ad_orders_exceed_total,ad_revenue_exceed_total",
            },
            {
                "sale_date": "2026-05-12",
                "marketplace_sku": "sku-reconciled",
                "total_orders_qty": 338,
                "total_orders_revenue": 7523052,
                "ad_orders_qty": 164,
                "ad_orders_revenue": 4220315,
                "organic_orders_qty": 174,
                "organic_orders_revenue": 3302737,
                "calculation_status": "ok",
                "warning": None,
            },
        ]

        breakdown = organic.build_reconciliation_breakdown(rows)

        self.assertEqual(breakdown["raw_gap_orders_qty"], 36.0)
        self.assertEqual(breakdown["raw_gap_orders_revenue"], 364337.0)
        self.assertEqual(breakdown["missing_total_rows_count"], 4)
        self.assertEqual(breakdown["missing_total_ad_orders_qty"], 33.0)
        self.assertEqual(breakdown["missing_total_ad_orders_revenue"], 337801.0)
        self.assertEqual(breakdown["ad_exceeds_total_rows_count"], 1)
        self.assertEqual(breakdown["ad_exceeds_total_orders_qty_excess"], 3.0)
        self.assertEqual(breakdown["ad_exceeds_total_revenue_excess"], 26536.0)
        self.assertEqual(breakdown["explained_gap_orders_qty"], 36.0)
        self.assertEqual(breakdown["explained_gap_orders_revenue"], 364337.0)
        self.assertEqual(breakdown["unexplained_gap_orders_qty"], 0.0)
        self.assertEqual(breakdown["unexplained_gap_orders_revenue"], 0.0)
        self.assertEqual(breakdown["reconciled_rows_count"], 1)

    def test_calculate_row_logic_is_unchanged_for_missing_total(self):
        row = organic.calculate_row(
            total_row=None,
            ad_row={"ad_orders_qty": 1, "ad_orders_revenue": 112863},
            ad_coverage_exists=True,
        )

        self.assertEqual(row["calculation_status"], "missing_total")
        self.assertEqual(row["organic_orders_qty"], 0)
        self.assertEqual(row["organic_orders_revenue"], 0)
        self.assertEqual(row["ad_orders_qty"], 1)
        self.assertEqual(row["ad_orders_revenue"], 112863)

    def test_zero_total_with_ad_attribution_is_not_ok(self):
        row = organic.calculate_row(
            total_row={"total_orders_qty": 0, "total_orders_revenue": 0},
            ad_row={"ad_orders_qty": 2, "ad_orders_revenue": 15530},
            ad_coverage_exists=True,
        )

        self.assertEqual(row["calculation_status"], "missing_total")
        self.assertEqual(row["warning"], "zero_total_with_ad_attribution")
        self.assertEqual(row["organic_orders_qty"], 0)
        self.assertEqual(row["organic_orders_revenue"], 0)
        self.assertEqual(row["ad_orders_qty"], 2)
        self.assertEqual(row["ad_orders_revenue"], 15530)

    def test_load_ad_attribution_keeps_direct_rows_without_filtering_ad_source(self):
        rows = [
            {
                "sale_date": "2026-05-12",
                "marketplace_code": "ozon",
                "marketplace_sku": "1300079194",
                "article": "F000283615",
                "product_name": "Item",
                "ad_source": "cpc",
                "attribution_type": "direct",
                "ad_orders_qty": 1,
                "ad_orders_revenue": 112863,
            },
            {
                "sale_date": "2026-05-12",
                "marketplace_code": "ozon",
                "marketplace_sku": "1300079194",
                "article": "F000283615",
                "product_name": "Item",
                "ad_source": "cpo_selected_products",
                "attribution_type": "direct",
                "ad_orders_qty": 0,
                "ad_orders_revenue": 0,
            },
        ]

        with mock.patch.object(organic, "fetch_all", return_value=rows):
            grouped, attribution_dates = organic.load_ad_attribution("2026-05-12", "2026-05-12")

        key = ("2026-05-12", "1300079194")
        self.assertIn(key, grouped)
        self.assertIn("2026-05-12", attribution_dates)
        self.assertEqual(grouped[key]["ad_orders_qty"], 1.0)
        self.assertEqual(grouped[key]["ad_orders_revenue"], 112863.0)


if __name__ == "__main__":
    unittest.main()
