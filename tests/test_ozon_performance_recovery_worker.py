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


def _progress(pending_batch_indexes=None):
    pending = list(pending_batch_indexes or [131, 132])
    return (
        "cpc_progress:pending-tail",
        {
            "ordered_campaign_ids": [f"{9834000 + i}" for i in range(1323)],
            "batch_size": 10,
            "total_campaigns": 1323,
            "completed_batches": max(0, 133 - len(pending)),
            "pending_batches": len(pending),
            "next_batch_index": pending[0] if pending else None,
            "pending_batch_indexes": pending,
        },
        "daily_yesterday_pending",
    )


class OzonPerformanceRecoveryWorkerTests(unittest.TestCase):
    def test_wait_for_minutes_creates_future_deadline(self):
        now_utc = loader.datetime(2026, 5, 25, 0, 10, 0, tzinfo=loader.ZoneInfo("UTC"))
        with mock.patch.object(worker.loader, "utcnow", return_value=now_utc):
            deadline = worker.parse_relative_wait_deadline(180, now_utc=now_utc)
        self.assertEqual(worker.loader.to_iso(deadline), "2026-05-25T03:10:00+00:00")

    def test_wait_until_past_time_returns_deadline_already_passed(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient()
        with mock.patch.object(worker.loader, "today_local", return_value=loader.datetime(2026, 5, 25, tzinfo=loader.ZoneInfo("Europe/Moscow"))), \
             mock.patch.object(worker.loader, "utcnow", return_value=loader.datetime(2026, 5, 25, 20, 59, 30, tzinfo=loader.ZoneInfo("UTC"))), \
             mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0):
            plan = worker.build_recovery_plan(
                target_date="2026-05-21",
                phase="post",
                wait_until="23:59",
                timezone="Europe/Moscow",
                db_client=db,
                client=client,
            )
        self.assertEqual(plan["status"], "deadline_already_passed")
        self.assertTrue(plan["deadline_already_passed"])
        self.assertFalse(plan["will_run"])

    def test_cooldown_time_is_displayed_in_utc_and_local(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient(cooldown_until="2026-05-24T21:29:36+00:00")
        with mock.patch.object(worker.loader, "today_local", return_value=loader.datetime(2026, 5, 25, tzinfo=loader.ZoneInfo("Europe/Moscow"))), \
             mock.patch.object(worker.loader, "utcnow", return_value=loader.datetime(2026, 5, 24, 21, 0, 0, tzinfo=loader.ZoneInfo("UTC"))), \
             mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0):
            plan = worker.build_recovery_plan(
                target_date="2026-05-21",
                phase="post",
                wait_for_minutes=180,
                timezone="Europe/Moscow",
                db_client=db,
                client=client,
            )
        self.assertEqual(plan["cooldown_until_utc"], "2026-05-24T21:29:36+00:00")
        self.assertEqual(plan["cooldown_until_local"], "2026-05-25T00:29:36+03:00")
        self.assertEqual(plan["current_time_local"], "2026-05-25T00:00:00+03:00")

    def test_wait_for_minutes_and_wait_until_together_fail(self):
        parser = worker.make_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["--wait-until", "09:40", "--wait-for-minutes", "180"]
            )

    def test_finds_partial_ads_candidate(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        rows = worker.get_partial_candidates(db, "acct_test")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target_date"], "2026-05-21")

    def test_pre_phase_caps_recovery_budget(self):
        guard = worker.build_budget_guard(1400, phase="pre")
        self.assertEqual(guard["recovery_budget_available"], 200)

    def test_post_phase_uses_remaining_minus_reserve_without_200_cap(self):
        guard = worker.build_budget_guard(1400, phase="post")
        self.assertEqual(guard["recovery_budget_available"], 400)
        self.assertTrue(guard["will_run"])

    def test_budget_guard_uses_dynamic_daily_limit(self):
        guard = worker.build_budget_guard(500, phase="post", daily_limit=960)
        self.assertEqual(guard["daily_limit"], 960)
        self.assertEqual(guard["recovery_budget_available"], 260)

    def test_plan_exposes_budget_source_and_confidence(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient()
        client.get_statistics_json_usage_events = mock.Mock(return_value=[])
        with mock.patch.object(worker.loader, "get_statistics_json_budget_diagnostics", return_value={
            "daily_budget_used_today": 40,
            "budget_source": "runtime_usage_ledger",
            "budget_confidence": "high",
            "usage_event_count": 4,
        }), \
             mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=_progress()):
            plan = worker.build_recovery_plan(
                target_date="2026-05-21",
                db_client=db,
                client=client,
                phase="post",
            )
        self.assertEqual(plan["budget_source"], "runtime_usage_ledger")
        self.assertEqual(plan["budget_confidence"], "high")
        self.assertEqual(plan["usage_event_count"], 4)

    def test_post_phase_skips_when_recovery_budget_non_positive(self):
        guard = worker.build_budget_guard(1800, phase="post")
        self.assertFalse(guard["will_run"])
        self.assertEqual(guard["budget_skip_reason"], "skipped_no_recovery_budget")

    def test_skips_when_pre_phase_budget_used_above_1500(self):
        guard = worker.build_budget_guard(1501, phase="pre")
        self.assertFalse(guard["will_run"])
        self.assertEqual(guard["budget_skip_reason"], "skipped_daily_budget_guard")

    def test_dry_run_wait_mode_reports_wait_and_does_not_sleep(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient(cooldown_until="2026-05-24T06:10:00+00:00")
        with mock.patch.object(worker.loader, "today_local", return_value=loader.datetime(2026, 5, 24, tzinfo=loader.ZoneInfo("Europe/Moscow"))), \
             mock.patch.object(worker.loader, "utcnow", return_value=loader.datetime(2026, 5, 24, 6, 0, 0, tzinfo=loader.ZoneInfo("UTC"))), \
             mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0):
            plan = worker.execute_recovery_session(
                target_date="2026-05-21",
                phase="post",
                wait_until="09:40",
                timezone="Europe/Moscow",
                dry_run=True,
                db_client=db,
                client=client,
            )
        self.assertTrue(plan["cooldown_active"])
        self.assertTrue(plan["will_wait"])
        self.assertGreater(plan["wait_seconds"], 0)
        self.assertEqual(plan["planned_attempts"], 10)
        self.assertEqual(plan["deadline_local"], "2026-05-24T09:40:00+03:00")

    def test_deadline_before_cooldown_returns_controlled_status(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient(cooldown_until="2026-05-24T09:00:00+00:00")
        with mock.patch.object(worker.loader, "today_local", return_value=loader.datetime(2026, 5, 24, tzinfo=loader.ZoneInfo("Europe/Moscow"))), \
             mock.patch.object(worker.loader, "utcnow", return_value=loader.datetime(2026, 5, 24, 6, 0, 0, tzinfo=loader.ZoneInfo("UTC"))), \
             mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0):
            result = worker.execute_recovery_session(
                target_date="2026-05-21",
                phase="post",
                wait_until="09:10",
                timezone="Europe/Moscow",
                dry_run=False,
                approve_write=True,
                db_client=db,
                client=client,
                sleep_fn=lambda _seconds: None,
            )
        self.assertEqual(result["status"], "deadline_before_cooldown")

    def test_wait_mode_sleeps_until_cooldown_when_deadline_allows(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient(cooldown_until="2026-05-24T06:10:00+00:00")
        sleeps = []
        with mock.patch.object(worker.loader, "today_local", return_value=loader.datetime(2026, 5, 24, tzinfo=loader.ZoneInfo("Europe/Moscow"))), \
             mock.patch.object(worker.loader, "utcnow", return_value=loader.datetime(2026, 5, 24, 6, 0, 0, tzinfo=loader.ZoneInfo("UTC"))), \
             mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0), \
             mock.patch.object(worker, "build_recovery_plan", side_effect=[
                 {
                     "status": "waiting_for_cooldown",
                     "cooldown_active": True,
                     "will_wait": True,
                     "wait_seconds": 20,
                     "deadline": "2026-05-24T06:40:00+00:00",
                 },
                 {
                     "status": "complete",
                     "cooldown_active": False,
                 },
             ]):
            result = worker.execute_recovery_session(
                target_date="2026-05-21",
                phase="post",
                wait_until="09:40",
                timezone="Europe/Moscow",
                dry_run=False,
                approve_write=True,
                db_client=db,
                client=client,
                sleep_fn=lambda seconds: sleeps.append(seconds),
            )
        self.assertEqual(sleeps, [20])
        self.assertEqual(result["status"], "complete")

    def test_wait_for_minutes_enables_sleep_loop(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient()
        sleeps = []
        with mock.patch.object(worker, "build_recovery_plan", side_effect=[
            {
                "status": "waiting_for_cooldown",
                "cooldown_active": True,
                "will_wait": True,
                "wait_seconds": 12,
                "deadline": "2026-05-24T09:40:00+00:00",
            },
            {
                "status": "complete",
                "cooldown_active": False,
            },
        ]):
            result = worker.execute_recovery_session(
                target_date="2026-05-21",
                phase="post",
                wait_for_minutes=180,
                timezone="Europe/Moscow",
                dry_run=False,
                approve_write=True,
                db_client=db,
                client=client,
                sleep_fn=lambda seconds: sleeps.append(seconds),
            )
        self.assertEqual(sleeps, [12])
        self.assertEqual(result["status"], "complete")

    def test_runs_only_pending_batch_when_budget_and_cooldown_allow(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient()
        with mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0), \
             mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=_progress()):
            plan = worker.build_recovery_plan(
                target_date="2026-05-21",
                db_client=db,
                client=client,
                phase="post",
                max_batches_per_run=1,
            )
        self.assertTrue(plan["will_run"])
        candidate = plan["candidates"][0]
        self.assertEqual(candidate["planned_batch_indexes"], [131])
        self.assertEqual(candidate["planned_recovery_units"], 10)

    def test_dry_run_writes_nothing(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient()
        with mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0), \
             mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=_progress()), \
             mock.patch.object(worker.subprocess, "run") as run_mock:
            plan = worker.execute_recovery_session(
                target_date="2026-05-21",
                phase="post",
                dry_run=True,
                db_client=db,
                client=client,
            )
        run_mock.assert_not_called()
        self.assertTrue(plan["will_run"])

    def test_write_requires_approval(self):
        with self.assertRaises(RuntimeError):
            worker.run_recovery_write({"will_run": True, "candidates": []}, approve_write=False)

    def test_repeated_429_loops_until_deadline(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient()
        plan_before = {
            "status": "planned",
            "cooldown_active": False,
            "will_run": True,
            "candidates": [{"will_run": True}],
        }
        plan_after = {
            "status": "waiting_for_cooldown",
            "cooldown_active": True,
            "cooldown_until": "2026-05-24T06:05:00+00:00",
            "will_wait": True,
            "wait_seconds": 15,
            "deadline": "2026-05-24T06:40:00+00:00",
            "candidates": [{"status": "waiting_for_cooldown"}],
        }
        plan_deadline = {
            "status": "deadline_before_cooldown",
            "cooldown_active": True,
            "cooldown_until": "2026-05-24T07:00:00+00:00",
            "will_wait": False,
            "wait_seconds": 0,
            "deadline": "2026-05-24T06:40:00+00:00",
            "candidates": [{"status": "deadline_before_cooldown"}],
        }
        sleeps = []
        with mock.patch.object(worker, "build_recovery_plan", side_effect=[plan_before, plan_after, plan_before, plan_deadline]), \
             mock.patch.object(worker, "run_recovery_write", return_value={"status": "pending_429"}):
            result = worker.execute_recovery_session(
                target_date="2026-05-21",
                phase="post",
                wait_until="09:40",
                timezone="Europe/Moscow",
                dry_run=False,
                approve_write=True,
                db_client=db,
                client=client,
                sleep_fn=lambda seconds: sleeps.append(seconds),
            )
        self.assertEqual(sleeps, [15])
        self.assertEqual(result["status"], "deadline_after_429")

    def test_successful_batch_followed_by_pending_continues(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient()
        first_plan = {
            "status": "planned",
            "cooldown_active": False,
            "will_run": True,
            "candidates": [{"will_run": True}],
        }
        second_plan = {
            "status": "planned",
            "cooldown_active": False,
            "will_run": True,
            "candidates": [{"will_run": True}],
        }
        complete_plan = {"status": "complete", "cooldown_active": False}
        with mock.patch.object(worker, "build_recovery_plan", side_effect=[first_plan, second_plan, complete_plan]), \
             mock.patch.object(worker, "run_recovery_write", return_value={"status": "success"}):
            result = worker.execute_recovery_session(
                target_date="2026-05-21",
                phase="post",
                wait_until="23:59",
                timezone="Europe/Moscow",
                dry_run=False,
                approve_write=True,
                db_client=db,
                client=client,
                sleep_fn=lambda _seconds: None,
            )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["attempts"], 2)

    def test_pending_zero_returns_complete(self):
        db = _FakeDbClient(
            {loader.DAILY_LOAD_STATUS_TABLE: [_status_row(run_status="success", cpc_status="success", pending_campaigns=0, pending_units=0)]}
        )
        client = _FakeClient()
        plan = worker.build_recovery_plan(
            target_date="2026-05-21",
            db_client=db,
            client=client,
            phase="post",
        )
        self.assertEqual(plan["status"], "complete")

    def test_budget_exhausted_stops_controlled(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row()]})
        client = _FakeClient()
        with mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=1999):
            plan = worker.build_recovery_plan(
                target_date="2026-05-21",
                db_client=db,
                client=client,
                phase="post",
            )
        self.assertEqual(plan["status"], "skipped")
        self.assertEqual(plan["budget_skip_reason"], "skipped_no_recovery_budget")

    def test_write_command_is_gated_and_bounded(self):
        command = worker.build_loader_command("2026-05-21", 1, write=True, progress_key="cpc_progress:pending-tail")
        self.assertIn("--mode", command)
        self.assertIn("cpc-backfill", command)
        self.assertIn("--write", command)
        self.assertIn("--approve-cpc-recovery-write", command)
        self.assertIn("--allow-recovery-worker-before-daily-status", command)
        self.assertIn("--allow-recovery-worker-before-backfill-window", command)
        self.assertIn("--progress-key", command)
        self.assertIn("cpc_progress:pending-tail", command)

    def test_build_candidate_plan_passes_progress_key_to_command(self):
        client = _FakeClient()
        db = _FakeDbClient({})
        status_row = _status_row()
        budget_guard = worker.build_budget_guard(0, phase="post")
        with mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=_progress()):
            candidate = worker.build_candidate_plan(db, client, status_row, budget_guard, 1)
        self.assertEqual(candidate["progress_key"], "cpc_progress:pending-tail")
        self.assertIn("--progress-key", candidate["recovery_command"])
        self.assertIn("cpc_progress:pending-tail", candidate["recovery_command"])

    def test_worker_disambiguates_multiple_pending_progress_rows(self):
        client = _FakeClient()
        db = _FakeDbClient(
            {
                loader.PIPELINE_RUNTIME_STATE_TABLE: [
                    {
                        "state_key": "cpc_progress:cpc_progress:older",
                        "state_type": "cpc_progress",
                        "account_signature": "acct_test",
                        "updated_at": "2026-05-24T20:00:00+00:00",
                        "payload": {
                            "date_from": "2026-05-21",
                            "date_to": "2026-05-21",
                            "selection_mode": "complete",
                            "account_signature": "acct_test",
                            "pending_batches": 10,
                            "pending_batch_indexes": list(range(70, 80)),
                            "batch_size": 10,
                            "total_campaigns": 1200,
                        },
                    },
                    {
                        "state_key": "cpc_progress:cpc_progress:newer",
                        "state_type": "cpc_progress",
                        "account_signature": "acct_test",
                        "updated_at": "2026-05-24T21:00:00+00:00",
                        "payload": {
                            "date_from": "2026-05-21",
                            "date_to": "2026-05-21",
                            "selection_mode": "complete",
                            "account_signature": "acct_test",
                            "pending_batches": 66,
                            "pending_batch_indexes": list(range(67, 133)),
                            "batch_size": 10,
                            "total_campaigns": 1323,
                        },
                    },
                ]
            }
        )
        status_row = _status_row()
        budget_guard = worker.build_budget_guard(0, phase="post")
        with mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=(None, None, None)):
            candidate = worker.build_candidate_plan(db, client, status_row, budget_guard, 1)
        self.assertEqual(candidate["progress_key"], "cpc_progress:newer")
        self.assertEqual(candidate["source_progress_kind"], "daily_yesterday_pending_disambiguated")

    def test_missing_progress_requires_reconstruction_when_status_has_completed_units(self):
        client = _FakeClient()
        db = _FakeDbClient({})
        status_row = _status_row(pending_units=285)
        status_row["cpc_campaign_units_completed_total"] = 1390
        budget_guard = worker.build_budget_guard(0, phase="post")
        with mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=(None, None, None)):
            candidate = worker.build_candidate_plan(db, client, status_row, budget_guard, 1)
        self.assertEqual(candidate["status"], "missing_progress_reconstruction_required")
        self.assertFalse(candidate["will_run"])

    def test_progress_reset_to_batch_zero_requires_reconstruction(self):
        client = _FakeClient()
        db = _FakeDbClient({})
        status_row = _status_row(pending_units=285)
        status_row["cpc_campaign_units_completed_total"] = 1390
        progress = (
            "cpc_progress:reset",
            {
                "ordered_campaign_ids": [f"{9834000 + i}" for i in range(1323)],
                "batch_size": 10,
                "pending_batch_indexes": [0, 1, 2],
            },
            "existing_backfill_progress",
        )
        budget_guard = worker.build_budget_guard(0, phase="post")
        with mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=progress):
            candidate = worker.build_candidate_plan(db, client, status_row, budget_guard, 1)
        self.assertEqual(candidate["status"], "missing_progress_reconstruction_required")
        self.assertFalse(candidate["will_run"])

    def test_failed_status_with_live_progress_is_recoverable_partial_crash(self):
        client = _FakeClient()
        db = _FakeDbClient({})
        status_row = _status_row(run_status="failed", cpc_status="failed", pending_units=1085, pending_campaigns=1085)
        status_row["cpc_campaign_units_completed_total"] = 150
        budget_guard = worker.build_budget_guard(0, phase="post")
        with mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=_progress([15, 16])):
            candidate = worker.build_candidate_plan(db, client, status_row, budget_guard, 1)
        self.assertEqual(candidate["status"], "recoverable_partial_crash")
        self.assertTrue(candidate["will_run"])
        self.assertEqual(candidate["next_batch_index"], 15)
        self.assertEqual(candidate["planned_batch_indexes"], [15])
        self.assertEqual(candidate["total_campaigns"], 1323)

    def test_missing_status_with_live_progress_is_recoverable_progress_without_status(self):
        client = _FakeClient()
        db = _FakeDbClient(
            {
                loader.PIPELINE_RUNTIME_STATE_TABLE: [
                    {
                        "state_key": "cpc_progress:cpc_progress:smart-tail",
                        "state_type": "cpc_progress",
                        "account_signature": "acct_test",
                        "updated_at": "2026-05-28T06:58:17+00:00",
                        "payload": {
                            "date_from": "2026-05-27",
                            "date_to": "2026-05-27",
                            "selection_mode": "smart_recent_active",
                            "account_signature": "acct_test",
                            "total_campaigns": 1316,
                            "batch_size": 10,
                            "completed_batches": 68,
                            "pending_batches": 64,
                            "next_batch_index": 68,
                            "pending_batch_indexes": list(range(68, 132)),
                            "ordered_campaign_ids": [f"{9834000 + i}" for i in range(1316)],
                        },
                    }
                ]
            }
        )
        with mock.patch.object(worker.loader, "get_statistics_json_budget_diagnostics", return_value={
            "daily_budget_used_today": 0,
            "budget_source": "runtime_usage_ledger",
            "budget_confidence": "high",
            "usage_event_count": 0,
            "daily_limit": 2000,
        }), mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=(None, None, None)):
            plan = worker.build_recovery_plan(
                target_date="2026-05-27",
                db_client=db,
                client=client,
                phase="post",
                max_batches_per_run=1,
            )
        self.assertEqual(plan["status"], "planned")
        candidate = plan["candidates"][0]
        self.assertEqual(candidate["status"], "recoverable_progress_without_status")
        self.assertEqual(candidate["selection_mode"], "smart_recent_active")
        self.assertEqual(candidate["next_batch_index"], 68)
        self.assertEqual(candidate["planned_batch_indexes"], [68])

    def test_missing_status_with_no_progress_is_not_recoverable(self):
        client = _FakeClient()
        db = _FakeDbClient({})
        with mock.patch.object(worker.loader, "get_statistics_json_budget_diagnostics", return_value={
            "daily_budget_used_today": 0,
            "budget_source": "runtime_usage_ledger",
            "budget_confidence": "high",
            "usage_event_count": 0,
            "daily_limit": 2000,
        }), mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=(None, None, None)):
            plan = worker.build_recovery_plan(
                target_date="2026-05-27",
                db_client=db,
                client=client,
                phase="post",
                max_batches_per_run=1,
            )
        self.assertEqual(plan["status"], "no_partial_candidates")

    def test_failed_status_with_no_progress_is_failed_no_progress(self):
        client = _FakeClient()
        db = _FakeDbClient({})
        status_row = _status_row(run_status="failed", cpc_status="failed", pending_units=0, pending_campaigns=0)
        status_row["cpc_campaign_units_completed_total"] = 0
        budget_guard = worker.build_budget_guard(0, phase="post")
        with mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=(None, None, None)):
            candidate = worker.build_candidate_plan(db, client, status_row, budget_guard, 1)
        self.assertEqual(candidate["status"], "failed_no_progress")
        self.assertFalse(candidate["will_run"])

    def test_usage_ledger_does_not_override_progress_next_batch_index(self):
        client = _FakeClient()
        client.get_statistics_json_usage_events = mock.Mock(return_value=[
            {
                "target_date": "2026-05-21",
                "load_date": "2026-05-22",
                "batch_index": 99,
                "response_kind": "success",
                "campaign_units": 10,
            }
        ])
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row(run_status="failed", cpc_status="failed", pending_units=1085, pending_campaigns=1085)]})
        with mock.patch.object(worker.loader, "get_statistics_json_budget_diagnostics", return_value={
            "daily_budget_used_today": 40,
            "budget_source": "runtime_usage_ledger",
            "budget_confidence": "high",
            "usage_event_count": 1,
        }), mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=_progress([15, 16])):
            plan = worker.build_recovery_plan(
                target_date="2026-05-21",
                db_client=db,
                client=client,
                phase="post",
                max_batches_per_run=1,
            )
        candidate = plan["candidates"][0]
        self.assertEqual(candidate["next_batch_index"], 15)
        self.assertEqual(candidate["planned_batch_indexes"], [15])

    def test_runtime_state_unavailable_is_controlled_worker_result(self):
        plan = {
            "will_run": True,
            "candidates": [
                {
                    "will_run": True,
                    "target_date": "2026-05-23",
                    "planned_batch_indexes": [67],
                    "progress_key": "cpc_progress:pending-tail",
                }
            ],
        }
        completed = mock.Mock(returncode=0, stdout='{"status": "runtime_state_unavailable"}', stderr="")
        with mock.patch.object(worker.subprocess, "run", return_value=completed):
            result = worker.run_recovery_write(plan, approve_write=True)
        self.assertEqual(result["status"], "runtime_state_unavailable")

    def test_pending_quota_is_controlled_worker_result(self):
        plan = {
            "will_run": True,
            "candidates": [
                {
                    "will_run": True,
                    "target_date": "2026-05-25",
                    "planned_batch_indexes": [139],
                    "progress_key": "cpc_progress:pending-tail",
                }
            ],
        }
        completed = mock.Mock(returncode=0, stdout='{"status": "pending_quota"}', stderr="")
        with mock.patch.object(worker.subprocess, "run", return_value=completed):
            result = worker.run_recovery_write(plan, approve_write=True)
        self.assertEqual(result["status"], "pending_quota")

    def test_daily_quota_exhausted_stops_without_wait_loop(self):
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [_status_row(target_date="2026-05-25")]})
        client = _FakeClient()
        first_plan = {
            "status": "planned",
            "cooldown_active": False,
            "will_run": True,
            "candidates": [{"will_run": True}],
        }
        quota_plan = {
            "status": "skipped_daily_quota_exhausted",
            "cooldown_active": False,
            "will_run": False,
            "candidates": [{"status": "skipped_daily_quota_exhausted", "next_attempt_at": "2026-05-27T00:05:00+00:00"}],
        }
        sleeps = []
        with mock.patch.object(worker, "build_recovery_plan", side_effect=[first_plan, quota_plan]), \
             mock.patch.object(worker, "run_recovery_write", return_value={"status": "pending_quota"}):
            result = worker.execute_recovery_session(
                target_date="2026-05-25",
                phase="post",
                wait_for_minutes=240,
                timezone="Europe/Moscow",
                dry_run=False,
                approve_write=True,
                db_client=db,
                client=client,
                sleep_fn=lambda seconds: sleeps.append(seconds),
            )
        self.assertEqual(result["status"], "skipped_daily_quota_exhausted")
        self.assertEqual(sleeps, [])


    def test_quota_exhausted_from_prior_window_allows_recovery(self):
        prior_status_row = {
            "load_date": "2026-05-28",
            "target_date": "2026-05-27",
            "marketplace_code": "ozon",
            "account_signature": "acct_test",
            "run_status": "partial_quota",
            "cpc_status": "pending_quota",
            "cpo_status": "success",
            "cpc_pending_campaigns": 496,
            "cpc_campaign_units_pending_total": 496,
            "cpc_campaign_units_completed_total": 820,
            "updated_at": "2026-05-28T07:31:43+00:00",
        }
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [prior_status_row]})
        client = _FakeClient()
        today = loader.date(2026, 5, 30)
        with mock.patch.object(worker.loader, "today_local", return_value=today), \
             mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0), \
             mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=_progress([83])):
            plan = worker.build_recovery_plan(
                target_date="2026-05-27",
                db_client=db,
                client=client,
                phase="post",
                max_batches_per_run=1,
            )
        self.assertTrue(plan["will_run"])
        candidate = plan["candidates"][0]
        self.assertNotEqual(candidate.get("status"), "skipped_daily_quota_exhausted")
        self.assertTrue(candidate.get("will_run"))

    def test_quota_exhausted_same_window_stays_blocked(self):
        same_day_status_row = {
            "load_date": "2026-05-30",
            "target_date": "2026-05-29",
            "marketplace_code": "ozon",
            "account_signature": "acct_test",
            "run_status": "partial_quota",
            "cpc_status": "pending_quota",
            "cpo_status": "success",
            "cpc_pending_campaigns": 100,
            "cpc_campaign_units_pending_total": 100,
            "cpc_campaign_units_completed_total": 900,
            "updated_at": "2026-05-30T04:00:00+00:00",
        }
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [same_day_status_row]})
        client = _FakeClient()
        today = loader.date(2026, 5, 30)
        with mock.patch.object(worker.loader, "today_local", return_value=today), \
             mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0), \
             mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=_progress([90])):
            plan = worker.build_recovery_plan(
                target_date="2026-05-29",
                db_client=db,
                client=client,
                phase="post",
                max_batches_per_run=1,
            )
        self.assertFalse(plan["will_run"])
        candidate = plan["candidates"][0]
        self.assertEqual(candidate.get("status"), "skipped_daily_quota_exhausted")

    def test_pre_phase_prioritizes_yesterday_over_older_date(self):
        today = loader.date(2026, 5, 31)
        yesterday_row = {
            "load_date": "2026-05-31",
            "target_date": "2026-05-30",
            "marketplace_code": "ozon",
            "account_signature": "acct_test",
            "run_status": "partial_ads",
            "cpc_status": "pending_backfill",
            "cpo_status": "success",
            "cpc_pending_campaigns": 260,
            "cpc_campaign_units_pending_total": 260,
            "cpc_campaign_units_completed_total": 990,
            "updated_at": "2026-05-31T03:18:00+00:00",
        }
        older_row = {
            "load_date": "2026-05-31",
            "target_date": "2026-05-27",
            "marketplace_code": "ozon",
            "account_signature": "acct_test",
            "run_status": "partial_ads",
            "cpc_status": "pending_backfill",
            "cpo_status": "success",
            "cpc_pending_campaigns": 486,
            "cpc_campaign_units_pending_total": 486,
            "cpc_campaign_units_completed_total": 830,
            "updated_at": "2026-05-31T10:00:00+00:00",
        }
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [yesterday_row, older_row]})
        client = _FakeClient()
        with mock.patch.object(worker.loader, "today_local", return_value=today), \
             mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0), \
             mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=_progress([99])):
            plan = worker.build_recovery_plan(
                db_client=db,
                client=client,
                phase="pre",
                max_batches_per_run=1,
            )
        self.assertTrue(plan["will_run"])
        self.assertEqual(plan["selected_target_date"], "2026-05-30")

    def test_pre_phase_falls_back_to_older_date_when_no_yesterday(self):
        today = loader.date(2026, 5, 31)
        older_row = {
            "load_date": "2026-05-30",
            "target_date": "2026-05-27",
            "marketplace_code": "ozon",
            "account_signature": "acct_test",
            "run_status": "partial_ads",
            "cpc_status": "pending_backfill",
            "cpo_status": "success",
            "cpc_pending_campaigns": 486,
            "cpc_campaign_units_pending_total": 486,
            "cpc_campaign_units_completed_total": 830,
            "updated_at": "2026-05-31T03:18:00+00:00",
        }
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [older_row]})
        client = _FakeClient()
        with mock.patch.object(worker.loader, "today_local", return_value=today), \
             mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0), \
             mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=_progress([83])):
            plan = worker.build_recovery_plan(
                db_client=db,
                client=client,
                phase="pre",
                max_batches_per_run=1,
            )
        self.assertTrue(plan["will_run"])
        self.assertEqual(plan["selected_target_date"], "2026-05-27")

    def test_post_phase_prioritizes_yesterday_over_older_date(self):
        today = loader.date(2026, 6, 1)
        yesterday_row = {
            "load_date": "2026-06-01",
            "target_date": "2026-05-31",
            "marketplace_code": "ozon",
            "account_signature": "acct_test",
            "run_status": "partial_ads",
            "cpc_status": "pending_backfill",
            "cpo_status": "success",
            "cpc_pending_campaigns": 260,
            "cpc_campaign_units_pending_total": 260,
            "cpc_campaign_units_completed_total": 990,
            "updated_at": "2026-06-01T00:30:00+00:00",
        }
        older_row = {
            "load_date": "2026-06-01",
            "target_date": "2026-05-27",
            "marketplace_code": "ozon",
            "account_signature": "acct_test",
            "run_status": "partial_ads",
            "cpc_status": "pending_backfill",
            "cpo_status": "success",
            "cpc_pending_campaigns": 476,
            "cpc_campaign_units_pending_total": 476,
            "cpc_campaign_units_completed_total": 840,
            "updated_at": "2026-06-01T07:58:20+00:00",  # more recently updated
        }
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [yesterday_row, older_row]})
        client = _FakeClient()
        with mock.patch.object(worker.loader, "today_local", return_value=today), \
             mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0), \
             mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=_progress([99])):
            plan = worker.build_recovery_plan(
                db_client=db,
                client=client,
                phase="post",
                max_batches_per_run=1,
            )
        self.assertTrue(plan["will_run"])
        self.assertEqual(plan["selected_target_date"], "2026-05-31")

    def test_post_phase_sorts_by_minimum_pending_when_no_yesterday(self):
        today = loader.date(2026, 6, 1)
        high_pending_row = {
            "load_date": "2026-06-01",
            "target_date": "2026-05-27",
            "marketplace_code": "ozon",
            "account_signature": "acct_test",
            "run_status": "partial_ads",
            "cpc_status": "pending_backfill",
            "cpo_status": "success",
            "cpc_pending_campaigns": 476,
            "cpc_campaign_units_pending_total": 476,
            "cpc_campaign_units_completed_total": 840,
            "updated_at": "2026-06-01T07:58:20+00:00",  # more recently updated → comes first from DB
        }
        low_pending_row = {
            "load_date": "2026-06-01",
            "target_date": "2026-05-30",
            "marketplace_code": "ozon",
            "account_signature": "acct_test",
            "run_status": "partial_ads",
            "cpc_status": "pending_backfill",
            "cpo_status": "success",
            "cpc_pending_campaigns": 250,
            "cpc_campaign_units_pending_total": 250,
            "cpc_campaign_units_completed_total": 1000,
            "updated_at": "2026-06-01T00:28:06+00:00",
        }
        db = _FakeDbClient({loader.DAILY_LOAD_STATUS_TABLE: [high_pending_row, low_pending_row]})
        client = _FakeClient()
        with mock.patch.object(worker.loader, "today_local", return_value=today), \
             mock.patch.object(worker.loader, "read_attempted_campaign_units_for_load_date", return_value=0), \
             mock.patch.object(worker.loader, "resolve_cpc_backfill_progress", return_value=_progress([99])):
            plan = worker.build_recovery_plan(
                db_client=db,
                client=client,
                phase="post",
                max_batches_per_run=1,
            )
        self.assertTrue(plan["will_run"])
        self.assertEqual(plan["selected_target_date"], "2026-05-30")  # 250 pending < 476


if __name__ == "__main__":
    unittest.main()
