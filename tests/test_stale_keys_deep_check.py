"""Тридцать седьмая §5: глубокая проверка застрявших ключей против сырья на диске.

Правила: сравниваются только дни с сырьём и старше двух суток; ключ есть в таблице, среди построенных нет — застрял; реклама
Performance в расходах не сравнивается; ключи в окне ночи помечаются (их снимет ночной механизм); ключи трёх таблиц строятся
теми же функциями, что ночью. Сети нет.
"""
import os
import sys
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import stale_keys_deep_check as deep  # noqa: E402


def m(v):
    return {"amount": str(v), "currency": "RUB"}


ACCRUAL = [{"date": "2026-08-20T00:00:00Z", "posting": {"posting_number": "P-1", "products": [
    {"sku": "111", "commission": {"sale_amount": m("1000"), "sale_commission": m("-400"), "sale_price": m("600"), "bonus": m("390"), "coinvestment": m("10")}}],
    "services": [{"type_id": 46, "name": "Логистика", "price": m("-12")}]},
    "services": []}]


class Keys(unittest.TestCase):
    def test_built_keys_come_from_the_nightly_builders(self):
        keys = deep.built_keys_for_day("2026-08-20", ACCRUAL, {46: "Логистика"})
        self.assertIn(("2026-08-20", "111"), keys["buyouts"])
        self.assertTrue(all(k[0] == "2026-08-20" for k in keys["expenses"] | keys["ledger"]))

    def test_table_key_functions(self):
        self.assertEqual(deep.TABLES["buyouts"]["key"]({"buyout_date": "2026-08-20", "marketplace_sku": 111}), ("2026-08-20", "111"))
        self.assertEqual(deep.TABLES["expenses"]["key"]({"expense_date": "d", "marketplace_sku": None, "expense_type": "other"}), ("d", "", "other"))
        self.assertEqual(deep.TABLES["ledger"]["key"]({"accrual_date": "d", "type_id": "41"}), ("d", 41))
        self.assertTrue(deep.TABLES["expenses"]["skip"]({"expense_type": "advertising_clicks"}))
        self.assertFalse(deep.TABLES["expenses"]["skip"]({"expense_type": "logistics"}))


class Plan(unittest.TestCase):
    def test_only_days_with_raw_and_older_than_two_days(self):
        rows = {"buyouts": [
            {"id": 1, "buyout_date": "2026-08-20", "marketplace_code": "ozon", "marketplace_sku": "111", "buyouts_amount_seller": "1000"},   # есть в сырье
            {"id": 2, "buyout_date": "2026-08-20", "marketplace_code": "ozon", "marketplace_sku": "222", "buyouts_amount_seller": "34416"},  # застрял
            {"id": 3, "buyout_date": "2026-08-21", "marketplace_code": "ozon", "marketplace_sku": "333", "buyouts_amount_seller": "5"},      # дня без сырья
            {"id": 4, "buyout_date": "2026-09-23", "marketplace_code": "ozon", "marketplace_sku": "444", "buyouts_amount_seller": "7"},      # моложе двух суток
            {"id": 5, "buyout_date": "2026-09-10", "marketplace_code": "ozon", "marketplace_sku": "555", "buyouts_amount_seller": "9"}]}     # застрял в окне ночи
        built = {"2026-08-20": {"buyouts": {("2026-08-20", "111")}, "expenses": set(), "ledger": set()},
                 "2026-09-10": {"buyouts": set(), "expenses": set(), "ledger": set()}}
        plan, counters = deep.deep_plan(rows, built, {"2026-08-20", "2026-09-10"}, "2026-09-24", "2026-08-24")
        ids = [r["id"] for r in plan["buyouts"]]
        self.assertEqual(ids, [2, 5])
        self.assertEqual([r["_in_night_window"] for r in plan["buyouts"]], [False, True])
        self.assertEqual(plan["buyouts"][0]["_money"], "34416")
        self.assertEqual((counters["buyouts:rows_without_raw"], counters["buyouts:young_rows"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
