import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader


def render_alert(summary):
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent["text"] = json["text"]
        return mock.Mock()

    with mock.patch.object(loader, "TELEGRAM_BOT_TOKEN", "token"), \
            mock.patch.object(loader, "TELEGRAM_CHAT_ID", "chat"), \
            mock.patch.object(loader.requests, "post", side_effect=fake_post):
        loader.send_telegram_partial_ads_alert(summary)

    for line in sent["text"].splitlines():
        if line.startswith("reason: "):
            return line.split("reason: ", 1)[1]
    raise AssertionError("В алерте нет строки reason")


class PartialAdsAlertReasonTest(unittest.TestCase):
    def test_reason_comes_from_summary_not_from_cpc_status(self):
        cases = {
            "429": "429",
            "daily_quota_exhausted": "daily_quota_exhausted",
            "batch_cap_reached": "batch_cap_reached",
            "server_500_graceful_stop": "server_500_graceful_stop",
            "report_error_graceful_stop": "report_error_graceful_stop",
            "exception_after_partial_progress": "exception_after_partial_progress",
        }
        for stored, expected in cases.items():
            with self.subTest(stop_reason=stored):
                self.assertEqual(
                    render_alert({"cpc": {"status": "pending_backfill"}, "cpc_stop_reason": stored}),
                    expected,
                )

    def test_batch_cap_stop_is_not_reported_as_429(self):
        # pending_backfill + батч-кап: старая развилка по статусу писала сюда "429".
        reason = render_alert(
            {
                "cpc": {"status": "pending_backfill", "failed_batch_index": 5},
                "cpc_stop_reason": "batch_cap_reached",
                "batch_cap_limited": True,
            }
        )
        self.assertEqual(reason, "batch_cap_reached")

    def test_missing_or_unknown_reason_falls_back_to_unknown(self):
        for stored in (None, "", "   ", "some_future_reason"):
            with self.subTest(stop_reason=stored):
                self.assertEqual(
                    render_alert({"cpc": {"status": "pending_backfill"}, "cpc_stop_reason": stored}),
                    "unknown",
                )

    def test_missing_reason_never_defaults_to_a_concrete_cause(self):
        # Именно подстановка конкретной причины по умолчанию и породила баг.
        summary = {"cpc": {"status": "pending_backfill"}}
        self.assertEqual(render_alert(summary), "unknown")
        self.assertEqual(loader.resolve_cpc_stop_reason({}), "unknown")
        self.assertEqual(loader.resolve_cpc_stop_reason(None), "unknown")


if __name__ == "__main__":
    unittest.main()
