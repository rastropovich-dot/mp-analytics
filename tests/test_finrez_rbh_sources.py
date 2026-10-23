"""Книга «Фин рез» у РБХ (дополнение 23.10 п. 2): бренд из прайса, категория — тип карточки Ozon / предмет WB, комиссия WB расчётная."""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import test_000_no_network as _guard  # noqa: E402,F401
import report_finrez as rf  # noqa: E402
import report_finrez_wb as fw  # noqa: E402
import report_wb_month as rwm  # noqa: E402
from loaders import unit_cost_history as uch  # noqa: E402


class DescribeSources(unittest.TestCase):
    def test_price_brand_and_platform_type_under_rbh_switches(self):
        saved = (rf.BRAND_SOURCE, rf.CATEGORY_SOURCE, dict(rf.PRICE_BRANDS))
        catalog = {"1": {"brand": "Beautyhome.me", "category": "прочее", "type_name": " Тени ", "name": "Тени для век", "offer_id": "930281"},
                   "2": {"brand": "Beautyhome.me", "category": "прочее", "type_name": None, "name": "Без типа", "offer_id": "X1"}}
        try:
            rf.BRAND_SOURCE, rf.CATEGORY_SOURCE = "price", "platform"
            rf.PRICE_BRANDS.clear(); rf.PRICE_BRANDS.update({"930281": "ESSENCE"})
            self.assertEqual(rf.describe("1", {}, catalog, {}), ("ESSENCE", "Тени", "930281", "Тени для век"))
            self.assertEqual(rf.describe("2", {}, catalog, {}), ("Beautyhome.me", rf.NO_TYPE, "X1", "Без типа"))   # нет в прайсе — бренд карточки
            rf.BRAND_SOURCE, rf.CATEGORY_SOURCE = "catalog", "owner"
            self.assertEqual(rf.describe("1", {}, catalog, {})[:2], ("Beautyhome.me", "прочее"))                   # KARATOV как раньше
        finally:
            rf.BRAND_SOURCE, rf.CATEGORY_SOURCE = saved[0], saved[1]
            rf.PRICE_BRANDS.clear(); rf.PRICE_BRANDS.update(saved[2])

    def test_wb_brand_and_category_sources(self):
        saved = (fw.BRAND_SOURCE, fw.CATEGORY_SOURCE, dict(fw.PRICE_BRANDS))
        try:
            fw.BRAND_SOURCE, fw.CATEGORY_SOURCE = "price", "platform"
            fw.PRICE_BRANDS.clear(); fw.PRICE_BRANDS.update({"930281": "ESSENCE"})
            self.assertEqual(fw.brand_of(" 930281 ", "Beautyhome.me"), "ESSENCE")
            self.assertEqual(fw.brand_of("51050-W", None), fw.BRAND_DEFAULT)
            self.assertEqual(fw.category_of(" Блеск для губ "), "Блеск для губ")
            self.assertEqual(fw.category_of(""), fw.CATEGORY_UNKNOWN)
        finally:
            fw.BRAND_SOURCE, fw.CATEGORY_SOURCE = saved[0], saved[1]
            fw.PRICE_BRANDS.clear(); fw.PRICE_BRANDS.update(saved[2])

    def test_brand_map_takes_latest_snapshot_per_key(self):
        class FakeQ:
            def __init__(self, rows): self.rows = rows
            def select(self, *_a, **_k): return self
            def eq(self, *_a): return self
            def order(self, *_a): return self
            def range(self, a, b): self._r = (a, b); return self
            def execute(self):
                a, b = self._r
                class R: pass
                r = R(); r.data = self.rows[a:b + 1]; return r
        class FakeSb:
            def __init__(self, rows): self.rows = rows
            def table(self, _n): return FakeQ(self.rows)
        rows = [{"offer_id_norm": "a", "snapshot_date": "2026-05-20", "analytics_group": "OLD"},
                {"offer_id_norm": "a", "snapshot_date": "2026-10-15", "analytics_group": "NEW "},
                {"offer_id_norm": "b", "snapshot_date": "2026-10-15", "analytics_group": None}]
        self.assertEqual(uch.load_brand_map(FakeSb(rows)), {"a": "NEW"})


class WbOrdersMultiplier(unittest.TestCase):
    def test_after_commission_parameter_overrides_missing_owner_constant(self):
        saved = rwm.OWNER_ORDERS_AFTER_COMMISSION
        try:
            rwm.OWNER_ORDERS_AFTER_COMMISSION = None
            days = ["2026-10-01"]
            funnel = [{"day": "2026-10-01", "nm_id": 1, "vendor_code": "930281", "order_count": 2, "order_sum": "244.00", "buyout_count": 0, "buyout_sum": 0,
                       "cancel_count": 0, "cancel_sum": 0, "subject_name": "Тени"}]
            cost_fn = lambda code, size, day: (Decimal("50.00"), "точная строка")  # noqa: E731 — договор _cost_call: (СС, источник)
            import datetime
            rows_none = rwm.build_orders_daily(funnel, days, cost_fn, {}, {}, datetime.date(2026, 10, 23), ads_known=False)
            self.assertIsNone(rows_none[0]["revenue"])
            rows_k = rwm.build_orders_daily(funnel, days, cost_fn, {}, {}, datetime.date(2026, 10, 23), ads_known=False, after_commission=Decimal("0.5786"))
            self.assertEqual(rows_k[0]["revenue"], (Decimal("244.00") * Decimal("0.5786") / rows_k[0]["vat"]))
        finally:
            rwm.OWNER_ORDERS_AFTER_COMMISSION = saved


if __name__ == "__main__":
    unittest.main()
