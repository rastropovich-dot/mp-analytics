import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader


CSV_TEXT = (
    "\ufeff;Отчёт по заказам, период 06.05.2026-06.05.2026\n"
    "Дата;ID заказа;Номер заказа;SKU;SKU продвигаемого товара;Артикул;Источник заказов;Название товара;"
    "Количество;Стоимость продажи, ₽;Стоимость, ₽;Ставка, %;Ставка, ₽;Расход, ₽\n"
    "06.05.2026;36192717417;74636537-0920;1300079194;1300079194;F000283615;Кампания за клики;"
    "\"Серьги\";1;111645,00;111645,00;10,00;11164,50;11164,50\n"
    "06.05.2026;36200596809;10001049-0181;1620655754;1300079194;F000312626;Кампания за клики;"
    "\"Серьги 2\";1;106452,00;106452,00;10,00;10645,20;10645,20\n"
    "06.05.2026;36503743780;81194692-0153;1499239951;1499239951;F000283607;Кампания за клики;"
    "\"Подвеска\";1;40321,00;40321,00;10,00;4032,10;4032,10\n"
    "Всего;;;;;;;;;;;;;25841,80\n"
)


def build_source_rows():
    parsed = loader.parse_search_promo_organisation_orders_csv(CSV_TEXT)
    normalized = loader.normalize_search_promo_selected_cpo_rows(
        parsed,
        source_uuid="uuid-1",
        source_kind="SEARCH_PROMO_ORGANISATION_ORDERS",
    )
    return loader.build_selected_cpo_source_table_rows(normalized)


class SelectedCpoDownstreamMappingTests(unittest.TestCase):
    def test_marketplace_expenses_rows_aggregate_by_ordered_sku(self):
        rows = loader.build_selected_cpo_marketplace_expenses_rows(build_source_rows())

        self.assertEqual(len(rows), 3)
        self.assertEqual(
            rows,
            [
                {
                    "expense_date": "2026-05-06",
                    "marketplace_code": "ozon",
                    "marketplace_sku": "1300079194",
                    "article": "F000283615",
                    "expense_type": "advertising_order_selected_cpo",
                    "expense_amount": 11164.5,
                },
                {
                    "expense_date": "2026-05-06",
                    "marketplace_code": "ozon",
                    "marketplace_sku": "1499239951",
                    "article": "F000283607",
                    "expense_type": "advertising_order_selected_cpo",
                    "expense_amount": 4032.1,
                },
                {
                    "expense_date": "2026-05-06",
                    "marketplace_code": "ozon",
                    "marketplace_sku": "1620655754",
                    "article": "F000312626",
                    "expense_type": "advertising_order_selected_cpo",
                    "expense_amount": 10645.2,
                },
            ],
        )
        self.assertAlmostEqual(sum(row["expense_amount"] for row in rows), 25841.8, places=2)
        self.assertNotIn("advertising_order_5", {row["expense_type"] for row in rows})

    def test_ad_attribution_rows_use_selected_cpo_identity(self):
        rows = loader.build_selected_cpo_ad_attribution_rows(build_source_rows())

        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["ad_source"] == "cpo_selected_products" for row in rows))
        self.assertTrue(all(row["attribution_type"] == "direct" for row in rows))
        self.assertTrue(all(row["campaign_id"] == "" for row in rows))
        self.assertTrue(all(row["marketplace_sku"] == row["order_sku"] for row in rows))
        self.assertTrue(all(row["promoted_sku"] == "" for row in rows))
        self.assertNotIn("cpo", {row["ad_source"] for row in rows})
        self.assertAlmostEqual(sum(row["ad_spend"] for row in rows), 25841.8, places=2)

    def test_downstream_dry_run_prepares_would_write_rows_without_api_calls(self):
        client = loader.OzonPerformanceClient.__new__(loader.OzonPerformanceClient)
        client.request = mock.Mock(side_effect=AssertionError("no API calls"))
        client.wait_statistics = mock.Mock(side_effect=AssertionError("no API calls"))
        client.download_report_by_link = mock.Mock(side_effect=AssertionError("no API calls"))

        summary = loader.OzonPerformanceClient.selected_cpo_downstream_dry_run(
            client,
            date="2026-05-06",
            write=False,
            source_rows=build_source_rows(),
        )

        self.assertEqual(summary["mode"], "selected_cpo_downstream_dry_run")
        self.assertEqual(summary["source_row_count"], 3)
        self.assertAlmostEqual(summary["source_sum_spend"], 25841.8, places=2)
        self.assertEqual(summary["db_writes"], 0)
        self.assertEqual(summary["marketplace_expenses_writes"], 0)
        self.assertEqual(summary["ozon_daily_sku_ad_attribution_writes"], 0)
        self.assertFalse(summary["used_statistics_json"])
        self.assertFalse(summary["used_general_statistics_submit"])
        self.assertEqual(
            summary["marketplace_expenses_rows"],
            summary["would_write"]["marketplace_expenses"]["rows"],
        )
        self.assertEqual(
            summary["ad_attribution_rows"],
            summary["would_write"]["ozon_daily_sku_ad_attribution"]["rows"],
        )
        self.assertAlmostEqual(summary["marketplace_expenses_total"], 25841.8, places=2)
        self.assertAlmostEqual(summary["ad_attribution_total_spend"], 25841.8, places=2)

    def test_downstream_write_true_remains_guarded(self):
        client = loader.OzonPerformanceClient.__new__(loader.OzonPerformanceClient)

        with self.assertRaises(loader.SelectedCpoDownstreamWriteNotApprovedError):
            loader.OzonPerformanceClient.selected_cpo_downstream_dry_run(
                client,
                date="2026-05-06",
                write=True,
                source_rows=build_source_rows(),
            )

    def test_downstream_write_true_with_explicit_approval_uses_mocked_upserts(self):
        client = loader.OzonPerformanceClient.__new__(loader.OzonPerformanceClient)

        execute_mock = mock.Mock()
        table_mock = mock.Mock()
        table_mock.upsert.return_value.execute = execute_mock
        db_client = mock.Mock()
        db_client.table.return_value = table_mock

        summary = loader.OzonPerformanceClient.selected_cpo_downstream_dry_run(
            client,
            date="2026-05-06",
            write=True,
            approve_downstream_write=True,
            db_client=db_client,
            source_rows=build_source_rows(),
        )

        self.assertEqual(summary["mode"], "selected_cpo_downstream_write")
        self.assertEqual(summary["marketplace_expenses_writes"], 3)
        self.assertEqual(summary["ozon_daily_sku_ad_attribution_writes"], 3)
        self.assertEqual(summary["db_writes"], 6)
        self.assertEqual(db_client.table.call_count, 2)
        db_client.table.assert_any_call("marketplace_expenses")
        db_client.table.assert_any_call("ozon_daily_sku_ad_attribution")


if __name__ == "__main__":
    unittest.main()
