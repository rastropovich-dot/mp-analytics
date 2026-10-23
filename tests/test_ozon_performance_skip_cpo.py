"""--skip-cpo у загрузчика Performance: только --mode full, CPO помечается skipped, в других режимах — отказ (2026-10-23, РБХ-2)."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_000_no_network as _guard  # noqa: E402,F401
import loaders.ozon_performance_ads_loader as loader  # noqa: E402


class SkipCpoTests(unittest.TestCase):
    def test_full_with_flag_skips_with_named_reason(self):
        args = SimpleNamespace(mode="full", skip_cpo=True)
        self.assertEqual(loader.cpo_stage_skip_reason(args), "skip_cpo_flag")

    def test_full_without_flag_requests_cpo(self):
        args = SimpleNamespace(mode="full", skip_cpo=False)
        self.assertIsNone(loader.cpo_stage_skip_reason(args))

    def test_cpc_backfill_always_skips_as_before(self):
        self.assertEqual(loader.cpo_stage_skip_reason(SimpleNamespace(mode="cpc-backfill", skip_cpo=False)), "cpc_backfill_mode")
        self.assertEqual(loader.cpo_stage_skip_reason(SimpleNamespace(mode="cpc-backfill", skip_cpo=True)), "cpc_backfill_mode")

    def test_flag_outside_full_is_refused(self):
        for mode in ("daily-yesterday", "cpc-recovery", "cpo-report-check"):
            with self.assertRaises(SystemExit):
                loader.cpo_stage_skip_reason(SimpleNamespace(mode=mode, skip_cpo=True))

    def test_args_without_attribute_request_cpo(self):
        # старые вызовы run() через SimpleNamespace без skip_cpo — поведение прежнее
        self.assertIsNone(loader.cpo_stage_skip_reason(SimpleNamespace(mode="daily-yesterday")))

    def test_parser_accepts_flag(self):
        with mock.patch.object(sys, "argv", ["x", "--mode", "full", "--date-from", "2026-08-23", "--date-to", "2026-10-21", "--skip-cpo"]):
            args = loader.parse_args()
        self.assertTrue(args.skip_cpo)
        with mock.patch.object(sys, "argv", ["x", "--mode", "full", "--date", "2026-10-20"]):
            args = loader.parse_args()
        self.assertFalse(args.skip_cpo)


if __name__ == "__main__":
    unittest.main()
