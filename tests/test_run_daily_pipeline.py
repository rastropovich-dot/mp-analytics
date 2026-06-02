import subprocess
import types
import unittest
from unittest import mock

import run_daily_pipeline as pipeline


class RunDailyPipelineTests(unittest.TestCase):
    def test_pre_recovery_is_first_step(self):
        self.assertEqual(
            pipeline.STEPS[0][0],
            "Ozon Performance: CPC recovery before daily",
        )

    def test_post_recovery_follows_ozon_daily_step(self):
        titles = [title for title, _command in pipeline.STEPS]
        daily_index = titles.index("Ozon: реклама Performance API")
        self.assertEqual(
            titles[daily_index + 1],
            "Ozon Performance: CPC recovery after daily",
        )

    def test_post_recovery_uses_relative_deadline(self):
        post_recovery_command = dict(pipeline.STEPS)["Ozon Performance: CPC recovery after daily"]
        self.assertIn("--wait-for-minutes 240", post_recovery_command)
        self.assertNotIn("--wait-until 09:40", post_recovery_command)

    def test_daily_ozon_command_receives_smart_flags(self):
        args = types.SimpleNamespace(
            ozon_campaign_selection="smart_recent_active",
            ozon_recent_activity_days=7,
            ozon_dormant_probe_size=100,
            ozon_max_daily_cpc_units=1000,
            ozon_allow_staged_cpc_partial=True,
        )
        steps = pipeline.build_steps(args)
        daily_command = dict(steps)["Ozon: реклама Performance API"]
        self.assertIn("--campaign-selection smart_recent_active", daily_command)
        self.assertIn("--recent-activity-days 7", daily_command)
        self.assertIn("--dormant-probe-size 100", daily_command)
        self.assertIn("--max-daily-cpc-units 1000", daily_command)
        self.assertIn("--allow-staged-cpc-partial", daily_command)

        recovery_before = dict(steps)["Ozon Performance: CPC recovery before daily"]
        recovery_after = dict(steps)["Ozon Performance: CPC recovery after daily"]
        self.assertNotIn("--campaign-selection", recovery_before)
        self.assertNotIn("--campaign-selection", recovery_after)
        self.assertNotIn("--max-daily-cpc-units", recovery_after)

    def test_skip_recovery_skips_both_recovery_steps(self):
        args = types.SimpleNamespace(
            skip_recovery=True,
            skip_excel=False,
            skip_decision=False,
            skip_telegram=False,
        )
        skip_before, _msg_before = pipeline.should_skip_pipeline_step(
            "Ozon Performance: CPC recovery before daily", args, ozon_downstream_allowed=None
        )
        skip_after, _msg_after = pipeline.should_skip_pipeline_step(
            "Ozon Performance: CPC recovery after daily", args, ozon_downstream_allowed=None
        )
        self.assertTrue(skip_before)
        self.assertTrue(skip_after)

    def test_controlled_recovery_statuses_do_not_allow_ozon_downstream(self):
        self.assertFalse(
            pipeline.recovery_result_allows_ozon_downstream({"status": "deadline_after_429"})
        )
        self.assertFalse(
            pipeline.recovery_result_allows_ozon_downstream({"status": "max_attempts_exhausted"})
        )

    def test_complete_recovery_allows_ozon_downstream(self):
        self.assertTrue(
            pipeline.recovery_result_allows_ozon_downstream({"status": "complete"})
        )

    def test_partial_ozon_status_skips_only_ozon_organic(self):
        args = types.SimpleNamespace(
            skip_recovery=False,
            skip_excel=False,
            skip_decision=False,
            skip_telegram=False,
        )
        should_skip, message = pipeline.should_skip_pipeline_step(
            "Ozon: расчет organic sales по SKU", args, ozon_downstream_allowed=False
        )
        self.assertTrue(should_skip)
        self.assertIn("partial/incomplete", message)

        should_skip, message = pipeline.should_skip_pipeline_step(
            "KPI: расчет SKU", args, ozon_downstream_allowed=False
        )
        self.assertFalse(should_skip)
        self.assertIsNone(message)

        should_skip, message = pipeline.should_skip_pipeline_step(
            "KPI: расчет маркетплейсов", args, ozon_downstream_allowed=False
        )
        self.assertFalse(should_skip)
        self.assertIsNone(message)

    def test_ozon_run_summary_success_marks_complete(self):
        self.assertTrue(
            pipeline.ozon_run_summary_is_complete({"overall_status": "success"})
        )
        self.assertFalse(
            pipeline.ozon_run_summary_is_complete({"overall_status": "partial_ads"})
        )

    def test_non_recovery_skip_flags_unchanged(self):
        args = types.SimpleNamespace(
            skip_recovery=False,
            skip_excel=True,
            skip_decision=True,
            skip_telegram=True,
        )
        self.assertTrue(
            pipeline.should_skip_pipeline_step(
                "Excel: выгрузка управленческого отчета", args, ozon_downstream_allowed=None
            )[0]
        )
        self.assertTrue(
            pipeline.should_skip_pipeline_step(
                "Decision: SKU daily input", args, ozon_downstream_allowed=None
            )[0]
        )
        self.assertTrue(
            pipeline.should_skip_pipeline_step(
                "Telegram: отправка сигналов", args, ozon_downstream_allowed=None
            )[0]
        )


class RunStepNonFatalTests(unittest.TestCase):
    def _make_process(self, returncode, output=""):
        proc = mock.Mock()
        stdout_mock = mock.MagicMock()
        stdout_mock.__iter__ = mock.Mock(return_value=iter([output] if output else []))
        proc.stdout = stdout_mock
        proc.wait = mock.Mock(return_value=returncode)
        return proc

    def test_fatal_true_exits_on_nonzero(self):
        proc = self._make_process(1, "error\n")
        with mock.patch("subprocess.Popen", return_value=proc), \
             mock.patch.object(pipeline, "send_failure_alert"), \
             self.assertRaises(SystemExit):
            pipeline.run_step("Some step", "false", fatal=True)

    def test_fatal_false_returns_failed_dict_on_nonzero(self):
        proc = self._make_process(1, "error\n")
        with mock.patch("subprocess.Popen", return_value=proc), \
             mock.patch.object(pipeline, "send_failure_alert"):
            result = pipeline.run_step("Some step", "false", fatal=False)
        self.assertTrue(result.get("failed"))
        self.assertEqual(result.get("returncode"), 1)

    def test_fatal_false_does_not_exit_on_success(self):
        proc = self._make_process(0, "ok\n")
        with mock.patch("subprocess.Popen", return_value=proc):
            result = pipeline.run_step("Some step", "true", fatal=False)
        self.assertFalse(result.get("failed"))

    def test_ozon_performance_step_uses_non_fatal(self):
        """Ozon Performance step sets ozon_downstream_allowed=False on failure."""
        args = types.SimpleNamespace(
            skip_recovery=False,
            skip_excel=False,
            skip_decision=False,
            skip_telegram=False,
        )
        titles_run = []
        step_results = {}

        def fake_run_step(title, command, fatal=True):
            titles_run.append(title)
            if title == "Ozon: реклама Performance API":
                self.assertFalse(fatal, "Ozon Performance step must be non-fatal")
                return {"failed": True, "returncode": 1, "output_text": "", "recovery_result": None, "ozon_run_summary": None}
            return {"output_text": "", "recovery_result": None, "ozon_run_summary": None}

        steps = [
            ("Ozon: реклама Performance API", "ozon_cmd"),
            ("Ozon: расчет organic sales по SKU", "organic_cmd"),
            ("KPI: расчет SKU", "kpi_cmd"),
        ]
        with mock.patch.object(pipeline, "run_step", side_effect=fake_run_step), \
             mock.patch.object(pipeline, "build_steps", return_value=steps), \
             mock.patch.object(pipeline, "parse_args", return_value=args), \
             mock.patch.object(pipeline, "send_failure_alert"):
            pipeline.main()

        self.assertIn("KPI: расчет SKU", titles_run)
        self.assertNotIn("Ozon: расчет organic sales по SKU", titles_run)


if __name__ == "__main__":
    unittest.main()
