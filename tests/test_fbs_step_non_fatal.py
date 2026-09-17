"""Шаг заказов FBS не должен уносить с собой весь ночной прогон.

С 2026-09-16 загрузчик FBS на /v4 падает вместо частичного результата (как FBO
с 2026-09-14): фатальный шаг стал бы падать чаще, а стоит он перед расходами,
рекламой и KPI. Одна ночь заказов добирается 30-дневным окном; отказ обязан
быть громким — тревога с именем шага и блокер ozon_fbs_orders_missing утром.
"""
import unittest
from unittest import mock

import run_daily_pipeline as pipeline

STEP = "Ozon: загрузка FBS заказов"


class FbsStepNonFatalTests(unittest.TestCase):
    def test_fbs_step_is_non_fatal(self):
        self.assertIn(STEP, pipeline.NON_FATAL_STEPS)

    def test_fbs_step_runs_before_expenses_and_ads(self):
        titles = [title for title, _ in pipeline.build_steps()]
        self.assertLess(titles.index(STEP), titles.index("Ozon: расходы и комиссии"))
        self.assertLess(titles.index(STEP), titles.index("Ozon: реклама Performance API"),
                        "порядок изменился — обоснование нефатальности надо пересмотреть")

    def test_fbs_failure_alert_names_the_step_and_is_not_a_crash(self):
        sent = {}

        def fake_post(url, json=None, timeout=None, **kwargs):
            sent["text"] = (json or {}).get("text", "")
            return mock.Mock(status_code=200)

        with mock.patch.object(pipeline, "TELEGRAM_BOT_TOKEN", "t"), \
             mock.patch.object(pipeline, "TELEGRAM_CHAT_ID", "c"), \
             mock.patch.object(pipeline.requests, "post", side_effect=fake_post):
            pipeline.send_failure_alert(STEP, 1, ["RuntimeError: Ozon FBS: 429 не прошёл за 3 попыток"], fatal=False)
        self.assertIn("Шаг не выполнен, прогон продолжен", sent["text"])
        self.assertIn(STEP, sent["text"])
        self.assertIn("429", sent["text"])
        self.assertNotIn("упал", sent["text"])

    def test_fbs_failure_does_not_stop_the_pipeline(self):
        process = mock.MagicMock()
        process.stdout = mock.MagicMock()
        process.stdout.__iter__.return_value = iter(["RuntimeError: Ozon FBS: HTTP 500\n"])
        process.wait.return_value = 1
        with mock.patch.object(pipeline.subprocess, "Popen", return_value=process), \
             mock.patch.object(pipeline, "send_failure_alert") as alert:
            result = pipeline.run_step(STEP, "cmd", fatal=STEP not in pipeline.NON_FATAL_STEPS)
        self.assertTrue(result["failed"])
        self.assertEqual(alert.call_args.kwargs.get("fatal", alert.call_args.args[3] if len(alert.call_args.args) > 3 else None), False)

    def test_alert_is_sent_even_with_skip_telegram(self):
        args = mock.Mock(skip_telegram=True, skip_organic=False, skip_recovery=False,
                         skip_excel=False, skip_decision=False)
        should_skip, _ = pipeline.should_skip_pipeline_step(STEP, args, None, True)
        self.assertFalse(should_skip)


if __name__ == "__main__":
    unittest.main()
