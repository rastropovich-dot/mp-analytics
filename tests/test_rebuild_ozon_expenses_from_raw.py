"""Пересборка marketplace_expenses из сырья: план по ключам и контроль «Σ статей за день до = после».

День, где сумма всех статей после пересборки не равна сумме в таблице, в план не входит и называется; ключи
старой статьи, которых новой свёрткой нет, — к удалению; реклама Performance читателю не видна.
"""
import importlib.util
import os
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("rebuild_ozon_expenses_from_raw", os.path.join(ROOT, "scripts", "rebuild_ozon_expenses_from_raw.py"))
rb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rb)
D = Decimal


def row(day, sku, kind, amount, id_=None):
    r = {"expense_date": day, "marketplace_code": "ozon", "marketplace_sku": sku, "expense_type": kind, "expense_amount": str(amount)}
    if id_ is not None:
        r["id"] = id_
    return r


class PlanTests(unittest.TestCase):
    def test_keys_are_added_rewritten_deleted_or_kept_when_the_day_sum_holds(self):
        existing = [row("2026-05-01", "1", "logistics", "12.00", 1), row("2026-05-01", "1", "other", "5.00", 2), row("2026-05-01", "2", "commission", "100.00", 3)]
        built = {"2026-05-01": [row("2026-05-01", "1", "other", "17.00"), row("2026-05-01", "2", "commission", "100.00")]}
        p = rb.plan(existing, built, ["2026-05-01"])
        self.assertEqual(p["days_ok"], ["2026-05-01"])
        self.assertEqual([r["expense_type"] for r in p["delete"]], ["logistics"])
        self.assertEqual([(o["expense_type"], n["expense_amount"]) for o, n in p["rewrite"]], [("other", "17.00")])
        self.assertEqual((p["add"], p["same"]), ([], 1))
        m = p["by_month"]["2026-05"]
        self.assertEqual((m["before"]["logistics"], m["after"].get("logistics", D(0)), m["after"]["other"]), (D("12.00"), D(0), D("17.00")))

    def test_day_with_a_different_sum_is_skipped_and_named(self):
        existing = [row("2026-05-02", "1", "logistics", "12.00", 1)]
        built = {"2026-05-02": [row("2026-05-02", "1", "other", "15.00")]}          # начисление доехало после ночи — сумма дня другая
        p = rb.plan(existing, built, ["2026-05-02"])
        self.assertEqual(p["days_ok"], [])
        self.assertEqual(p["skipped_days"], [("2026-05-02", D("12.00"), D("15.00"), D("3.00"))])
        self.assertEqual((p["add"], p["rewrite"], p["delete"]), ([], [], []))

    def test_day_without_raw_is_not_touched(self):
        existing = [row("2026-05-03", "1", "logistics", "12.00", 1)]
        p = rb.plan(existing, {}, ["2026-05-03"])
        self.assertEqual((p["no_raw"], p["delete"]), (["2026-05-03"], []))

    def test_new_key_on_a_day_where_table_is_empty_is_skipped_by_the_sum_control(self):
        # таблица за день пуста, сырьё что-то строит — суммы разные → день не трогаем (не пишем историю в обход ночи)
        built = {"2026-05-04": [row("2026-05-04", "1", "other", "3.00")]}
        p = rb.plan([], built, ["2026-05-04"])
        self.assertEqual(p["days_ok"], [])
        self.assertEqual(len(p["skipped_days"]), 1)

    def test_key_normalises_sku_to_text(self):
        existing = [row("2026-05-05", 7, "other", "1.00", 1)]
        built = {"2026-05-05": [row("2026-05-05", "7", "other", "1.00")]}
        p = rb.plan(existing, built, ["2026-05-05"])
        self.assertEqual((p["same"], p["add"], p["delete"]), (1, [], []))


class ApplyGuardTests(unittest.TestCase):
    def test_apply_requires_the_approval_flag(self):
        with self.assertRaises(SystemExit):
            rb.main(["--date-from", "2026-05-01", "--date-to", "2026-05-01", "--apply"])


if __name__ == "__main__":
    unittest.main()
