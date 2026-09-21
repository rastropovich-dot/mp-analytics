"""Шаг заказов FBO не должен уносить с собой весь ночной прогон.

С 2026-09-11 шаг ловит 429 rate_limit_per_second, а загрузчик на /v3 (2026-09-14)
падает вместо частичной записи. Одна ночь заказов добирается 30-дневным окном;
расходы, реклама и KPI — нет. Отказ при этом обязан быть громким.
"""
import unittest
from unittest import mock

import run_daily_pipeline as pipeline


class FboStepNonFatalTests(unittest.TestCase):
    def test_fbo_step_is_non_fatal(self):
        self.assertIn("Ozon: загрузка FBO заказов", pipeline.NON_FATAL_STEPS)

    def test_fbo_step_runs_before_expenses_and_ads(self):
        titles = [title for title, _ in pipeline.build_steps()]
        self.assertLess(titles.index("Ozon: загрузка FBO заказов"),
                        titles.index("Ozon: расходы и комиссии"))
        self.assertLess(titles.index("Ozon: загрузка FBO заказов"),
                        titles.index("Ozon: реклама Performance API"),
                        "порядок изменился — обоснование нефатальности надо пересмотреть")

    def test_fbo_failure_alert_names_the_step_and_is_not_a_crash(self):
        sent = {}

        def fake_post(url, json=None, timeout=None, **kwargs):
            sent["text"] = (json or {}).get("text", "")
            return mock.Mock(status_code=200)

        with mock.patch.object(pipeline, "TELEGRAM_BOT_TOKEN", "t"), \
             mock.patch.object(pipeline, "TELEGRAM_CHAT_ID", "c"), \
             mock.patch.object(pipeline.requests, "post", side_effect=fake_post):
            pipeline.send_failure_alert("Ozon: загрузка FBO заказов", 1,
                                        ["RuntimeError: Ozon FBO: 429 не прошёл за 3 попыток"], fatal=False)
        self.assertIn("Шаг не выполнен", sent["text"])
        self.assertIn("Ozon: загрузка FBO заказов", sent["text"])
        self.assertIn("429", sent["text"])
        self.assertNotIn("упал", sent["text"])

    def test_alert_is_sent_even_with_skip_telegram(self):
        """--skip-telegram пропускает только шаг «Telegram: …», не тревогу об отказе."""
        args = mock.Mock(skip_telegram=True, skip_organic=False, skip_recovery=False,
                         skip_excel=False, skip_decision=False)
        should_skip, _ = pipeline.should_skip_pipeline_step(
            "Ozon: загрузка FBO заказов", args, None, True)
        self.assertFalse(should_skip)


if __name__ == "__main__":
    unittest.main()
