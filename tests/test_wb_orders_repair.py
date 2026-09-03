"""Ремонт старых дат WB через flag=1 (вариант C).

Что здесь важно удержать:
  * ремонтируем только то, что старше окна записи — внутри окна чинить нечего;
  * flag=1 обязан вернуть ровно одну дату, иначе день не трогаем;
  * без --approve-wb-orders-write не должно быть ни одной записи.
"""

import unittest
from datetime import date
from unittest import mock

import scripts.wb_orders_repair as repair


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


class DetectorTests(unittest.TestCase):
    def test_only_dates_older_than_the_window_are_targets(self):
        items = [
            order("2026-09-01"),   # внутри окна: загрузчик напишет сам
            order("2026-08-04"),   # граница окна: тоже внутри
            order("2026-07-21"),
            order("2026-07-21"),
            order("2026-05-22"),
        ]

        targets = repair.detect_touched_dates(items, date(2026, 8, 4), repair.SOURCES["orders"])

        self.assertEqual(targets, {"2026-05-22": 1, "2026-07-21": 2})

    def test_empty_response_yields_no_targets(self):
        self.assertEqual(repair.detect_touched_dates([], date(2026, 8, 4), repair.SOURCES["orders"]), {})


class TruthDayTests(unittest.TestCase):
    def test_foreign_dates_abort_the_day(self):
        """Если flag=1 перестанет означать «дата заказа», ремонт станет порчей."""
        with mock.patch.object(repair, "fetch", return_value=[order("2026-07-21"), order("2026-07-22")]):
            self.assertIsNone(repair.truth_day("2026-07-21", repair.SOURCES["orders"]))

    def test_failed_request_aborts_the_day(self):
        with mock.patch.object(repair, "fetch", return_value=None):
            self.assertIsNone(repair.truth_day("2026-07-21", repair.SOURCES["orders"]))

    def test_clean_day_is_aggregated(self):
        with mock.patch.object(repair, "fetch", return_value=[order("2026-07-21", 5), order("2026-07-21", 5)]):
            rows = repair.truth_day("2026-07-21", repair.SOURCES["orders"])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["orders_qty"], 2)


class CompareTests(unittest.TestCase):
    def test_delta_and_stale_skus(self):
        rows = [
            {"marketplace_sku": "1", "orders_qty": 5, "orders_amount_seller": 5000.0},
            {"marketplace_sku": "2", "orders_qty": 2, "orders_amount_seller": 2000.0},
        ]
        stored = {"1": (1.0, 1000.0), "9": (3.0, 3000.0)}

        diff = repair.compare("2026-07-21", rows, stored, repair.SOURCES["orders"])

        self.assertEqual(diff["qty_truth"], 7)
        self.assertEqual(diff["qty_stored"], 4)
        self.assertEqual(diff["qty_delta"], 3)
        self.assertEqual(diff["amount_delta"], 3000.0)
        self.assertEqual(diff["stale_skus"], ["9"])


class DryRunTests(unittest.TestCase):
    def test_dry_run_makes_no_writes(self):
        rows = [{"marketplace_sku": "1", "orders_qty": 5, "orders_amount_seller": 5000.0}]

        with mock.patch.object(repair, "fetch", return_value=[order("2026-07-21")]), \
             mock.patch.object(repair, "truth_day", return_value=rows), \
             mock.patch.object(repair, "stored_day", return_value={"1": (1.0, 1000.0)}), \
             mock.patch.object(repair, "write_day") as write_day, \
             mock.patch.object(repair.time, "sleep"):
            code = repair.main(["--dates", "2026-07-21"])

        self.assertEqual(code, 0)
        write_day.assert_not_called()

    def test_write_needs_the_approval_flag(self):
        rows = [{"marketplace_sku": "1", "orders_qty": 5, "orders_amount_seller": 5000.0}]

        with mock.patch.object(repair, "truth_day", return_value=rows), \
             mock.patch.object(repair, "stored_day", return_value={}), \
             mock.patch.object(repair, "write_day") as write_day, \
             mock.patch.object(repair.time, "sleep"):
            repair.main(["--dates", "2026-07-21", "--approve-wb-orders-write"])

        write_day.assert_called_once()

    def test_detector_failure_stops_before_any_repair(self):
        with mock.patch.object(repair, "fetch", return_value=None), \
             mock.patch.object(repair, "truth_day") as truth_day:
            code = repair.main([])

        self.assertEqual(code, 1)
        truth_day.assert_not_called()

    def test_max_dates_caps_the_queue(self):
        rows = [{"marketplace_sku": "1", "orders_qty": 1, "orders_amount_seller": 1000.0}]

        with mock.patch.object(repair, "truth_day", return_value=rows) as truth_day, \
             mock.patch.object(repair, "stored_day", return_value={}), \
             mock.patch.object(repair.time, "sleep"):
            repair.main(["--dates", "2026-07-21", "2026-07-22", "2026-07-23", "--max-dates", "2"])

        self.assertEqual(truth_day.call_count, 2)


if __name__ == "__main__":
    unittest.main()
