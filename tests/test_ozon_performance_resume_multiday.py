"""Резюм CPC-прогресса по многодневному окну.

До 2026-09-17 все читатели прогресса требовали date_from == date_to == target_date:
прогресс окна cpc-backfill (--date-from/--date-to) записывался, но для даты внутри
окна был невидим, и recovery-воркер начинал такую дату с batch 0. Правило §2
«не начинать recovery с batch 0, если есть partial progress» было неисполнимо,
воркер выключен (--skip-recovery). Здесь — чтение окна, предпочтение точного дня,
резюм с середины окна и запрет повтора batch 0. Живых вызовов нет.
"""
import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader
import scripts.ozon_performance_recovery_worker as worker
from tests.test_ozon_performance_cpc_recovery import _FakeClient, _FakeDbClient

ACCT = "acct_d743d49318d3"


def window_progress(date_from, date_to, pending=(5, 6), completed=(0, 1, 2, 3, 4), **extra):
    progress = {
        "date_from": date_from,
        "date_to": date_to,
        "selection_mode": "complete",
        "account_signature": ACCT,
        "completed_batch_indexes": list(completed),
        "completed_batches": len(completed),
        "pending_batch_indexes": list(pending),
        "pending_batches": len(pending),
        "total_campaigns": 70,
        "batch_size": 10,
        "ordered_campaign_ids": [str(1000 + i) for i in range(70)],
        "updated_at": "2026-08-31T03:00:00+00:00",
    }
    progress.update(extra)
    return progress


def db_row(key, payload):
    return {"state_key": f"cpc_progress:{key}", "state_type": "cpc_progress", "account_signature": ACCT,
            "updated_at": payload.get("updated_at"), "payload": payload}


class WindowHelpersTests(unittest.TestCase):
    def test_window_covers_dates_inside_and_edges(self):
        p = window_progress("2026-07-13", "2026-07-30")
        for d in ("2026-07-13", "2026-07-20", "2026-07-30"):
            self.assertTrue(loader.cpc_progress_covers_date(p, d), d)
        for d in ("2026-07-12", "2026-07-31"):
            self.assertFalse(loader.cpc_progress_covers_date(p, d), d)
        self.assertFalse(loader.cpc_progress_is_exact_day(p, "2026-07-20"))
        self.assertTrue(loader.cpc_progress_is_exact_day(window_progress("2026-07-20", "2026-07-20"), "2026-07-20"))

    def test_window_days(self):
        self.assertEqual(loader.cpc_progress_window_days(window_progress("2026-07-13", "2026-07-15")),
                         ["2026-07-13", "2026-07-14", "2026-07-15"])
        self.assertEqual(loader.cpc_progress_window_days({}), [])


class ResolveExistingTests(unittest.TestCase):
    def test_window_progress_is_found_for_a_date_inside(self):
        client = _FakeClient(progress_map={"win": window_progress("2026-07-13", "2026-07-30")})
        key, progress = loader.resolve_existing_cpc_backfill_progress(client, "2026-07-20")
        self.assertEqual(key, "win")
        self.assertEqual(progress["pending_batch_indexes"], [5, 6])

    def test_exact_day_is_preferred_over_window(self):
        client = _FakeClient(progress_map={
            "win": window_progress("2026-07-13", "2026-07-30", total_campaigns=2000),
            "day": window_progress("2026-07-20", "2026-07-20", pending=(9,), completed=tuple(range(9))),
        })
        key, _ = loader.resolve_existing_cpc_backfill_progress(client, "2026-07-20")
        self.assertEqual(key, "day")

    def test_date_outside_window_is_not_found(self):
        client = _FakeClient(progress_map={"win": window_progress("2026-07-13", "2026-07-30")})
        self.assertEqual(loader.resolve_existing_cpc_backfill_progress(client, "2026-08-01"), (None, None))


class ResolveDailyPendingFromDbTests(unittest.TestCase):
    def test_single_window_row_is_found(self):
        fake = _FakeDbClient({"pipeline_runtime_state": [db_row("win", window_progress("2026-07-13", "2026-07-30"))]})
        with mock.patch.object(loader, "supabase", fake):
            key, progress = loader.resolve_daily_pending_cpc_progress_from_db(_FakeClient(progress_map={}), "2026-07-20")
        self.assertEqual(key, "win")
        self.assertEqual(progress["pending_batch_indexes"], [5, 6])

    def test_exact_day_row_wins_over_window_row(self):
        fake = _FakeDbClient({"pipeline_runtime_state": [
            db_row("win", window_progress("2026-07-13", "2026-07-30")),
            db_row("day", window_progress("2026-07-20", "2026-07-20", pending=(9,), completed=tuple(range(9)))),
        ]})
        with mock.patch.object(loader, "supabase", fake):
            key, _ = loader.resolve_daily_pending_cpc_progress_from_db(_FakeClient(progress_map={}), "2026-07-20")
        self.assertEqual(key, "day")

    def test_two_windows_stay_ambiguous(self):
        fake = _FakeDbClient({"pipeline_runtime_state": [
            db_row("a", window_progress("2026-07-13", "2026-07-30")),
            db_row("b", window_progress("2026-07-01", "2026-07-25")),
        ]})
        with mock.patch.object(loader, "supabase", fake):
            self.assertEqual(loader.resolve_daily_pending_cpc_progress_from_db(_FakeClient(progress_map={}), "2026-07-20"), (None, None))


class ExactKeyTests(unittest.TestCase):
    def test_window_record_resumes_by_key_for_a_date_inside(self):
        fake = _FakeDbClient({"pipeline_runtime_state": [db_row("win", window_progress("2026-07-13", "2026-07-30"))]})
        with mock.patch.object(loader, "supabase", fake):
            key, progress, kind = loader.resolve_cpc_backfill_progress_by_key(_FakeClient(progress_map={}), "win", "2026-07-20", sleep_fn=lambda _s: None)
        self.assertEqual((key, kind), ("win", "exact_progress_key"))
        self.assertEqual(progress["next_batch_index"], 5)

    def test_window_record_rejects_a_date_outside(self):
        fake = _FakeDbClient({"pipeline_runtime_state": [db_row("win", window_progress("2026-07-13", "2026-07-30"))]})
        with mock.patch.object(loader, "supabase", fake), self.assertRaises(RuntimeError) as ctx:
            loader.resolve_cpc_backfill_progress_by_key(_FakeClient(progress_map={}), "win", "2026-08-05", sleep_fn=lambda _s: None)
        self.assertIn("does not cover", str(ctx.exception))


class ResumeFromMiddleTests(unittest.TestCase):
    def test_validate_continues_from_first_pending_not_batch_zero(self):
        progress = loader.validate_cpc_resume_progress(window_progress("2026-07-13", "2026-07-30"), "win", target_date="2026-07-20")
        self.assertEqual(progress["next_batch_index"], 5)
        self.assertNotIn(0, progress["pending_batch_indexes"])

    def test_seed_for_resume_keeps_completed_batches_out_of_pending(self):
        client = _FakeClient(progress_map={})
        seeded = loader.seed_existing_cpc_progress_for_resume(client, "win", window_progress("2026-07-13", "2026-07-30"))
        self.assertEqual(seeded["next_batch_index"], 5)
        self.assertEqual(seeded["completed_batches"], 5)
        self.assertEqual(seeded["pending_batch_indexes"], [5, 6])

    def test_validate_refuses_progress_without_pending(self):
        with self.assertRaises(RuntimeError):
            loader.validate_cpc_resume_progress(window_progress("2026-07-13", "2026-07-30", pending=(), completed=tuple(range(7))), "win", target_date="2026-07-20")


class AdoptWindowTests(unittest.TestCase):
    def test_single_day_inside_window_adopts_window_and_writes_only_that_day(self):
        date_from, date_to, targets, adopted = loader.adopt_cpc_resume_window(
            "2026-07-20", "2026-07-20", "2026-07-20", window_progress("2026-07-13", "2026-07-30"), [])
        self.assertTrue(adopted)
        self.assertEqual((date_from, date_to), ("2026-07-13", "2026-07-30"))
        self.assertEqual(targets, ["2026-07-20"])

    def test_same_window_is_left_alone(self):
        result = loader.adopt_cpc_resume_window("2026-07-13", "2026-07-30", None, window_progress("2026-07-13", "2026-07-30"), ["2026-07-20", "2026-07-21"])
        self.assertEqual(result, ("2026-07-13", "2026-07-30", ["2026-07-20", "2026-07-21"], False))

    def test_explicit_target_dates_are_kept(self):
        _f, _t, targets, adopted = loader.adopt_cpc_resume_window("2026-07-20", "2026-07-20", "2026-07-20", window_progress("2026-07-13", "2026-07-30"), ["2026-07-20", "2026-07-21"])
        self.assertTrue(adopted)
        self.assertEqual(targets, ["2026-07-20", "2026-07-21"])

    def test_day_outside_window_raises(self):
        with self.assertRaises(RuntimeError):
            loader.adopt_cpc_resume_window("2026-08-05", "2026-08-05", "2026-08-05", window_progress("2026-07-13", "2026-07-30"), [])


class WorkerTests(unittest.TestCase):
    def test_worker_finds_window_progress_in_db_fallback(self):
        client = _FakeClient(progress_map={})
        db = _FakeDbClient({"pipeline_runtime_state": [db_row("win", window_progress("2026-07-13", "2026-07-30"))]})
        with mock.patch.object(loader, "supabase", _FakeDbClient({"pipeline_runtime_state": []})):
            key, progress, kind = worker.resolve_worker_progress_candidate(db, client, "2026-07-20")
        self.assertEqual(key, "win")
        self.assertEqual(progress["pending_batch_indexes"], [5, 6])

    def test_worker_prefers_exact_day_row(self):
        client = _FakeClient(progress_map={})
        db = _FakeDbClient({"pipeline_runtime_state": [
            db_row("win", window_progress("2026-07-13", "2026-07-30", total_campaigns=2000)),
            db_row("day", window_progress("2026-07-20", "2026-07-20", pending=(9,), completed=tuple(range(9)))),
        ]})
        with mock.patch.object(loader, "supabase", _FakeDbClient({"pipeline_runtime_state": []})):
            key, _p, _k = worker.resolve_worker_progress_candidate(db, client, "2026-07-20")
        self.assertEqual(key, "day")


if __name__ == "__main__":
    unittest.main()
