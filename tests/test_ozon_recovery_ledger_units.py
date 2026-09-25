"""Тридцать восьмая §6: выгрузки cpc-recovery ложатся в леджер с units = кампании пачки, mode и датой; опросы и скачивания — units 0. Сети нет."""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from loaders import ozon_performance_ads_loader as loader  # noqa: E402


class UsageEvent(unittest.TestCase):
    def test_submit_units_from_context_polls_zero(self):
        ctx = loader.build_cpc_recovery_usage_context("2026-09-14", 3, [1, 2, 3])
        self.assertEqual((ctx["mode"], ctx["target_date"], ctx["batch_index"], ctx["campaign_units"]), ("cpc-recovery", "2026-09-14", 3, 3))
        ev = loader.build_statistics_usage_event("acc", "submit", 1, "success", usage_context=ctx)
        self.assertEqual((ev["campaign_units"], ev["mode"], ev["target_date"], ev["batch_index"]), (3, "cpc-recovery", "2026-09-14", 3))
        self.assertEqual(loader.build_statistics_usage_event("acc", "poll", 5, "poll_success", usage_context=ctx)["campaign_units"], 0)
        self.assertEqual(loader.build_statistics_usage_event("acc", "download", 1, "download_success", usage_context=ctx)["campaign_units"], 0)
        self.assertEqual(loader.build_statistics_usage_event("acc", "submit", 1, "success")["campaign_units"], 0)   # без контекста — как раньше

    def test_recovery_loop_hands_context_to_the_fetcher(self):
        seen = []

        def fetcher(client, campaign_batch, date_from, date_to, group_by, usage_context=None):
            seen.append(usage_context)
            return {"uuid": f"u{len(seen)}", "report_data": {"rows": []}}

        plan = {"cpc_batches": [[1, 2], [3]], "campaigns_by_id": {}, "selected_campaign_ids": [1, 2, 3], "ordered_campaign_ids": [1, 2, 3],
                "requested_campaign_ids": [], "campaign_units": 3, "fits_safe_budget": True, "cpc_missing_globally": False, "downstream_verification": {},
                "campaign_count": 3, "batch_size": 2, "total_batches": 2, "expected_statistics_json_submit_count": 2, "safe_campaign_budget": 1800,
                "stale_progress": {}, "stale_progress_key": None}
        with mock.patch.object(loader, "build_cpc_recovery_plan", return_value=plan), \
             mock.patch.object(loader, "build_rows", return_value=([], {})), \
             mock.patch.object(loader, "build_cpc_attribution_rows", return_value=([], {})):
            try:
                loader.run_cpc_recovery_mode(client=mock.Mock(), target_date="2026-09-14", group_by="DATE", requested_batch_size=10, max_stats_campaigns=1800,
                                             dry_run=True, write=False, fetch_batch_fn=fetcher)
            except Exception:
                pass   # дальнейшая запись/сводка не предмет теста — контекст уже передан
        self.assertEqual([c["batch_index"] for c in seen], [0, 1])
        self.assertEqual([c["campaign_units"] for c in seen], [2, 1])
        self.assertTrue(all(c["mode"] == "cpc-recovery" and c["target_date"] == "2026-09-14" for c in seen))


if __name__ == "__main__":
    unittest.main()
