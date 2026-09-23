"""Шаг «WB: отчёт реализации» — нефатальный, после продаж WB, до Ozon и KPI."""
import types
import unittest
from unittest import mock

import run_daily_pipeline as pipeline

STEP = "WB: отчёт реализации"


class StepTests(unittest.TestCase):
    def test_step_is_declared_and_non_fatal(self):
        titles = [title for title, _ in pipeline.build_steps()]
        self.assertIn(STEP, titles)
        self.assertIn(STEP, pipeline.NON_FATAL_STEPS)
        self.assertEqual(dict(pipeline.build_steps())[STEP], "python3 loaders/wb_sales_report_loader.py")

    def test_step_runs_after_wb_sales_and_before_ozon_and_kpi(self):
        titles = [title for title, _ in pipeline.build_steps()]
        self.assertGreater(titles.index(STEP), titles.index("WB: загрузка продаж/выкупов"))
        for later in ("WB: загрузка остатков", "Ozon: загрузка FBS заказов", "KPI: расчет SKU"):
            self.assertLess(titles.index(STEP), titles.index(later))

    def test_failure_does_not_stop_the_pipeline(self):
        process = mock.MagicMock()
        process.stdout = mock.MagicMock()
        process.stdout.__iter__.return_value = iter(["RuntimeError: WB sales report: 429 не изжит\n"])
        process.wait.return_value = 1
        with mock.patch.object(pipeline.subprocess, "Popen", return_value=process), \
             mock.patch.object(pipeline, "send_failure_alert") as alert:
            result = pipeline.run_step(STEP, "cmd", fatal=STEP not in pipeline.NON_FATAL_STEPS)
        self.assertTrue(result["failed"])
        alert.assert_called_once()

    def test_steps_after_the_failure_still_run(self):
        args = types.SimpleNamespace(
            skip_recovery=True, skip_organic=True, skip_excel=True, skip_decision=True, skip_telegram=True,
            ozon_recovery_current_day_only=False, ozon_campaign_selection=None, ozon_recent_activity_days=None,
            ozon_dormant_probe_size=None, ozon_max_daily_cpc_units=None, ozon_allow_staged_cpc_partial=False,
        )
        ran = []

        def fake_run_step(title, command, fatal=True, nonfatal_returncodes=()):
            ran.append(title)
            if title == STEP:
                return {"failed": True, "returncode": 1, "output_text": "", "recovery_result": None, "ozon_run_summary": None}
            return {"output_text": "", "recovery_result": None, "ozon_run_summary": None}

        with mock.patch.object(pipeline, "parse_args", return_value=args), \
             mock.patch.object(pipeline, "is_yesterday_cpc_loaded", return_value=False), \
             mock.patch.object(pipeline, "run_step", side_effect=fake_run_step):
            pipeline.main()
        for title in ("WB: загрузка остатков", "Ozon: загрузка FBS заказов", "KPI: расчет SKU", "KPI: расчет маркетплейсов"):
            self.assertIn(title, ran)


if __name__ == "__main__":
    unittest.main()
