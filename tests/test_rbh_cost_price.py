"""Прайс себестоимости РБХ → строки article_unit_costs (loaders/rbh_cost_price.py, дополнение 23.10): НДС ÷ 1,22, артикул как текст, бренд, конфликты."""
import os
import sys
import tempfile
import unittest
from decimal import Decimal

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_000_no_network as _guard  # noqa: E402,F401
from loaders import rbh_cost_price as rc  # noqa: E402

HEADER = ("КодID", "Артикул", "Бренд", "С-ть до МП, руб/шт", "Sku OZ")


def make_price(rows, path):
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Лист1"
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    wb.save(path)


class RbhCostPriceTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory(); self.path = os.path.join(self.d.name, "rbh_cost_20261015.xlsx")

    def tearDown(self):
        self.d.cleanup()

    def test_rows_vat_division_text_article_and_brand(self):
        make_price([(1, 930281, "ESSENCE", 181.85424182522559, None),
                    (2, "00123AB", "ASTRA", 122.0, 1234567),
                    (3, 73554, "ESSENCE", 303.027849, 7654321)], self.path)
        rows, conflicts, stats = rc.read_price(self.path, "2026-10-15")
        self.assertEqual(conflicts, []); self.assertEqual(len(rows), 3)
        r0 = {r["offer_id"]: r for r in rows}
        self.assertEqual(r0["930281"]["offer_id_norm"], "930281")                       # число → текст без «.0»
        self.assertEqual(r0["00123AB"]["offer_id_norm"], "00123ab")                     # ведущие нули целы, ключ без регистра
        self.assertEqual((r0["930281"]["unit_cost"], r0["930281"]["_price_with_vat"]), ("149.06", "181.85"))   # 181,85 ÷ 1,22
        self.assertEqual((r0["00123AB"]["unit_cost"], r0["00123AB"]["_price_with_vat"]), ("100.00", "122.00"))
        self.assertTrue(all(r["unit_cost_uniform"] is None for r in rows))                   # цена с НДС в таблицу не пишется
        self.assertEqual((r0["930281"]["variant_type"], r0["930281"]["article_1c"], r0["930281"]["analytics_group"]), ("main", "930281", "ESSENCE"))
        self.assertEqual(stats["sku_oz_filled"], 2); self.assertEqual(stats["sku_oz_duplicates"], [])
        self.assertEqual(stats["brands"]["ESSENCE"][0], 2)
        self.assertEqual(stats["sum_with_vat"], Decimal("606.88")); self.assertEqual(stats["sum_without_vat"], Decimal("497.45"))   # 149,06 + 100,00 + 248,39
        db = rc.db_rows(rows)
        self.assertFalse(any(k.startswith("_") for k in db[0]))
        self.assertEqual(rc.sku_oz_map(rows), {"1234567": "00123AB", "7654321": "73554"})

    def test_conflict_duplicate_and_skips(self):
        make_price([(1, 111, "A", 122.0, None), (2, 111, "A", 244.0, None),          # один артикул, две цены — конфликт
                    (3, 222, "B", 122.0, 5), (4, 222, "B", 122.0, 5),                  # дубль с той же ценой — свёрнут, Sku OZ дубль по разным строкам одного артикула — нет
                    (5, None, "C", 10.0, None), (6, 333, "C", 0, None), (7, 444, "C", None, None),
                    (8, 555, "D", 122.0, 9), (9, 666, "D", 122.0, 9)], self.path)     # один Sku OZ у двух артикулов — дубль
        rows, conflicts, stats = rc.read_price(self.path, "2026-10-15")
        self.assertEqual([c[0] for c in conflicts], ["111"]); self.assertEqual(conflicts[0][1], ["122.00", "244.00"])
        self.assertEqual(sorted(r["offer_id"] for r in rows), ["222", "555", "666"])
        self.assertEqual((stats["empty_article"], stats["empty_cost"], stats["duplicates_same_cost"], stats["conflicts"]), (1, 2, 1, 1))
        self.assertEqual(stats["sku_oz_duplicates"], [("9", ["555", "666"])])

    def test_no_vat_rate_keeps_price(self):
        make_price([(1, 1, "A", 122.0, None)], self.path)
        rows, _c, _s = rc.read_price(self.path, "2026-10-15", vat_rate=None)
        self.assertEqual((rows[0]["unit_cost"], rows[0]["_price_with_vat"], rows[0]["unit_cost_uniform"]), ("122.00", "122.00", None))

    def test_missing_required_column_is_loud(self):
        wb = openpyxl.Workbook(); ws = wb.active; ws.append(("КодID", "Бренд")); wb.save(self.path)
        with self.assertRaises(RuntimeError):
            rc.read_price(self.path, "2026-10-15")


if __name__ == "__main__":
    unittest.main()
