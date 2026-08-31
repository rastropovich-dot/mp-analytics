import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader


class _Recorder:
    """Клиент-заглушка: копит ledger-события вместо записи в БД."""

    account_signature = "acct_test"

    def __init__(self):
        self.events = []
        self.statistics_poll_tally = {}

    poll_tally_store = loader.OzonPerformanceClient.poll_tally_store
    begin_statistics_poll_tally = loader.OzonPerformanceClient.begin_statistics_poll_tally
    tally_statistics_poll_request = loader.OzonPerformanceClient.tally_statistics_poll_request
    flush_statistics_poll_tally = loader.OzonPerformanceClient.flush_statistics_poll_tally

    def record_statistics_json_usage_event(self, event):
        self.events.append(event)

    def ensure_token(self):
        return "token"


class ClassifyStatisticsUsageRequestTest(unittest.TestCase):
    def test_statistics_family_is_split_by_kind(self):
        self.assertEqual(
            loader.classify_statistics_usage_request("POST", "/api/client/statistics/json"), "submit"
        )
        self.assertEqual(
            loader.classify_statistics_usage_request("GET", "/api/client/statistics/report"), "download"
        )
        self.assertEqual(
            loader.classify_statistics_usage_request("GET", "/api/client/statistics/abc-123"), "poll"
        )

    def test_non_statistics_endpoints_are_not_tracked(self):
        for method, endpoint in [
            ("GET", "/api/client/campaign"),
            ("POST", "/api/client/token"),
            ("GET", "/api/client/statistics/all_sku_promo/orders/generate"),
        ]:
            with self.subTest(endpoint=endpoint):
                self.assertIsNone(loader.classify_statistics_usage_request(method, endpoint))


class PollTallyTest(unittest.TestCase):
    def test_polls_are_aggregated_into_one_row(self):
        client = _Recorder()
        client.begin_statistics_poll_tally("uuid-1")
        for _ in range(321):
            client.tally_statistics_poll_request("uuid-1")
        client.flush_statistics_poll_tally("uuid-1", {"response_kind": "poll_success", "http_status": 200})

        self.assertEqual(len(client.events), 1, "321 опрос должен дать одну запись, а не 321")
        event = client.events[0]
        self.assertEqual(event["request_kind"], "poll")
        self.assertEqual(event["request_count"], 321)
        self.assertEqual(event["report_uuid"], "uuid-1")

    def test_poll_rows_never_carry_campaign_units(self):
        # Иначе раздуется daily_budget_used_today и guard "used > 1500"
        # молча выключит recovery worker.
        client = _Recorder()
        client.begin_statistics_poll_tally("uuid-1")
        client.tally_statistics_poll_request("uuid-1")
        client.flush_statistics_poll_tally("uuid-1", None)
        self.assertEqual(client.events[0]["campaign_units"], 0)

    def test_nothing_is_written_when_no_poll_happened(self):
        client = _Recorder()
        client.begin_statistics_poll_tally("uuid-1")
        client.flush_statistics_poll_tally("uuid-1", None)
        self.assertEqual(client.events, [])

    def test_429_on_poll_is_recorded_explicitly(self):
        client = _Recorder()
        client.begin_statistics_poll_tally("uuid-1")
        client.tally_statistics_poll_request("uuid-1")
        client.tally_statistics_poll_request("uuid-1", "http_429")
        client.flush_statistics_poll_tally("uuid-1", {"response_kind": "poll_success", "http_status": 200})
        self.assertEqual(client.events[0]["response_kind"], "poll_429")

    def test_daily_quota_on_poll_outranks_plain_429(self):
        client = _Recorder()
        client.begin_statistics_poll_tally("uuid-1")
        client.tally_statistics_poll_request("uuid-1")
        client.tally_statistics_poll_request("uuid-1", "http_429")
        client.tally_statistics_poll_request("uuid-1", "daily_quota_exhausted")
        client.flush_statistics_poll_tally("uuid-1", {"response_kind": "poll_429", "http_status": 429})
        self.assertEqual(client.events[0]["response_kind"], "poll_daily_quota_exhausted")


class WaitStatisticsFlushTest(unittest.TestCase):
    def _client(self, responses):
        client = mock.Mock()
        client.account_signature = "acct_test"
        client.statistics_poll_tally = {}
        client.poll_tally_store = lambda: loader.OzonPerformanceClient.poll_tally_store(client)
        client.begin_statistics_poll_tally = lambda u: loader.OzonPerformanceClient.begin_statistics_poll_tally(client, u)
        client.tally_statistics_poll_request = lambda u, f="requests": loader.OzonPerformanceClient.tally_statistics_poll_request(client, u, f)
        client.flush_statistics_poll_tally = lambda u, o=None, c=None: loader.OzonPerformanceClient.flush_statistics_poll_tally(client, u, o, c)
        client.recorded = []
        client.record_statistics_json_usage_event = client.recorded.append

        def fake_request(method, endpoint, **kwargs):
            client.tally_statistics_poll_request(endpoint.rsplit("/", 1)[-1])
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return mock.Mock(json=mock.Mock(return_value=item), status_code=200)

        client.request = fake_request
        client.forget_jobs_by_uuid = mock.Mock()
        return client

    def test_count_is_written_on_success(self):
        client = self._client([{"state": "PROCESSING"}, {"state": "PROCESSING"}, {"state": "OK"}])
        with mock.patch.object(loader.time, "sleep"):
            loader.OzonPerformanceClient.wait_statistics(client, "uuid-1", poll_profile="statistics_json")
        self.assertEqual(len(client.recorded), 1)
        self.assertEqual(client.recorded[0]["request_count"], 3)
        self.assertEqual(client.recorded[0]["response_kind"], "poll_success")

    def test_count_survives_a_429_raised_mid_wait(self):
        exc = loader.RateLimitPending(
            endpoint="/api/client/statistics/uuid-1",
            retry_after_seconds=60,
            cooldown_until=None,
            attempt=1,
            response_kind="daily_quota_exhausted",
        )
        client = self._client([{"state": "PROCESSING"}, exc])
        with mock.patch.object(loader.time, "sleep"), self.assertRaises(loader.RateLimitPending):
            loader.OzonPerformanceClient.wait_statistics(client, "uuid-1", poll_profile="statistics_json")
        self.assertEqual(len(client.recorded), 1, "счёт должен сохраняться и при исключении")
        self.assertEqual(client.recorded[0]["request_count"], 2)
        self.assertEqual(client.recorded[0]["response_kind"], "poll_daily_quota_exhausted")

    def test_count_survives_report_error_state(self):
        client = self._client([{"state": "PROCESSING"}, {"state": "ERROR"}])
        with mock.patch.object(loader.time, "sleep"), self.assertRaises(loader.OzonReportErrorStateError):
            loader.OzonPerformanceClient.wait_statistics(client, "uuid-1", poll_profile="statistics_json")
        self.assertEqual(client.recorded[0]["request_count"], 2)
        self.assertEqual(client.recorded[0]["response_kind"], "poll_report_error")


class BudgetIsolationTest(unittest.TestCase):
    def test_poll_and_download_rows_stay_out_of_the_unit_budget(self):
        events = [
            {"account_signature": "a", "load_date": "2026-08-31", "response_kind": "success",
             "request_kind": "submit", "campaign_units": 10},
            {"account_signature": "a", "load_date": "2026-08-31", "response_kind": "poll_success",
             "request_kind": "poll", "request_count": 321, "campaign_units": 0},
            {"account_signature": "a", "load_date": "2026-08-31", "response_kind": "download_success",
             "request_kind": "download", "request_count": 1, "campaign_units": 0},
        ]
        summary = loader.summarize_statistics_json_usage_budget_from_events(events, "2026-08-31", "a")
        self.assertEqual(summary["daily_budget_used_today"], 10)
        self.assertEqual(summary["usage_event_count"], 1)

    def test_legacy_rows_without_request_kind_still_count_as_submit(self):
        events = [
            {"account_signature": "a", "load_date": "2026-08-31", "response_kind": "success",
             "campaign_units": 10},
        ]
        summary = loader.summarize_statistics_json_usage_budget_from_events(events, "2026-08-31", "a")
        self.assertEqual(summary["daily_budget_used_today"], 10)


class StatelessRecoveryPathTest(unittest.TestCase):
    def test_stateless_submit_poll_and_download_all_reach_the_ledger(self):
        client = _Recorder()
        poll_states = [{"state": "PROCESSING"}, {"state": "OK"}]

        def fake_request(method, url, headers=None, timeout=None, **kwargs):
            if url.endswith("/api/client/statistics/json"):
                return mock.Mock(status_code=200, json=mock.Mock(return_value={"UUID": "uuid-9"}),
                                 raise_for_status=mock.Mock())
            if url.endswith("/api/client/statistics/report"):
                return mock.Mock(status_code=200, text='{"1": {}}',
                                 json=mock.Mock(return_value={"1": {}}), raise_for_status=mock.Mock())
            return mock.Mock(status_code=200, json=mock.Mock(return_value=poll_states.pop(0)),
                             raise_for_status=mock.Mock())

        with mock.patch.object(loader.requests, "request", side_effect=fake_request), \
                mock.patch.object(loader.time, "sleep"):
            loader.fetch_cpc_recovery_batch_stateless(client, ["1"], "2026-08-30", "2026-08-30", "DATE")

        by_kind = {e["request_kind"]: e for e in client.events}
        self.assertEqual(set(by_kind), {"submit", "poll", "download"},
                         "весь трафик recovery-воркера должен попадать в ledger")
        self.assertEqual(by_kind["poll"]["request_count"], 2)
        self.assertEqual(by_kind["submit"]["request_count"], 1)
        self.assertEqual(by_kind["download"]["request_count"], 1)


if __name__ == "__main__":
    unittest.main()
