import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)
        self._eq = {}
        self._in = {}
        self._start = 0
        self._end = None

    def select(self, _fields):
        return self

    def eq(self, field, value):
        self._eq[field] = value
        return self

    def in_(self, field, values):
        self._in[field] = set(values)
        return self

    def range(self, start, end):
        self._start = start
        self._end = end
        return self

    def execute(self):
        rows = list(self._rows)
        for field, value in self._eq.items():
            rows = [row for row in rows if row.get(field) == value]
        for field, values in self._in.items():
            rows = [row for row in rows if row.get(field) in values]
        if self._end is None:
            batch = rows[self._start :]
        else:
            batch = rows[self._start : self._end + 1]
        return _FakeResult(batch)


class _FakeDbClient:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _FakeQuery(self.tables.get(name, []))


class _FakeClient:
    def __init__(self, progress_map=None):
        self.state = {"cpc_progress": {key: {} for key in (progress_map or {}).keys()}}
        self._progress_map = progress_map or {}

    def get_cpc_progress(self, key):
        return self._progress_map.get(key)

    def ensure_token(self):
        return "token"


def _sample_campaign(campaign_id="24375352"):
    return {
        "id": campaign_id,
        "title": f"CPC campaign {campaign_id}",
        "paymentModel": "CPC",
        "state": "RUNNING",
        "from": "2026-05-12T00:00:00Z",
        "to": "2026-05-12T23:59:59Z",
    }


def _sample_report(campaign_id="24375352", sku="1300079194", spend=1412.30, orders=1, revenue=112863):
    return {
        str(campaign_id): {
            "title": f"CPC campaign {campaign_id}",
            "report": {
                "rows": [
                    {
                        "date": "2026-05-12",
                        "campaignId": str(campaign_id),
                        "sku": sku,
                        "orders": orders,
                        "ordersMoney": revenue,
                        "moneySpent": spend,
                        "clicks": 10,
                        "views": 100,
                    }
                ]
            },
        }
    }


class OzonPerformanceCpcRecoveryTests(unittest.TestCase):
    def test_write_without_approval_raises(self):
        client = _FakeClient()
        with self.assertRaises(loader.CpcRecoveryWriteNotApprovedError):
            loader.run_cpc_recovery_mode(
                client=client,
                target_date="2026-05-12",
                group_by="DATE",
                requested_batch_size=10,
                max_stats_campaigns=1800,
                dry_run=False,
                write=True,
                approve_write=False,
                ignore_stale_progress_for_date_only=True,
                campaigns=[],
                db_client=_FakeDbClient({}),
            )

    def test_first_submit_429_stops_immediately(self):
        client = _FakeClient(
            progress_map={
                "target-progress": {
                    "date_from": "2026-05-12",
                    "date_to": "2026-05-12",
                    "selection_mode": "complete",
                    "completed_batches": 97,
                    "pending_batches": 0,
                    "total_campaigns": 10,
                    "batch_size": 10,
                    "updated_at": "2026-05-13T00:00:00Z",
                }
            }
        )
        db_client = _FakeDbClient({"marketplace_expenses": [], "ozon_daily_sku_ad_attribution": []})
        fetcher = mock.Mock(
            side_effect=loader.RateLimitPending(
                endpoint="/api/client/statistics/json",
                retry_after_seconds=1800,
                cooldown_until="2026-05-16T00:30:00Z",
                attempt=1,
            )
        )

        with mock.patch.object(loader, "save_rows") as save_rows_mock, mock.patch.object(
            loader, "save_ad_attribution_rows"
        ) as save_attr_mock:
            summary = loader.run_cpc_recovery_mode(
                client=client,
                target_date="2026-05-12",
                group_by="DATE",
                requested_batch_size=10,
                max_stats_campaigns=1800,
                dry_run=True,
                write=False,
                approve_write=False,
                ignore_stale_progress_for_date_only=True,
                no_write=True,
                db_client=db_client,
                campaigns=[_sample_campaign()],
                fetch_batch_fn=fetcher,
            )

        self.assertEqual(summary["status"], "quota_limited_before_refetch")
        self.assertEqual(summary["statistics_json_submit_attempts"], 1)
        self.assertEqual(summary["processed_batches"], 0)
        self.assertEqual(summary["db_writes"], 0)
        self.assertTrue(summary["preflight"]["stale_progress_ignored"])
        fetcher.assert_called_once()
        save_rows_mock.assert_not_called()
        save_attr_mock.assert_not_called()

    def test_stale_progress_is_ignored_only_for_target_date(self):
        client = _FakeClient(
            progress_map={
                "target-progress": {
                    "date_from": "2026-05-12",
                    "date_to": "2026-05-12",
                    "selection_mode": "complete",
                    "completed_batches": 97,
                    "pending_batches": 0,
                    "total_campaigns": 10,
                    "batch_size": 10,
                    "updated_at": "2026-05-13T00:00:00Z",
                },
                "other-progress": {
                    "date_from": "2026-05-11",
                    "date_to": "2026-05-11",
                    "selection_mode": "complete",
                    "completed_batches": 42,
                    "pending_batches": 0,
                    "total_campaigns": 20,
                    "batch_size": 10,
                    "updated_at": "2026-05-12T00:00:00Z",
                },
            }
        )

        summary = loader.run_cpc_recovery_mode(
            client=client,
            target_date="2026-05-12",
            group_by="DATE",
            requested_batch_size=10,
            max_stats_campaigns=1800,
            dry_run=True,
            write=False,
            approve_write=False,
            ignore_stale_progress_for_date_only=True,
            no_write=True,
            db_client=_FakeDbClient({"marketplace_expenses": [], "ozon_daily_sku_ad_attribution": []}),
            campaigns=[],
            fetch_batch_fn=mock.Mock(),
        )

        self.assertEqual(summary["preflight"]["stale_progress"]["progress_key"], "target-progress")
        self.assertEqual(summary["preflight"]["stale_progress"]["campaign_count"], 10)
        self.assertTrue(summary["preflight"]["stale_progress_ignored"])

    def test_campaign_id_limits_selection_to_one_campaign_and_one_submit(self):
        client = _FakeClient()
        campaigns = [_sample_campaign("24375352"), _sample_campaign("24375331")]

        summary = loader.run_cpc_recovery_mode(
            client=client,
            target_date="2026-05-12",
            group_by="DATE",
            requested_batch_size=10,
            max_stats_campaigns=1800,
            dry_run=True,
            write=False,
            approve_write=False,
            ignore_stale_progress_for_date_only=True,
            no_write=True,
            db_client=_FakeDbClient({"marketplace_expenses": [], "ozon_daily_sku_ad_attribution": []}),
            campaigns=campaigns,
            campaign_ids=["24375352"],
            fetch_batch_fn=mock.Mock(return_value={"uuid": "uuid-1", "report_data": _sample_report()}),
        )

        self.assertEqual(summary["preflight"]["requested_campaign_ids"], ["24375352"])
        self.assertEqual(summary["preflight"]["selected_campaign_ids"], ["24375352"])
        self.assertEqual(summary["preflight"]["campaign_count"], 1)
        self.assertEqual(summary["preflight"]["campaign_units"], 1)
        self.assertEqual(summary["preflight"]["total_batches"], 1)
        self.assertEqual(summary["preflight"]["expected_statistics_json_submit_count"], 1)
        self.assertEqual(summary["statistics_json_submit_attempts"], 1)

    def test_dry_run_builds_cpc_rows_and_does_not_write(self):
        client = _FakeClient()
        db_client = _FakeDbClient({"marketplace_expenses": [], "ozon_daily_sku_ad_attribution": []})
        fetcher = mock.Mock(return_value={"uuid": "uuid-1", "report_data": _sample_report()})

        with mock.patch.object(loader, "save_rows") as save_rows_mock, mock.patch.object(
            loader, "save_ad_attribution_rows"
        ) as save_attr_mock:
            summary = loader.run_cpc_recovery_mode(
                client=client,
                target_date="2026-05-12",
                group_by="DATE",
                requested_batch_size=10,
                max_stats_campaigns=1800,
                dry_run=True,
                write=False,
                approve_write=False,
                ignore_stale_progress_for_date_only=True,
                no_write=True,
                db_client=db_client,
                campaigns=[_sample_campaign()],
                fetch_batch_fn=fetcher,
            )

        self.assertEqual(summary["status"], "dry_run_no_write")
        self.assertEqual(summary["db_writes"], 0)
        self.assertEqual(summary["marketplace_expenses_writes"], 0)
        self.assertEqual(summary["ozon_daily_sku_ad_attribution_writes"], 0)
        self.assertEqual(summary["expense_rows_count"], 1)
        self.assertEqual(summary["attribution_rows_count"], 1)
        self.assertAlmostEqual(summary["advertising_clicks_total"], 1412.30, places=2)
        self.assertAlmostEqual(summary["cpc_attribution_spend_total"], 1412.30, places=2)
        self.assertEqual(summary["marketplace_expenses_rows"][0]["expense_type"], "advertising_clicks")
        self.assertEqual(summary["ad_attribution_rows"][0]["ad_source"], "cpc")
        self.assertEqual(summary["ad_attribution_rows"][0]["campaign_id"], "24375352")
        save_rows_mock.assert_not_called()
        save_attr_mock.assert_not_called()

    def test_existing_report_uuid_path_uses_existing_report_only(self):
        client = _FakeClient()
        db_client = _FakeDbClient({"marketplace_expenses": [], "ozon_daily_sku_ad_attribution": []})
        fetch_batch_fn = mock.Mock()
        fetch_existing_report_fn = mock.Mock(
            return_value={"uuid": "15c6d258-e4e8-4c9b-bd53-cbcee9ecbc15", "report_data": _sample_report()}
        )

        with mock.patch.object(loader, "save_rows") as save_rows_mock, mock.patch.object(
            loader, "save_ad_attribution_rows"
        ) as save_attr_mock:
            summary = loader.run_cpc_recovery_mode(
                client=client,
                target_date="2026-05-12",
                group_by="DATE",
                requested_batch_size=10,
                max_stats_campaigns=1800,
                dry_run=True,
                write=False,
                approve_write=False,
                ignore_stale_progress_for_date_only=True,
                no_write=True,
                db_client=db_client,
                campaigns=[_sample_campaign()],
                campaign_ids=["24375352"],
                existing_report_uuid="15c6d258-e4e8-4c9b-bd53-cbcee9ecbc15",
                fetch_batch_fn=fetch_batch_fn,
                fetch_existing_report_fn=fetch_existing_report_fn,
            )

        self.assertEqual(summary["status"], "dry_run_no_write")
        self.assertFalse(summary["used_statistics_json"])
        self.assertTrue(summary["used_existing_report_uuid"])
        self.assertEqual(summary["statistics_json_submit_attempts"], 0)
        self.assertEqual(summary["processed_batches"], 1)
        self.assertEqual(summary["preflight"]["expected_statistics_json_submit_count"], 0)
        self.assertEqual(summary["preflight"]["selected_campaign_ids"], ["24375352"])
        self.assertAlmostEqual(summary["advertising_clicks_total"], 1412.30, places=2)
        self.assertEqual(summary["marketplace_expenses_rows"][0]["marketplace_sku"], "1300079194")
        self.assertEqual(summary["ad_attribution_rows"][0]["campaign_id"], "24375352")
        fetch_batch_fn.assert_not_called()
        fetch_existing_report_fn.assert_called_once()
        save_rows_mock.assert_not_called()
        save_attr_mock.assert_not_called()

    def test_existing_report_uuid_fails_when_campaign_scoped_row_not_found(self):
        client = _FakeClient()
        fetch_existing_report_fn = mock.Mock(
            return_value={"uuid": "15c6d258-e4e8-4c9b-bd53-cbcee9ecbc15", "report_data": _sample_report(campaign_id="999")}
        )

        summary = loader.run_cpc_recovery_mode(
            client=client,
            target_date="2026-05-12",
            group_by="DATE",
            requested_batch_size=10,
            max_stats_campaigns=1800,
            dry_run=True,
            write=False,
            approve_write=False,
            ignore_stale_progress_for_date_only=True,
            no_write=True,
            db_client=_FakeDbClient({"marketplace_expenses": [], "ozon_daily_sku_ad_attribution": []}),
            campaigns=[_sample_campaign()],
            campaign_ids=["24375352"],
            existing_report_uuid="15c6d258-e4e8-4c9b-bd53-cbcee9ecbc15",
            fetch_existing_report_fn=fetch_existing_report_fn,
        )

        self.assertEqual(summary["status"], "expected_row_not_found")
        self.assertEqual(summary["reason"], "campaign_scoped_report_has_no_matching_rows")
        self.assertEqual(summary["db_writes"], 0)


if __name__ == "__main__":
    unittest.main()
