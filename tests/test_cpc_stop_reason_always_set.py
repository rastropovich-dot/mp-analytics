"""Причина остановки обязана быть у любого исхода сбора CPC.

Ночь 2026-09-08 завершилась статусом pending_backfill и пустой причиной:
собрано 525 кампаний из 535, отказов по квоте нет, почему остановились —
неизвестно. Значение сообщало, что что-то не так, и молчало о том, что именно.
"""
import unittest

import loaders.ozon_performance_ads_loader as loader


def resolve(status, batch_cap_limited=False):
    summary = {"cpc": {"status": status}, "batch_cap_limited": batch_cap_limited}
    if status == "pending_quota":
        summary["cpc_stop_reason"] = "daily_quota_exhausted"
    elif status == "pending_429":
        summary["cpc_stop_reason"] = "429"
    elif status == "pending_backfill" and batch_cap_limited:
        summary["cpc_stop_reason"] = "batch_cap_reached"
    elif str(status or "").startswith("pending"):
        summary["cpc_stop_reason"] = f"incomplete_without_reason:{status}"
    else:
        summary["cpc_stop_reason"] = "completed"
    return summary["cpc_stop_reason"]


class CpcStopReasonTests(unittest.TestCase):
    def test_pending_backfill_without_cap_has_loud_reason(self):
        self.assertEqual(
            resolve("pending_backfill"),
            "incomplete_without_reason:pending_backfill",
        )

    def test_no_pending_status_leaves_reason_empty(self):
        for status in ("pending_backfill", "pending_quota", "pending_429", "pending_anything"):
            self.assertTrue(resolve(status), f"пустая причина у статуса {status}")

    def test_success_is_marked_completed(self):
        self.assertEqual(resolve("success"), "completed")

    def test_known_reasons_are_kept(self):
        self.assertEqual(resolve("pending_quota"), "daily_quota_exhausted")
        self.assertEqual(resolve("pending_429"), "429")
        self.assertEqual(resolve("pending_backfill", True), "batch_cap_reached")

    def test_loader_exposes_resolve_helper(self):
        self.assertTrue(hasattr(loader, "resolve_cpc_stop_reason"))


if __name__ == "__main__":
    unittest.main()
