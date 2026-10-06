"""Сорок шестая §3: читатели книг несут источник себестоимости (дата снимка / «после последнего снимка …») до листов книг."""
import os
import sys
import unittest
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from loaders import unit_cost_history as uch  # noqa: E402
import report_finrez as fr  # noqa: E402
import report_ozon_month as rep  # noqa: E402

HIST = uch.CostHistory({"2026-04-20": {"f1": D("100")}, "2026-05-20": {"f1": D("120"), "f2": D("50")}})
UNIT_COST = uch.unit_cost_fn(HIST, {"11": "F1", "22": "F2"})


class LongRowsCarrySource(unittest.TestCase):
    def test_long_rows_from_raw_write_snapshot_date_or_after_last(self):
        acc = [{"accrual_id": "a1", "date": "2026-04-25T00:00:00Z", "posting": {"products": [{"sku": "11", "commission": {
            "sale_amount": {"amount": "1000"}, "sale_commission": {"amount": "-100"}}}]}},
               {"accrual_id": "a2", "date": "2026-04-25T00:00:00Z", "posting": {"products": [{"sku": "22", "commission": {
            "sale_amount": {"amount": "500"}, "sale_commission": {"amount": "-50"}}}]}}]
        from collections import defaultdict
        rows = fr.long_rows_from_raw("2026-04-25", acc, {}, UNIT_COST, lambda sku: ("KARATOV", "кольца", "F" + sku, "Товар"), defaultdict(int))
        turn = {r.sku: r for r in rows if r.key == "turnover"}
        self.assertEqual(turn["11"].cost_source, "2026-04-20")
        self.assertEqual(turn["11"].cogs, D("100"))
        self.assertEqual(turn["22"].cost_source, "2026-05-20 (ключа нет в снимке ≤ даты, взят более поздний)")
        self.assertEqual([r.cost_source for r in rows if r.key == "commission"], ["", ""])      # источник — только под Товарооборотом
        self.assertEqual(fr.LONG_COLS[-1][1], "cost_source")

    def test_rows_after_last_snapshot_say_so_and_stub_without_source_stays_valid(self):
        from collections import defaultdict
        acc = [{"accrual_id": "a1", "date": "2026-09-15T00:00:00Z", "posting": {"products": [{"sku": "11", "commission": {
            "sale_amount": {"amount": "1000"}, "sale_commission": {"amount": "-100"}}}]}}]
        rows = fr.long_rows_from_raw("2026-09-15", acc, {}, UNIT_COST, lambda sku: ("", "", "F" + sku, ""), defaultdict(int))
        self.assertEqual(rows[0].cost_source, "после последнего снимка 2026-05-20")
        rows = fr.long_rows_from_raw("2026-09-15", acc, {}, lambda sku, day=None, qty=None: D("7"), lambda sku: ("", "", "F" + sku, ""), defaultdict(int))
        self.assertEqual(rows[0].cost_source, "")                                              # заглушка без источника — колонка пустая, не падает

    def test_wide_rows_and_order_rows_carry_sources(self):
        buyouts = [{"buyout_date": "2026-04-25", "marketplace_sku": "11", "buyouts_amount_seller": "1000", "commission_amount": "100", "buyouts_qty": 1,
                    "buyouts_units": 1, "bonus_amount": "0", "coinvestment_amount": "0"}]
        daily = [{"date": "2026-04-25", "vat": D("1.22"), "logistics": D(0), "other": D(0), "subscription": D(0), "ads": D(0), "acquiring": D(0)}]
        rows, _stats = fr.build_buyout_rows(["2026-04-25"], buyouts, [], [], daily, UNIT_COST, {"11": "F1"}, {}, {})
        self.assertEqual([r["cost_source"] for r in rows if r["sku"] == "11"], ["2026-04-20"])
        self.assertEqual([r.cost_source for r in fr.explode_wide(rows[0]) if r.key == "turnover"], ["2026-04-20"])
        orders = [{"order_date": "2026-09-15", "marketplace_sku": "11", "orders_qty": 1, "orders_amount_seller": "1000", "cancelled_orders_qty": 0,
                   "cancelled_orders_amount_seller": "0", "orders_amount_buyer": "900", "cancelled_orders_amount_buyer": "0"}]
        orows, _s = fr.build_order_rows(["2026-09-15"], orders, [], daily, UNIT_COST, {"11": "F1"}, {}, {}, "2026-10-06")
        self.assertEqual([r["cost_source"] for r in orows if r["sku"] == "11"], ["после последнего снимка 2026-05-20"])
        data = fr.order_data_row(orows[0], D("0.4"), None, D("0.5"), "Ozon", "KARATOV")
        self.assertEqual(data["cost_source"], "после последнего снимка 2026-05-20")
        self.assertEqual(fr.ORDER_DATA_COLS[-1][1], "cost_source")


class SkuSheetCarriesSources(unittest.TestCase):
    def test_build_sku_joins_sources_of_the_month(self):
        kpi = [{"marketplace_sku": "11", "kpi_date": "2026-04-25", "buyouts_qty": 1, "buyouts_amount_seller": "1000", "commission_amount": "100",
                "ad_spend": "0", "logistics_amount": "0", "other_expenses_amount": "0", "article": "F1", "product_name": "Товар"},
               {"marketplace_sku": "11", "kpi_date": "2026-05-25", "buyouts_qty": 1, "buyouts_amount_seller": "1000", "commission_amount": "100",
                "ad_spend": "0", "logistics_amount": "0", "other_expenses_amount": "0", "article": "F1", "product_name": "Товар"}]
        rows = rep.build_sku(kpi, {"11": "F1"}, UNIT_COST, D("1.22"))
        self.assertEqual(rows[0]["cost_source"], "2026-04-20; после последнего снимка 2026-05-20")
        self.assertEqual(rows[0]["cogs"], D("220"))


if __name__ == "__main__":
    unittest.main()
