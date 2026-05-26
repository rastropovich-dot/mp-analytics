import importlib.util
from pathlib import Path
import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader


MODULE_PATH = Path("/Users/mihaileliseev/mp-analytics/scripts/reconstruct_ozon_cpc_progress.py")
SPEC = importlib.util.spec_from_file_location("reconstruct_ozon_cpc_progress", MODULE_PATH)
reconstruct = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reconstruct)


class _FakeResult:
    def __init__(self, data=None):
        self.data = data or []


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)
        self._eq = {}
        self._order_field = None
        self._order_desc = False
        self._limit = None
        self.upsert_rows = None
        self.upsert_conflict = None
        self.action = "select"

    def select(self, _fields):
        self.action = "select"
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

    def upsert(self, rows, on_conflict=None):
        self.action = "upsert"
        self.upsert_rows = rows
        self.upsert_conflict = on_conflict
        return self

    def execute(self):
        if self.action == "upsert":
            return _FakeResult([])
        rows = list(self._rows)
        for field, value in self._eq.items():
            rows = [row for row in rows if row.get(field) == value]
        if self._order_field:
            rows = sorted(rows, key=lambda row: row.get(self._order_field), reverse=self._order_desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return _FakeResult(rows)


class _FakeSupabase:
    def __init__(self, daily_rows=None):
        self.daily_query = _FakeQuery(daily_rows or [])
        self.runtime_query = _FakeQuery([])

    def table(self, name):
        if name == loader.DAILY_LOAD_STATUS_TABLE:
            return self.daily_query
        if name == loader.PIPELINE_RUNTIME_STATE_TABLE:
            return self.runtime_query
        raise AssertionError(f"Unexpected table {name}")


class _FakeClient:
    def __init__(self, campaigns, account_signature="acct_d743d49318d3"):
        self._campaigns = list(campaigns)
        self.account_signature = account_signature

    def list_campaigns(self):
        return list(self._campaigns)

    def build_cpc_progress_context(self, *args, **kwargs):
        return loader.OzonPerformanceClient.build_cpc_progress_context(self, *args, **kwargs)


def _campaign(campaign_id):
    return {
        "id": str(campaign_id),
        "paymentType": "CPC",
        "from": "2026-05-23T00:00:00Z",
        "to": "2026-05-23T23:59:59Z",
        "updatedAt": "2026-05-24T00:00:00Z",
        "state": "RUNNING",
    }


def _daily_status(**overrides):
    row = {
        "load_date": "2026-05-24",
        "target_date": "2026-05-23",
        "marketplace_code": "ozon",
        "account_signature": "acct_d743d49318d3",
        "run_status": "partial_ads",
        "cpc_status": "pending_429",
        "cpo_status": "success",
        "cpc_campaign_count": 1323,
        "cpc_campaign_units_completed_total": 670,
        "cpc_campaign_units_pending_total": 653,
        "cpc_stop_batch_index": 67,
        "updated_at": "2026-05-24T20:59:38.78585+00:00",
    }
    row.update(overrides)
    return row


def _run_evidence(**overrides):
    payload = {
        "run_key": "2026-05-23:2026-05-23",
        "cpc_progress_key": "cpc_progress:2a090cd8790741d88d7c71127158738f6aa6bbd19dddf79bb17570c601dbfa2d",
        "batch_size": 10,
        "total_batches": 133,
        "failed_batch_index": 67,
        "failed_batch": [str(cid) for cid in range(25049153, 25049163)],
        "campaign_count": 1323,
        "campaign_list_hash": loader.compute_campaign_list_hash([str(cid) for cid in range(25048483, 25049806)]),
        "ordering_source": "saved_ordered_campaign_ids",
    }
    payload.update(overrides)
    return payload


class ReconstructOzonCpcProgressTests(unittest.TestCase):
    def test_reconstruction_refuses_if_daily_status_not_partial_ads(self):
        fake_supabase = _FakeSupabase([_daily_status(run_status="success", cpc_status="success", cpc_campaign_units_pending_total=0)])
        fake_client = _FakeClient([_campaign(cid) for cid in range(25048483, 25049806)])
        with mock.patch.object(reconstruct.loader, "supabase", fake_supabase), \
             mock.patch.object(reconstruct.loader, "OzonPerformanceClient", return_value=fake_client):
            plan = reconstruct.reconstruct_progress_plan("2026-05-23")
        self.assertEqual(plan["status"], "reconstruction_unsafe")
        self.assertEqual(plan["reason"], "daily_status_not_partial_ads")

    def test_reconstruction_refuses_if_campaign_count_mismatch(self):
        fake_supabase = _FakeSupabase([_daily_status(cpc_campaign_count=1200)])
        fake_client = _FakeClient([_campaign(cid) for cid in range(25048483, 25049806)])
        with mock.patch.object(reconstruct.loader, "supabase", fake_supabase), \
             mock.patch.object(reconstruct.loader, "OzonPerformanceClient", return_value=fake_client), \
             mock.patch.object(reconstruct, "load_run_evidence_from_file", return_value=_run_evidence()):
            plan = reconstruct.reconstruct_progress_plan("2026-05-23")
        self.assertFalse(plan["validation_result"])
        self.assertFalse(plan["validation_checks"]["campaign_count_matches"])

    def test_reconstruction_refuses_if_failed_batch_ids_mismatch(self):
        fake_supabase = _FakeSupabase([_daily_status()])
        fake_client = _FakeClient([_campaign(cid) for cid in range(25048483, 25049806)])
        bad_evidence = _run_evidence(failed_batch=[str(cid) for cid in range(1, 11)])
        with mock.patch.object(reconstruct.loader, "supabase", fake_supabase), \
             mock.patch.object(reconstruct.loader, "OzonPerformanceClient", return_value=fake_client), \
             mock.patch.object(reconstruct, "load_run_evidence_from_file", return_value=bad_evidence):
            plan = reconstruct.reconstruct_progress_plan("2026-05-23")
        self.assertFalse(plan["validation_result"])
        self.assertFalse(plan["validation_checks"]["failed_batch_ids_match"])

    def test_reconstruction_produces_expected_pending_batch_indexes(self):
        fake_supabase = _FakeSupabase([_daily_status()])
        fake_client = _FakeClient([_campaign(cid) for cid in range(25048483, 25049806)])
        with mock.patch.object(reconstruct.loader, "supabase", fake_supabase), \
             mock.patch.object(reconstruct.loader, "OzonPerformanceClient", return_value=fake_client), \
             mock.patch.object(reconstruct, "load_run_evidence_from_file", return_value=_run_evidence()):
            plan = reconstruct.reconstruct_progress_plan("2026-05-23")
        self.assertTrue(plan["validation_result"])
        self.assertEqual(plan["progress_payload"]["pending_batch_indexes"][0], 67)
        self.assertEqual(plan["progress_payload"]["completed_batch_indexes"][-1], 66)
        self.assertEqual(plan["pending_units"], 653)

    def test_reconstruction_write_touches_only_runtime_state(self):
        fake_supabase = _FakeSupabase([_daily_status()])
        fake_client = _FakeClient([_campaign(cid) for cid in range(25048483, 25049806)])
        with mock.patch.object(reconstruct.loader, "supabase", fake_supabase), \
             mock.patch.object(reconstruct.loader, "OzonPerformanceClient", return_value=fake_client), \
             mock.patch.object(reconstruct, "load_run_evidence_from_file", return_value=_run_evidence()):
            plan = reconstruct.reconstruct_progress_plan("2026-05-23")
            result = reconstruct.write_reconstructed_progress(plan)
        self.assertEqual(result["status"], "written")
        self.assertEqual(fake_supabase.runtime_query.upsert_conflict, "state_key")
        self.assertEqual(fake_supabase.runtime_query.upsert_rows["state_type"], "cpc_progress")


if __name__ == "__main__":
    unittest.main()
