import unittest
from unittest import mock

import reports_sku_decision_daily_input as decision


class SkuDecisionDailyInputOrganicFlagsTests(unittest.TestCase):
    def _base_kpi_row(self):
        return {
            "kpi_date": "2026-05-12",
            "marketplace_code": "ozon",
            "marketplace_sku": "1045776466",
            "article": "",
            "product_name": "Item",
            "orders_qty": 0,
            "orders_amount_seller": 0,
            "buyouts_qty": 1,
            "buyouts_amount_seller": 10566,
            "ad_spend": 528.30,
            "ad_orders_revenue": 10566,
            "organic_orders_revenue": 0,
            "commission_amount": 4437.72,
            "logistics_amount": 0,
            "other_expenses_amount": 112.75,
        }

    def _build_rows(
        self,
        organic_row,
        reconciliation_rows=None,
        stock_rows=None,
    ):
        reconciliation_rows = reconciliation_rows or []
        stock_rows = stock_rows or []
        with mock.patch.object(decision, "load_daily_kpi", return_value=[self._base_kpi_row()]), \
            mock.patch.object(decision, "load_organic_rows", return_value=[organic_row] if organic_row else []), \
            mock.patch.object(decision, "load_recent_stock", return_value={"by_stock_sku": {}, "by_decision_sku": {}, "by_article": {}, "decision_sku_by_article_count": 0}), \
            mock.patch.object(decision, "load_identity_stock_evidence", return_value={"by_decision_sku": {}, "by_article": {}}), \
            mock.patch.object(decision, "load_recent_price_points", return_value={}), \
            mock.patch.object(decision, "load_latest_ozon_run_status", return_value={}), \
            mock.patch.object(decision, "build_stock_quality_rows", return_value=(stock_rows, {})), \
            mock.patch.object(decision, "build_reconciliation_rows", return_value=(reconciliation_rows, {})):
            rows, summary = decision.build_rows("2026-05-12", "2026-05-12")
        return rows, summary

    def test_missing_total_fallback_sets_organic_reconciliation_status(self):
        organic_row = {
            "sale_date": "2026-05-12",
            "marketplace_code": "ozon",
            "marketplace_sku": "1045776466",
            "calculation_status": "missing_total",
            "warning": "zero_total_with_ad_attribution",
            "ad_orders_revenue": 10566,
            "organic_orders_revenue": 0,
            "article": "",
            "product_name": "Item",
        }

        rows, _ = self._build_rows(organic_row)
        row = rows[0]

        self.assertEqual(row["organic_reconciliation_status"], "missing_total")
        self.assertIn("missing_total", row["data_quality_status"])
        self.assertIn("zero_total_with_ad_attribution", row["data_quality_status"])

    def test_ok_without_warning_without_reconciliation_issue_stays_clean(self):
        organic_row = {
            "sale_date": "2026-05-12",
            "marketplace_code": "ozon",
            "marketplace_sku": "1045776466",
            "calculation_status": "ok",
            "warning": "",
            "ad_orders_revenue": 0,
            "organic_orders_revenue": 0,
            "article": "",
            "product_name": "Item",
        }

        rows, _ = self._build_rows(organic_row)
        row = rows[0]

        self.assertEqual(row["organic_reconciliation_status"], "clean")
        self.assertNotIn("missing_total", row["data_quality_status"])

    def test_existing_reconciliation_issue_row_wins(self):
        organic_row = {
            "sale_date": "2026-05-12",
            "marketplace_code": "ozon",
            "marketplace_sku": "1045776466",
            "calculation_status": "missing_total",
            "warning": "zero_total_with_ad_attribution",
            "ad_orders_revenue": 10566,
            "organic_orders_revenue": 0,
            "article": "",
            "product_name": "Item",
        }
        reconciliation_rows = [
            {
                "sale_date": "2026-05-12",
                "marketplace_sku": "1045776466",
                "reconciliation_status": "possible_date_semantics",
                "unreconciled_revenue": 123.0,
            }
        ]

        rows, _ = self._build_rows(organic_row, reconciliation_rows=reconciliation_rows)
        row = rows[0]

        self.assertEqual(row["organic_reconciliation_status"], "possible_date_semantics")
        self.assertIn("possible_date_semantics", row["data_quality_status"])

    def test_zero_total_ad_row_remains_hold(self):
        organic_row = {
            "sale_date": "2026-05-12",
            "marketplace_code": "ozon",
            "marketplace_sku": "1045776466",
            "calculation_status": "missing_total",
            "warning": "zero_total_with_ad_attribution",
            "ad_orders_revenue": 10566,
            "organic_orders_revenue": 0,
            "article": "",
            "product_name": "Item",
        }

        rows, _ = self._build_rows(organic_row)
        row = rows[0]

        self.assertEqual(row["decision_status"], "hold")
        self.assertIn("low_data_volume", row["data_quality_status"])
        self.assertIn("missing_total", row["data_quality_status"])
        self.assertIn("zero_total_with_ad_attribution", row["data_quality_status"])


if __name__ == "__main__":
    unittest.main()
