import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader


class WriteUsageToDbTests(unittest.TestCase):
    def test_inserts_correct_fields(self):
        event = {
            "event_at": "2026-06-02T01:00:00+00:00",
            "target_date": "2026-06-01",
            "load_date": "2026-06-02",
            "mode": "daily-yesterday",
            "batch_index": 5,
            "campaign_units": 10,
            "http_status": 200,
            "response_kind": "success",
            "retry_after_seconds": None,
            "account_signature": "abc***xyz",
            "report_uuid": "uuid-123",
            "raw_error_preview": None,
        }
        table_mock = mock.MagicMock()
        with mock.patch.object(loader, "supabase") as mock_sb:
            mock_sb.table.return_value = table_mock
            table_mock.insert.return_value = table_mock
            loader.write_statistics_json_usage_to_db(event)

        mock_sb.table.assert_called_once_with(loader.STATISTICS_JSON_USAGE_TABLE)
        inserted = table_mock.insert.call_args[0][0]
        self.assertEqual(inserted["load_date"], "2026-06-02")
        self.assertEqual(inserted["campaign_units"], 10)
        self.assertEqual(inserted["response_kind"], "success")

    def test_db_write_failure_is_non_fatal(self):
        with mock.patch.object(loader, "supabase") as mock_sb:
            mock_sb.table.side_effect = Exception("DB down")
            loader.write_statistics_json_usage_to_db({"event_at": "x", "campaign_units": 1})


class ReadUsageFromDbTests(unittest.TestCase):
    def test_returns_rows_for_matching_load_date_and_account(self):
        rows = [
            {"load_date": "2026-06-02", "account_signature": "acc1", "campaign_units": 5, "response_kind": "success"},
        ]
        table_mock = mock.MagicMock()
        table_mock.select.return_value = table_mock
        table_mock.eq.return_value = table_mock
        table_mock.execute.return_value = mock.Mock(data=rows)

        with mock.patch.object(loader, "supabase") as mock_sb:
            mock_sb.table.return_value = table_mock
            result = loader.read_statistics_json_usage_from_db("2026-06-02", "acc1")

        self.assertEqual(result, rows)

    def test_returns_empty_list_on_db_error(self):
        with mock.patch.object(loader, "supabase") as mock_sb:
            mock_sb.table.side_effect = Exception("timeout")
            result = loader.read_statistics_json_usage_from_db("2026-06-02", "acc1")
        self.assertEqual(result, [])


class BudgetDiagnosticsDbLedgerTests(unittest.TestCase):
    def _make_client(self, in_memory_events=None):
        client = mock.Mock()
        client.account_signature = "acc1"
        client.get_statistics_json_usage_events = mock.Mock(return_value=in_memory_events or [])
        client.list_campaigns = mock.Mock(side_effect=Exception("no campaigns"))
        return client

    def _db_events(self, units, response_kind="success"):
        return [
            {
                "event_at": "2026-06-02T00:30:00+00:00",
                "load_date": "2026-06-02",
                "account_signature": "acc1",
                "campaign_units": units,
                "response_kind": response_kind,
            }
        ]

    def test_db_ledger_used_when_in_memory_empty(self):
        client = self._make_client(in_memory_events=[])

        with mock.patch.object(loader, "read_statistics_json_usage_from_db", return_value=self._db_events(50)):
            diag = loader.get_statistics_json_budget_diagnostics("2026-06-02", "acc1", client=client)

        self.assertEqual(diag["budget_source"], "db_usage_ledger")
        self.assertEqual(diag["budget_confidence"], "high")
        self.assertEqual(diag["daily_budget_used_today"], 50)

    def test_in_memory_wins_over_db(self):
        in_mem = [
            {
                "event_at": "2026-06-02T00:10:00+00:00",
                "load_date": "2026-06-02",
                "account_signature": "acc1",
                "campaign_units": 30,
                "response_kind": "success",
            }
        ]
        client = self._make_client(in_memory_events=in_mem)

        with mock.patch.object(loader, "read_statistics_json_usage_from_db", return_value=self._db_events(999)):
            diag = loader.get_statistics_json_budget_diagnostics("2026-06-02", "acc1", client=client)

        self.assertEqual(diag["budget_source"], "runtime_usage_ledger")
        self.assertEqual(diag["daily_budget_used_today"], 30)

    def test_falls_back_to_status_snapshot_when_db_empty(self):
        client = self._make_client(in_memory_events=[])
        status_rows = [{"cpc_campaign_units_attempted": 80}]

        table_mock = mock.MagicMock()
        table_mock.select.return_value = table_mock
        table_mock.eq.return_value = table_mock
        table_mock.execute.return_value = mock.Mock(data=status_rows)

        with mock.patch.object(loader, "read_statistics_json_usage_from_db", return_value=[]), \
             mock.patch.object(loader, "supabase") as mock_sb:
            mock_sb.table.return_value = table_mock
            diag = loader.get_statistics_json_budget_diagnostics("2026-06-02", "acc1", client=client)

        self.assertEqual(diag["budget_source"], "status_snapshot")
        self.assertEqual(diag["budget_confidence"], "low")
        self.assertEqual(diag["daily_budget_used_today"], 80)

    def test_record_usage_event_writes_to_db(self):
        client = loader.OzonPerformanceClient.__new__(loader.OzonPerformanceClient)
        client.account_signature = "acc1"
        client.state = {}
        client.save_state = mock.Mock()

        event = {
            "event_at": "2026-06-02T01:00:00+00:00",
            "load_date": "2026-06-02",
            "account_signature": "acc1",
            "campaign_units": 5,
            "response_kind": "success",
            "http_status": 200,
        }

        with mock.patch.object(loader, "write_statistics_json_usage_to_db") as mock_write:
            client.record_statistics_json_usage_event(event)

        mock_write.assert_called_once()
        written = mock_write.call_args[0][0]
        self.assertEqual(written["response_kind"], "success")


if __name__ == "__main__":
    unittest.main()
