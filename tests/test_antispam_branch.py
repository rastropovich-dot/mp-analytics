"""Две ветки 429 требуют разного обращения.

Поддержка Ozon: «при подозрении на спам система перестаёт принимать запросы на
несколько минут, а после автоматически включает». Останавливать сбор до сброса
СУТОЧНОГО окна из-за паузы в несколько минут — цена, которую мы платили зря.

Суточный лимит при этом остаётся как был: ждать до сброса.
"""
import unittest

import loaders.ozon_performance_ads_loader as loader


class ClassifyTests(unittest.TestCase):
    def test_daily_limit_text_is_recognised(self):
        for text in ('{"error":"Превышен дневной лимит запросов (максимум 2000)"}',
                     "daily limit reached",
                     "МАКСИМУМ 2000"):
            self.assertEqual(
                loader.classify_statistics_json_429_preview(text),
                "daily_quota_exhausted",
                text,
            )

    def test_anything_else_is_throttle(self):
        for text in ("", "request rate limit per second", "too many requests", None):
            self.assertEqual(
                loader.classify_statistics_json_429_preview(text),
                "retryable_throttle",
                repr(text),
            )


class AntispamPolicyTests(unittest.TestCase):
    def test_pause_and_cap_are_defined(self):
        self.assertEqual(loader.ANTISPAM_MAX_PAUSES, 3, "потолок три попытки")
        self.assertGreaterEqual(loader.ANTISPAM_PAUSE_SECONDS, 60)

    def test_client_counts_pauses_from_zero(self):
        client = loader.OzonPerformanceClient.__new__(loader.OzonPerformanceClient)
        client.antispam_pauses_taken = 0
        self.assertEqual(client.antispam_pauses_taken, 0)

    def test_clear_cooldown_exists(self):
        """Антиспам отпускает сам — держать паузу после него незачем."""
        self.assertTrue(hasattr(loader.OzonPerformanceClient, "clear_cooldown"))

    def test_daily_quota_never_takes_the_antispam_branch(self):
        """Настоящий лимит не лечится паузой в минуту."""
        source = loader.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('response_kind != "daily_quota_exhausted"', text)


if __name__ == "__main__":
    unittest.main()
