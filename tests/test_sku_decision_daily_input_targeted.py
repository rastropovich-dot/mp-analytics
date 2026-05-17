import unittest
from types import SimpleNamespace
from unittest import mock

import reports_sku_decision_daily_input as decision


def _kpi_row(
    sku="1300079194",
    article="F000283615",
    product_name="Item",
    ad_spend=1412.30,
    ad_orders_revenue=112863,
    organic_orders_revenue=0,
):
    return {
        "kpi_date": "2026-05-12",
        "marketplace_code": "ozon",
        "marketplace_sku": sku,
        "article": article,
        "product_name": product_name,
        "orders_qty": 1,
        "orders_amount_seller": 112863,
        "buyouts_qty": 1,
        "buyouts_amount_seller": 116126,
        "ad_spend": ad_spend,
        "ad_orders_qty": 1 if ad_orders_revenue else 0,
        "ad_orders_revenue": ad_orders_revenue,
        "organic_orders_qty": 0 if ad_orders_revenue else 1,
        "organic_orders_revenue": organic_orders_revenue,
        "commission_amount": 48772.92,
        "logistics_amount": 144.0,
        "other_expenses_amount": 1071.5,
    }


def _organic_row(
    sku="1300079194",
    status="ok",
    warning="",
    ad_orders_revenue=112863,
    organic_orders_revenue=0,
):
    return {
        "sale_date": "2026-05-12",
        "marketplace_code": "ozon",
        "marketplace_sku": sku,
        "article": "F000283615" if sku == "1300079194" else "",
        "product_name": "Item",
        "calculation_status": status,
        "warning": warning,
        "ad_orders_qty": 1 if ad_orders_revenue else 0,
        "ad_orders_revenue": ad_orders_revenue,
        "organic_orders_qty": 0 if ad_orders_revenue else 1,
        "organic_orders_revenue": organic_orders_revenue,
    }


class TargetedSkuDecisionDailyInputTests(unittest.TestCase):
    def _patch_dependencies(self, kpi_rows=None, organic_rows=None):
        return mock.patch.multiple(
            decision,
            load_daily_kpi=mock.Mock(return_value=kpi_rows or []),
            load_organic_rows=mock.Mock(return_value=organic_rows or []),
            load_recent_stock=mock.Mock(
                return_value={"by_stock_sku": {}, "by_decision_sku": {}, "by_article": {}, "decision_sku_by_article_count": 0}
            ),
            load_identity_stock_evidence=mock.Mock(return_value={"by_decision_sku": {}, "by_article": {}}),
            load_recent_price_points=mock.Mock(return_value={}),
            load_latest_ozon_run_status=mock.Mock(return_value={}),
            build_stock_quality_rows=mock.Mock(return_value=([], {})),
            build_reconciliation_rows=mock.Mock(return_value=([], {})),
        )

    def test_sku_filter_limits_final_rows_to_one_sku(self):
        kpi_rows = [_kpi_row("1300079194"), _kpi_row("9999999999", article="A2", product_name="Item 2")]
        organic_rows = [_organic_row("1300079194"), _organic_row("9999999999")]
        with self._patch_dependencies(kpi_rows, organic_rows):
            rows, summary = decision.build_rows("2026-05-12", "2026-05-12", sku_filter="1300079194")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["marketplace_sku"], "1300079194")
        self.assertEqual(summary["rows"], 1)

    def test_targeted_row_uses_recovered_cpc_and_organic_values(self):
        with self._patch_dependencies([_kpi_row()], [_organic_row()]):
            rows, _ = decision.build_rows("2026-05-12", "2026-05-12", sku_filter="1300079194")
        row = rows[0]
        self.assertEqual(row["marketplace_sku"], "1300079194")
        self.assertAlmostEqual(row["ad_spend"], 1412.30, places=2)
        self.assertAlmostEqual(row["ad_attributed_revenue"], 112863.0, places=2)
        self.assertAlmostEqual(row["organic_revenue"], 0.0, places=2)

    def test_targeted_dry_run_does_not_call_save_rows(self):
        args = SimpleNamespace(
            mode="full",
            date="2026-05-12",
            date_from=None,
            date_to=None,
            sku="1300079194",
            sku_offset=0,
            sku_batch_size=None,
            list_skus_only=False,
            days_back=7,
            dry_run=True,
            debug_sample=False,
        )
        with self._patch_dependencies([_kpi_row()], [_organic_row()]), \
            mock.patch.object(decision, "parse_args", return_value=args), \
            mock.patch.object(decision, "save_rows") as save_rows_mock:
            decision.main()
        save_rows_mock.assert_not_called()

    def test_targeted_write_calls_save_rows_with_one_row(self):
        args = SimpleNamespace(
            mode="full",
            date="2026-05-12",
            date_from=None,
            date_to=None,
            sku="1300079194",
            sku_offset=0,
            sku_batch_size=None,
            list_skus_only=False,
            days_back=7,
            dry_run=False,
            debug_sample=False,
        )
        with self._patch_dependencies([_kpi_row()], [_organic_row()]), \
            mock.patch.object(decision, "parse_args", return_value=args), \
            mock.patch.object(decision, "save_rows") as save_rows_mock:
            decision.main()
        save_rows_mock.assert_called_once()
        saved_rows = save_rows_mock.call_args[0][0]
        self.assertEqual(len(saved_rows), 1)
        self.assertEqual(saved_rows[0]["marketplace_sku"], "1300079194")

    def test_organic_warning_propagation_still_works(self):
        with self._patch_dependencies(
            [_kpi_row(sku="1045776466", article="", product_name="Warn Item", ad_spend=528.3, ad_orders_revenue=10566, organic_orders_revenue=0)],
            [_organic_row(sku="1045776466", status="missing_total", warning="zero_total_with_ad_attribution", ad_orders_revenue=10566, organic_orders_revenue=0)],
        ):
            rows, _ = decision.build_rows("2026-05-12", "2026-05-12", sku_filter="1045776466")
        row = rows[0]
        self.assertIn("missing_total", row["data_quality_status"])
        self.assertIn("zero_total_with_ad_attribution", row["data_quality_status"])
        self.assertNotEqual(row["organic_reconciliation_status"], "clean")

    def test_batch_selection_uses_sorted_skus_deterministically(self):
        kpi_rows = [
            _kpi_row("300"),
            _kpi_row("100"),
            _kpi_row("200"),
        ]
        organic_rows = [_organic_row("300"), _organic_row("100"), _organic_row("200")]
        with self._patch_dependencies(kpi_rows, organic_rows):
            rows, summary = decision.build_rows(
                "2026-05-12",
                "2026-05-12",
                sku_offset=1,
                sku_batch_size=1,
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["marketplace_sku"], "200")
        self.assertEqual(summary["total_available_skus"], 3)
        self.assertEqual(summary["selected_sku_count"], 1)
        self.assertEqual(summary["first_selected_sku"], "200")
        self.assertEqual(summary["last_selected_sku"], "200")

    def test_batch_dry_run_does_not_call_save_rows(self):
        args = SimpleNamespace(
            mode="full",
            date="2026-05-12",
            date_from=None,
            date_to=None,
            sku=None,
            sku_offset=0,
            sku_batch_size=2,
            list_skus_only=False,
            days_back=7,
            dry_run=True,
            debug_sample=False,
        )
        kpi_rows = [_kpi_row("1300079194"), _kpi_row("9999999999", article="A2", product_name="Item 2")]
        organic_rows = [_organic_row("1300079194"), _organic_row("9999999999")]
        with self._patch_dependencies(kpi_rows, organic_rows), \
            mock.patch.object(decision, "parse_args", return_value=args), \
            mock.patch.object(decision, "save_rows") as save_rows_mock:
            decision.main()
        save_rows_mock.assert_not_called()

    def test_batch_write_calls_save_rows_only_with_selected_rows(self):
        args = SimpleNamespace(
            mode="full",
            date="2026-05-12",
            date_from=None,
            date_to=None,
            sku=None,
            sku_offset=0,
            sku_batch_size=1,
            list_skus_only=False,
            days_back=7,
            dry_run=False,
            debug_sample=False,
        )
        kpi_rows = [_kpi_row("1300079194"), _kpi_row("9999999999", article="A2", product_name="Item 2")]
        organic_rows = [_organic_row("1300079194"), _organic_row("9999999999")]
        with self._patch_dependencies(kpi_rows, organic_rows), \
            mock.patch.object(decision, "parse_args", return_value=args), \
            mock.patch.object(decision, "save_rows") as save_rows_mock:
            decision.main()
        save_rows_mock.assert_called_once()
        saved_rows = save_rows_mock.call_args[0][0]
        self.assertEqual(len(saved_rows), 1)
        self.assertEqual(saved_rows[0]["marketplace_sku"], "1300079194")

    def test_sku_and_batch_args_are_mutually_exclusive(self):
        args = SimpleNamespace(
            mode="full",
            date="2026-05-12",
            date_from=None,
            date_to=None,
            sku="1300079194",
            sku_offset=0,
            sku_batch_size=100,
            list_skus_only=False,
            days_back=7,
            dry_run=True,
            debug_sample=False,
        )
        with mock.patch.object(decision, "parse_args", return_value=args):
            with self.assertRaises(RuntimeError):
                decision.main()

    def test_zero_batch_writes_nothing(self):
        args = SimpleNamespace(
            mode="full",
            date="2026-05-12",
            date_from=None,
            date_to=None,
            sku=None,
            sku_offset=10,
            sku_batch_size=5,
            list_skus_only=False,
            days_back=7,
            dry_run=False,
            debug_sample=False,
        )
        kpi_rows = [_kpi_row("1300079194")]
        organic_rows = [_organic_row("1300079194")]
        with self._patch_dependencies(kpi_rows, organic_rows), \
            mock.patch.object(decision, "parse_args", return_value=args), \
            mock.patch.object(decision, "save_rows") as save_rows_mock:
            decision.main()
        save_rows_mock.assert_not_called()

    def test_1300079194_like_row_can_be_included_in_batch(self):
        kpi_rows = [
            _kpi_row("1200000000", article="A0", product_name="Item 0", ad_spend=0, ad_orders_revenue=0, organic_orders_revenue=5000),
            _kpi_row("1300079194"),
            _kpi_row("1400000000", article="A1", product_name="Item 1", ad_spend=0, ad_orders_revenue=0, organic_orders_revenue=7000),
        ]
        organic_rows = [
            _organic_row("1200000000", ad_orders_revenue=0, organic_orders_revenue=5000),
            _organic_row("1300079194"),
            _organic_row("1400000000", ad_orders_revenue=0, organic_orders_revenue=7000),
        ]
        with self._patch_dependencies(kpi_rows, organic_rows):
            rows, _ = decision.build_rows(
                "2026-05-12",
                "2026-05-12",
                sku_offset=1,
                sku_batch_size=2,
            )
        target_rows = [row for row in rows if row["marketplace_sku"] == "1300079194"]
        self.assertEqual(len(target_rows), 1)
        self.assertAlmostEqual(target_rows[0]["ad_spend"], 1412.30, places=2)
        self.assertAlmostEqual(target_rows[0]["ad_attributed_revenue"], 112863.0, places=2)
        self.assertAlmostEqual(target_rows[0]["organic_revenue"], 0.0, places=2)


if __name__ == "__main__":
    unittest.main()
