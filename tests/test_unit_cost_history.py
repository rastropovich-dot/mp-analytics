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


class RateRuleTests(unittest.TestCase):
    """Сорок восьмая §2: СС по курсу 1С = снимок × (1 + k × Δ); Δ = 0 → снимок; 925 — по серебру; без пробы / k / курсов — без поправки; устаревший курс — пометка."""

    RATES = uch.MetalRates([{"metal": "gold585", "set_date": "2026-05-20", "rate": "6000.00", "seen_to": "2026-05-22"},
                            {"metal": "gold585", "set_date": "2026-09-01", "rate": "6600.00", "seen_to": "2026-09-30"},
                            {"metal": "silver925", "set_date": "2026-05-20", "rate": "160.00", "seen_to": "2026-05-22"},
                            {"metal": "silver925", "set_date": "2026-09-01", "rate": "200.00", "seen_to": "2026-09-30"},
                            {"metal": "usd", "set_date": "2026-05-20", "rate": "74.00", "seen_to": "2026-09-30"}])
    K = {"585": {"*": D("0.95"), "Цепь": D("0.99")}, "375": {"*": D("0.93")}, "925": {"*": D("0.45")}}
    META = {"2026-05-20": {"f1": ("585", "Кольцо"), "f2": ("925", "Серьги"), "f3": ("585", "Цепь"), "f4": (None, None), "f5": ("750", "Кольцо"), "f6": ("375", "Серьги")},
            "2026-04-20": {"f1": ("585", "Кольцо")}}
    SNAPS = {"2026-04-20": {"f1": D("100")}, "2026-05-20": {"f1": D("1000"), "f2": D("1000"), "f3": D("1000"), "f4": D("1000"), "f5": D("1000"), "f6": D("1000")}}

    def hist(self, **kw):
        return uch.CostHistory(self.SNAPS, meta=self.META, rates=self.RATES, k_table=self.K, **kw)

    def test_rate_unchanged_since_snapshot_gives_snapshot_cost_and_plain_source(self):
        h = self.hist()
        self.assertEqual(h.cost_source("f1", "2026-06-10", 1), (D("1000"), "после последнего снимка 2026-05-20"))
        self.assertEqual(h.rate_counters["rate_same"], 1)

    def test_gold_plus_ten_percent_with_k_095_gives_plus_nine_and_half(self):
        h = self.hist()
        cost, src = h.cost_source("f1", "2026-09-10", 2)
        self.assertEqual(cost, D("1095.00"))                                         # 1000 × (1 + 0,95 × 0,10)
        self.assertEqual(src, "после последнего снимка 2026-05-20; по курсу 1С 2026-09-01 от снимка 2026-05-20, k=0,95")
        self.assertEqual((h.rate_counters["by_rate"], h.rate_rubles, h.base_rubles), (1, D("190.00"), D("2000")))
        self.assertEqual(h.rubles["after"], D("2190.00"))

    def test_kind_k_beats_fineness_default(self):
        self.assertEqual(self.hist().cost("f3", "2026-09-10"), D("1099.00"))          # цепь: k 0,99

    def test_silver_925_follows_the_silver_rate(self):
        self.assertEqual(self.hist().cost("f2", "2026-09-10"), D("1112.50"))          # 1000 × (1 + 0,45 × 0,25)

    def test_375_follows_gold(self):
        self.assertEqual(self.hist().cost("f6", "2026-09-10"), D("1093.00"))          # 1000 × (1 + 0,93 × 0,10)

    def test_without_fineness_or_unknown_fineness_no_adjustment(self):
        h = self.hist()
        self.assertEqual(h.cost_source("f4", "2026-09-10"), (D("1000"), "после последнего снимка 2026-05-20"))
        self.assertEqual(h.cost_source("f5", "2026-09-10"), (D("1000"), "после последнего снимка 2026-05-20"))
        self.assertEqual((h.rate_counters["no_meta"], h.rate_counters["unknown_fineness"]), (1, 1))

    def test_delta_is_measured_from_the_snapshot_that_was_chosen(self):
        h = self.hist()
        # 20.04 снимок: курса на 20.04 нет (первая установка 20.05) → без поправки, причина no_rate
        self.assertEqual(h.cost_source("f1", "2026-04-25"), (D("100"), "2026-04-20"))
        self.assertEqual(h.rate_counters["no_rate"], 1)

    def test_no_rates_or_empty_k_table_means_snapshot_cost(self):
        h1 = uch.CostHistory(self.SNAPS, meta=self.META, rates=None, k_table=self.K)
        h2 = uch.CostHistory(self.SNAPS, meta=self.META, rates=self.RATES, k_table={})      # rbh: «не задано»
        self.assertEqual(h1.cost("f1", "2026-09-10"), D("1000")); self.assertEqual(h2.cost("f1", "2026-09-10"), D("1000"))
        self.assertEqual((h1.rate_counters["no_rates"], h2.rate_counters["no_k_table"]), (1, 1))
        self.assertIn("без поправки", h1.rate_note()); self.assertIn("k кабинета не заданы", h2.rate_note())

    def test_day_none_latest_snapshot_without_adjustment(self):
        self.assertEqual(self.hist().cost_source("f1"), (D("1000"), "2026-05-20 (дата не задана)"))

    def test_stale_rate_is_named_in_source_and_note(self):
        h = self.hist()
        cost, src = h.cost_source("f1", "2026-10-08", 1)                             # последняя картинка 09-30 → 8 дней
        self.assertEqual(cost, D("1095.00"))
        self.assertTrue(src.endswith("k=0,95 (курс на 2026-09-30, устарел на 8 дней)"), src)
        self.assertEqual(h.rate_stale_days("2026-10-08"), 8); self.assertEqual(h.rate_stale_days("2026-10-06"), 0)
        self.assertIn("ВНИМАНИЕ: курс устарел — последняя картинка 2026-09-30, до 8 дней", h.note())

    def test_lookup_stays_unadjusted_and_base_is_exposed(self):
        h = self.hist()
        self.assertEqual(h.lookup("f1", "2026-09-10"), (D("1000"), "2026-05-20", "after"))
        adjusted, base, d, kind, info = h.lookup_by_rate("f1", "2026-09-10")
        self.assertEqual((adjusted, base, d, kind, info["reason"], info["delta"]), (D("1095.00"), D("1000"), "2026-05-20", "after", "by_rate", D("0.1")))
        fn = uch.unit_cost_fn(h, {"11": "F1"})
        self.assertEqual((fn("11", "2026-09-10"), fn.base("11", "2026-09-10")), (D("1095.00"), D("1000")))
        self.assertEqual(uch.with_base(fn, "11", "2026-09-10", 1), (D("1095.00"), D("1000"), "после последнего снимка 2026-05-20; по курсу 1С 2026-09-01 от снимка 2026-05-20, k=0,95"))
        stub = lambda sku, day=None, qty=None: D(7)  # noqa: E731
        self.assertEqual(uch.with_base(stub, "11", "2026-09-10"), (D(7), D(7), ""))

    def test_rate_adjust_is_usable_with_a_foreign_key(self):
        cost, info = self.hist().rate_adjust(D("500"), "2026-05-20", "2026-09-10", "925", "Серьги")
        self.assertEqual((cost, info["applied"], info["metal"], info["rate_day"], info["rate_snap"]), (D("556.25"), True, "silver925", D("200.00"), D("160.00")))

    def test_rates_rate_on_takes_last_setting_not_later_than_day(self):
        r = self.RATES
        self.assertEqual(r.rate_on("gold585", "2026-08-31"), (D("6000.00"), "2026-05-20"))
        self.assertEqual(r.rate_on("gold585", "2026-09-01"), (D("6600.00"), "2026-09-01"))
        self.assertEqual(r.rate_on("gold585", "2026-05-19"), (None, None))
        self.assertEqual(r.last_picture, "2026-09-30")
