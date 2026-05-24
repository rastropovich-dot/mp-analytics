import types
import unittest

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

    def test_partial_ozon_status_skips_organic_and_kpi(self):
        args = types.SimpleNamespace(
            skip_recovery=False,
            skip_excel=False,
            skip_decision=False,
            skip_telegram=False,
        )
        for title in (
            "Ozon: расчет organic sales по SKU",
            "KPI: расчет SKU",
            "KPI: расчет маркетплейсов",
        ):
            should_skip, message = pipeline.should_skip_pipeline_step(
                title, args, ozon_downstream_allowed=False
            )
            self.assertTrue(should_skip)
            self.assertIn("partial/incomplete", message)

    def test_ozon_run_summary_success_marks_complete(self):
        self.assertTrue(
            pipeline.ozon_run_summary_is_complete({"overall_status": "success"})
        )
        self.assertFalse(
            pipeline.ozon_run_summary_is_complete({"overall_status": "partial_ads"})
        )


if __name__ == "__main__":
    unittest.main()
