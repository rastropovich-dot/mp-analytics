import unittest
from unittest import mock

import scripts.ozon_search_promo_orders_controlled_submit as submit_probe


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


class ControlledSubmitTests(unittest.TestCase):
    def test_without_live_dry_run_makes_no_http_calls(self):
        with mock.patch.object(submit_probe.report_probe, "ensure_auth", side_effect=AssertionError("no auth")), mock.patch.object(
            submit_probe.requests, "post", side_effect=AssertionError("no post")
        ), mock.patch.object(submit_probe.report_probe.requests, "request", side_effect=AssertionError("no get")):
            with self.assertRaises(RuntimeError):
                submit_probe.run("4471285", "2026-05-06", "campaignId_number_from_to", False, 1, 0)

    def test_live_dry_run_uses_exact_submit_endpoint_and_only_candidate2(self):
        submit_calls = []

        def fake_auth():
            return "token"

        def fake_post(url, **kwargs):
            submit_calls.append((url, kwargs.get("json")))
            return FakeResponse(400, json_data={"error": "bad"})

        with mock.patch.object(submit_probe.report_probe, "ensure_auth", side_effect=fake_auth), mock.patch.object(
            submit_probe.requests, "post", side_effect=fake_post
        ), mock.patch.object(submit_probe.report_probe, "poll_status", side_effect=AssertionError("no poll")), mock.patch.object(
            submit_probe.report_probe, "download_report", side_effect=AssertionError("no download")
        ):
            summary = submit_probe.run("4471285", "2026-05-06", "campaignId_number_from_to", True, 1, 0)

        self.assertEqual(summary["submit"]["endpoint"], "/api/client/statistic/orders/generate")
        self.assertEqual(len(submit_calls), 1)
        self.assertEqual(
            submit_calls[0][1],
            {
                "campaignId": 4471285,
                "from": "2026-05-05T21:00:00Z",
                "to": "2026-05-06T20:59:59Z",
            },
        )
        self.assertEqual(summary["guardrails"]["candidates_tried"], ["campaignId_number_from_to"])
        self.assertFalse(summary["guardrails"]["candidate_1_retried"])
        self.assertTrue(summary["guardrails"]["candidate_2_tried"])
        self.assertFalse(summary["guardrails"]["candidate_3_tried"])
        self.assertFalse(summary["guardrails"]["candidate_4_tried"])
        self.assertFalse(summary["guardrails"]["products_report_tried"])

    def test_no_statistics_json_or_general_submit_called(self):
        def fake_auth():
            return "token"

        def fake_post(url, **kwargs):
            self.assertNotIn("/api/client/statistics/json", url)
            self.assertFalse(url.endswith("/api/client/statistics"))
            return FakeResponse(400, json_data={"error": "bad"})

        with mock.patch.object(submit_probe.report_probe, "ensure_auth", side_effect=fake_auth), mock.patch.object(
            submit_probe.requests, "post", side_effect=fake_post
        ):
            submit_probe.run("4471285", "2026-05-06", "campaignId_number_from_to", True, 1, 0)

    def test_submit_400_no_poll_or_download(self):
        with mock.patch.object(submit_probe.report_probe, "ensure_auth", return_value="token"), mock.patch.object(
            submit_probe.requests,
            "post",
            return_value=FakeResponse(400, json_data={"error": "bad payload"}),
        ), mock.patch.object(submit_probe.report_probe, "poll_status", side_effect=AssertionError("no poll")), mock.patch.object(
            submit_probe.report_probe, "download_report", side_effect=AssertionError("no download")
        ):
            summary = submit_probe.run("4471285", "2026-05-06", "campaignId_number_from_to", True, 1, 0)
        self.assertEqual(summary["result"], "submit_http_400")

    def test_submit_200_uuid_then_status_poll(self):
        with mock.patch.object(submit_probe.report_probe, "ensure_auth", return_value="token"), mock.patch.object(
            submit_probe.requests,
            "post",
            return_value=FakeResponse(200, json_data={"UUID": "u1"}),
        ), mock.patch.object(
            submit_probe.report_probe,
            "poll_status",
            return_value={
                "result": "ready",
                "endpoint_used": "/api/client/statistics/u1",
                "final_http_status": 200,
                "final_report_status": "OK",
                "attempts": [],
                "status_body": {
                    "kind": "SEARCH_PROMO_ORGANISATION_ORDERS",
                    "request": {
                        "campaignId": 4471285,
                        "from": "2026-05-05T21:00:00Z",
                        "to": "2026-05-06T20:59:59Z",
                    },
                },
            },
        ) as poll_mock, mock.patch.object(
            submit_probe.report_probe,
            "download_report",
            return_value={
                "result": "downloaded",
                "download": {
                    "endpoint_used": "/api/client/statistics/report",
                    "http_status": 200,
                    "content_type": "text/csv",
                    "bytes": 100,
                },
                "dry_parse": {"format": "csv", "row_count": 0},
                "attempts": [],
            },
        ):
            summary = submit_probe.run("4471285", "2026-05-06", "campaignId_number_from_to", True, 1, 0)
        self.assertTrue(poll_mock.called)
        self.assertEqual(summary["submit"]["uuid"], "u1")

    def test_status_echo_valid_then_download(self):
        with mock.patch.object(submit_probe.report_probe, "ensure_auth", return_value="token"), mock.patch.object(
            submit_probe.requests,
            "post",
            return_value=FakeResponse(200, json_data={"UUID": "u1"}),
        ), mock.patch.object(
            submit_probe.report_probe,
            "poll_status",
            return_value={
                "result": "ready",
                "endpoint_used": "/api/client/statistics/u1",
                "final_http_status": 200,
                "final_report_status": "OK",
                "attempts": [],
                "status_body": {
                    "kind": "SEARCH_PROMO_ORGANISATION_ORDERS",
                    "request": {
                        "campaignId": 4471285,
                        "from": "2026-05-05T21:00:00Z",
                        "to": "2026-05-06T20:59:59Z",
                    },
                },
            },
        ), mock.patch.object(
            submit_probe.report_probe,
            "download_report",
            return_value={
                "result": "downloaded",
                "download": {
                    "endpoint_used": "/api/client/statistics/report",
                    "http_status": 200,
                    "content_type": "text/csv",
                    "bytes": 100,
                },
                "dry_parse": {
                    "format": "csv",
                    "columns": ["Дата", "SKU", "Расход, ₽"],
                    "row_count": 1,
                    "preview_rows_sanitized": [],
                    "candidate_spend_columns": ["Расход, ₽"],
                    "candidate_sku_columns": ["SKU"],
                    "candidate_order_columns": [],
                    "candidate_date_columns": ["Дата"],
                    "spend_sum": 25841.8,
                    "expected_missing_selected_cpo": 25841.8,
                    "absolute_diff": 0.0,
                    "close_to_expected": True,
                    "preamble_lines": [],
                },
                "attempts": [],
            },
        ):
            summary = submit_probe.run("4471285", "2026-05-06", "campaignId_number_from_to", True, 1, 0)
        self.assertEqual(summary["result"], "downloaded_and_dry_parsed")
        self.assertTrue(summary["request_echo_validation"]["valid"])
        self.assertEqual(summary["request_echo_validation"]["actual_campaignId"], "4471285")

    def test_status_echo_invalid_no_download(self):
        with mock.patch.object(submit_probe.report_probe, "ensure_auth", return_value="token"), mock.patch.object(
            submit_probe.requests,
            "post",
            return_value=FakeResponse(200, json_data={"UUID": "u1"}),
        ), mock.patch.object(
            submit_probe.report_probe,
            "poll_status",
            return_value={
                "result": "ready",
                "endpoint_used": "/api/client/statistics/u1",
                "final_http_status": 200,
                "final_report_status": "OK",
                "attempts": [],
                "status_body": {
                    "kind": "SEARCH_PROMO_ORGANISATION_ORDERS",
                    "request": {
                        "campaignId": "0",
                        "from": "2026-05-05T21:00:00Z",
                        "to": "2026-05-06T20:59:59Z",
                    },
                },
            },
        ), mock.patch.object(submit_probe.report_probe, "download_report", side_effect=AssertionError("no download")):
            summary = submit_probe.run("4471285", "2026-05-06", "campaignId_number_from_to", True, 1, 0)
        self.assertEqual(summary["result"], "uuid_created_but_request_echo_invalid")
        self.assertFalse(summary["request_echo_validation"]["valid"])

    def test_download_plural_200_csv(self):
        with mock.patch.object(submit_probe.report_probe, "ensure_auth", return_value="token"), mock.patch.object(
            submit_probe.requests,
            "post",
            return_value=FakeResponse(200, json_data={"UUID": "u1"}),
        ), mock.patch.object(
            submit_probe.report_probe,
            "poll_status",
            return_value={
                "result": "ready",
                "endpoint_used": "/api/client/statistics/u1",
                "final_http_status": 200,
                "final_report_status": "OK",
                "attempts": [],
                "status_body": {
                    "kind": "SEARCH_PROMO_ORGANISATION_ORDERS",
                    "request": {
                        "campaignId": 4471285,
                        "from": "2026-05-05T21:00:00Z",
                        "to": "2026-05-06T20:59:59Z",
                    },
                },
            },
        ), mock.patch.object(
            submit_probe.report_probe,
            "download_report",
            return_value={
                "result": "downloaded",
                "download": {
                    "endpoint_used": "/api/client/statistics/report",
                    "http_status": 200,
                    "content_type": "text/csv",
                    "bytes": 100,
                },
                "dry_parse": {
                    "format": "csv",
                    "row_count": 1,
                    "columns": ["Дата", "SKU", "Расход, ₽"],
                    "preview_rows_sanitized": [],
                    "candidate_spend_columns": ["Расход, ₽"],
                    "candidate_sku_columns": ["SKU"],
                    "candidate_order_columns": [],
                    "candidate_date_columns": ["Дата"],
                    "spend_sum": 25841.8,
                    "expected_missing_selected_cpo": 25841.8,
                    "absolute_diff": 0.0,
                    "close_to_expected": True,
                    "preamble_lines": [],
                },
                "attempts": [],
            },
        ):
            summary = submit_probe.run("4471285", "2026-05-06", "campaignId_number_from_to", True, 1, 0)
        self.assertEqual(summary["download"]["endpoint_used"], "/api/client/statistics/report")
        self.assertEqual(summary["dry_parse"]["spend_sum"], 25841.8)


if __name__ == "__main__":
    unittest.main()
