"""Выгрузка для плана продаж РБХ (inbox_rbh 29.10): склейка брендов, день продажи WB по МСК, FIFO-возраст остатка Ozon, дата поставки."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import rbh_sales_plan_export as sp  # noqa: E402


class BrandNorm(unittest.TestCase):
    def test_case_and_trailing_dots_glue(self):
        self.assertEqual(sp.brand_norm("DEXclusive"), sp.brand_norm("DEXCLUSIVE"))
        self.assertEqual(sp.brand_norm("Revolution Makeup"), "REVOLUTION MAKEUP")
        self.assertEqual(sp.brand_norm("CATRICE."), "CATRICE")
        self.assertEqual(sp.brand_norm("  Essence   cosmetics . "), "ESSENCE COSMETICS")
        self.assertEqual(sp.brand_norm(None), "")

    def test_inner_dots_are_kept(self):
        self.assertEqual(sp.brand_norm("Beautyhome.me"), "BEAUTYHOME.ME")


class SaleDay(unittest.TestCase):
    def test_utc_evening_is_next_moscow_day(self):
        self.assertEqual(sp.sale_day_msk("2026-10-27T22:30:00Z"), "2026-10-28")
        self.assertEqual(sp.sale_day_msk("2026-10-27T20:59:59+00:00"), "2026-10-27")
        self.assertIsNone(sp.sale_day_msk(None))


def order(oid, slot_to, bundle):
    return {"order_id": oid, "timeslot": {"timeslot": {"from": slot_to, "to": slot_to}}, "supplies": [{"bundle_id": bundle}]}


class FifoAge(unittest.TestCase):
    def test_stock_goes_to_newest_supplies_first(self):
        sup = {"ids": {"1": "completed", "2": "completed", "3": "active"},
               "orders": [order(1, "2026-09-28T10:00:00Z", "a"), order(2, "2026-10-18T10:00:00Z", "b"), order(3, "2026-10-27T10:00:00Z", "c")],
               "bundles": {"a": [{"sku": 7, "quantity": 10}], "b": [{"sku": 7, "quantity": 4}], "c": [{"sku": 7, "quantity": 100}]}}
        f = sp.fifo_age(sup, {"7": 6}, "2026-10-28")["7"]
        # 4 шт от 18.10 (10 дней) + 2 шт от 28.09 (30 дней); активная заявка не в остатке
        self.assertEqual((f["covered"], f["oldest"], f["avg_age"]), (6, "2026-09-28", round((4 * 10 + 2 * 30) / 6, 1)))
        self.assertEqual(f["note"], "FIFO-допущение")

    def test_stock_older_than_supplies_is_named(self):
        sup = {"ids": {"1": "completed"}, "orders": [order(1, "2026-10-18T10:00:00Z", "b")], "bundles": {"b": [{"sku": 7, "quantity": 4}]}}
        f = sp.fifo_age(sup, {"7": 10, "8": 0}, "2026-10-28")
        self.assertEqual(f["7"]["covered"], 4)
        self.assertIn("6 шт старше", f["7"]["note"])
        self.assertNotIn("8", f)


class SupplyDay(unittest.TestCase):
    def test_slot_end_then_state_date(self):
        self.assertEqual(str(sp.supply_day(order(1, "2026-10-18T10:00:00Z", "b"))), "2026-10-18")
        self.assertEqual(str(sp.supply_day({"state_updated_date": "2026-10-20T01:00:00Z"})), "2026-10-20")


if __name__ == "__main__":
    unittest.main()
