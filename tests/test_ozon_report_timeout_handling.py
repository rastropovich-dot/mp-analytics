"""Часть 5: зависший отчёт не роняет прогон и не путается с 429.

Инцидент 2026-09-02: на батче 72 бэкфилла 2026-08-17 отчёт
7257e67e-af50-4f6b-9312-738c899261c7 не собрался, wait_statistics бросил
TimeoutError через 19 мин 20 с, исключение прошло насквозь и уронило воркер,
а с ним и всю витрину за 2026-09-01.

Отдельно проверяем, что таймаут НЕ ведёт себя как 429: правило «стоп до сброса
окна, без повторов» относится к исчерпанию лимита, а таймаут — это наш
собственный предел ожидания, и повторная отправка того же отчёта тоже стоит
квоты.
"""

import unittest

from unittest import mock

import loaders.ozon_performance_ads_loader as loader
import tests.test_ozon_performance_cpc_recovery as harness


class ReportTimeoutTypeTests(unittest.TestCase):
    def test_timeout_error_is_a_timeout_not_a_rate_limit(self):
        exc = loader.OzonReportTimeoutError("7257e67e", attempts=20, waited_seconds=1125)
        self.assertIsInstance(exc, TimeoutError)
        self.assertNotIsInstance(exc, loader.RateLimitPending)

    def test_timeout_error_carries_the_uuid_and_circumstances(self):
        exc = loader.OzonReportTimeoutError("7257e67e", attempts=20, waited_seconds=1125)
        self.assertEqual(exc.uuid, "7257e67e")
        self.assertEqual(exc.attempts, 20)
        self.assertEqual(exc.waited_seconds, 1125)
        self.assertIn("7257e67e", str(exc))


class PollBudgetTests(unittest.TestCase):
    """19 минут — это наш предел, а не сигнал Ozon. Фиксируем бюджет числом."""

    def test_statistics_json_budget_matches_the_observed_timeout(self):
        budget = loader.poll_profile_wait_budget_seconds("statistics_json")
        # 15 + 30 + 18 x 60 = 1125 с = 18 мин 45 с сна, плюс 20 HTTP-обходов.
        self.assertEqual(budget, 1125)

    def test_profile_shape_is_unchanged(self):
        profile = loader.POLL_PROFILES["statistics_json"]
        self.assertEqual(profile["max_attempts"], 20)
        self.assertEqual(profile["base_sleep_seconds"], 15)
        self.assertEqual(profile["cap_sleep_seconds"], 60)


class RecoveryModeTimeoutTests(unittest.TestCase):
    """Батч по таймауту пропускается, остальные батчи обрабатываются."""

    def _campaigns(self, count):
        return [harness._sample_campaign(str(24375352 + index)) for index in range(count)]

    def _client(self):
        return harness._FakeClient(progress_map={})

    def _run(self, campaigns, fetcher, batch_size=1):
        db_client = harness._FakeDbClient({"marketplace_expenses": [], "ozon_daily_sku_ad_attribution": []})
        with mock.patch.object(loader, "save_rows"), mock.patch.object(loader, "save_ad_attribution_rows"):
            return loader.run_cpc_recovery_mode(
                client=self._client(),
                target_date="2026-08-17",
                group_by="DATE",
                requested_batch_size=batch_size,
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

    def test_timeout_skips_the_batch_and_keeps_going(self):
        campaigns = self._campaigns(3)
        stuck = campaigns[1]["id"]
        seen = []

        def fetcher(client, campaign_batch, date_from, date_to, group_by, usage_context=None):
            seen.append(list(campaign_batch))
            if list(campaign_batch) == [stuck]:
                raise loader.OzonReportTimeoutError("stuck-uuid", attempts=20, waited_seconds=1125)
            return {"report_data": {}}

        summary = self._run(campaigns, fetcher)

        # Все три батча предприняты — прогон не оборвался на втором.
        self.assertEqual(len(seen), 3)
        self.assertEqual(summary["processed_batches"], 2)
        self.assertEqual(summary["timed_out_batch_count"], 1)
        self.assertEqual(summary["incomplete_reason"], "report_timeout")
        record = summary["timed_out_batches"][0]
        self.assertEqual(record["report_uuid"], "stuck-uuid")
        self.assertEqual(record["campaign_ids"], [stuck])
        self.assertEqual(record["poll_attempts"], 20)
        self.assertEqual(record["waited_seconds"], 1125)

    def test_timeout_does_not_produce_a_quota_status(self):
        """Таймаут не должен маскироваться под исчерпание лимита."""

        def fetcher(client, campaign_batch, date_from, date_to, group_by, usage_context=None):
            raise loader.OzonReportTimeoutError("stuck-uuid", attempts=20, waited_seconds=1125)

        summary = self._run(self._campaigns(1), fetcher)
        self.assertNotIn(
            summary.get("status"),
            {"quota_limited_before_refetch", "quota_limited_during_refetch"},
        )
        self.assertIsNone(summary.get("retry_after_seconds"))
        self.assertIsNone(summary.get("cooldown_until"))
        self.assertEqual(summary["incomplete_reason"], "report_timeout")

    def test_timeout_does_not_resubmit_the_same_report(self):
        """Повторный submit того же отчёта стоит ещё одну единицу квоты."""
        campaigns = self._campaigns(1)
        fetcher = mock.Mock(
            side_effect=loader.OzonReportTimeoutError("stuck-uuid", attempts=20, waited_seconds=1125)
        )
        self._run(campaigns, fetcher)
        fetcher.assert_called_once()

    def test_rate_limit_still_stops_the_run(self):
        """Контраст: 429 по-прежнему останавливает прогон на первом же отказе."""
        campaigns = self._campaigns(3)
        seen = []

        def fetcher(client, campaign_batch, date_from, date_to, group_by, usage_context=None):
            seen.append(list(campaign_batch))
            raise loader.RateLimitPending(
                endpoint="/api/client/statistics/json",
                retry_after_seconds=300,
                cooldown_until="2026-09-03T00:30:00Z",
                attempt=1,
            )

        summary = self._run(campaigns, fetcher)
        self.assertEqual(len(seen), 1)
        self.assertEqual(summary["status"], "quota_limited_before_refetch")
        self.assertEqual(summary["retry_after_seconds"], 300)
        self.assertNotIn("timed_out_batches", summary)


if __name__ == "__main__":
    unittest.main()
