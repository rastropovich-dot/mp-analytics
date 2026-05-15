import unittest

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


class OzonPerformanceCpcMaterializationGuardTests(unittest.TestCase):
    def test_completed_progress_with_existing_downstream_returns_verified_success(self):
        db_client = _FakeDbClient(
            {
                "marketplace_expenses": [
                    {
                        "marketplace_code": "ozon",
                        "expense_date": "2026-05-12",
                        "expense_type": "advertising_clicks",
                        "expense_amount": 1412.30,
                    }
                ],
                "ozon_daily_sku_ad_attribution": [
                    {
                        "marketplace_code": "ozon",
                        "sale_date": "2026-05-12",
                        "ad_source": "cpc",
                        "ad_spend": 1412.30,
                    }
                ],
            }
        )

        summary = loader.guard_cpc_materialization(
            target_date="2026-05-12",
            cpc_status="success",
            pending_batches=0,
            processed_batches_this_run=0,
            current_run_cpc_expense_rows_count=0,
            current_run_cpc_ad_attribution_rows_count=0,
            db_client=db_client,
        )

        self.assertTrue(summary["verification_performed"])
        self.assertEqual(summary["status_override"], "success_existing_downstream_verified")
        self.assertTrue(summary["downstream_verification"]["materialized"])
        self.assertEqual(summary["downstream_verification"]["marketplace_expenses_cpc_rows"], 1)
        self.assertEqual(summary["downstream_verification"]["ad_attribution_cpc_rows"], 1)

    def test_completed_progress_without_current_rows_and_without_downstream_raises(self):
        db_client = _FakeDbClient(
            {
                "marketplace_expenses": [],
                "ozon_daily_sku_ad_attribution": [],
            }
        )

        with self.assertRaises(loader.CpcMaterializationGuardError) as ctx:
            loader.guard_cpc_materialization(
                target_date="2026-05-12",
                cpc_status="success",
                pending_batches=0,
                processed_batches_this_run=0,
                current_run_cpc_expense_rows_count=0,
                current_run_cpc_ad_attribution_rows_count=0,
                db_client=db_client,
            )

        self.assertIn("no downstream CPC materialization", str(ctx.exception))

    def test_current_run_cpc_rows_skip_downstream_verification(self):
        db_client = _FakeDbClient({})

        summary = loader.guard_cpc_materialization(
            target_date="2026-05-12",
            cpc_status="success",
            pending_batches=0,
            processed_batches_this_run=1,
            current_run_cpc_expense_rows_count=2,
            current_run_cpc_ad_attribution_rows_count=2,
            db_client=db_client,
        )

        self.assertFalse(summary["verification_performed"])
        self.assertIsNone(summary["downstream_verification"])
        self.assertFalse(summary["guard_triggered"])

    def test_pending_batches_do_not_trigger_guard(self):
        db_client = _FakeDbClient({})

        summary = loader.guard_cpc_materialization(
            target_date="2026-05-12",
            cpc_status="pending_backfill",
            pending_batches=3,
            processed_batches_this_run=0,
            current_run_cpc_expense_rows_count=0,
            current_run_cpc_ad_attribution_rows_count=0,
            db_client=db_client,
        )

        self.assertFalse(summary["verification_performed"])
        self.assertFalse(summary["guard_triggered"])

    def test_cpo_only_rows_do_not_count_as_cpc_materialized(self):
        db_client = _FakeDbClient(
            {
                "marketplace_expenses": [
                    {
                        "marketplace_code": "ozon",
                        "expense_date": "2026-05-12",
                        "expense_type": "advertising_order_5",
                        "expense_amount": 234232.60,
                    }
                ],
                "ozon_daily_sku_ad_attribution": [
                    {
                        "marketplace_code": "ozon",
                        "sale_date": "2026-05-12",
                        "ad_source": "cpo",
                        "ad_spend": 234232.60,
                    }
                ],
            }
        )

        verification = loader.verify_cpc_downstream_materialized(
            target_date="2026-05-12",
            db_client=db_client,
        )

        self.assertEqual(verification["marketplace_expenses_cpc_rows"], 0)
        self.assertEqual(verification["ad_attribution_cpc_rows"], 0)
        self.assertEqual(verification["marketplace_expenses_cpc_sum"], 0.0)
        self.assertEqual(verification["ad_attribution_cpc_sum"], 0.0)
        self.assertFalse(verification["materialized"])


if __name__ == "__main__":
    unittest.main()
