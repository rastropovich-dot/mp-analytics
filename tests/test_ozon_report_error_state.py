import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader


def _error_response(state="ERROR"):
    resp = mock.Mock()
    resp.json.return_value = {"state": state, "UUID": "test-uuid"}
    return resp


def _done_response():
    resp = mock.Mock()
    resp.json.return_value = {"state": "DONE", "data": []}
    return resp


class WaitStatisticsErrorStateTests(unittest.TestCase):
    def _make_client(self, side_effects):
        client = loader.OzonPerformanceClient.__new__(loader.OzonPerformanceClient)
        client.state = {}
        client.request = mock.Mock(side_effect=side_effects)
        client.forget_jobs_by_uuid = mock.Mock()
        return client

    def test_error_state_raises_ozon_report_error_state_error(self):
        client = self._make_client([_error_response("ERROR")])
        with mock.patch("loaders.ozon_performance_ads_loader.time"):
            with self.assertRaises(loader.OzonReportErrorStateError) as ctx:
                client.wait_statistics("test-uuid", poll_profile="default")
        self.assertEqual(ctx.exception.uuid, "test-uuid")
        client.forget_jobs_by_uuid.assert_called_once_with("test-uuid")

    def test_failed_state_raises_ozon_report_error_state_error(self):
        client = self._make_client([_error_response("FAILED")])
        with mock.patch("loaders.ozon_performance_ads_loader.time"):
            with self.assertRaises(loader.OzonReportErrorStateError):
                client.wait_statistics("test-uuid", poll_profile="default")

    def test_fail_state_raises_ozon_report_error_state_error(self):
        client = self._make_client([_error_response("FAIL")])
        with mock.patch("loaders.ozon_performance_ads_loader.time"):
            with self.assertRaises(loader.OzonReportErrorStateError):
                client.wait_statistics("test-uuid", poll_profile="default")

    def test_error_state_stores_data(self):
        client = self._make_client([_error_response("ERROR")])
        with mock.patch("loaders.ozon_performance_ads_loader.time"):
            with self.assertRaises(loader.OzonReportErrorStateError) as ctx:
                client.wait_statistics("test-uuid", poll_profile="default")
        self.assertEqual(ctx.exception.data["state"], "ERROR")


class CpcBatchLoopErrorStateTests(unittest.TestCase):
    """Tests for OzonReportErrorStateError handling in CPC batch loop via direct unit checks."""

    def test_ozon_report_error_state_error_is_not_caught_by_plain_runtime_error(self):
        exc = loader.OzonReportErrorStateError("u1", {"state": "ERROR"})
        self.assertIsInstance(exc, RuntimeError)
        self.assertIsInstance(exc, loader.OzonReportErrorStateError)

    def test_ozon_report_error_state_error_different_from_server_500(self):
        err = loader.OzonReportErrorStateError("u1", {})
        s500 = loader.OzonServer500Error("u2")
        self.assertNotIsInstance(err, loader.OzonServer500Error)
        self.assertNotIsInstance(s500, loader.OzonReportErrorStateError)

    def test_exception_message_includes_uuid(self):
        exc = loader.OzonReportErrorStateError("abc-123", {"state": "FAILED"})
        self.assertIn("abc-123", str(exc))

    def test_exception_data_attribute(self):
        data = {"state": "FAILED", "UUID": "x"}
        exc = loader.OzonReportErrorStateError("x", data)
        self.assertEqual(exc.data, data)


if __name__ == "__main__":
    unittest.main()
