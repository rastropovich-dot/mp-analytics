import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader


class _FakeResult:
    def __init__(self, data=None):
        self.data = data or []


class _PipelineRuntimeStateTable:
    def __init__(
        self,
        existing_rows=None,
        fail_delete_for_chunk_sizes=None,
        fail_upsert=False,
        fail_select=False,
    ):
        self.existing_rows = list(existing_rows or [])
        self.fail_delete_for_chunk_sizes = set(fail_delete_for_chunk_sizes or [])
        self.fail_upsert = fail_upsert
        self.fail_select = fail_select
        self._action = "select"
        self._eq = {}
        self._in = {}
        self.delete_calls = []
        self.upsert_calls = []

    def select(self, _fields):
        self._action = "select"
        return self

    def eq(self, field, value):
        self._eq[field] = value
        return self

    def in_(self, field, values):
        self._in[field] = list(values)
        return self

    def delete(self):
        self._action = "delete"
        return self

    def upsert(self, rows, on_conflict=None):
        self._action = "upsert"
        self._upsert_rows = list(rows)
        self._upsert_conflict = on_conflict
        return self

    def execute(self):
        if self._action == "select":
            if self.fail_select:
                raise RuntimeError("_ssl.c:1112: The handshake operation timed out")
            rows = list(self.existing_rows)
            for field, value in self._eq.items():
                rows = [row for row in rows if row.get(field) == value]
            for field, values in self._in.items():
                rows = [row for row in rows if row.get(field) in values]
            return _FakeResult(rows)

        if self._action == "upsert":
            self.upsert_calls.append(
                {
                    "row_count": len(self._upsert_rows),
                    "on_conflict": self._upsert_conflict,
                }
            )
            if self.fail_upsert:
                raise RuntimeError("upsert failed")
            return _FakeResult([])

        if self._action == "delete":
            chunk = list(self._in.get("state_key", []))
            self.delete_calls.append(chunk)
            if len(chunk) in self.fail_delete_for_chunk_sizes:
                raise RuntimeError("URL component 'query' too long")
            return _FakeResult([])

        raise AssertionError(f"Unexpected action {self._action}")


class _FakeSupabase:
    def __init__(self, runtime_table):
        self.runtime_table = runtime_table

    def table(self, name):
        if name != loader.PIPELINE_RUNTIME_STATE_TABLE:
            raise AssertionError(f"Unexpected table {name}")
        return self.runtime_table


def _make_client(state=None):
    client = object.__new__(loader.OzonPerformanceClient)
    client.account_signature = "acct_test"
    client.state_backend = "db"
    client.state = state or loader.default_state()
    client.token = None
    client.token_expires_at = 0
    return client


class OzonPerformanceRuntimeStateCleanupTests(unittest.TestCase):
    def test_from_iso_accepts_short_fractional_seconds(self):
        parsed = loader.from_iso("2026-05-24T19:44:27.60661+00:00")
        self.assertEqual(parsed.isoformat(), "2026-05-24T19:44:27.606610+00:00")

    def test_cleanup_is_chunked_for_many_keys(self):
        client = _make_client()
        keys = [f"key-{idx}" for idx in range(60)]
        table = _PipelineRuntimeStateTable()

        with mock.patch.object(loader, "supabase", _FakeSupabase(table)):
            summary = client.cleanup_runtime_state_keys_nonfatal(keys)

        self.assertEqual(summary["deleted"], 60)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(len(table.delete_calls), 3)
        self.assertEqual([len(chunk) for chunk in table.delete_calls], [25, 25, 10])

    def test_cleanup_query_too_long_is_non_fatal(self):
        client = _make_client()
        keys = [f"key-{idx}" for idx in range(30)]
        table = _PipelineRuntimeStateTable(fail_delete_for_chunk_sizes={25})

        with mock.patch.object(loader, "supabase", _FakeSupabase(table)):
            with mock.patch("builtins.print") as print_mock:
                summary = client.cleanup_runtime_state_keys_nonfatal(keys)

        self.assertEqual(summary["deleted"], 5)
        self.assertEqual(summary["failed"], 25)
        printed = "\n".join(str(args[0]) for args, _kwargs in print_mock.call_args_list if args)
        self.assertIn("runtime_state_stale_cleanup_warning", printed)
        self.assertIn("query' too long", printed)

    def test_save_persistent_state_keeps_upsert_critical(self):
        state = loader.default_state()
        state["cooldowns"] = {"alpha": "2026-05-24T00:00:00+00:00"}
        client = _make_client(state=state)
        existing_rows = []
        table = _PipelineRuntimeStateTable(existing_rows=existing_rows, fail_upsert=True)

        with mock.patch.object(loader, "supabase", _FakeSupabase(table)):
            with self.assertRaises(RuntimeError):
                client.save_persistent_state_to_db()

    def test_save_persistent_state_cleanup_failure_does_not_raise(self):
        state = loader.default_state()
        state["cooldowns"] = {"keep": "2026-05-24T00:00:00+00:00"}
        client = _make_client(state=state)
        existing_rows = [
            {
                "state_key": f"stale-{idx}",
                "state_type": "cooldowns",
                "account_signature": "acct_test",
            }
            for idx in range(40)
        ]
        table = _PipelineRuntimeStateTable(existing_rows=existing_rows, fail_delete_for_chunk_sizes={25, 15})

        with mock.patch.object(loader, "supabase", _FakeSupabase(table)):
            with mock.patch("builtins.print") as print_mock:
                client.save_persistent_state_to_db()

        self.assertEqual(len(table.upsert_calls), 1)
        printed = "\n".join(str(args[0]) for args, _kwargs in print_mock.call_args_list if args)
        self.assertIn("runtime_state_stale_cleanup_warning", printed)

    def test_record_request_event_does_not_crash_on_cleanup_failure(self):
        state = loader.default_state()
        state["cooldowns"] = {"keep": "2026-05-24T00:00:00+00:00"}
        client = _make_client(state=state)
        existing_rows = [
            {
                "state_key": f"stale-{idx}",
                "state_type": "cooldowns",
                "account_signature": "acct_test",
            }
            for idx in range(40)
        ]
        table = _PipelineRuntimeStateTable(existing_rows=existing_rows, fail_delete_for_chunk_sizes={25, 15})

        with mock.patch.object(loader, "supabase", _FakeSupabase(table)):
            client.record_request_event("GET", "/api/client/statistics/test")

        self.assertEqual(client.state["request_history"][-1]["endpoint"], "/api/client/statistics/test")
        self.assertEqual(len(table.upsert_calls), 1)

    def test_save_persistent_state_existing_key_read_failure_is_non_fatal(self):
        state = loader.default_state()
        state["cooldowns"] = {"keep": "2026-05-24T00:00:00+00:00"}
        client = _make_client(state=state)
        table = _PipelineRuntimeStateTable(fail_select=True)

        with mock.patch.object(loader, "supabase", _FakeSupabase(table)):
            with mock.patch("builtins.print") as print_mock:
                client.save_persistent_state_to_db()

        self.assertEqual(len(table.upsert_calls), 1)
        printed = "\n".join(str(args[0]) for args, _kwargs in print_mock.call_args_list if args)
        self.assertIn("runtime_state_stale_cleanup_warning", printed)
        self.assertIn("read_existing_keys", printed)

    def test_record_request_event_does_not_crash_on_existing_key_read_failure(self):
        state = loader.default_state()
        state["cooldowns"] = {"keep": "2026-05-24T00:00:00+00:00"}
        client = _make_client(state=state)
        table = _PipelineRuntimeStateTable(fail_select=True)

        with mock.patch.object(loader, "supabase", _FakeSupabase(table)):
            client.record_request_event("GET", "/api/client/statistics/test")

        self.assertEqual(client.state["request_history"][-1]["endpoint"], "/api/client/statistics/test")
        self.assertEqual(len(table.upsert_calls), 1)

    def test_small_cleanup_normal_behavior(self):
        state = loader.default_state()
        state["cooldowns"] = {"keep": "2026-05-24T00:00:00+00:00"}
        client = _make_client(state=state)
        existing_rows = [
            {
                "state_key": loader.build_db_state_key("cooldowns", "stale-a"),
                "state_type": "cooldowns",
                "account_signature": "acct_test",
            },
            {
                "state_key": loader.build_db_state_key("cooldowns", "keep"),
                "state_type": "cooldowns",
                "account_signature": "acct_test",
            },
        ]
        table = _PipelineRuntimeStateTable(existing_rows=existing_rows)

        with mock.patch.object(loader, "supabase", _FakeSupabase(table)):
            client.save_persistent_state_to_db()

        self.assertEqual(len(table.delete_calls), 1)
        self.assertEqual(table.delete_calls[0], [loader.build_db_state_key("cooldowns", "stale-a")])


if __name__ == "__main__":
    unittest.main()
