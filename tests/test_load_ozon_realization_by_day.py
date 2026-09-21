"""Чистые функции сбора /v1/finance/realization/by-day.

Строка ответа из живого вызова 2026-08-31 (первая строка отчёта, sku 958696442):
в ней два блока, delivery_commission и return_commission; здесь они раскладываются
в две записи с side, а тождества сумм проверяются так же, как на 210 блоках 31.08.
"""
import unittest
from decimal import Decimal

from scripts.load_ozon_realization_by_day import check_identities, flatten_rows, summarize

SALE = {"price_per_instance": 17062, "quantity": 1, "amount": 17062, "compensation": 0, "commission": 0,
        "bonus": 19913.38, "standard_fee": 17087.16, "total": 20058.84, "stars": 0,
        "bank_coinvestment": 170.62, "pick_up_point_coinvestment": 0}
RETURN = {"price_per_instance": 1000, "quantity": 2, "amount": 2000, "compensation": 0, "commission": 0,
          "bonus": 100, "standard_fee": 900, "total": 1200, "stars": 0,
          "bank_coinvestment": 0, "pick_up_point_coinvestment": 0}
ROW = {"item": {"name": "Серьги", "offer_id": "F000281767-ИЗгт", "barcode": "OZN958696442", "sku": 958696442},
       "delivery_commission": SALE, "return_commission": None, "commission_ratio": 0.46, "rowNumber": 1,
       "seller_price_per_instance": 37146}


class FlattenTests(unittest.TestCase):
    def test_one_side_gives_one_record_with_normalized_offer_id(self):
        recs = flatten_rows([ROW], "2026-08-31", "2026-09-14T20:00:00+00:00")
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r["realization_date"], r["row_number"], r["side"]), ("2026-08-31", 1, "sale"))
        self.assertEqual(r["sku"], 958696442)
        self.assertEqual(r["offer_id"], "F000281767-ИЗгт")
        self.assertEqual(r["offer_id_norm"], "f000281767-изгт")
        self.assertEqual(r["quantity"], 1)
        self.assertEqual(r["bonus"], "19913.38")
        self.assertEqual(r["seller_price_per_instance"], "37146.00")
        self.assertEqual(r["commission_ratio"], "0.4600")
        self.assertIs(r["raw_row"], ROW)

    def test_both_sides_give_two_records_same_row_number(self):
        row = dict(ROW, return_commission=RETURN, rowNumber=7)
        recs = flatten_rows([row], "2026-08-31", "t")
        self.assertEqual([(r["row_number"], r["side"], r["quantity"]) for r in recs], [(7, "sale", 1), (7, "return", 2)])

    def test_zero_quantity_side_is_loud(self):
        row = dict(ROW, delivery_commission=dict(SALE, quantity=0))
        with self.assertRaises(RuntimeError):
            flatten_rows([row], "2026-08-31", "t")

    def test_protobuf_leak_in_commission_ratio_is_loud(self):
        row = dict(ROW, commission_ratio='value:"0.450000"')
        with self.assertRaises(RuntimeError) as ctx:
            flatten_rows([row], "2026-08-31", "t")
        self.assertIn("commission_ratio", str(ctx.exception))

    def test_missing_sku_is_loud(self):
        row = dict(ROW, item={"offer_id": "X", "sku": None})
        with self.assertRaises(RuntimeError):
            flatten_rows([row], "2026-08-31", "t")


class IdentityTests(unittest.TestCase):
    def test_live_row_satisfies_both_identities(self):
        recs = flatten_rows([dict(ROW, return_commission=RETURN)], "2026-08-31", "t")
        self.assertEqual(check_identities(recs), [])

    def test_broken_total_is_reported(self):
        row = dict(ROW, delivery_commission=dict(SALE, total=1))
        recs = flatten_rows([row], "2026-08-31", "t")
        bad = check_identities(recs)
        self.assertEqual(len(bad), 1)
        self.assertIn("total", bad[0][3])

    def test_summary_sums_per_side(self):
        recs = flatten_rows([dict(ROW, return_commission=RETURN)], "2026-08-31", "t")
        s = summarize(recs)
        self.assertEqual(s["sale"]["quantity"], 1)
        self.assertEqual(s["return"]["quantity"], 2)
        self.assertEqual(s["sale"]["bonus"], Decimal("19913.38"))
        self.assertEqual(s["return"]["amount"], Decimal("2000.00"))


if __name__ == "__main__":
    unittest.main()
