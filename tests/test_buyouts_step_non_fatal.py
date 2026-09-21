"""Шаг выкупов не должен уносить с собой весь ночной прогон.

/v3/finance/transaction/list отключён 2026-09-08, загрузчик выкупов падает.
Шаг стоит ПЕРЕД сбором рекламы: фатальным он уносил бы исправные рекламу,
органику, KPI и витрину. Плюс ложная тревога: падение одного известного шага
не должно приходить в телеграм как «Пайплайн упал».
"""
import types
import unittest
from unittest import mock

import run_daily_pipeline as pipeline


class BuyoutsStepNonFatalTests(unittest.TestCase):
    def test_buyouts_step_is_non_fatal(self):
        self.assertIn("Ozon: дневные финоперации", pipeline.NON_FATAL_STEPS)

    def test_buyouts_step_runs_before_ads(self):
        titles = [title for title, _ in pipeline.STEPS]
        self.assertLess(
            titles.index("Ozon: дневные финоперации"),
            titles.index("Ozon: реклама Performance API"),
            "порядок изменился — обоснование нефатальности надо пересмотреть",
        )

    def test_non_fatal_failure_does_not_exit(self):
        proc = mock.Mock()
        stdout = mock.MagicMock()
        stdout.__iter__ = mock.Mock(return_value=iter(["obsolete method cannot be used\n"]))
        proc.stdout = stdout
        proc.wait = mock.Mock(return_value=1)
        with mock.patch("subprocess.Popen", return_value=proc), \
             mock.patch.object(pipeline, "send_failure_alert") as alert:
            result = pipeline.run_step("Ozon: дневные финоперации", "cmd", fatal=False)
        self.assertTrue(result.get("failed"))
        self.assertFalse(alert.call_args.kwargs.get("fatal", True),
                         "нефатальный шаг не должен слать тревогу как крах прогона")

    def test_alert_text_differs_for_non_fatal(self):
        sent = {}

        def fake_post(url, json=None, timeout=None, **kwargs):
            sent["text"] = (json or {}).get("text", "")
            return mock.Mock(status_code=200)

        with mock.patch.object(pipeline, "TELEGRAM_BOT_TOKEN", "t"), \
             mock.patch.object(pipeline, "TELEGRAM_CHAT_ID", "c"), \
             mock.patch.object(pipeline.requests, "post", side_effect=fake_post):
            pipeline.send_failure_alert("Ozon: дневные финоперации", 1, ["boom"], fatal=False)
        self.assertIn("Шаг не выполнен", sent["text"])
        self.assertNotIn("упал", sent["text"])

    def test_alert_text_still_shouts_for_fatal(self):
        sent = {}

        def fake_post(url, json=None, timeout=None, **kwargs):
            sent["text"] = (json or {}).get("text", "")
            return mock.Mock(status_code=200)

        with mock.patch.object(pipeline, "TELEGRAM_BOT_TOKEN", "t"), \
             mock.patch.object(pipeline, "TELEGRAM_CHAT_ID", "c"), \
             mock.patch.object(pipeline.requests, "post", side_effect=fake_post):
            pipeline.send_failure_alert("KPI: расчет SKU", 1, ["boom"], fatal=True)
        self.assertIn("упал", sent["text"])


if __name__ == "__main__":
    unittest.main()
