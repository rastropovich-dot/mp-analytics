"""Единые категории РБХ (согласованы владельцем 26.10): словари перенесены дословно из правил советника, все категории — в группах,
пути fix / amb / none; нерешённая не уходит в «Прочее». Числа против «Сводки» черновика — scripts/rbh_categories_check.py (база)."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from loaders import rbh_categories as rc  # noqa: E402

RULES = os.path.join(ROOT, "data", "rbh_costs", "rbh_categories_rules.py")


class RbhCategories(unittest.TestCase):
    def test_taxonomy(self):
        self.assertEqual((len(rc.G), sum(len(v) for v in rc.G.values())), (17, 83))
        self.assertEqual({c for c in rc.FIX.values()} - set(rc.CAT2G), set())
        self.assertEqual({c for v in rc.AMB.values() for _p, c in v} - set(rc.CAT2G), set())
        self.assertEqual(set(rc.FIX) & set(rc.AMB), set())

    @unittest.skipUnless(os.path.exists(RULES), "правила советника лежат вне git (data/rbh_costs)")
    def test_copied_verbatim_from_the_advisor_rules(self):
        ns = {}
        exec(open(RULES, encoding="utf-8").read(), ns)
        self.assertEqual((ns["G"], ns["FIX"], ns["AMB"]), (rc.G, rc.FIX, rc.AMB))

    def test_paths(self):
        self.assertEqual(rc.classify("Пудры", "что угодно"), ("Макияж лица", "Пудры", "fix"))
        self.assertEqual(rc.classify("Кремы", "Крем для рук Vival"), ("Уход за телом и гигиена", "Кремы для рук", "amb"))
        self.assertEqual(rc.classify("", "Палетка теней для век"), ("Макияж глаз", "Тени для век", "amb"))      # «(нет)» — по названию
        self.assertEqual(rc.classify("Кремы", "без подсказки"), (None, None, "none"))
        self.assertEqual(rc.label("Новый предмет WB", "x"), (rc.UNRESOLVED, f"{rc.UNRESOLVED} Новый предмет WB"))   # не «Прочее»
        self.assertNotEqual(rc.label("Новый предмет WB", "x")[0], "Прочее")

    def test_profiles(self):
        import cabinet
        self.assertEqual((cabinet.profile("rbh1").CATEGORY_RULES, cabinet.profile("rbh2").CATEGORY_RULES), ("rbh", "rbh"))
        self.assertIsNone(cabinet.profile("karatov").CATEGORY_RULES)


if __name__ == "__main__":
    unittest.main()
