"""Прайс РБХ «до 01.09» (слово владельца 27.10): артикул «0» не грузится и считается; боковой блок «Артикул поставщика | Баркод |
Себестоимость по умолчанию» добавляет только артикулы, которых нет в основном блоке, с одной ценой; цена — с НДС, в unit_cost — ÷ 1,22."""
import os
import sys
import tempfile
import unittest
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from loaders import rbh_cost_price as rp  # noqa: E402


def write_book(path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["КодID", "Артикул", "Бренд", "С-ть до МП, руб/шт", "Sku OZ", None, None, None, None, None, None])
    ws.append([1, "71862", "Мочалки", "174.74", "111", None, None, None, "Артикул поставщика", "Баркод", "Себестоимость по умолчанию"])
    ws.append([2, "0", "X", "100", "222", None, None, None, "71862_16333", "460", "175"])
    ws.append([3, "0", "Y", "120", None, None, None, None, "6001311", "461", "418"])
    ws.append([4, "430341", "Pastel", "122", None, None, None, None, "6116435", "462", "342"])
    ws.append([None, None, None, None, None, None, None, None, "6116435", "463", "355"])
    ws.append([None, None, None, None, None, None, None, None, "430341", "464", "122"])
    wb.save(path)


class PriceUntil0901(unittest.TestCase):
    def test_zero_article_side_block_and_vat(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "price.xlsx")
            write_book(p)
            rows, conflicts, stats = rp.read_price(p, "2026-04-01")
            self.assertEqual(sorted(r["offer_id_norm"] for r in rows), ["430341", "71862"])
            self.assertEqual((stats["zero_article"], stats["zero_article_sku_oz"], conflicts), (2, 1, []))
            self.assertEqual(next(r for r in rows if r["offer_id_norm"] == "71862")["unit_cost"], "143.23")    # 174,74 ÷ 1,22
            side, sst = rp.read_side_block(p, {r["offer_id_norm"] for r in rows}, "2026-04-01")
            self.assertEqual([(r["offer_id_norm"], r["unit_cost"]) for r in side], [("6001311", "342.62"), ("71862_16333", "143.44")])  # 418 / 175 ÷ 1,22
            self.assertEqual((sst["in_main"], sst["double_price"]), (1, [("6116435", ["342.00", "355.00"])]))
            self.assertTrue(all(r["source_file"].endswith("#боковой блок") for r in side))
            self.assertEqual(rp.db_rows(side)[0].keys(), rp.db_rows(rows)[0].keys())                        # те же колонки, что у основного


if __name__ == "__main__":
    unittest.main()
