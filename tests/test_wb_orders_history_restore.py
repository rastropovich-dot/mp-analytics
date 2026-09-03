"""Восстановление истории заказов WB: порядок, прерываемость, отказ от записи.

Три вещи, которые тесты держат жёстко:
  * без починенного загрузчика запись запрещена — иначе ночь осыплет заново;
  * без снимка запись не начинается;
  * пустой ответ flag=1 там, где в базе данные, не считается истиной.
"""

import json
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

import scripts.wb_orders_history_restore as restore


def order(day, nm_id=1, price=1000):
    return {
        "date": f"{day}T10:00:00",
        "lastChangeDate": f"{day}T11:00:00",
        "nmId": nm_id,
        "supplierArticle": "F0001",
        "subject": "Серьги",
        "totalPrice": price,
        "finishedPrice": price,
        "priceWithDisc": price,
        "discountPercent": 0,
    }


class OrderGuardTests(unittest.TestCase):
    def test_write_is_refused_while_the_loader_is_unfixed(self):
        with mock.patch.object(restore, "loader_is_fixed", return_value=False), \
             mock.patch.object(restore, "stored_dates") as stored:
            code = restore.main(["--approve-wb-orders-write"])

        self.assertEqual(code, 2)
        stored.assert_not_called()

    def test_dry_run_does_not_need_the_loader_fix(self):
        with mock.patch.object(restore, "loader_is_fixed", return_value=False), \
             mock.patch.object(restore, "stored_dates", return_value={}):
            self.assertEqual(restore.main([]), 0)

    def test_snapshot_is_taken_before_the_first_write(self):
        calls = []

        def fake_snapshot(days):
            calls.append(list(days))
            return {"file": "snapshots/x.csv.gz", "rows": 1}

        with tempfile.TemporaryDirectory() as tmp:
            progress_path = os.path.join(tmp, "progress.json")
            with mock.patch.object(restore, "loader_is_fixed", return_value=True), \
                 mock.patch.object(restore, "stored_dates", return_value={"2026-07-21": {"qty": 1.0, "amount": 1000.0}}), \
                 mock.patch.object(restore, "snapshot_dates", side_effect=fake_snapshot), \
                 mock.patch.object(restore, "fetch_day", return_value=([order("2026-07-21")], None)), \
                 mock.patch.object(restore.time, "sleep"), \
                 mock.patch.object(restore, "supabase") as supabase:
                restore.main(["--approve-wb-orders-write", "--progress-path", progress_path])

            self.assertEqual(calls, [["2026-07-21"]])
            self.assertTrue(supabase.table.called)


class RestoreDayTests(unittest.TestCase):
    def test_empty_answer_with_stored_data_is_not_treated_as_truth(self):
        with mock.patch.object(restore, "fetch_day", return_value=([], None)):
            result, error = restore.restore_day("2026-07-21", {"qty": 280.0, "amount": 1.0}, True)

        self.assertIsNone(result)
        self.assertIn("вернул пусто", error)

    def test_foreign_dates_abort_the_day(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = [order("2026-07-21"), order("2026-07-22")]

        with mock.patch.object(restore.http_retry, "get", return_value=response):
            items, error = restore.fetch_day("2026-07-21")

        self.assertIsNone(items)
        self.assertIn("посторонние даты", error)

    def test_delta_is_measured_against_stored(self):
        items = [order("2026-07-21", 1), order("2026-07-21", 1), order("2026-07-21", 2)]

        with mock.patch.object(restore, "fetch_day", return_value=(items, None)):
            result, error = restore.restore_day("2026-07-21", {"qty": 1.0, "amount": 1000.0}, False)

        self.assertIsNone(error)
        self.assertEqual(result["qty_truth"], 3)
        self.assertEqual(result["qty_delta"], 2)
        self.assertFalse(result["written"])

    def test_nothing_is_written_without_approval(self):
        with mock.patch.object(restore, "fetch_day", return_value=([order("2026-07-21")], None)), \
             mock.patch.object(restore, "supabase") as supabase:
            restore.restore_day("2026-07-21", {"qty": 0.0, "amount": 0.0}, False)

        supabase.table.assert_not_called()


class ProgressTests(unittest.TestCase):
    def test_finished_dates_are_skipped_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress_path = os.path.join(tmp, "progress.json")
            with open(progress_path, "w", encoding="utf-8") as handle:
                json.dump({"done": {"2026-07-21": {"rows": 1}}, "failed": {}, "snapshot": {"file": "x"}}, handle)

            per_date = {
                "2026-07-21": {"qty": 1.0, "amount": 1.0},
                "2026-07-22": {"qty": 1.0, "amount": 1.0},
            }

            with mock.patch.object(restore, "loader_is_fixed", return_value=True), \
                 mock.patch.object(restore, "stored_dates", return_value=per_date), \
                 mock.patch.object(restore, "fetch_day", return_value=([order("2026-07-22")], None)) as fetch_day, \
                 mock.patch.object(restore.time, "sleep"), \
                 mock.patch.object(restore, "supabase"):
                restore.main(["--approve-wb-orders-write", "--progress-path", progress_path])

            self.assertEqual([call.args[0] for call in fetch_day.call_args_list], ["2026-07-22"])

    def test_failed_date_is_recorded_and_retried_only_on_demand(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress_path = os.path.join(tmp, "progress.json")
            per_date = {"2026-07-21": {"qty": 1.0, "amount": 1.0}}

            with mock.patch.object(restore, "loader_is_fixed", return_value=True), \
                 mock.patch.object(restore, "stored_dates", return_value=per_date), \
                 mock.patch.object(restore, "fetch_day", return_value=(None, "HTTP 429")), \
                 mock.patch.object(restore, "snapshot_dates", return_value={"file": "x"}), \
                 mock.patch.object(restore.time, "sleep"), \
                 mock.patch.object(restore, "supabase"):
                restore.main(["--approve-wb-orders-write", "--progress-path", progress_path])

            with open(progress_path, encoding="utf-8") as handle:
                progress = json.load(handle)
            self.assertIn("2026-07-21", progress["failed"])

            # Второй прогон без --retry-failed эту дату не берёт.
            with mock.patch.object(restore, "loader_is_fixed", return_value=True), \
                 mock.patch.object(restore, "stored_dates", return_value=per_date), \
                 mock.patch.object(restore, "fetch_day") as fetch_day, \
                 mock.patch.object(restore.time, "sleep"), \
                 mock.patch.object(restore, "supabase"):
                restore.main(["--approve-wb-orders-write", "--progress-path", progress_path])
            fetch_day.assert_not_called()

    def test_plan_mode_makes_no_requests(self):
        with mock.patch.object(restore, "loader_is_fixed", return_value=True), \
             mock.patch.object(restore, "stored_dates", return_value={"2026-07-21": {"qty": 1.0, "amount": 1.0}}), \
             mock.patch.object(restore, "fetch_day") as fetch_day, \
             mock.patch.object(restore, "snapshot_dates") as snapshot:
            code = restore.main(["--plan"])

        self.assertEqual(code, 0)
        fetch_day.assert_not_called()
        snapshot.assert_not_called()

    def test_max_dates_limits_one_run(self):
        per_date = {f"2026-07-{day:02d}": {"qty": 1.0, "amount": 1.0} for day in (21, 22, 23)}

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(restore, "loader_is_fixed", return_value=True), \
                 mock.patch.object(restore, "stored_dates", return_value=per_date), \
                 mock.patch.object(restore, "fetch_day", return_value=([order("2026-07-21")], None)) as fetch_day, \
                 mock.patch.object(restore, "snapshot_dates", return_value={"file": "x"}), \
                 mock.patch.object(restore.time, "sleep"), \
                 mock.patch.object(restore, "supabase"):
                restore.main(["--approve-wb-orders-write", "--max-dates", "2",
                              "--progress-path", os.path.join(tmp, "p.json")])

        self.assertEqual(fetch_day.call_count, 2)


if __name__ == "__main__":
    unittest.main()
