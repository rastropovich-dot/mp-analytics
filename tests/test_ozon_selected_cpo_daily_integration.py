import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader


class SelectedCpoDailyIntegrationTests(unittest.TestCase):
    def make_client(self):
        client = loader.OzonPerformanceClient.__new__(loader.OzonPerformanceClient)
        return client

    def test_feature_flag_off_skips_selected_cpo(self):
        client = self.make_client()
        client.fetch_search_promo_orders_csv = mock.Mock(side_effect=AssertionError("should not fetch"))
        client.selected_cpo_downstream_dry_run = mock.Mock(side_effect=AssertionError("should not aggregate"))

        summary = loader.OzonPerformanceClient.load_ozon_selected_cpo_for_date(
            client,
            date="2026-05-06",
            enabled=False,
            dry_run=True,
            write=False,
        )

        self.assertFalse(summary["selected_cpo_enabled"])
        self.assertEqual(summary["status"], "skipped")
        self.assertEqual(summary["reason"], "feature_flag_disabled")
        self.assertEqual(summary["db_writes"], 0)

    def test_explicit_dry_run_uses_existing_source_table_and_no_api_calls(self):
        client = self.make_client()
        client.fetch_search_promo_orders_csv = mock.Mock(side_effect=AssertionError("no API source fetch"))
        source_rows = [
            {"sale_date": "2026-05-06", "ordered_sku": "1300079194", "spend": 11164.50},
            {"sale_date": "2026-05-06", "ordered_sku": "1499239951", "spend": 4032.10},
            {"sale_date": "2026-05-06", "ordered_sku": "1620655754", "spend": 10645.20},
        ]

        def fake_downstream(**kwargs):
            return {
                "db_writes": 0,
                "marketplace_expenses_writes": 0,
                "ozon_daily_sku_ad_attribution_writes": 0,
                "marketplace_expenses_rows": [1, 2, 3],
                "marketplace_expenses_total": 25841.80,
                "ad_attribution_rows": [1, 2, 3],
                "ad_attribution_total_spend": 25841.80,
            }

        client.selected_cpo_downstream_dry_run = mock.Mock(side_effect=fake_downstream)

        with mock.patch.object(loader, "load_selected_cpo_source_rows", return_value=source_rows) as load_rows_mock:
            summary = loader.OzonPerformanceClient.load_ozon_selected_cpo_for_date(
                client,
                date="2026-05-06",
                enabled=True,
                dry_run=True,
                write=False,
                db_client=object(),
            )

        load_rows_mock.assert_called_once()
        client.fetch_search_promo_orders_csv.assert_not_called()
        client.selected_cpo_downstream_dry_run.assert_called_once()
        downstream_kwargs = client.selected_cpo_downstream_dry_run.call_args.kwargs
        self.assertEqual(downstream_kwargs["source_rows"], source_rows)
        self.assertFalse(downstream_kwargs["write"])
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["source_rows"], 3)
        self.assertEqual(summary["marketplace_expenses_rows"], 3)
        self.assertEqual(summary["ad_attribution_rows"], 3)
        self.assertTrue(summary["totals_match"])
        self.assertEqual(summary["db_writes"], 0)
        self.assertFalse(summary["used_statistics_json"])
        self.assertFalse(summary["used_general_statistics_submit"])

    def test_write_true_without_approval_raises(self):
        client = self.make_client()
        with self.assertRaises(loader.SelectedCpoDownstreamWriteNotApprovedError):
            loader.OzonPerformanceClient.load_ozon_selected_cpo_for_date(
                client,
                date="2026-05-06",
                enabled=True,
                dry_run=True,
                write=True,
                approve_write=False,
            )

    def test_daily_style_write_request_without_approval_becomes_safe_no_write(self):
        client = self.make_client()
        client.fetch_search_promo_orders_csv = mock.Mock(side_effect=AssertionError("no API source fetch"))
        source_rows = [
            {"sale_date": "2026-05-06", "ordered_sku": "1300079194", "spend": 11164.50},
            {"sale_date": "2026-05-06", "ordered_sku": "1499239951", "spend": 4032.10},
            {"sale_date": "2026-05-06", "ordered_sku": "1620655754", "spend": 10645.20},
        ]

        def fake_downstream(**kwargs):
            self.assertFalse(kwargs["write"])
            return {
                "db_writes": 0,
                "marketplace_expenses_writes": 0,
                "ozon_daily_sku_ad_attribution_writes": 0,
                "marketplace_expenses_rows": [1, 2, 3],
                "marketplace_expenses_total": 25841.80,
                "ad_attribution_rows": [1, 2, 3],
                "ad_attribution_total_spend": 25841.80,
            }

        client.selected_cpo_downstream_dry_run = mock.Mock(side_effect=fake_downstream)

        with mock.patch.object(loader, "load_selected_cpo_source_rows", return_value=source_rows):
            summary = loader.OzonPerformanceClient.load_ozon_selected_cpo_for_date(
                client,
                date="2026-05-06",
                enabled=True,
                dry_run=True,
                write=True,
                approve_write=False,
                skip_write_if_not_approved=True,
                db_client=object(),
            )

        client.fetch_search_promo_orders_csv.assert_not_called()
        self.assertTrue(summary["selected_cpo_enabled"])
        self.assertFalse(summary["write_approved"])
        self.assertEqual(summary["status"], "dry_run_no_write")
        self.assertEqual(summary["reason"], "write_not_approved")
        self.assertEqual(summary["db_writes"], 0)
        self.assertEqual(summary["marketplace_expenses_writes"], 0)
        self.assertEqual(summary["ozon_daily_sku_ad_attribution_writes"], 0)
        self.assertTrue(summary["totals_match"])

    def test_enabled_write_path_calls_source_and_downstream_in_order(self):
        client = self.make_client()
        calls = []
        db_client = object()

        def fake_source(**kwargs):
            calls.append(("source", kwargs))
            return {
                "db_writes": 3,
                "aggregation": {"total_spend_data_rows": 25841.80},
                "source_table_rows": [
                    {"sale_date": "2026-05-06", "ordered_sku": "1300079194", "spend": 11164.50},
                    {"sale_date": "2026-05-06", "ordered_sku": "1499239951", "spend": 4032.10},
                    {"sale_date": "2026-05-06", "ordered_sku": "1620655754", "spend": 10645.20},
                ],
            }

        def fake_downstream(**kwargs):
            calls.append(("downstream", kwargs))
            return {
                "db_writes": 6,
                "marketplace_expenses_writes": 3,
                "ozon_daily_sku_ad_attribution_writes": 3,
                "marketplace_expenses_rows": [
                    {"expense_type": "advertising_order_selected_cpo"},
                    {"expense_type": "advertising_order_selected_cpo"},
                    {"expense_type": "advertising_order_selected_cpo"},
                ],
                "marketplace_expenses_total": 25841.80,
                "ad_attribution_rows": [
                    {"ad_source": "cpo_selected_products"},
                    {"ad_source": "cpo_selected_products"},
                    {"ad_source": "cpo_selected_products"},
                ],
                "ad_attribution_total_spend": 25841.80,
            }

        client.fetch_search_promo_orders_csv = mock.Mock(side_effect=fake_source)
        client.selected_cpo_downstream_dry_run = mock.Mock(side_effect=fake_downstream)

        summary = loader.OzonPerformanceClient.load_ozon_selected_cpo_for_date(
            client,
            date="2026-05-06",
            enabled=True,
            dry_run=True,
            write=True,
            approve_write=True,
            db_client=db_client,
        )

        self.assertEqual([name for name, _ in calls], ["source", "downstream"])
        self.assertEqual(summary["status"], "written")
        self.assertEqual(summary["db_writes"], 9)
        self.assertEqual(summary["marketplace_expenses_writes"], 3)
        self.assertEqual(summary["ozon_daily_sku_ad_attribution_writes"], 3)
        self.assertTrue(all(row["expense_type"] == "advertising_order_selected_cpo" for row in summary["downstream_summary"]["marketplace_expenses_rows"]))
        self.assertTrue(all(row["ad_source"] == "cpo_selected_products" for row in summary["downstream_summary"]["ad_attribution_rows"]))
        self.assertFalse(any(row.get("expense_type") == "advertising_order_5" for row in summary["downstream_summary"]["marketplace_expenses_rows"]))
        self.assertFalse(any(row.get("ad_source") == "cpo" for row in summary["downstream_summary"]["ad_attribution_rows"]))
        self.assertIs(calls[0][1]["db_client"], db_client)
        self.assertIs(calls[1][1]["db_client"], db_client)


if __name__ == "__main__":
    unittest.main()
