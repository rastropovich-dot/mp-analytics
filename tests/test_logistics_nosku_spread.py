"""57-я §2: размещение (тип 46) не дважды; rFBS без SKU — на SKU отправления."""
import unittest
from decimal import Decimal

import reports_daily_sku_kpi as kpi
from loaders import ozon_finance_accrual as A


def _acc(unit, day="2026-10-20", item=None, non_item=None, aid=1):
    a = {"accrual_id": aid, "date": day, "unit_number": unit, "posting": None, "item_fees": None, "non_item_fee": None, "container_fees": None}
    if item:
        a["item_fees"] = {"fees": [{"sku": s, "quantity": q, "fees": [{"type_id": 1, "accrued": {"amount": "-1.00"}}]} for s, q in item]}
    if non_item:
        a["non_item_fee"] = {"type_id": non_item[0], "accrued": {"amount": non_item[1]}}
    return a


class RfbsSpreadTests(unittest.TestCase):
    def test_rfbs_fee_goes_to_the_sku_of_its_posting(self):
        accruals = [_acc("0107284972-0153-1", item=[(1017543579, 1)], aid=1),
                    _acc("0107284972-0153-1", non_item=(64, "-633.08"), aid=2),
                    _acc("0107284972-0153-1", non_item=(63, "-20.00"), aid=3)]
        rows, counters, _ = A.build_expense_rows(accruals)
        logistics = {(r["marketplace_sku"]): r["expense_amount"] for r in rows if r["expense_type"] == "logistics"}
        self.assertEqual(logistics, {"1017543579": 653.08})
        self.assertEqual(counters["rfbs_nosku_spread"], 2)

    def test_posting_without_items_stays_without_sku_and_is_counted(self):
        rows, counters, _ = A.build_expense_rows([_acc("0999-1", non_item=(64, "-100.00"))])
        self.assertEqual([(r["marketplace_sku"], r["expense_amount"]) for r in rows], [("", 100.0)])
        self.assertEqual(counters["rfbs_nosku_not_found"], 1)

    def test_split_by_quantity_keeps_every_kopeck(self):
        parts = A.split_by_quantity(-100.00, {"1": 1, "2": 2})
        self.assertEqual(sum(p for _, p in parts), Decimal("-100.00"))
        self.assertEqual(dict(parts), {"1": Decimal("-33.33"), "2": Decimal("-66.67")})
        self.assertEqual(sum(p for _, p in A.split_by_quantity(10.01, {"a": 0, "b": 0, "c": 0})), Decimal("10.01"))

    def test_cross_dock_and_placement_are_not_touched(self):
        accruals = [_acc("2000066888088", non_item=(12, "-5320.00")), _acc(None, non_item=(46, "-238.38"), aid=2)]
        rows, _, _ = A.build_expense_rows(accruals)
        self.assertEqual({r["marketplace_sku"] for r in rows}, {""})


def _grouped(day, nosku_logistics, skus):
    g = {(day, "ozon", ""): dict(kpi.empty_kpi_row(day, "ozon", ""), logistics_amount=nosku_logistics)}
    for sku, (log, storage) in skus.items():
        g[(day, "ozon", sku)] = dict(kpi.empty_kpi_row(day, "ozon", sku), logistics_amount=log, storage_cost=storage)
    return g


class PlacementMoveTests(unittest.TestCase):
    def setUp(self):
        self.placement = [{"day": "2026-10-21", "sku": 1, "warehouse": "A", "amount": "-200.00"},
                          {"day": "2026-10-21", "sku": 2, "warehouse": "B", "amount": "-38.38"}]

    def test_day_that_matches_the_ledger_moves_into_sku_logistics(self):
        g = _grouped("2026-10-21", 238.38, {"1": (10.0, 200.0), "2": (0.0, 38.38)})
        stats = kpi.move_placement_into_sku_logistics(g, self.placement, {"2026-10-21": Decimal("238.38")})
        self.assertEqual(stats["moved_days"], 1)
        self.assertAlmostEqual(g[("2026-10-21", "ozon", "")]["logistics_amount"], 0.0)
        self.assertAlmostEqual(g[("2026-10-21", "ozon", "1")]["logistics_amount"], 210.0)
        self.assertAlmostEqual(sum(r["logistics_amount"] for r in g.values()), 248.38)    # итог дня тот же

    def test_mismatch_or_short_row_is_not_touched(self):
        g = _grouped("2026-10-21", 238.38, {"1": (0.0, 200.0), "2": (0.0, 38.38)})
        stats = kpi.move_placement_into_sku_logistics(g, self.placement, {"2026-10-21": Decimal("238.00")})
        self.assertEqual(stats["moved_days"], 0); self.assertEqual(len(stats["mismatch"]), 1)
        g = _grouped("2026-10-21", 100.0, {"1": (0.0, 200.0), "2": (0.0, 38.38)})
        stats = kpi.move_placement_into_sku_logistics(g, self.placement, {"2026-10-21": Decimal("238.38")})
        self.assertEqual(stats["moved_days"], 0); self.assertEqual(len(stats["short"]), 1)
        self.assertEqual(g[("2026-10-21", "ozon", "")]["logistics_amount"], 100.0)


if __name__ == "__main__":
    unittest.main()


class StorageOnlyWindowTests(unittest.TestCase):
    """Слово 2026-10-26: ключ «только хранение» — только за 30 дней; хранение у ключей с продажами — за всю историю."""

    def build(self, placement, expenses):
        from unittest import mock
        with mock.patch.object(kpi, "load_orders", return_value=[]), mock.patch.object(kpi, "load_buyouts", return_value=[]), \
             mock.patch.object(kpi, "load_expenses", return_value=expenses), mock.patch.object(kpi, "load_ozon_organic", return_value=[]), \
             mock.patch.object(kpi, "load_placement", return_value=placement), \
             mock.patch.object(kpi, "load_ledger_type46", return_value={"2026-06-10": Decimal("3.00"), "2026-10-20": Decimal("3.00")}), \
             mock.patch.object(kpi, "load_wb_costs", return_value={"storage": [], "ads": {}, "other": [], "articles": {}}), \
             mock.patch.object(kpi, "load_wb_cohort", return_value=({}, {})), mock.patch("builtins.print"):
            return {(r["kpi_date"], r["marketplace_sku"]): r for r in kpi.build_kpi(today=kpi.date(2026, 10, 26))}

    def test_old_storage_only_key_is_not_created_and_its_storage_stays_without_sku(self):
        placement = [{"day": d, "sku": s, "warehouse": "A", "amount": a, "offer_id": ""}
                     for d in ("2026-06-10", "2026-10-20") for s, a in ((1, "-2.00"), (2, "-1.00"))]
        expenses = [{"id": i, "expense_date": d, "marketplace_code": "ozon", "marketplace_sku": sku, "expense_type": "logistics", "expense_amount": v}
                    for i, (d, sku, v) in enumerate((("2026-06-10", "1", 10), ("2026-06-10", "", 3), ("2026-10-20", "1", 10), ("2026-10-20", "", 3)))]
        rows = self.build(placement, expenses)
        self.assertNotIn(("2026-06-10", "2"), rows)                       # старше 30 дней, только хранение — строки нет
        self.assertIn(("2026-10-20", "2"), rows)                          # в окне — строка есть
        self.assertAlmostEqual(rows[("2026-06-10", "1")]["storage_cost"], 2.0)   # ключ с продажами — хранение за всю историю
        self.assertAlmostEqual(rows[("2026-06-10", "1")]["logistics_amount"], 12.0)
        self.assertAlmostEqual(rows[("2026-06-10", "")]["logistics_amount"], 1.0)  # хранение SKU 2 осталось без SKU
        self.assertAlmostEqual(rows[("2026-10-20", "")]["logistics_amount"], 0.0)
        for day in ("2026-06-10", "2026-10-20"):                          # итог логистики дня не меняется
            self.assertAlmostEqual(sum(r["logistics_amount"] for (d, _), r in rows.items() if d == day), 13.0)
