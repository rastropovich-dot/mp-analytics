"""Одно правило сборки строк заказов WB: без отмены и отменённые рядом.

Что эти тесты держат:
  * созданные = подтверждённые + отменённые — по штукам и по деньгам, ничего не
    выбрасывается и ничего не задваивается;
  * деньги копятся в Decimal: три раза по 0,10 — это 0,30, а не 0,30000000000000004;
  * отсутствующая цена и не-bool isCancel роняют сборку, а не превращаются в ноль
    или в «подтверждённый по умолчанию»;
  * observed_at стоит у каждой строки.
"""

import json
import unittest
from decimal import Decimal

from loaders import wb_orders_rows as rules

OBSERVED = "2026-09-21T17:30:00+00:00"


def order(day="2026-07-21", nm_id=1, seller="1000", buyer="900", cancelled=False, **extra):
    row = {
        "date": f"{day}T12:00:00",
        "lastChangeDate": f"{day}T13:00:00",
        "nmId": nm_id,
        "supplierArticle": "F0001",
        "subject": "Серьги",
        "totalPrice": Decimal("2000"),
        "priceWithDisc": Decimal(seller),
        "finishedPrice": Decimal(buyer),
        "isCancel": cancelled,
        "cancelDate": f"{day}T00:00:00" if cancelled else "0001-01-01T00:00:00",
        "srid": f"srid-{day}-{nm_id}-{seller}-{cancelled}",
    }
    row.update(extra)
    return row


class RuleTests(unittest.TestCase):
    def test_created_equals_confirmed_plus_cancelled(self):
        items = [
            order(nm_id=7, seller="1000.10", buyer="900.05"),
            order(nm_id=7, seller="2000.20", buyer="1800.15", cancelled=True),
            order(nm_id=7, seller="3000.30", buyer="2700.25", cancelled=True),
            order(nm_id=8, seller="500", buyer="450"),
            order(day="2026-07-22", nm_id=7, seller="700", buyer="650", cancelled=True),
        ]

        rows, counters = rules.build_order_rows(items, OBSERVED)

        self.assertEqual(len(rows), 3)
        cell = next(r for r in rows if r["order_date"] == "2026-07-21" and r["marketplace_sku"] == "7")
        self.assertEqual(cell["orders_qty"], 1)
        self.assertEqual(cell["cancelled_orders_qty"], 2)
        self.assertEqual(cell["orders_amount_seller"], 1000.10)
        self.assertEqual(cell["cancelled_orders_amount_seller"], 5000.50)
        self.assertEqual(cell["orders_amount_buyer"], 900.05)
        self.assertEqual(cell["cancelled_orders_amount_buyer"], 4500.40)

        confirmed_qty, confirmed_amount, cancelled_qty, cancelled_amount = rules.sums(rows)
        self.assertEqual(confirmed_qty + cancelled_qty, len(items))
        self.assertEqual(confirmed_amount + cancelled_amount,
                         sum((i["priceWithDisc"] for i in items), Decimal(0)))
        self.assertEqual(counters["confirmed"], 2)
        self.assertEqual(counters["cancelled"], 3)

    def test_fully_cancelled_cell_keeps_its_key_with_zero_orders(self):
        rows, _ = rules.build_order_rows([order(cancelled=True)], OBSERVED)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["orders_qty"], 0)
        self.assertEqual(rows[0]["orders_amount_seller"], 0)
        self.assertEqual(rows[0]["cancelled_orders_qty"], 1)

    def test_row_carries_key_and_observed_at(self):
        rows, _ = rules.build_order_rows([order(), order(nm_id=2, cancelled=True)], OBSERVED)

        for row in rows:
            self.assertEqual(row["observed_at"], OBSERVED)
            self.assertEqual(row["marketplace_code"], "wb")
            self.assertEqual(row["order_schema"], "marketplace")

    def test_observed_at_defaults_to_now_and_is_the_same_for_all_rows(self):
        rows, _ = rules.build_order_rows([order(), order(nm_id=2)])

        self.assertEqual(len({r["observed_at"] for r in rows}), 1)
        self.assertTrue(rows[0]["observed_at"].endswith("+00:00"))


class MoneyTests(unittest.TestCase):
    def test_kopecks_do_not_grow_float_tails(self):
        items = [order(seller="0.10", buyer="0.10") for _ in range(3)]

        rows, _ = rules.build_order_rows(items, OBSERVED)

        self.assertEqual(rows[0]["orders_amount_seller"], 0.3)
        self.assertEqual(json.dumps(rows[0]["orders_amount_seller"]), "0.3")

    def test_float_prices_from_plain_json_are_read_by_their_text(self):
        """Ночной загрузчик разбирает ответ без parse_float — цены приходят float."""
        items = [order() for _ in range(3)]
        for item in items:
            item["priceWithDisc"] = 1753176.17
            item["finishedPrice"] = 1753176.17

        rows, _ = rules.build_order_rows(items, OBSERVED)

        self.assertEqual(json.dumps(rows[0]["orders_amount_seller"]), "5259528.51")

    def test_zero_price_falls_back_along_the_old_chain(self):
        rows, _ = rules.build_order_rows([order(seller="0", buyer="900")], OBSERVED)

        self.assertEqual(rows[0]["orders_amount_seller"], 900)
        self.assertEqual(rows[0]["orders_amount_buyer"], 900)

    def test_missing_price_field_is_a_failure_not_a_zero(self):
        item = order()
        del item["priceWithDisc"]

        with self.assertRaises(RuntimeError) as caught:
            rules.build_order_rows([item], OBSERVED)
        self.assertIn("priceWithDisc", str(caught.exception))

    def test_null_price_is_a_failure_not_a_zero(self):
        with self.assertRaises(RuntimeError):
            rules.build_order_rows([order(finishedPrice=None)], OBSERVED)

    def test_all_prices_zero_is_a_failure(self):
        with self.assertRaises(RuntimeError):
            rules.build_order_rows([order(seller="0", buyer="0", totalPrice=0)], OBSERVED)


class StatusTests(unittest.TestCase):
    def test_missing_is_cancel_is_a_failure_not_confirmed_by_default(self):
        item = order()
        del item["isCancel"]

        with self.assertRaises(RuntimeError) as caught:
            rules.build_order_rows([item], OBSERVED)
        self.assertIn("isCancel", str(caught.exception))

    def test_non_boolean_is_cancel_is_a_failure(self):
        with self.assertRaises(RuntimeError):
            rules.build_order_rows([order(isCancel="false")], OBSERVED)

    def test_rows_without_nm_or_date_are_counted_not_swallowed(self):
        rows, counters = rules.build_order_rows([order(nmId=None), order(date=None), order()], OBSERVED)

        self.assertEqual(len(rows), 1)
        self.assertEqual(counters["no_nm_id"], 1)
        self.assertEqual(counters["no_date"], 1)
        self.assertIn("пропущено", rules.describe_counters(counters))


if __name__ == "__main__":
    unittest.main()
