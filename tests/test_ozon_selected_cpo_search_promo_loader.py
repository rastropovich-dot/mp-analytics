import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


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


class SearchPromoOrganisationLoaderTests(unittest.TestCase):
    def make_client(self):
        client = loader.OzonPerformanceClient.__new__(loader.OzonPerformanceClient)
        return client

    def test_plan_only_does_not_perform_http_calls_or_require_credentials(self):
        client = self.make_client()
        client.request = mock.Mock(side_effect=AssertionError("no request"))
        client.wait_statistics = mock.Mock(side_effect=AssertionError("no wait"))
        client.download_report_by_link = mock.Mock(side_effect=AssertionError("no download"))

        summary = loader.OzonPerformanceClient.fetch_search_promo_orders_csv(
            client,
            date="2026-05-06",
            plan_only=True,
        )

        self.assertEqual(summary["mode"], "search_promo_organisation_orders_plan")
        self.assertEqual(summary["plan"]["endpoint"], "/api/client/statistic/orders/generate")
        self.assertEqual(
            summary["plan"]["payload"],
            {
                "from": "2026-05-05T21:00:00Z",
                "to": "2026-05-06T20:59:59Z",
            },
        )
        self.assertEqual(summary["plan"]["status_endpoint"], "/api/client/statistics/{UUID}")
        self.assertEqual(summary["plan"]["download_endpoint"], "/api/client/statistics/report?UUID={UUID}")
        self.assertNotIn("campaignId", summary["plan"]["payload"])
        self.assertFalse(summary["plan"]["used_statistics_json"])
        self.assertFalse(summary["plan"]["used_general_statistics_submit"])
        self.assertEqual(summary["plan"]["db_writes"], 0)
        self.assertEqual(summary["plan"]["target_table"], "ozon_search_promo_selected_cpo_orders")
        self.assertFalse(summary["plan"]["writes_marketplace_expenses"])
        self.assertFalse(summary["plan"]["writes_ozon_daily_sku_ad_attribution"])
        self.assertTrue(summary["plan"]["safe_write_blockers"]["source_table"]["supported"])
        self.assertEqual(
            summary["plan"]["safe_write_blockers"]["source_table"]["table_name"],
            "ozon_search_promo_selected_cpo_orders",
        )
        self.assertFalse(summary["plan"]["safe_write_blockers"]["ozon_daily_sku_ad_attribution"]["supported"])
        self.assertFalse(summary["plan"]["safe_write_blockers"]["marketplace_expenses"]["supported"])

    def test_parser_excludes_vsego_from_spend_sum(self):
        parsed = loader.parse_search_promo_organisation_orders_csv(CSV_TEXT)
        self.assertEqual(parsed["row_count_raw"], 4)
        self.assertEqual(parsed["data_row_count"], 3)
        self.assertEqual(parsed["total_row_count"], 1)
        self.assertAlmostEqual(parsed["spend_sum_data_rows"], 25841.80, places=2)
        self.assertAlmostEqual(parsed["spend_sum_total_rows"], 25841.80, places=2)
        self.assertAlmostEqual(parsed["spend_sum_including_total_rows"], 51683.60, places=2)
        self.assertAlmostEqual(parsed["spend_sum"], 25841.80, places=2)
        self.assertEqual(parsed["spend_sum_basis"], "data_rows_excluding_total_rows")
        self.assertEqual(parsed["data_rows"][0]["SKU"], "1300079194")
        self.assertEqual(parsed["total_rows"][0]["Дата"], "Всего")

    def test_dry_run_uses_submit_status_download_contract_and_never_general_statistics(self):
        client = self.make_client()
        client.request = mock.Mock(return_value=FakeResponse({"UUID": "u1"}))
        client.wait_statistics = mock.Mock(return_value={"state": "OK", "kind": "SEARCH_PROMO_ORGANISATION_ORDERS"})
        client.download_report_by_link = mock.Mock(return_value=(CSV_TEXT, {"content-type": "text/csv"}))

        summary = loader.OzonPerformanceClient.fetch_search_promo_orders_csv(
            client,
            date="2026-05-06",
            dry_run=True,
            write=False,
        )

        client.request.assert_called_once()
        method, endpoint = client.request.call_args[0][:2]
        kwargs = client.request.call_args.kwargs
        self.assertEqual(method, "POST")
        self.assertEqual(endpoint, "/api/client/statistic/orders/generate")
        self.assertEqual(
            kwargs["json"],
            {
                "from": "2026-05-05T21:00:00Z",
                "to": "2026-05-06T20:59:59Z",
            },
        )
        self.assertEqual(summary["classification"]["promotion_type"], "cpo_selected_products")
        self.assertEqual(summary["classification"]["source_report"], "search_promo_organisation_orders")
        self.assertEqual(summary["classification"]["scope"], "organisation")
        self.assertTrue(summary["classification"]["safe_for_db_load"])
        self.assertFalse(summary["used_statistics_json"])
        self.assertFalse(summary["used_general_statistics_submit"])
        self.assertEqual(summary["db_writes"], 0)
        self.assertEqual(summary["target_table"], "ozon_search_promo_selected_cpo_orders")
        self.assertAlmostEqual(summary["parsed"]["spend_sum"], 25841.80, places=2)
        self.assertEqual(summary["aggregation"]["data_row_count"], 3)
        self.assertEqual(summary["aggregation"]["total_row_count"], 1)
        self.assertEqual(summary["aggregation"]["order_count"], 3)
        self.assertEqual(summary["aggregation"]["unique_promoted_sku_count"], 2)
        self.assertEqual(summary["aggregation"]["unique_ordered_sku_count"], 3)
        self.assertEqual(len(summary["normalized_rows"]), 3)
        self.assertEqual(len(summary["source_table_rows"]), 3)
        first_row = summary["normalized_rows"][1]
        self.assertEqual(first_row["ordered_sku"], "1620655754")
        self.assertEqual(first_row["promoted_sku"], "1300079194")
        self.assertEqual(
            first_row["attribution_sku_basis"],
            "existing_ozon_daily_sku_ad_attribution_convention_uses_ordered_sku_as_marketplace_sku",
        )
        source_write_row = summary["source_table_rows"][1]
        self.assertEqual(source_write_row["ordered_sku"], "1620655754")
        self.assertEqual(source_write_row["promoted_sku"], "1300079194")
        self.assertEqual(source_write_row["source_report"], "search_promo_organisation_orders")
        self.assertEqual(source_write_row["promotion_type"], "cpo_selected_products")
        self.assertEqual(summary["would_write"]["preferred_target"], "ozon_search_promo_selected_cpo_orders")
        self.assertTrue(summary["would_write"]["source_table"]["supported"])
        self.assertFalse(summary["would_write"]["ozon_daily_sku_ad_attribution"]["supported"])
        self.assertFalse(summary["would_write"]["marketplace_expenses"]["supported"])
        self.assertIn("promotion_type", " ".join(summary["would_write"]["source_table"]["idempotency_key"]))
        self.assertNotIn("source_uuid", " ".join(summary["would_write"]["source_table"]["idempotency_key"]))

    def test_write_true_is_not_implemented(self):
        client = self.make_client()
        with self.assertRaises(loader.SelectedCpoSchemaNotAppliedError):
            loader.OzonPerformanceClient.fetch_search_promo_orders_csv(
                client,
                date="2026-05-06",
                dry_run=True,
                write=True,
            )

    def test_write_true_can_use_mocked_db_client_when_schema_applied(self):
        client = self.make_client()
        client.request = mock.Mock(return_value=FakeResponse({"UUID": "u1"}))
        client.wait_statistics = mock.Mock(return_value={"state": "OK", "kind": "SEARCH_PROMO_ORGANISATION_ORDERS"})
        client.download_report_by_link = mock.Mock(return_value=(CSV_TEXT, {"content-type": "text/csv"}))

        execute_mock = mock.Mock()
        table_mock = mock.Mock()
        table_mock.upsert.return_value.execute = execute_mock
        db_client = mock.Mock()
        db_client.table.return_value = table_mock

        summary = loader.OzonPerformanceClient.fetch_search_promo_orders_csv(
            client,
            date="2026-05-06",
            dry_run=True,
            write=True,
            schema_applied=True,
            db_client=db_client,
        )

        db_client.table.assert_called_once_with("ozon_search_promo_selected_cpo_orders")
        table_mock.upsert.assert_called_once()
        _, kwargs = table_mock.upsert.call_args
        self.assertIn("sale_date,marketplace_code,source_report,promotion_type,order_id,posting_number,ordered_sku,promoted_sku", kwargs["on_conflict"])
        self.assertEqual(summary["db_writes"], 3)
        self.assertFalse(summary["writes_marketplace_expenses"])
        self.assertFalse(summary["writes_ozon_daily_sku_ad_attribution"])


if __name__ == "__main__":
    unittest.main()
