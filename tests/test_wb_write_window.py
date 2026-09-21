"""Окно записи WB (вариант D): пишем только то, что ответ flag=0 отдаёт полным.

Ловушка, которую эти тесты держат: flag=0 отбирает по дате изменения, поэтому
по старым датам приходит огрызок дня, а upsert замещает хранимое целиком.
"""

import unittest
from datetime import date

import loaders.wb_orders_loader as wb_orders
import loaders.wb_sales_loader as wb_sales


def order(day, nm_id=1, price=1000, last_change=None, cancelled=False):
    return {
        "date": f"{day}T12:00:00",
        "lastChangeDate": f"{last_change or day}T13:00:00",
        "nmId": nm_id,
        "supplierArticle": "F0001",
        "subject": "Серьги",
        "totalPrice": price,
        "finishedPrice": price,
        "priceWithDisc": price,
        "discountPercent": 0,
        "isCancel": cancelled,
        "cancelDate": f"{day}T00:00:00" if cancelled else "0001-01-01T00:00:00",
        "srid": f"srid-{day}-{nm_id}-{price}",
    }


def sale(day, nm_id=1, price=1000, last_change=None):
    row = order(day, nm_id, price, last_change)
    row["saleID"] = "S1"
    return row


class FakeTable:
    def __init__(self, sink):
        self.sink = sink

    def upsert(self, batch, on_conflict=None):
        self.sink.append({"rows": list(batch), "on_conflict": on_conflict})
        return self

    def execute(self):
        return self


class FakeSupabase:
    def __init__(self):
        self.writes = []

    def table(self, name):
        self.writes.append({"table": name})
        self.writes.pop()
        return FakeTable(self.writes)


class WindowBoundaryTests(unittest.TestCase):
    def test_window_start_is_days_back_from_today(self):
        self.assertEqual(
            wb_orders.write_window_start(30, today=date(2026, 9, 3)),
            date(2026, 8, 4),
        )

    def test_request_and_write_use_the_same_boundary(self):
        """Иначе прогон, перешагнувший полночь, взял бы окна от разных дат."""
        today = date(2026, 9, 3)
        self.assertEqual(
            wb_orders.write_window_start(30, today).isoformat(),
            wb_sales.write_window_start(30, today).isoformat(),
        )

    def test_boundary_date_itself_is_writable(self):
        rows = [{"order_date": "2026-08-04", "orders_qty": 1}]
        writable, held = wb_orders.split_by_write_window(rows, date(2026, 8, 4))
        self.assertEqual(len(writable), 1)
        self.assertEqual(held, [])

    def test_day_before_boundary_is_held(self):
        rows = [{"order_date": "2026-08-03", "orders_qty": 1}]
        writable, held = wb_orders.split_by_write_window(rows, date(2026, 8, 4))
        self.assertEqual(writable, [])
        self.assertEqual(len(held), 1)


class AggregationUnchangedTests(unittest.TestCase):
    """Группировка по (дата, SKU) прежняя. Само правило — tests/test_wb_orders_rows.py."""

    def test_orders_group_by_date_and_sku(self):
        grouped = wb_orders.aggregate_orders([
            order("2026-08-20", nm_id=7, price=100),
            order("2026-08-20", nm_id=7, price=250),
            order("2026-08-20", nm_id=8, price=300),
            order("2026-08-21", nm_id=7, price=400),
        ])

        self.assertEqual(len(grouped), 3)
        cell = grouped[("2026-08-20", "wb", "7")]
        self.assertEqual(cell["orders_qty"], 2)
        self.assertEqual(cell["orders_amount_buyer"], 350)
        self.assertEqual(cell["orders_amount_seller"], 350)

    def test_orders_without_nm_or_date_are_dropped(self):
        grouped = wb_orders.aggregate_orders([
            {"nmId": None, "date": "2026-08-20T00:00:00"},
            {"nmId": 5, "date": None},
        ])
        self.assertEqual(grouped, {})

    def test_sales_negative_amounts_count_as_returns(self):
        grouped = wb_sales.aggregate_sales([
            sale("2026-08-20", nm_id=7, price=1000),
            sale("2026-08-20", nm_id=7, price=-1000),
        ])
        cell = grouped[("2026-08-20", "wb", "7")]
        self.assertEqual(cell["buyouts_qty"], 0)


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSupabase()
        self._orders_supabase = wb_orders.supabase
        self._sales_supabase = wb_sales.supabase
        wb_orders.supabase = self.fake
        wb_sales.supabase = self.fake

    def tearDown(self):
        wb_orders.supabase = self._orders_supabase
        wb_sales.supabase = self._sales_supabase

    def test_only_in_window_orders_are_written(self):
        items = [
            order("2026-08-20", nm_id=1),          # внутри окна
            order("2026-07-21", nm_id=2, last_change="2026-09-01"),  # старая, тронутая
        ]

        summary = wb_orders.save_wb_orders(items, days_back=30, today=date(2026, 9, 3))

        written = [row for write in self.fake.writes for row in write["rows"]]
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["order_date"], "2026-08-20")
        self.assertEqual(summary["rows_written"], 1)
        self.assertEqual(summary["held_rows"], 1)

    def test_written_rows_follow_the_ozon_rule(self):
        """Внутри окна отмена перекладывает заказ в cancelled_*, а не выбрасывает его."""
        items = [
            order("2026-08-20", nm_id=1, price=1000),
            order("2026-08-20", nm_id=1, price=700, cancelled=True),
            order("2026-07-21", nm_id=1, price=500, cancelled=True, last_change="2026-09-01"),
        ]

        summary = wb_orders.save_wb_orders(items, days_back=30, today=date(2026, 9, 3),
                                           observed_at="2026-09-03T00:20:00+00:00")

        written = [row for write in self.fake.writes for row in write["rows"]]
        self.assertEqual(len(written), 1)
        row = written[0]
        self.assertEqual((row["orders_qty"], row["orders_amount_seller"]), (1, 1000))
        self.assertEqual((row["cancelled_orders_qty"], row["cancelled_orders_amount_seller"]), (1, 700))
        self.assertEqual(row["observed_at"], "2026-09-03T00:20:00+00:00")
        self.assertEqual(summary["held_rows"], 1)

    def test_held_count_includes_cancelled_orders(self):
        held = [{"order_date": "2026-07-21", "orders_qty": 0.0, "cancelled_orders_qty": 2.0}]
        self.assertIn("2 заказов", wb_orders.describe_held(held))

    def test_old_only_response_writes_nothing_at_all(self):
        """Ночь, в которую из старых дат пришли огрызки, не должна ничего писать."""
        items = [order("2026-07-21", nm_id=2, last_change="2026-09-01")]

        summary = wb_orders.save_wb_orders(items, days_back=30, today=date(2026, 9, 3))

        self.assertEqual(self.fake.writes, [])
        self.assertEqual(summary["rows_written"], 0)

    def test_only_in_window_sales_are_written(self):
        items = [sale("2026-08-20", nm_id=1), sale("2026-05-10", nm_id=2, last_change="2026-05-22")]

        summary = wb_sales.save_wb_sales(items, days_back=30, today=date(2026, 9, 3))

        written = [row for write in self.fake.writes for row in write["rows"]]
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["buyout_date"], "2026-08-20")
        self.assertEqual(summary["held_rows"], 1)

    def test_held_dates_are_reported_not_swallowed(self):
        held = [
            {"order_date": "2026-07-21", "orders_qty": 3},
            {"order_date": "2026-07-22", "orders_qty": 2},
        ]
        message = wb_orders.describe_held(held)
        self.assertIn("2 дат", message)
        self.assertIn("5 заказов", message)
        self.assertIn("wb_orders_repair", message)


if __name__ == "__main__":
    unittest.main()
