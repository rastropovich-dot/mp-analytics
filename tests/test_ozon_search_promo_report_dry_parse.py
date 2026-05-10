import unittest
from unittest import mock

import scripts.ozon_search_promo_report_dry_parse as probe


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

HEADER_ONLY_CSV_TEXT = (
    "\ufeff;Отчёт по заказам, период 06.05.2026-06.05.2026\n"
    "Дата;ID заказа;Номер заказа;SKU;SKU продвигаемого товара;Артикул;Источник заказов;Название товара;"
    "Количество;Стоимость продажи, ₽;Стоимость, ₽;Ставка, %;Ставка, ₽;Расход, ₽\n"
)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", content=b"", headers=None):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.content = content if content else text.encode("utf-8")
        self.headers = headers or {}

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class OzonSearchPromoReportDryParseTests(unittest.TestCase):
    def setUp(self):
        self.base_patches = [
            mock.patch.object(probe, "CLIENT_ID", "test_client_id"),
            mock.patch.object(probe, "CLIENT_SECRET", "test_client_secret"),
            mock.patch.object(probe, "BASE_URL", "https://api-performance.ozon.ru"),
        ]
        for patcher in self.base_patches:
            patcher.start()
        self.addCleanup(self._cleanup_patches)

    def _cleanup_patches(self):
        for patcher in reversed(self.base_patches):
            patcher.stop()

    def test_parser_handles_bom_preamble_semicolon_and_total_row(self):
        columns, rows, preamble_lines = probe.parse_search_promo_csv_text(CSV_TEXT)
        summary = probe.analyze_rows(rows, detected_format="csv", columns=columns, preamble_lines=preamble_lines)

        self.assertEqual(preamble_lines, [";Отчёт по заказам, период 06.05.2026-06.05.2026"])
        self.assertEqual(
            columns,
            [
                "Дата",
                "ID заказа",
                "Номер заказа",
                "SKU",
                "SKU продвигаемого товара",
                "Артикул",
                "Источник заказов",
                "Название товара",
                "Количество",
                "Стоимость продажи, ₽",
                "Стоимость, ₽",
                "Ставка, %",
                "Ставка, ₽",
                "Расход, ₽",
            ],
        )
        self.assertEqual(summary["row_count_raw"], 4)
        self.assertEqual(summary["data_row_count"], 3)
        self.assertEqual(summary["total_row_count"], 1)
        self.assertEqual(summary["row_count"], 3)
        self.assertEqual(summary["preview_rows_sanitized"][0]["Дата"], "06.05.2026")
        self.assertEqual(summary["total_rows_preview_sanitized"][0]["Дата"], "Всего")
        self.assertAlmostEqual(summary["spend_sum_data_rows"], 25841.80, places=2)
        self.assertAlmostEqual(summary["spend_sum_total_rows"], 25841.80, places=2)
        self.assertAlmostEqual(summary["spend_sum_including_total_rows"], 51683.60, places=2)
        self.assertAlmostEqual(summary["spend_sum"], 25841.80, places=2)
        self.assertEqual(summary["spend_sum_basis"], "data_rows_excluding_total_rows")
        self.assertAlmostEqual(summary["absolute_diff"], 0.0, places=2)
        self.assertTrue(summary["close_to_expected"])
        self.assertIn("Расход, ₽", summary["candidate_spend_columns"])
        self.assertIn("SKU", summary["candidate_sku_columns"])
        self.assertTrue(
            "ID заказа" in summary["candidate_order_columns"]
            or "Номер заказа" in summary["candidate_order_columns"]
        )
        self.assertIn("Дата", summary["candidate_date_columns"])
        self.assertEqual(summary["classification"]["source_report"], "search_promo_organisation_orders")
        self.assertEqual(summary["classification"]["promotion_type"], "cpo_selected_products")
        self.assertEqual(summary["classification"]["scope"], "organisation")
        self.assertFalse(summary["classification"]["safe_for_db_load"])

    def test_header_only_csv_gives_zero_data_rows(self):
        columns, rows, preamble_lines = probe.parse_search_promo_csv_text(HEADER_ONLY_CSV_TEXT)
        summary = probe.analyze_rows(rows, detected_format="csv", columns=columns, preamble_lines=preamble_lines)
        self.assertEqual(summary["row_count_raw"], 0)
        self.assertEqual(summary["data_row_count"], 0)
        self.assertEqual(summary["total_row_count"], 0)
        self.assertEqual(summary["preview_rows_sanitized"], [])
        self.assertEqual(summary["total_rows_preview_sanitized"], [])
        self.assertIsNone(summary["spend_sum"])

    def test_without_live_dry_run_makes_no_http_calls(self):
        def fail(*args, **kwargs):
            raise AssertionError("HTTP should not be called")

        with mock.patch.object(probe.requests, "post", side_effect=fail), mock.patch.object(
            probe.requests, "request", side_effect=fail
        ):
            with self.assertRaises(RuntimeError):
                probe.run(
                    uuid="u1",
                    campaign_id="4471285",
                    target_date="2026-05-06",
                    live_dry_run=False,
                    max_polls=1,
                    poll_interval_sec=0,
                )

    def test_live_dry_run_never_calls_submit_endpoints(self):
        seen = []

        def fake_post(*args, **kwargs):
            return FakeResponse(200, json_data={"access_token": "token"})

        def fake_request(method, url, **kwargs):
            seen.append((method, url))
            self.assertEqual(method, "GET")
            self.assertNotIn("/api/client/statistics/json", url)
            self.assertFalse(method == "POST" and url.endswith("/api/client/statistics"))
            self.assertFalse(method == "POST" and url.endswith("/api/client/statistic/orders/generate"))
            if url.endswith("/api/client/statistic/u1"):
                return FakeResponse(404, text="404")
            if url.endswith("/api/client/statistics/u1"):
                return FakeResponse(404, text="404")
            raise AssertionError(f"Unexpected URL {url}")

        with mock.patch.object(probe.requests, "post", side_effect=fake_post), mock.patch.object(
            probe.requests, "request", side_effect=fake_request
        ):
            summary = probe.run("u1", "4471285", "2026-05-06", True, 1, 0)
        self.assertEqual(summary["result"], "uuid_not_found_or_expired")
        self.assertTrue(all(method == "GET" for method, _ in seen))

    def test_not_started_is_pending(self):
        with mock.patch.object(probe.requests, "post", return_value=FakeResponse(200, json_data={"access_token": "token"})), mock.patch.object(
            probe.requests,
            "request",
            side_effect=[
                FakeResponse(404, text="404"),
                FakeResponse(
                    200,
                    json_data={
                        "state": "NOT_STARTED",
                        "kind": "SEARCH_PROMO_ORGANISATION_ORDERS",
                        "request": {"campaignId": "0", "from": "2026-05-05T21:00:00Z", "to": "2026-05-06T20:59:59Z"},
                    },
                ),
                FakeResponse(
                    200,
                    json_data={
                        "state": "OK",
                        "kind": "SEARCH_PROMO_ORGANISATION_ORDERS",
                        "request": {"campaignId": "0", "from": "2026-05-05T21:00:00Z", "to": "2026-05-06T20:59:59Z"},
                    },
                ),
                FakeResponse(404, text="404"),
                FakeResponse(200, text=CSV_TEXT, headers={"Content-Type": "text/csv; charset=utf-8"}),
            ],
        ), mock.patch.object(probe.time, "sleep", side_effect=lambda *_: None):
            summary = probe.run(
                "u1",
                "4471285",
                "2026-05-06",
                True,
                3,
                0,
                allow_organisation_wide_date_valid=True,
            )
        self.assertEqual(summary["result"], "downloaded_and_dry_parsed")
        self.assertEqual(summary["status"]["final_report_status"], "OK")
        self.assertFalse(summary["request_echo_validation"]["campaign_id_exact_match"])

    def test_old_uuid_default_1970_request_echo_is_marked_invalid_and_no_submit(self):
        def fake_post(*args, **kwargs):
            return FakeResponse(200, json_data={"access_token": "token"})

        def fake_request(method, url, **kwargs):
            self.assertNotIn("/api/client/statistics/json", url)
            self.assertFalse(method == "POST" and url.endswith("/api/client/statistics"))
            if url.endswith("/api/client/statistic/u1"):
                return FakeResponse(404, text="404")
            if url.endswith("/api/client/statistics/u1"):
                return FakeResponse(
                    200,
                    json_data={
                        "state": "OK",
                        "kind": "SEARCH_PROMO_ORGANISATION_ORDERS",
                        "request": {
                            "campaignId": "0",
                            "campaigns": [],
                            "from": "1970-01-01T00:00:00Z",
                            "to": "1970-01-01T23:59:59Z",
                            "groupBy": "NO_GROUP_BY",
                            "objects": [],
                            "dateFrom": "",
                            "dateTo": "",
                            "attributionDays": "0",
                        },
                    },
                )
            if url.endswith("/api/client/statistic/report"):
                return FakeResponse(404, text="404")
            if url.endswith("/api/client/statistics/report"):
                return FakeResponse(200, text=HEADER_ONLY_CSV_TEXT, headers={"Content-Type": "text/csv; charset=utf-8"})
            raise AssertionError(url)

        with mock.patch.object(probe.requests, "post", side_effect=fake_post), mock.patch.object(
            probe.requests, "request", side_effect=fake_request
        ):
            summary = probe.run("u1", "4471285", "2026-05-06", True, 2, 0)

        self.assertEqual(summary["result"], "uuid_created_but_request_echo_invalid")
        self.assertFalse(summary["request_echo_validation"]["valid"])
        self.assertEqual(summary["request_echo_validation"]["actual_campaignId"], "0")
        self.assertEqual(summary["request_echo_validation"]["campaign_scope"], "campaign_unbound_or_invalid")
        self.assertNotIn("download", summary)

    def test_organisation_wide_uuid_can_download_when_date_valid_and_flag_enabled(self):
        def fake_post(*args, **kwargs):
            return FakeResponse(200, json_data={"access_token": "token"})

        def fake_request(method, url, **kwargs):
            if url.endswith("/api/client/statistic/u1"):
                return FakeResponse(404, text="404")
            if url.endswith("/api/client/statistics/u1"):
                return FakeResponse(
                    200,
                    json_data={
                        "state": "OK",
                        "kind": "SEARCH_PROMO_ORGANISATION_ORDERS",
                        "request": {
                            "campaignId": "0",
                            "campaigns": [],
                            "from": "2026-05-05T21:00:00Z",
                            "to": "2026-05-06T20:59:59Z",
                            "dateFrom": "",
                            "dateTo": "",
                        },
                    },
                )
            if url.endswith("/api/client/statistic/report"):
                return FakeResponse(404, text="404")
            if url.endswith("/api/client/statistics/report"):
                return FakeResponse(200, text=CSV_TEXT, headers={"Content-Type": "text/csv; charset=utf-8"})
            raise AssertionError(url)

        with mock.patch.object(probe.requests, "post", side_effect=fake_post), mock.patch.object(
            probe.requests, "request", side_effect=fake_request
        ):
            summary = probe.run(
                "u1",
                "4471285",
                "2026-05-06",
                True,
                2,
                0,
                allow_organisation_wide_date_valid=True,
            )

        self.assertEqual(summary["result"], "downloaded_and_dry_parsed")
        self.assertEqual(summary["classification"]["campaign_scope"], "organisation_wide_campaign_unbound")
        self.assertFalse(summary["classification"]["campaign_id_exact_match"])
        self.assertEqual(summary["dry_parse"]["row_count_raw"], 4)
        self.assertEqual(summary["dry_parse"]["data_row_count"], 3)
        self.assertEqual(summary["dry_parse"]["total_row_count"], 1)
        self.assertAlmostEqual(summary["dry_parse"]["spend_sum"], 25841.80, places=2)
        self.assertAlmostEqual(summary["dry_parse"]["spend_sum_total_rows"], 25841.80, places=2)
        self.assertAlmostEqual(summary["dry_parse"]["spend_sum_including_total_rows"], 51683.60, places=2)
        self.assertTrue(summary["dry_parse"]["close_to_expected"])


if __name__ == "__main__":
    unittest.main()
