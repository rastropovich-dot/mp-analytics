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


class FakeQuery:
    """PostgREST-цепочка: помнит, чем её сортировали, и отдаёт страницы по range()."""

    def __init__(self, rows, log):
        self.rows = rows
        self.log = log
        self.ordered_by = []

    def select(self, *_):
        return self

    def eq(self, *_):
        return self

    def lt(self, *_):
        return self

    def order(self, column):
        self.ordered_by.append(column)
        return self

    def range(self, start, end):
        self.log.append(list(self.ordered_by))
        self.page = self.rows[start:end + 1]
        return self

    def execute(self):
        return mock.Mock(data=self.page)


def stored_row(day, sku, qty="1", amount="1000.10"):
    return {"order_date": day, "marketplace_sku": str(sku), "order_schema": "marketplace",
            "orders_qty": qty, "orders_amount_seller": amount}


class StoredDatesTests(unittest.TestCase):
    def fake_supabase(self, rows, log):
        supabase = mock.Mock()
        supabase.table.side_effect = lambda _: FakeQuery(rows, log)
        return supabase

    def test_every_page_is_read_in_key_order(self):
        rows = [stored_row("2026-07-21", sku) for sku in range(2500)]
        log = []

        with mock.patch.object(restore, "supabase", self.fake_supabase(rows, log)):
            per_date = restore.stored_dates("2026-08-22")

        self.assertEqual(len(log), 3)
        for ordered_by in log:
            self.assertEqual(ordered_by, ["order_date", "marketplace_sku", "order_schema"])
        self.assertEqual(per_date["2026-07-21"]["rows"], 2500)
        self.assertEqual(per_date["2026-07-21"]["qty"], 2500)

    def test_money_is_summed_without_float_tails(self):
        rows = [stored_row("2026-07-21", sku, amount="0.10") for sku in range(3)]

        with mock.patch.object(restore, "supabase", self.fake_supabase(rows, [])):
            per_date = restore.stored_dates("2026-08-22")

        self.assertEqual(str(per_date["2026-07-21"]["amount"]), "0.30")

    def test_key_repeated_across_pages_is_a_failure_not_a_double_count(self):
        rows = [stored_row("2026-07-21", sku) for sku in range(1000)] + [stored_row("2026-07-21", 0)]

        with mock.patch.object(restore, "supabase", self.fake_supabase(rows, [])):
            with self.assertRaises(RuntimeError):
                restore.stored_dates("2026-08-22")

    def test_empty_amount_is_a_failure_not_a_zero(self):
        rows = [stored_row("2026-07-21", 1, amount=None)]

        with mock.patch.object(restore, "supabase", self.fake_supabase(rows, [])):
            with self.assertRaises(ValueError):
                restore.stored_dates("2026-08-22")


if __name__ == "__main__":
    unittest.main()
