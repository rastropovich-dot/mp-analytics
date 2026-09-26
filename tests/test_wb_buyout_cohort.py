"""WB-10 §2: выкуп по когорте дня заказа — то же определение, что у коэффициента листа; зрелость и прогноз."""
import unittest
from decimal import Decimal

from loaders import wb_buyout_cohort as cohort
import scripts.report_finrez_wb as fr

D = Decimal


def sale(order_ts, nm, price, qty=1, oper="Продажа"):
    return {"rrd_id": 1, "rr_date": order_ts[:10], "order_dt": order_ts, "seller_oper_name": oper, "nm_id": nm, "retail_price_with_disc": price, "quantity": qty}


FUNNEL = [{"day": "2026-08-01", "nm_id": 1, "vendor_code": "F1", "order_count": 2, "order_sum": 2000},
          {"day": "2026-08-01", "nm_id": 2, "vendor_code": "t2", "order_count": 1, "order_sum": 500},
          {"day": "2026-08-31", "nm_id": 1, "vendor_code": "F1", "order_count": 1, "order_sum": 900},
          {"day": "2026-09-10", "nm_id": 1, "vendor_code": "F1", "order_count": 4, "order_sum": 4000}]
SALES = [sale("2026-08-01T10:00:00Z", 1, "1000"), sale("2026-08-01T11:00:00Z", 1, "100", oper="Возврат"), sale("2026-08-01T12:00:00Z", 2, "500"),
         sale("2026-08-30T21:30:00Z", 1, "900"),                    # 00:30 МСК 31.08 → когорта 31.08
         sale("2026-09-10T10:00:00Z", 1, "1000"),                    # незрелый день
         sale("2026-08-15T10:00:00Z", 7, "300"),                     # продажа без строки воронки
         {"rrd_id": 9, "rr_date": "2026-08-20", "order_dt": None, "seller_oper_name": "Продажа", "nm_id": 1, "retail_price_with_disc": "5", "quantity": 1},
         {"rrd_id": 10, "rr_date": "2026-08-20", "order_dt": "2026-08-20T10:00:00Z", "seller_oper_name": "Доставка", "nm_id": 1, "retail_price_with_disc": None, "quantity": 1}]


class CohortTests(unittest.TestCase):
    def test_constants_match_the_book_module(self):
        self.assertEqual(cohort.MATURE_DAYS, fr.BUYOUT_RATE_MATURE_DAYS)

    def test_sku_rows_by_order_day_with_maturity(self):
        rows, stats = cohort.build_sku_rows(FUNNEL, SALES, "2026-09-25")
        by = {(r["day"], r["nm_id"]): r for r in rows}
        self.assertEqual(sorted(by), [("2026-08-01", 1), ("2026-08-01", 2), ("2026-08-15", 7), ("2026-08-31", 1), ("2026-09-10", 1)])
        r = by[("2026-08-01", 1)]
        self.assertEqual((r["created_qty"], r["created_sum"], r["sold_qty"], r["sold_sum"], r["age_days"], r["mature"], r["rate_qty"], r["rate_sum"]), (2, D("2000"), 0, D("900"), 55, True, D("0"), D("0.4500")))
        self.assertEqual((by[("2026-08-31", 1)]["sold_sum"], by[("2026-08-31", 1)]["rate_sum"], by[("2026-08-31", 1)]["mature"]), (D("900"), D("1.0000"), True))   # 31.08 + 25 = 25.09 ≤ сегодня
        r9 = by[("2026-09-10", 1)]
        self.assertEqual((r9["mature"], r9["rate_sum"], r9["sold_sum"], r9["created_sum"]), (False, None, D("1000"), D("4000")))
        r7 = by[("2026-08-15", 7)]
        self.assertEqual((r7["created_qty"], r7["sold_sum"], r7["rate_sum"], r7["vendor_code"]), (0, D("300"), None, None))
        self.assertEqual((stats["sales_without_funnel_row"], stats["sales_without_funnel_sum"], stats["skipped"], stats["keys"], stats["mature_keys"]), (1, D("300"), {"no_order_dt": 1, "no_nm": 0}, 5, 4))
        rows_w, _s = cohort.build_sku_rows(FUNNEL, SALES, "2026-09-25", day_from="2026-08-31")
        self.assertEqual([(r["day"], r["nm_id"]) for r in rows_w], [("2026-08-31", 1), ("2026-09-10", 1)])

    def test_daily_rows_forecast_from_the_last_30_mature_days(self):
        rows, _s = cohort.build_sku_rows(FUNNEL, SALES, "2026-09-25")
        daily, forecast = cohort.build_daily_rows(rows, "2026-09-25")
        by = {r["day"]: r for r in daily}
        self.assertEqual((by["2026-08-01"]["created_sum"], by["2026-08-01"]["sold_sum"], by["2026-08-01"]["rate_sum"], by["2026-08-01"]["forecast_rate_sum"]), (D("2500"), D("1400"), D("0.5600"), None))
        self.assertEqual(forecast["window"], "2026-08-02 … 2026-08-31")                                  # 30 дней назад от последнего зрелого дня 31.08
        self.assertEqual((forecast["rate_sum"], forecast["rate_qty"]), (ratio_(D("1200"), D("900")), ratio_(2, 1)))   # в окне 15.08 (0 / 300) и 31.08 (900 / 900)
        r9 = by["2026-09-10"]
        self.assertEqual((r9["mature"], r9["rate_sum"], r9["forecast_rate_sum"], r9["forecast_window"]), (False, None, forecast["rate_sum"], forecast["window"]))

    def test_month_table_equals_the_book_coefficient_on_mature_months(self):
        rows, _s = cohort.build_sku_rows(FUNNEL, SALES, "2026-09-26")
        daily, _f = cohort.build_daily_rows(rows, "2026-09-26")
        months = cohort.month_table(daily)
        self.assertEqual((months["2026-08"]["mature_days"], months["2026-08"]["days"], months["2026-08"]["rate_sum"], months["2026-08"]["rate_qty"]), (3, 3, D("0.7647"), D("0.7500")))   # 2600 / 3400, 3 / 4
        orders = {"2026-08": (D("3400"), 3), "2026-09": (D("4000"), 4)}
        book = fr.buyout_rate_for_finrez("2026-08", "2026-09", sb=object(), rows=[dict(r, sale_dt=r["order_dt"]) for r in SALES], orders_by_month=orders, today="2026-09-26")
        self.assertEqual(book["авг"], months["2026-08"]["rate_sum"])                                       # одно определение — одно число
        self.assertEqual((months["2026-09"]["mature_days"], months["2026-09"]["rate_sum"], months["2026-09"]["all_rate_sum"]), (0, None, D("0.2500")))


class StepPayloadTests(unittest.TestCase):
    def test_payload_carries_this_write_time_and_strings_for_decimals(self):
        import scripts.wb_buyout_cohort_step as step
        rows, _s = cohort.build_sku_rows(FUNNEL, SALES, "2026-09-25")
        daily, _f = cohort.build_daily_rows(rows, "2026-09-25")
        sku, day = step.payloads(rows, daily, "2026-09-26T00:29:02+00:00")
        self.assertTrue(all(r["observed_at"] == "2026-09-26T00:29:02+00:00" for r in sku + day))
        self.assertEqual((sku[0]["created_sum"], sku[0]["rate_sum"], day[0]["rate_sum"]), ("2000", "0.4500", "0.5600"))
        self.assertIsNone([r for r in day if r["day"] == "2026-09-10"][0]["rate_sum"])


def ratio_(a, b):
    return cohort.ratio(a, b)


if __name__ == "__main__":
    unittest.main()
