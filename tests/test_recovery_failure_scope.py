"""Часть 4: граница фатальности шага recovery.

Сбой бэкфилла ИСТОРИЧЕСКОЙ даты не должен уносить с собой построение текущего
дня. Сбой сбора за ВЧЕРА — обязан. Неизвестность трактуется как сегодняшний
сбой: молчаливое продолжение хуже лишней остановки.
"""

import sys
import unittest
from datetime import date
from unittest import mock

import run_daily_pipeline as pipeline
from scripts import ozon_performance_recovery_worker as worker


TODAY = date(2026, 9, 3)
YESTERDAY = "2026-09-02"


class RecoveryFailureScopeTests(unittest.TestCase):
    def test_yesterday_failure_is_current_day(self):
        self.assertEqual(
            worker.recovery_failure_scope(YESTERDAY, today_local_date=TODAY),
            "current_day",
        )

    def test_old_date_failure_is_historical_backfill(self):
        self.assertEqual(
            worker.recovery_failure_scope("2026-08-17", today_local_date=TODAY),
            "historical_backfill",
        )

    def test_unknown_date_is_not_downgraded(self):
        self.assertEqual(
            worker.recovery_failure_scope(None, today_local_date=TODAY),
            "unknown",
        )

    def test_first_runnable_candidate_is_the_reported_date(self):
        plan = {
            "candidates": [
                {"target_date": "2026-07-04", "will_run": False},
                {"target_date": "2026-08-17", "will_run": True},
                {"target_date": "2026-08-13", "will_run": True},
            ]
        }
        self.assertEqual(worker.first_runnable_target_date(plan), "2026-08-17")

    def test_no_runnable_candidate_reports_nothing(self):
        plan = {"candidates": [{"target_date": "2026-07-04", "will_run": False}]}
        self.assertIsNone(worker.first_runnable_target_date(plan))


class RunStepNonFatalTests(unittest.TestCase):
    """Код 2 не должен вызывать sys.exit, любой другой ненулевой — должен."""

    def _run(self, returncode, nonfatal):
        process = mock.MagicMock()
        process.stdout = mock.MagicMock()
        process.stdout.__iter__.return_value = iter(["line\n"])
        process.wait.return_value = returncode
        with mock.patch.object(pipeline.subprocess, "Popen", return_value=process), \
             mock.patch.object(pipeline, "send_failure_alert"):
            return pipeline.run_step("шаг", "cmd", fatal=True, nonfatal_returncodes=nonfatal)

    def test_historical_backfill_code_does_not_stop_pipeline(self):
        result = self._run(pipeline.HISTORICAL_BACKFILL_FAILURE_EXIT_CODE, (2,))
        self.assertTrue(result["failed"])
        self.assertEqual(result["returncode"], 2)

    def test_current_day_failure_still_stops_pipeline(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(1, (2,))
        self.assertEqual(ctx.exception.code, 1)

    def test_without_allowance_code_two_is_still_fatal(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(2, ())
        self.assertEqual(ctx.exception.code, 2)


class OzonDownstreamGateTests(unittest.TestCase):
    """Витрину открывает вчерашняя дата, а не пустота всего бэклога.

    Регрессия, из-за которой ozon_daily_sku_organic не пополнялась с 2026-05-21:
    воркер почти никогда не возвращает "complete", потому что исторический
    бэклог непустой, и шаг органики пропускался каждую ночь.
    """

    def test_complete_still_opens_downstream(self):
        self.assertTrue(
            pipeline.recovery_result_allows_ozon_downstream({"status": "complete"}, yesterday=YESTERDAY)
        )

    def test_backlog_on_old_dates_does_not_close_downstream(self):
        result = {
            "status": "partial_remaining",
            "plan": {
                "candidates": [
                    {"target_date": YESTERDAY, "pending_campaign_units": 0},
                    {"target_date": "2026-08-17", "pending_campaign_units": 790},
                ]
            },
        }
        self.assertTrue(
            pipeline.recovery_result_allows_ozon_downstream(result, yesterday=YESTERDAY)
        )

    def test_pending_tail_on_yesterday_closes_downstream(self):
        result = {
            "status": "partial_remaining",
            "plan": {
                "candidates": [
                    {"target_date": YESTERDAY, "pending_campaign_units": 120},
                    {"target_date": "2026-08-17", "pending_campaign_units": 790},
                ]
            },
        }
        self.assertFalse(
            pipeline.recovery_result_allows_ozon_downstream(result, yesterday=YESTERDAY)
        )

    def test_missing_result_closes_downstream(self):
        self.assertFalse(pipeline.recovery_result_allows_ozon_downstream(None, yesterday=YESTERDAY))


if __name__ == "__main__":
    unittest.main()
