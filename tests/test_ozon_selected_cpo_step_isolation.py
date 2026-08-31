import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader


def _args(mode="daily-yesterday", dry_run=False):
    return mock.Mock(mode=mode, dry_run=dry_run)


class SelectedCpoStepIsolationTest(unittest.TestCase):
    def test_failure_is_swallowed_and_recorded(self):
        client = mock.Mock()
        client.load_ozon_selected_cpo_for_date.side_effect = TimeoutError("report timeout")
        run_summary = {}

        result = loader.run_selected_cpo_step(client, _args(), "2026-08-30", run_summary)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_type"], "TimeoutError")
        self.assertEqual(result["db_writes"], 0)
        self.assertIs(run_summary["selected_cpo"], result)

    def test_every_failure_kind_is_contained(self):
        for exc in (
            TimeoutError("poll timeout"),
            loader.SelectedCpoSchemaNotAppliedError("no schema"),
            loader.SelectedCpoDbMappingError("no db client"),
            ValueError("broken CSV"),
            loader.RateLimitPending(endpoint="/api/client/statistic/orders/generate",
                                    retry_after_seconds=60, cooldown_until=None, attempt=1),
        ):
            with self.subTest(exc=type(exc).__name__):
                client = mock.Mock()
                client.load_ozon_selected_cpo_for_date.side_effect = exc
                run_summary = {}
                result = loader.run_selected_cpo_step(client, _args(), "2026-08-30", run_summary)
                self.assertEqual(result["status"], "failed")

    def test_step_is_skipped_outside_daily_mode(self):
        client = mock.Mock()
        run_summary = {}
        self.assertIsNone(loader.run_selected_cpo_step(client, _args(mode="cpc-backfill"), "2026-08-30", run_summary))
        client.load_ozon_selected_cpo_for_date.assert_not_called()
        self.assertNotIn("selected_cpo", run_summary)

    def test_dry_run_never_writes_and_passes_no_db_client(self):
        client = mock.Mock()
        client.load_ozon_selected_cpo_for_date.return_value = {"status": "success"}
        loader.run_selected_cpo_step(client, _args(dry_run=True), "2026-08-30", {})
        kwargs = client.load_ozon_selected_cpo_for_date.call_args.kwargs
        self.assertFalse(kwargs["write"])
        self.assertIsNone(kwargs["db_client"])

    def test_step_respects_the_feature_flags_it_is_given(self):
        client = mock.Mock()
        client.load_ozon_selected_cpo_for_date.return_value = {"status": "skipped"}
        loader.run_selected_cpo_step(client, _args(), "2026-08-30", {})
        kwargs = client.load_ozon_selected_cpo_for_date.call_args.kwargs
        self.assertEqual(kwargs["enabled"], loader.ENABLE_OZON_SELECTED_CPO_DAILY)
        self.assertEqual(kwargs["approve_write"], loader.APPROVE_OZON_SELECTED_CPO_DAILY_WRITE)


class SelectedCpoGuardsAreRealTest(unittest.TestCase):
    """Раньше эти проверки не могли сработать: вызывающий код передавал
    dry_run=True вместе с write=True, а все стопы висели на not dry_run."""

    def _client(self):
        return loader.OzonPerformanceClient.__new__(loader.OzonPerformanceClient)

    def test_write_without_applied_schema_is_refused(self):
        client = self._client()
        client.request = mock.Mock(side_effect=AssertionError("не должны дойти до Ozon"))
        with self.assertRaises(loader.SelectedCpoSchemaNotAppliedError):
            loader.OzonPerformanceClient.fetch_search_promo_orders_csv(
                client, date="2026-08-30", write=True, schema_applied=False,
            )

    def test_write_without_db_client_is_refused(self):
        client = self._client()
        client.request = mock.Mock(side_effect=AssertionError("не должны дойти до Ozon"))
        with self.assertRaises(loader.SelectedCpoDbMappingError):
            loader.OzonPerformanceClient.fetch_search_promo_orders_csv(
                client, date="2026-08-30", write=True, schema_applied=True, db_client=None,
            )

    def test_dry_run_parameter_is_gone(self):
        # Параметр был чисто декоративным: на нём висели RuntimeError,
        # которые никогда не срабатывали.
        import inspect
        params = inspect.signature(loader.OzonPerformanceClient.fetch_search_promo_orders_csv).parameters
        self.assertNotIn("dry_run", params)

    def test_schema_check_is_a_real_probe_not_bool_of_write(self):
        db = mock.Mock()
        self.assertTrue(loader.selected_cpo_source_schema_applied(db))
        db.table.assert_called_once_with("ozon_search_promo_selected_cpo_orders")

        broken = mock.Mock()
        broken.table.side_effect = RuntimeError("relation does not exist")
        self.assertFalse(loader.selected_cpo_source_schema_applied(broken))

    def test_daily_path_no_longer_self_certifies_the_schema(self):
        client = loader.OzonPerformanceClient.__new__(loader.OzonPerformanceClient)
        client.fetch_search_promo_orders_csv = mock.Mock(return_value={
            "source_table_rows": [], "aggregation": {}, "db_writes": 0,
        })
        client.selected_cpo_downstream_dry_run = mock.Mock(return_value={
            "marketplace_expenses_rows": [], "ad_attribution_rows": [], "db_writes": 0,
        })
        db = mock.Mock()

        with mock.patch.object(loader, "selected_cpo_source_schema_applied", return_value=False) as probe:
            loader.OzonPerformanceClient.load_ozon_selected_cpo_for_date(
                client, "2026-08-30", write=True, dry_run=True,
                approve_write=True, enabled=True, db_client=db,
            )

        probe.assert_called_once()
        self.assertFalse(client.fetch_search_promo_orders_csv.call_args.kwargs["schema_applied"])


if __name__ == "__main__":
    unittest.main()
