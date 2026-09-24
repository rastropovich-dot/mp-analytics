"""Тридцать седьмая §3: категория товара из карточки Ozon.

Правила, которые закрепляют тесты: категория берётся по type_name карточки через явный словарь, незнакомый тип — «прочее» и
считается вслух; SKU без карточки — по названию с пометкой источника; карточка ищется и по sources[].sku; бренд — по первой
букве артикула; сверки карточка/название и карточка/1С считают совпадения и расхождения. Сети нет.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ozon_product_catalog as cat  # noqa: E402

TREE = {"result": [{"description_category_id": 1, "category_name": "Ювелирные изделия", "children": [
    {"description_category_id": 17027900, "category_name": "Ювелирные украшения", "children": [
        {"type_id": 92590, "type_name": "Кольцо ювелирное"}, {"type_id": 92597, "type_name": "Серьги ювелирные"},
        {"type_id": 92592, "type_name": "Крестик ювелирный"}, {"type_id": 234953747, "type_name": "Сувенир ювелирный"},
        {"type_id": 92579, "type_name": "Браслет ювелирный"}]}]}]}


def item(sku, type_id, name, offer_id, extra_source=None):
    it = {"id": sku * 10, "sku": sku, "offer_id": offer_id, "name": name, "description_category_id": 17027900, "type_id": type_id,
          "is_archived": False, "statuses": {"status_name": "Продается"}, "sources": [{"sku": sku}]}
    if extra_source:
        it["sources"].append({"sku": extra_source})
    return it


class Rules(unittest.TestCase):
    def test_type_dictionary_and_unknown(self):
        self.assertEqual(cat.category_by_type("Кольцо ювелирное"), "кольца")
        self.assertEqual(cat.category_by_type("Крестик ювелирный"), "подвески")
        self.assertIsNone(cat.category_by_type("Сувенир ювелирный"))
        self.assertTrue(set(cat.TYPE_TO_CATEGORY.values()) <= set(cat.OWNER_CATEGORIES))

    def test_name_rules_order(self):
        self.assertEqual(cat.category_by_name("Кольцо серебряное для пирсинга"), "пирсинг")
        self.assertEqual(cat.category_by_name("Браслет золотой женский 585 пробы, браслет-цепочка"), "цепочки")   # по названию — цепочка; карточка скажет браслет
        self.assertEqual(cat.category_by_name("Медальон с образом"), "прочее")

    def test_brand_and_metal(self):
        self.assertEqual((cat.brand_of("T000123"), cat.brand_of("F000283615"), cat.brand_of("S1"), cat.brand_of("")), ("Топаз", "KARATOV", "KARATOV", None))
        self.assertEqual(cat.metal_of("Серьги золотые 585 пробы"), "Золото")
        self.assertIsNone(cat.metal_of("Медальон"))


class Rows(unittest.TestCase):
    INFO = {"11": {"article": "F1", "name": "Кольцо серебряное 925", "turnover": 0}, "12": {"article": "T2", "name": "Серьги золотые 585", "turnover": 0},
            "13": {"article": "F3", "name": "Подвеска крест золотой", "turnover": 0}, "14": {"article": "F4", "name": "Медальон с образом", "turnover": 0},
            "15": {"article": "F5", "name": "Цепь золотая 585", "turnover": 0}, "16": {"article": "F6", "name": "Браслет золотой, браслет-цепочка", "turnover": 0}}

    def rows(self):
        items = [item(11, 92590, "Кольцо серебряное 925, KARATOV", "F1"), item(99, 92597, "Серьги золотые 585, KARATOV", "T2", extra_source=12),
                 item(13, 92592, "Крестик золотой", "F3"), item(14, 234953747, "Медальон с образом Ангела", "F4"),
                 item(16, 92579, "Браслет золотой женский 585 пробы, браслет-цепочка", "F6")]
        return cat.build_rows(self.INFO, items, TREE, "2026-09-24T13:22:41+00:00")

    def test_categories_sources_and_counters(self):
        rows, counters, other = self.rows()
        by = {r["sku"]: r for r in rows}
        self.assertEqual((by["11"]["category"], by["11"]["category_source"], by["11"]["type_name"]), ("кольца", "card", "Кольцо ювелирное"))
        self.assertEqual((by["12"]["category"], by["12"]["category_source"]), ("серьги", "card"))          # найдена по sources[].sku
        self.assertEqual(by["13"]["category"], "подвески")
        self.assertEqual((by["14"]["category"], by["14"]["category_source"]), ("прочее", "card"))          # тип без словаря — прочее и вслух
        self.assertEqual(dict(other), {"Сувенир ювелирный": 1})
        self.assertEqual((by["15"]["category"], by["15"]["category_source"]), ("цепочки", "name"))       # карточки нет — по названию
        self.assertEqual(by["16"]["category"], "браслеты")                                              # карточка сильнее названия («браслет-цепочка»)
        self.assertEqual(counters, {"card": 5, "no_card": 1})
        self.assertEqual((by["12"]["brand"], by["11"]["metal"], by["12"]["metal"]), ("Топаз", "Серебро", "Золото"))
        self.assertEqual(by["11"]["category_path"], "Ювелирные изделия / Ювелирные украшения")
        self.assertEqual(by["11"]["observed_at"], "2026-09-24T13:22:41+00:00")

    def test_compare_against_name_and_1c(self):
        rows, _c, _o = self.rows()
        vs_name, vs_1c = cat.compare(rows, {"f1": "Кольцо", "t2": "Серьги", "f3": "Крест", "f4": "Иконка", "f6": "Браслет"})
        self.assertEqual((vs_name["agree"], vs_name["differ"]), (4, 1))          # расходится только браслет-цепочка
        self.assertEqual(vs_name["examples"][0][0], "16")
        self.assertEqual((vs_1c["agree"], vs_1c["differ"], vs_1c["no_1c"]), (4, 1, 0))   # медальон: карточка «прочее», 1С «Иконка» → подвески
        self.assertEqual(vs_1c["examples"][0][0], "14")

    def test_flatten_tree(self):
        types, cats = cat.flatten_tree(TREE)
        self.assertEqual(types[92590][0], "Кольцо ювелирное")
        self.assertEqual(cats[17027900], "Ювелирные изделия / Ювелирные украшения")


if __name__ == "__main__":
    unittest.main()
