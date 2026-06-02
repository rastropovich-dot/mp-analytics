import unittest
from unittest import mock

import requests

import loaders.ozon_performance_ads_loader as loader


def _make_http_error(status_code):
    response = mock.Mock()
    response.status_code = status_code
    exc = requests.HTTPError(response=response)
    return exc


class WaitStatistics500RetryTests(unittest.TestCase):
    def _make_client(self, request_side_effects):
        client = loader.OzonPerformanceClient.__new__(loader.OzonPerformanceClient)
        client.state = {}
        client.request = mock.Mock(side_effect=request_side_effects)
        client.forget_jobs_by_uuid = mock.Mock()
        return client

    def _done_response(self):
        resp = mock.Mock()
        resp.json.return_value = {"state": "DONE", "data": []}
        return resp

    def test_500_retried_twice_then_raises_ozon_server_500_error(self):
        sleeps = []
        client = self._make_client([
            _make_http_error(500),
            _make_http_error(500),
            _make_http_error(500),
        ])
        with mock.patch("loaders.ozon_performance_ads_loader.time") as mock_time:
            mock_time.sleep = lambda s: sleeps.append(s)
            with self.assertRaises(loader.OzonServer500Error):
                client.wait_statistics("test-uuid", poll_profile="default")

        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(s == 30 for s in sleeps))

    def test_500_then_success_returns_data(self):
        sleeps = []
        client = self._make_client([
            _make_http_error(500),
            _make_http_error(500),
            self._done_response(),
        ])
        with mock.patch("loaders.ozon_performance_ads_loader.time") as mock_time:
            mock_time.sleep = lambda s: sleeps.append(s)
            result = client.wait_statistics("test-uuid", poll_profile="default")

        self.assertEqual(result["state"], "DONE")
        self.assertEqual(len(sleeps), 2)

    def test_non_500_http_error_propagates_immediately(self):
        client = self._make_client([_make_http_error(403)])
        with self.assertRaises(requests.HTTPError) as ctx:
            client.wait_statistics("test-uuid", poll_profile="default")
        self.assertEqual(ctx.exception.response.status_code, 403)

    def test_single_500_then_success_returns_data(self):
        sleeps = []
        client = self._make_client([
            _make_http_error(500),
            self._done_response(),
        ])
        with mock.patch("loaders.ozon_performance_ads_loader.time") as mock_time:
            mock_time.sleep = lambda s: sleeps.append(s)
            result = client.wait_statistics("test-uuid", poll_profile="default")

        self.assertEqual(result["state"], "DONE")
        self.assertEqual(len([s for s in sleeps if s == 30]), 1)


if __name__ == "__main__":
    unittest.main()
