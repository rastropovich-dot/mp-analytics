"""Сорок шестая §3: правило «снимок ≤ дате продажи», «нет ключа → ближайший более поздний», «после последнего снимка — последний»."""
import os
import sys
import unittest
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from loaders import unit_cost_history as uch  # noqa: E402

SNAPS = {"2026-03-30": {"f1": D("100"), "f2": D("200")},
         "2026-04-20": {"f1": D("110")},
         "2026-05-20": {"f1": D("120"), "f2": D("220"), "f3": D("300")}}


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.h = uch.CostHistory(SNAPS)

    def test_latest_snapshot_not_later_than_day(self):
        self.assertEqual(self.h.lookup("f1", "2026-04-25"), (D("110"), "2026-04-20", "exact"))
        self.assertEqual(self.h.lookup("f1", "2026-04-20"), (D("110"), "2026-04-20", "exact"))    # день снимка — сам снимок
        self.assertEqual(self.h.lookup("f1", "2026-03-30"), (D("100"), "2026-03-30", "exact"))

    def test_key_missing_in_snapshot_takes_nearest_later(self):
        self.assertEqual(self.h.lookup("f2", "2026-04-25"), (D("220"), "2026-05-20", "later"))   # в 20.04 f2 нет — 20.05
        self.assertEqual(self.h.lookup("f3", "2026-04-01"), (D("300"), "2026-05-20", "later"))   # f3 появился только в 20.05

    def test_before_first_snapshot_is_later(self):
        self.assertEqual(self.h.lookup("f1", "2026-03-01"), (D("100"), "2026-03-30", "later"))

    def test_after_last_snapshot_is_last(self):
        self.assertEqual(self.h.lookup("f1", "2026-09-15"), (D("120"), "2026-05-20", "after"))
        self.assertEqual(self.h.lookup("f2", "2026-09-15"), (D("220"), "2026-05-20", "after"))
        self.assertEqual(self.h.lookup("f1", "2026-05-20"), (D("120"), "2026-05-20", "exact"))    # день снимка — ещё не «после»

    def test_source_label_is_snapshot_date_or_after_last(self):
        self.assertEqual(uch.source_label("2026-04-20", "exact"), "2026-04-20")
        self.assertEqual(uch.source_label("2026-05-20", "after"), "после последнего снимка 2026-05-20")
        self.assertEqual(uch.source_label("2026-05-20", "later"), "2026-05-20 (ключа нет в снимке ≤ даты, взят более поздний)")
        self.assertEqual(uch.source_label(None, "none"), "нет снимка")
        self.assertEqual(self.h.cost_source("f1", "2026-04-25"), (D("110"), "2026-04-20"))
        self.assertEqual(self.h.cost_source("f1", "2026-09-15"), (D("120"), "после последнего снимка 2026-05-20"))
        self.assertEqual(self.h.cost_source("f2", "2026-04-25"), (D("220"), "2026-05-20 (ключа нет в снимке ≤ даты, взят более поздний)"))
        self.assertEqual(dict(self.h.counters), {"exact": 1, "after": 1, "later": 1})

    def test_readers_get_source_through_unit_cost_fn_and_stubs_stay_valid(self):
        fn = uch.unit_cost_fn(self.h, {"11": "F1"})
        self.assertEqual(fn.with_source("11", "2026-09-15", qty=2), (D("120"), "после последнего снимка 2026-05-20"))
        self.assertEqual(uch.with_source(fn, "11", "2026-04-25"), (D("110"), "2026-04-20"))
        self.assertEqual(uch.with_source(fn, "99", "2026-04-25"), (None, "нет артикула"))
        stub = lambda sku, day=None, qty=None: D("7")                                 # noqa: E731 — заглушка читателей в тестах
        self.assertEqual(uch.with_source(stub, "11", "2026-04-25"), (D("7"), ""))
        self.assertEqual(self.h.rubles["after"], D("240"))

    def test_key_only_in_earlier_snapshot(self):
        h = uch.CostHistory({"2026-03-30": {"old": D("50")}, "2026-05-20": {"f1": D("1")}})
        self.assertEqual(h.lookup("old", "2026-06-01"), (D("50"), "2026-03-30", "earlier"))

    def test_no_day_means_latest_snapshot(self):
        self.assertEqual(self.h.lookup("f1"), (D("120"), "2026-05-20", "latest"))
        self.assertEqual(self.h.lookup("f2"), (D("220"), "2026-05-20", "latest"))
        self.assertEqual(self.h.lookup("nope"), (None, None, "none"))
        self.assertEqual(uch.CostHistory({}).lookup("f1", "2026-05-01"), (None, None, "none"))

    def test_single_snapshot_behaves_like_before(self):
        """Пока снимок один (20.05) — все даты дают его: числа читателей не меняются до посева новых снимков."""
        h = uch.CostHistory({"2026-05-20": {"f1": D("120")}})
        for day in ("2026-04-01", "2026-05-20", "2026-09-29", None):
            self.assertEqual(h.lookup("f1", day)[0], D("120"))
        self.assertEqual(h.lookup("f1", "2026-04-01")[2], "later")

    def test_counters_and_rubles(self):
        fn = uch.unit_cost_fn(self.h, {"11": "F1", "22": "F2", "33": "ZZ"})
        self.assertEqual(fn("11", "2026-04-25", qty=2), D("110"))      # ключ без регистра: F1 → f1
        self.assertEqual(fn("22", "2026-04-25", qty=3), D("220"))
        self.assertIsNone(fn("33", "2026-04-25"))
        self.assertIsNone(fn("44"))
        self.assertEqual(dict(self.h.counters), {"exact": 1, "later": 1, "none": 1})
        self.assertEqual((self.h.rubles["exact"], self.h.rubles["later"]), (D("220"), D("660")))
        note = self.h.note()
        self.assertIn("2026-03-30, 2026-04-20, 2026-05-20", note)
        self.assertIn("более поздний: 1 строк, 660.00 ₽", note)

    def test_note_names_rows_after_last_snapshot(self):
        self.h.cost("f1", "2026-09-15", qty=1)
        self.assertIn("день после последнего снимка — взят последний: 1 строк, 120.00 ₽", self.h.note())


class LoadTests(unittest.TestCase):
    def test_load_history_pages_by_key_and_refuses_truncated_page(self):
        calls = []

        class Q:
            def __init__(self, rows): self.rows = rows
            def select(self, *a): return self
            def eq(self, *a): return self
            def in_(self, col, vals): calls.append(list(vals)); return self
            def order(self, *a, **k): return self
            def execute(self): return type("R", (), {"data": self.rows})()

        class SB:
            def __init__(self, rows): self.rows = rows
            def table(self, name): return Q(self.rows)

        rows = [{"offer_id_norm": "f1", "snapshot_date": "2026-03-30", "unit_cost": "100"}, {"offer_id_norm": "f1", "snapshot_date": "2026-05-20", "unit_cost": "120"}]
        h = uch.load_history(SB(rows), ["F1", "f2", "F1"], chunk=1)
        self.assertEqual(calls, [["f1"], ["f2"]])                                       # без регистра, без повторов, кусками
        self.assertEqual(h.dates, ["2026-03-30", "2026-05-20"])
        self.assertEqual(h.lookup("f1", "2026-04-01"), (D("100"), "2026-03-30", "exact"))
        with self.assertRaises(RuntimeError):
            uch.load_history(SB([{"offer_id_norm": "f1", "snapshot_date": "2026-05-20", "unit_cost": "1"}] * 1000), ["f1"], chunk=1)


if __name__ == "__main__":
    unittest.main()
