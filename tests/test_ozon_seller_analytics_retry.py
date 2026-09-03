"""Два разных 429: Seller повторяем, Performance — нет.

Ночь на 2026-09-03: первый же запрос страницы Seller Analytics вернул
429 code 8 («request rate limit per second»), у загрузчика не было ретраев
вовсе, шаг упал с кодом 1 и унёс витрину за весь день. Накануне тот же запрос
отдал 200 с первой попытки — отказ был транзиентным.

Контраст важен не меньше самого ретрая: правило CLAUDE.md «стоп на первом 429,
без retry storm» относится к Performance API, где 429 = исчерпанная суточная
квота и повтор бессмыслен. Перенести его на Seller API значит терять витрину
из-за секундного троттлинга; перенести обратное — устроить retry storm по
исчерпанной квоте.
"""

import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as perf
import loaders.ozon_sku_total_analytics_loader as seller
import tests.test_ozon_performance_cpc_recovery as harness


def _response(status, body=None, headers=None, text=""):
    resp = mock.Mock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.text = text
    if body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = body
    return resp


OK = {"result": {"data": []}}
RATE_LIMIT = {"code": 8, "message": "You have reached request rate limit per second"}


class SellerRetryTests(unittest.TestCase):
    def _call(self, responses):
        slept = []
        with mock.patch.object(seller.requests, "post", side_effect=responses) as post:
            data, _payload = seller.request_page(
                "2026-09-02", "2026-09-02", 1000, 0, sleep_fn=slept.append
            )
        return data, slept, post

    def test_rate_limit_per_second_is_retried_and_succeeds(self):
        data, slept, post = self._call([
            _response(429, RATE_LIMIT),
            _response(429, RATE_LIMIT),
            _response(200, OK),
        ])
        self.assertEqual(data, OK)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(slept, [1, 2])   # короткий backoff, секундный лимит

    def test_server_error_is_retried(self):
        data, slept, post = self._call([
            _response(500, None, text="upstream"),
            _response(200, OK),
        ])
        self.assertEqual(data, OK)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(slept, [1])

    def test_retry_after_header_wins_over_backoff(self):
        _data, slept, _post = self._call([
            _response(429, RATE_LIMIT, headers={"Retry-After": "5"}),
            _response(200, OK),
        ])
        self.assertEqual(slept, [5])

    def test_retry_after_is_capped(self):
        _data, slept, _post = self._call([
            _response(429, RATE_LIMIT, headers={"Retry-After": "3600"}),
            _response(200, OK),
        ])
        self.assertEqual(slept, [seller.SELLER_RETRY_CAP_SLEEP_SECONDS])

    def test_exhausted_attempts_still_raise(self):
        """Молча проглотить нельзя: шаг кормит органику и KPI."""
        responses = [_response(429, RATE_LIMIT)] * seller.SELLER_RETRY_MAX_ATTEMPTS
        slept = []
        with mock.patch.object(seller.requests, "post", side_effect=responses) as post:
            with self.assertRaises(RuntimeError) as ctx:
                seller.request_page("2026-09-02", "2026-09-02", 1000, 0, sleep_fn=slept.append)
        self.assertEqual(post.call_count, seller.SELLER_RETRY_MAX_ATTEMPTS)
        self.assertIn("rate_limit_per_second", str(ctx.exception))
        self.assertIn(f"attempts={seller.SELLER_RETRY_MAX_ATTEMPTS}", str(ctx.exception))

    def test_429_with_another_code_is_not_retried(self):
        """Неизвестный отказ не превращается в серию повторов."""
        slept = []
        with mock.patch.object(seller.requests, "post",
                               side_effect=[_response(429, {"code": 3, "message": "daily"})]) as post:
            with self.assertRaises(RuntimeError) as ctx:
                seller.request_page("2026-09-02", "2026-09-02", 1000, 0, sleep_fn=slept.append)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(slept, [])
        self.assertIn("429_code_3", str(ctx.exception))

    def test_client_error_is_not_retried(self):
        slept = []
        with mock.patch.object(seller.requests, "post",
                               side_effect=[_response(400, {"code": 1, "message": "bad"})]) as post:
            with self.assertRaises(RuntimeError):
                seller.request_page("2026-09-02", "2026-09-02", 1000, 0, sleep_fn=slept.append)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(slept, [])

    def test_first_attempt_success_does_not_sleep(self):
        data, slept, post = self._call([_response(200, OK)])
        self.assertEqual(data, OK)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(slept, [])


class PerformanceStillFailsFastTests(unittest.TestCase):
    """Контраст: на Performance API 429 по-прежнему останавливает, а не повторяет."""

    def test_statistics_json_profile_fails_fast_on_429(self):
        self.assertTrue(perf.REQUEST_PROFILES["statistics_json"]["fail_fast_on_429"])

    def test_daily_quota_429_stops_the_recovery_run_on_first_refusal(self):
        campaigns = [harness._sample_campaign(str(24375352 + i)) for i in range(3)]
        seen = []

        def fetcher(client, campaign_batch, date_from, date_to, group_by, usage_context=None):
            seen.append(list(campaign_batch))
            raise perf.RateLimitPending(
                endpoint="/api/client/statistics/json",
                retry_after_seconds=355,
                cooldown_until="2026-09-04T00:05:00Z",
                attempt=1,
                response_kind="daily_quota_exhausted",
                raw_error_preview="Превышен дневной лимит запросов (максимум 2000)",
            )

        db_client = harness._FakeDbClient({"marketplace_expenses": [], "ozon_daily_sku_ad_attribution": []})
        with mock.patch.object(perf, "save_rows"), mock.patch.object(perf, "save_ad_attribution_rows"):
            summary = perf.run_cpc_recovery_mode(
                client=harness._FakeClient(progress_map={}),
                target_date="2026-09-02",
                group_by="DATE",
                requested_batch_size=1,
                max_stats_campaigns=1800,
                dry_run=True,
                write=False,
                approve_write=False,
                ignore_stale_progress_for_date_only=True,
                no_write=True,
                db_client=db_client,
                campaigns=campaigns,
                fetch_batch_fn=fetcher,
            )

        # Один заход и стоп — никакого перебора остальных батчей.
        self.assertEqual(len(seen), 1)
        self.assertEqual(summary["status"], "quota_limited_before_refetch")
        self.assertEqual(summary["statistics_json_submit_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
