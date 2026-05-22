import importlib.util
from pathlib import Path
import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader


MODULE_PATH = Path("/Users/mihaileliseev/mp-analytics/scripts/ozon_performance_recovery_worker.py")
SPEC = importlib.util.spec_from_file_location("ozon_performance_recovery_worker", MODULE_PATH)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)
        self._eq = {}
        self._order_field = None
        self._order_desc = False
        self._limit = None

    def select(self, _fields):
        return self

    def eq(self, field, value):
        self._eq[field] = value
        return self

    def order(self, field, desc=False):
        self._order_field = field
        self._order_desc = desc
        return self

    def limit(self, value):
        self._limit = value
        return self

    def execute(self):
        rows = list(self._rows)
        for field, value in self._eq.items():
            rows = [row for row in rows if row.get(field) == value]
        if self._order_field:
            rows = sorted(rows, key=lambda row: row.get(self._order_field), reverse=self._order_desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return _FakeResult(rows)


class _FakeDbClient:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _FakeQuery(self.tables.get(name, []))


class _FakeClient:
    def __init__(self, cooldown_until=None, account_signature="acct_test"):
        self.account_signature = account_signature
        self._cooldown_until = cooldown_until

    def scoped_state_key(self, key):
        return f"{key}:{self.account_signature}"

    def get_cooldown(self, _key):
        return loader.from_iso(self._cooldown_until) if self._cooldown_until else None


def _status_row(
    target_date="2026-05-21",
    updated_at="2026-05-22T06:48:30Z",
    run_status="partial_ads",
    cpc_status="pending_429",
    cpo_status="success",
    pending_campaigns=13,
    pending_units=13,
):
    return {
        "load_date": "2026-05-22",
        "target_date": target_date,
        "marketplace_code": "ozon",
        "account_signature": "acct_test",
        "run_status": run_status,
        "cpc_status": cpc_status,
        "cpo_status": cpo_status,
        "cpc_pending_campaigns": pending_campaigns,
        "cpc_campaign_units_pending_total": pending_units,
        "cpc_campaign_units_completed_total": 1310,
        "updated_at": updated_at,
    }


def _progress():
    return (
        "cpc_progress:pending-tail",
        {
            "ordered_campaign_ids": [f"{9834000 + i}" for i in range(1323)],
            "batch_size": 10,
            "pending_batch_indexes": [131, 132],
        },
        "daily_yesterday_pending",
    )


class OzonPerformanceRecoveryWorkerTests(unittest.TestCase):
    def test_finds_partial_ads_candidate(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        rows = worker.get_partial_candidates(db, "acct_test")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target_date"], "2026-05-21")

    def test_skips_when_cooldown_active(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient(cooldown_until="2026-05-23T00:00:00+00:00")
        with mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0):
            plan = worker.build_recovery_plan(
                target_date="2026-05-21",
                db_client=db,
                client=client,
            )
        self.assertFalse(plan["will_run"])
        self.assertEqual(plan["candidates"][0]["status"], "skipped_cooldown_active")

    def test_skips_when_daily_budget_used_today_above_1500(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient()
        with mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=1501):
            plan = worker.build_recovery_plan(
                target_date="2026-05-21",
                db_client=db,
                client=client,
            )
        self.assertFalse(plan["will_run"])
        self.assertEqual(plan["budget_skip_reason"], "skipped_daily_budget_guard")

    def test_computes_recovery_budget_available(self):
        guard = worker.build_budget_guard(1400)
        self.assertEqual(guard["recovery_budget_available"], 200)

        guard = worker.build_budget_guard(1700)
        self.assertFalse(guard["will_run"])
        self.assertEqual(guard["budget_skip_reason"], "skipped_daily_budget_guard")

    def test_skips_when_recovery_budget_available_non_positive(self):
        guard = worker.build_budget_guard(1800)
        self.assertFalse(guard["will_run"])
        self.assertEqual(guard["budget_skip_reason"], "skipped_daily_budget_guard")

    def test_runs_only_pending_batch_when_budget_and_cooldown_allow(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient()
        with mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0), \
             mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=_progress()):
            plan = worker.build_recovery_plan(
                target_date="2026-05-21",
                db_client=db,
                client=client,
                max_batches_per_run=1,
            )
        self.assertTrue(plan["will_run"])
        candidate = plan["candidates"][0]
        self.assertEqual(candidate["planned_batch_indexes"], [131])
        self.assertEqual(candidate["planned_recovery_units"], 10)

    def test_caps_recovery_units_by_budget(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient()
        with mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=55), \
             mock.patch.object(worker.loader, "STATS_DAILY_CAMPAIGN_LIMIT", 260), \
             mock.patch.object(worker.loader, "STATS_DAILY_CAMPAIGN_RESERVE", 200), \
             mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=_progress()):
            plan = worker.build_recovery_plan(
                target_date="2026-05-21",
                db_client=db,
                client=client,
                max_batches_per_run=1,
            )
        self.assertFalse(plan["will_run"])
        self.assertEqual(plan["candidates"][0]["status"], "skipped_no_recovery_budget")

    def test_never_selects_completed_success_dates(self):
        db = _FakeDbClient(
            {loader.DAILY_LOAD_STATUS_TABLE: [_status_row(run_status="success", cpc_status="success", pending_campaigns=0, pending_units=0)]}
        )
        rows = worker.get_partial_candidates(db, "acct_test")
        self.assertEqual(rows, [])

    def test_dry_run_writes_nothing(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient()
        with mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0), \
             mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=_progress()), \
             mock.patch.object(worker.subprocess, "run") as run_mock:
            plan = worker.build_recovery_plan(
                target_date="2026-05-21",
                db_client=db,
                client=client,
            )
        run_mock.assert_not_called()
        self.assertTrue(plan["will_run"])

    def test_write_requires_approval(self):
        with self.assertRaises(RuntimeError):
            worker.run_recovery_write({"will_run": True, "candidates": []}, approve_write=False)

    def test_stops_on_429(self):
        plan = {
            "will_run": True,
            "candidates": [
                {
                    "will_run": True,
                    "target_date": "2026-05-21",
                    "planned_batch_indexes": [131],
                }
            ],
        }
        completed = mock.Mock(returncode=0, stdout='{"status":"pending_429"}', stderr="")
        with mock.patch.object(worker.subprocess, "run", return_value=completed):
            result = worker.run_recovery_write(plan, approve_write=True)
        self.assertEqual(result["status"], "pending_429")

    def test_write_command_is_gated_and_bounded(self):
        command = worker.build_loader_command("2026-05-21", 1, write=True)
        self.assertIn("--mode", command)
        self.assertIn("cpc-backfill", command)
        self.assertIn("--write", command)
        self.assertIn("--approve-cpc-recovery-write", command)


if __name__ == "__main__":
    unittest.main()
