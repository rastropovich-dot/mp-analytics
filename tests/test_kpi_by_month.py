"""59-я: витрина SKU по месяцам = витрине целиком до бита (подменённые источники трёх месяцев, несколько строк на ключ с дробными
суммами, хранение Ozon с переносом типа 46, WB хранение / реклама / «прочее», органика); границы месяцев; генератор чтения по id."""
import unittest
from collections import defaultdict
from datetime import date
from decimal import Decimal
from unittest import mock

import reports_daily_sku_kpi as kpi

TODAY = date(2026, 10, 27)


def _orders():
    rows, i = [], 0
    for day in ("2026-08-30", "2026-08-31", "2026-09-01", "2026-09-22", "2026-09-30", "2026-10-01", "2026-10-26"):
        for sku in ("100", "200"):
            for schema, qty, amount in (("fbo", 1, 1000.1), ("fbs", 2, 2000.2), ("fbo2", 1, 0.3)):
                i += 1
                rows.append({"id": i, "order_date": day, "marketplace_code": "ozon", "marketplace_sku": sku, "article": f"A{sku}",
                             "product_name": f"Товар {sku}", "orders_qty": qty, "orders_amount_seller": amount, "cancelled_orders_qty": 1,
                             "order_schema": schema})
        i += 1
        rows.append({"id": i, "order_date": day, "marketplace_code": "wb", "marketplace_sku": "777", "article": "", "product_name": None,
                     "orders_qty": 1, "orders_amount_seller": 10.7, "cancelled_orders_qty": 0})
    return rows


def _buyouts():
    return [{"id": n, "buyout_date": d, "marketplace_code": "ozon", "marketplace_sku": "300", "article": "", "product_name": None,
             "buyouts_qty": 1, "buyouts_amount_seller": 500.05} for n, d in enumerate(("2026-08-31", "2026-09-15", "2026-10-02"), 1)]


def _expenses():
    rows, i = [], 0
    for day in ("2026-08-30", "2026-09-01", "2026-10-26"):
        for sku, etype, amount in (("100", "commission", 10.01), ("100", "advertising_clicks", 3.3), ("", "logistics", 100.0),
                                   ("200", "logistics", 7.7), ("", "unknown_127", 28.0)):
            i += 1
            rows.append({"id": i, "expense_date": day, "marketplace_code": "ozon", "marketplace_sku": sku, "article": "",
                         "expense_type": etype, "expense_amount": amount})
    return rows


PLACEMENT = [{"day": d, "sku": sku, "amount": a} for d in ("2026-08-30", "2026-09-01", "2026-10-26")
             for sku, a in (("100", "-1.10"), ("200", "-2.20"), ("999", "-0.70"))]
LEDGER46 = {"2026-08-30": Decimal("4.00"), "2026-09-01": Decimal("4.00"), "2026-10-26": Decimal("4.00")}
WB_STORAGE = [{"day": d, "nm_id": nm, "warehouse_price": p, "vendor_code": vc}
              for d in ("2026-08-31", "2026-09-02", "2026-10-03") for nm, p, vc in ((777, 1.15, "VC777"), (888, 2.25, ""), (888, 0, "VC888"))]
WB_ADS = {("2026-08-31", 777): Decimal("5.55"), ("2026-09-02", 999): Decimal("6.66"), ("2026-10-03", 777): Decimal("7.77")}
WB_OTHER = [("2026-09-02", 777, Decimal("1.01"), "row"), ("2026-10-26", 888, Decimal("2.02"), "review_text")]
ORGANIC = [{"sale_date": "2026-09-01", "marketplace_code": "ozon", "marketplace_sku": "100", "article": "A100", "product_name": None,
            "ad_orders_qty": 1, "ad_orders_revenue": 100.5, "organic_orders_qty": 2, "organic_orders_revenue": 200.25}]


def _placement_loader(keep=None, day_from=None, day_to=None):
    by_day = defaultdict(lambda: defaultdict(Decimal))
    for r in PLACEMENT:
        if day_from is None or day_from <= r["day"] <= day_to:
            by_day[r["day"]][r["sku"]] += Decimal(r["amount"])
    rows, totals, skipped = [], {}, [0, Decimal(0)]
    for day in sorted(by_day):
        totals[day] = -sum(by_day[day].values(), Decimal(0))
        for sku, amount in by_day[day].items():
            if keep is None or keep(day, sku):
                rows.append({"day": day, "sku": sku, "amount": amount, "offer_id": ""})
            else:
                skipped[0] += 1; skipped[1] += -amount
    return rows, totals, skipped


def _period(rows, col):
    return lambda day_from=None, day_to=None: [r for r in rows if day_from is None or day_from <= r[col] <= day_to]


def _common(stack):
    for p in (mock.patch.object(kpi, "load_orders", side_effect=_period(_orders(), "order_date")),
              mock.patch.object(kpi, "load_buyouts", side_effect=_period(_buyouts(), "buyout_date")),
              mock.patch.object(kpi, "load_expenses", side_effect=_period(_expenses(), "expense_date")),
              mock.patch.object(kpi, "load_placement", side_effect=_placement_loader),
              mock.patch.object(kpi, "load_ledger_type46", return_value=dict(LEDGER46)),
              mock.patch.object(kpi, "load_wb_cohort", return_value=({}, {})),
              mock.patch("builtins.print")):
        stack.enter_context(p)


def _by_key(rows):
    return {(r["kpi_date"], r["marketplace_code"], r["marketplace_sku"]): r for r in rows}


class ByMonthEqualsWhole(unittest.TestCase):
    def whole(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            _common(stack)
            stack.enter_context(mock.patch.object(kpi, "load_ozon_organic", return_value=list(ORGANIC)))
            stack.enter_context(mock.patch.object(kpi, "load_wb_costs", return_value={
                "storage": list(WB_STORAGE), "ads": dict(WB_ADS), "other": list(WB_OTHER), "articles": kpi.wb_articles_of(WB_STORAGE)}))
            return kpi.build_kpi(today=TODAY)

    def by_month(self, saved, failures=()):
        from contextlib import ExitStack
        shared = {"wb_cohort": ({}, {}), "ledger46": dict(LEDGER46), "organic": list(ORGANIC),
                  "article_map": kpi.build_article_map(_orders()), "wb_ads": dict(WB_ADS), "wb_other": list(WB_OTHER),
                  "wb_articles": kpi.wb_articles_of(WB_STORAGE)}
        with ExitStack() as stack:
            _common(stack)
            stack.enter_context(mock.patch.object(kpi, "load_shared", return_value=shared))
            stack.enter_context(mock.patch.object(kpi, "source_bounds", return_value=("2026-01-01", "2026-10-27")))
            stack.enter_context(mock.patch.object(kpi, "load_wb_storage", side_effect=lambda today, f, t: [r for r in WB_STORAGE if f <= r["day"] <= t]))
            stack.enter_context(mock.patch.object(kpi, "SOURCE_READ_FAILURES", list(failures)))
            stack.enter_context(mock.patch.object(kpi, "save_kpi", side_effect=lambda rows, failures=None, final=True: saved.append((list(rows), list(failures)))))
            return kpi.build_by_month(today=TODAY, write=True)

    def test_rows_equal_to_the_bit(self):
        whole = _by_key(self.whole())
        saved = []
        total, window_keys = self.by_month(saved)
        months = _by_key([r for rows, _f in saved for r in rows])
        self.assertEqual(total, len(whole))
        self.assertEqual(set(months), set(whole))
        for key, row in whole.items():
            self.assertEqual(months[key], row, key)          # dict ==: float до бита, все поля
        self.assertEqual(len(saved), 10)                      # январь … октябрь, пустые месяцы — пустая запись
        self.assertGreater(len([1 for rows, _f in saved if rows]), 2)
        # ключи чисток: только Ozon, окно 30 дней и 7 дней перед ним (строки «только хранение» выпавших дней, 09-22 — там)
        expected = {k for k in whole if k[1] == "ozon" and k[0] >= "2026-09-20"}
        self.assertIn(("2026-09-22", "ozon", "100"), expected)
        self.assertEqual({(r["kpi_date"], r["marketplace_code"], r["marketplace_sku"]) for r in window_keys}, expected)

    def test_type46_moved_the_same(self):
        whole = _by_key(self.whole())
        saved = []
        self.by_month(saved)
        months = _by_key([r for rows, _f in saved for r in rows])
        for day in ("2026-08-30", "2026-09-01", "2026-10-26"):
            self.assertEqual(months[(day, "ozon", "")]["logistics_amount"], whole[(day, "ozon", "")]["logistics_amount"])

    def test_shared_failure_reaches_every_month(self):
        saved = []
        self.by_month(saved, failures=["wb_paid_storage_nm_daily"])
        self.assertTrue(all(f == ["wb_paid_storage_nm_daily"] for _rows, f in saved))


class MonthPeriods(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(kpi.month_periods("2026-01-15", "2026-03-02"),
                         [("2026-01-15", "2026-01-31"), ("2026-02-01", "2026-02-28"), ("2026-03-01", "2026-03-02")])
        self.assertEqual(kpi.month_periods("2026-12-01", "2027-01-01"), [("2026-12-01", "2026-12-31"), ("2027-01-01", "2027-01-01")])


class IterById(unittest.TestCase):
    class _Sb:
        def __init__(self, pages):
            self.pages = pages

        def table(self, _name):
            return self

        def __getattr__(self, _name):
            return lambda *a, **k: self

        def execute(self):
            return type("R", (), {"data": self.pages.pop(0)})()

    def test_streams_pages_and_stops(self):
        sb = self._Sb([[{"id": i} for i in range(1, 1001)], [{"id": 1001}]])
        with mock.patch.object(kpi, "supabase", sb):
            self.assertEqual([r["id"] for r in kpi.iter_by_id("t")], list(range(1, 1002)))

    def test_repeat_raises(self):
        sb = self._Sb([[{"id": i} for i in range(1, 1001)], [{"id": 1000}]])
        with mock.patch.object(kpi, "supabase", sb), self.assertRaises(RuntimeError):
            list(kpi.iter_by_id("t"))


if __name__ == "__main__":
    unittest.main()
