"""Выкупаемость — к созданным заказам (подтверждённые + отменённые), одинаково для Ozon и WB.

После правила «orders_* = подтверждённые» выкупы / orders_qty у WB давали 1,005 на плато и 1,843 в окне — не доля.
Доли рекламы (ad_share_of_orders, ad_share_orders) и roas остаются на подтверждённых.
"""
import unittest
from unittest import mock

import reports_daily_marketplace_kpi as mkpi
import reports_daily_sku_kpi as kpi

DAY = "2026-09-10"


def order(sku, qty, cancelled, amount, market="ozon", day=DAY):
    return {"order_date": day, "marketplace_code": market, "marketplace_sku": sku, "orders_qty": qty,
            "cancelled_orders_qty": cancelled, "orders_amount_seller": amount, "article": "A"}


def buyout(sku, qty, amount, market="ozon", day=DAY):
    return {"buyout_date": day, "marketplace_code": market, "marketplace_sku": sku, "buyouts_qty": qty, "buyouts_amount_seller": amount}


def build(orders, buyouts, expenses=()):
    with mock.patch.object(kpi, "load_orders", return_value=list(orders)), mock.patch.object(kpi, "load_buyouts", return_value=list(buyouts)), \
            mock.patch.object(kpi, "load_expenses", return_value=list(expenses)), mock.patch.object(kpi, "load_ozon_organic", return_value=[]), mock.patch.object(kpi, "load_wb_cohort", return_value=({}, {})), \
            mock.patch("builtins.print"):
        return {(r["kpi_date"], r["marketplace_code"], r["marketplace_sku"]): r for r in kpi.build_kpi()}


class SkuShowcase(unittest.TestCase):
    def test_denominator_is_created_orders(self):
        rows = build([order("1", 2, 1, 2000)], [buyout("1", 2, 2000)])
        self.assertEqual(rows[(DAY, "ozon", "1")]["buyout_rate"], round(2 / 3, 4))     # не 2 / 2 = 1,0

    def test_same_rule_for_wb(self):
        rows = build([order("w", 1, 3, 500, market="wb")], [buyout("w", 2, 1000, market="wb")])
        self.assertEqual(rows[(DAY, "wb", "w")]["buyout_rate"], 0.5)

    def test_wb_sku_row_takes_the_cohort_key_rate_or_the_day_forecast(self):
        """WB-11 §3 по SKU: зрелый день — ставка ключа когорты (ключа нет → 0), незрелый — прогноз площадки, до когорты — как было."""
        orders = [order("w", 1, 3, 500, market="wb"), order("w", 2, 0, 900, market="wb", day="2026-09-24"), order("w", 1, 0, 100, market="wb", day="2026-03-01"),
                  order("1", 2, 1, 2000)]
        buyouts = [buyout("w", 2, 1000, market="wb"), buyout("v", 1, 500, market="wb"), buyout("1", 2, 2000)]
        daily = {DAY: {"mature": True, "rate": 0.45, "forecast": None}, "2026-09-24": {"mature": False, "rate": None, "forecast": 0.4151}}
        sku = {(DAY, "w"): 0.5}
        with mock.patch.object(kpi, "load_orders", return_value=orders), mock.patch.object(kpi, "load_buyouts", return_value=buyouts), \
                mock.patch.object(kpi, "load_expenses", return_value=[]), mock.patch.object(kpi, "load_ozon_organic", return_value=[]), mock.patch("builtins.print"):
            rows = {(r["kpi_date"], r["marketplace_code"], r["marketplace_sku"]): r for r in kpi.build_kpi(wb_cohort=(daily, sku), with_source=True)}
            rows2 = {(r["kpi_date"], r["marketplace_code"], r["marketplace_sku"]): r for r in kpi.build_kpi(wb_cohort=(daily, {}), with_source=False)}
        self.assertEqual((rows[(DAY, "wb", "w")]["buyout_rate"], rows[(DAY, "wb", "w")]["buyout_rate_source"]), (0.5, "cohort"))       # ставка ключа когорты, не 2 / 4
        self.assertEqual(rows2[(DAY, "wb", "w")]["buyout_rate"], 0)                                                                    # ключа в когорте нет → 0
        self.assertEqual((rows[(DAY, "wb", "v")]["buyout_rate"], rows[(DAY, "wb", "v")]["buyout_rate_source"]), (0, "cohort"))         # выкуп по дню продажи без заказа → 0
        self.assertEqual((rows[("2026-09-24", "wb", "w")]["buyout_rate"], rows[("2026-09-24", "wb", "w")]["buyout_rate_source"]), (0.4151, "cohort_forecast"))
        self.assertEqual((rows[("2026-03-01", "wb", "w")]["buyout_rate"], rows[("2026-03-01", "wb", "w")]["buyout_rate_source"]), (0, "calendar"))   # 0 выкупов / 1 созданный
        self.assertEqual((rows[(DAY, "ozon", "1")]["buyout_rate"], rows[(DAY, "ozon", "1")]["buyout_rate_source"]), (round(2 / 3, 4), "calendar"))
        self.assertNotIn("buyout_rate_source", rows2[(DAY, "wb", "w")])

    def test_only_cancelled_orders_still_count_as_created(self):
        rows = build([order("1", 0, 2, 0)], [buyout("1", 1, 1000)])
        self.assertEqual(rows[(DAY, "ozon", "1")]["buyout_rate"], 0.5)

    def test_key_without_orders_has_zero_rate(self):
        rows = build([], [buyout("9", 1, 1000)])
        self.assertEqual(rows[(DAY, "ozon", "9")]["buyout_rate"], 0)

    def test_helper_field_never_reaches_the_table(self):
        rows = build([order("1", 2, 1, 2000)], [buyout("1", 2, 2000)])
        self.assertNotIn(kpi.CREATED_QTY, rows[(DAY, "ozon", "1")])

    def test_ad_shares_stay_on_confirmed(self):
        rows = build([order("1", 2, 2, 2000)], [], [{"expense_date": DAY, "marketplace_code": "ozon", "marketplace_sku": "1",
                                                     "expense_type": "advertising_clicks", "expense_amount": 200}])
        self.assertEqual(rows[(DAY, "ozon", "1")]["ad_share_of_orders"], 0.1)          # 200 / 2 000 подтверждённых


class MarketplaceShowcase(unittest.TestCase):
    def build(self, sku_rows, created, wb_cohort=None, with_source=False):
        with mock.patch.object(mkpi, "load_daily_sku_kpi", return_value=sku_rows), mock.patch.object(mkpi, "load_wb_cohort_daily", return_value={}), \
                mock.patch("builtins.print"):
            return {(r["kpi_date"], r["marketplace_code"]): r for r in mkpi.build_marketplace_kpi(created=created, wb_cohort=wb_cohort, with_source=with_source)}

    def test_denominator_is_created_orders_from_the_orders_table(self):
        sku_rows = [{"kpi_date": DAY, "marketplace_code": "wb", "orders_qty": 10, "orders_amount_seller": 10000, "buyouts_qty": 18}]
        rows = self.build(sku_rows, {(DAY, "wb"): 20})
        self.assertEqual(rows[(DAY, "wb")]["buyout_rate"], 0.9)                          # прежде 18 / 10 = 1,8
        self.assertEqual(rows[(DAY, "wb")]["orders_qty"], 10)

    def test_no_created_orders_gives_zero(self):
        rows = self.build([{"kpi_date": DAY, "marketplace_code": "ozon", "buyouts_qty": 5}], {})
        self.assertEqual(rows[(DAY, "ozon")]["buyout_rate"], 0)

    def test_wb_day_in_the_cohort_takes_the_cohort_rate_or_the_forecast_and_ozon_stays_calendar(self):
        """WB-11 §3: строки WB — выкуп по когорте дня заказа; Ozon и дни WB вне когорты — календарное правило."""
        sku_rows = [{"kpi_date": DAY, "marketplace_code": "wb", "orders_qty": 10, "buyouts_qty": 18},
                    {"kpi_date": "2026-09-24", "marketplace_code": "wb", "orders_qty": 10, "buyouts_qty": 7},
                    {"kpi_date": "2026-03-01", "marketplace_code": "wb", "orders_qty": 10, "buyouts_qty": 5},
                    {"kpi_date": DAY, "marketplace_code": "ozon", "orders_qty": 10, "buyouts_qty": 18}]
        created = {(DAY, "wb"): 20, ("2026-09-24", "wb"): 11, ("2026-03-01", "wb"): 10, (DAY, "ozon"): 20}
        wb_cohort = {DAY: {"mature": True, "rate": 0.4221, "forecast": None}, "2026-09-24": {"mature": False, "rate": None, "forecast": 0.4151}}
        rows = self.build(sku_rows, created, wb_cohort=wb_cohort, with_source=True)
        self.assertEqual((rows[(DAY, "wb")]["buyout_rate"], rows[(DAY, "wb")]["buyout_rate_source"]), (0.4221, "cohort"))              # было 18 / 20 = 0,9
        self.assertEqual((rows[("2026-09-24", "wb")]["buyout_rate"], rows[("2026-09-24", "wb")]["buyout_rate_source"]), (0.4151, "cohort_forecast"))   # было 7 / 11
        self.assertEqual((rows[("2026-03-01", "wb")]["buyout_rate"], rows[("2026-03-01", "wb")]["buyout_rate_source"]), (0.5, "calendar"))   # до когорты — как было
        self.assertEqual((rows[(DAY, "ozon")]["buyout_rate"], rows[(DAY, "ozon")]["buyout_rate_source"]), (0.9, "calendar"))
        rows = self.build(sku_rows, created, wb_cohort=wb_cohort)
        self.assertNotIn("buyout_rate_source", rows[(DAY, "wb")])                                     # колонки нет — источник не пишется
        rows = self.build(sku_rows, created, wb_cohort={})
        self.assertEqual(rows[(DAY, "wb")]["buyout_rate"], 0.9)                                        # когорта не прочитана — календарное

    def test_created_is_read_with_the_ordered_reader_and_summed_per_day(self):
        orders = [{"id": 1, "order_date": DAY, "marketplace_code": "ozon", "orders_qty": 2, "cancelled_orders_qty": 1},
                  {"id": 2, "order_date": DAY, "marketplace_code": "ozon", "orders_qty": 3, "cancelled_orders_qty": None}]
        with mock.patch.object(mkpi, "read_all_by_id", return_value=orders) as reader:
            self.assertEqual(mkpi.load_created_orders_qty(), {(DAY, "ozon"): 6})
        self.assertEqual(reader.call_args[0][0], "marketplace_orders")
        self.assertIn("cancelled_orders_qty", reader.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
