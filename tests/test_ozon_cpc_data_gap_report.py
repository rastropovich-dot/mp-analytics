import unittest

import scripts.ozon_cpc_data_gap_report as gap


def fact(day, spend, rows=None, cabinet=1_000_000.0, orders=400.0):
    return {"date": day, "cpc_spend": spend,
            "cpc_rows": rows if rows is not None else (0 if spend == 0 else 100),
            "cpc_campaigns": 0, "cabinet_spend": cabinet,
            "orders_qty": orders, "orders_amount": 0.0}


def series(spends, start=1):
    return [fact(f"2026-06-{start + i:02d}", s) for i, s in enumerate(spends)]


class MedianTest(unittest.TestCase):
    def test_odd_and_even(self):
        self.assertEqual(gap.median([3, 1, 2]), 2)
        self.assertEqual(gap.median([1, 2, 3, 4]), 2.5)
        self.assertIsNone(gap.median([]))

    def test_neighbour_median_excludes_the_date_itself(self):
        facts = series([100.0, 0.0, 100.0])
        # Если бы включали саму дату, медиана поехала бы вниз.
        self.assertEqual(gap.neighbour_median(facts, 1, "cpc_spend", set()), 100.0)

    def test_neighbour_median_excludes_listed_dates(self):
        facts = series([100.0, 0.0, 0.0, 100.0])
        base = gap.neighbour_median(facts, 1, "cpc_spend", {"2026-06-03"})
        self.assertEqual(base, 100.0)


class ClassificationTest(unittest.TestCase):
    def test_levels_follow_the_thresholds(self):
        facts = series([100.0] * 3 + [0.0] + [100.0] * 3)
        gap.classify(facts)
        self.assertEqual(facts[3]["level"], gap.LEVEL_ABSENT)

        for spend, expected in ((10.0, gap.LEVEL_COLLAPSED),      # 10%
                                (34.0, gap.LEVEL_COLLAPSED),      # 34% < 35
                                (36.0, gap.LEVEL_SUSPICIOUS),     # 36% >= 35
                                (69.0, gap.LEVEL_SUSPICIOUS),     # 69% < 70
                                (71.0, gap.LEVEL_NORMAL)):        # 71% >= 70
            with self.subTest(spend=spend):
                probe = series([100.0] * 3 + [spend] + [100.0] * 3)
                gap.classify(probe)
                self.assertEqual(probe[3]["level"], expected)

    def test_zero_rows_is_absent_even_with_spend_key(self):
        facts = series([100.0] * 3 + [0.0] + [100.0] * 3)
        gap.classify(facts)
        self.assertEqual(facts[3]["ratio"], 0.0)


class IterativeMedianTest(unittest.TestCase):
    def test_a_run_of_collapses_cannot_hide_itself(self):
        # Три подряд нуля: без исключения кандидатов каждый был бы «нормальным
        # соседом» для другого и медиана уехала бы вниз.
        facts = series([100.0] * 3 + [0.0, 0.0, 0.0] + [100.0] * 3)
        gap.classify(facts)
        for index in (3, 4, 5):
            self.assertEqual(facts[index]["level"], gap.LEVEL_ABSENT)
            self.assertEqual(facts[index]["neighbour_median"], 100.0,
                             "медиана должна считаться без соседних провалов")

    def test_partial_collapse_run_is_still_detected(self):
        facts = series([100.0] * 3 + [5.0, 5.0, 5.0] + [100.0] * 3)
        gap.classify(facts)
        for index in (3, 4, 5):
            self.assertEqual(facts[index]["level"], gap.LEVEL_COLLAPSED)

    def test_normal_series_yields_no_candidates(self):
        facts = series([100.0, 105.0, 95.0, 100.0, 98.0, 102.0, 99.0])
        gap.classify(facts)
        self.assertEqual({f["level"] for f in facts}, {gap.LEVEL_NORMAL})


class CabinetSignalTest(unittest.TestCase):
    def test_quiet_cabinet_is_flagged(self):
        facts = series([100.0] * 7)
        facts[3].update({"cpc_spend": 0.0, "cpc_rows": 0,
                         "cabinet_spend": 1000.0, "orders_qty": 1.0})
        gap.classify(facts)
        gap.add_cabinet_signal(facts)
        self.assertTrue(facts[3]["quiet_cabinet"])

    def test_busy_cabinet_is_not_flagged(self):
        # CPC упал, а кабинет работал — это потеря данных, не тихий день.
        facts = series([100.0] * 7)
        facts[3].update({"cpc_spend": 0.0, "cpc_rows": 0})
        gap.classify(facts)
        gap.add_cabinet_signal(facts)
        self.assertFalse(facts[3]["quiet_cabinet"])


class RealShapesTest(unittest.TestCase):
    """Известные точки: 1,5% (2026-05-12) и 11,5% (2026-07-13) — обе collapsed."""

    def test_known_collapse_ratios_land_in_collapsed(self):
        for spend in (1.5, 11.5):
            with self.subTest(pct=spend):
                facts = series([100.0] * 3 + [spend] + [100.0] * 3)
                gap.classify(facts)
                self.assertEqual(facts[3]["level"], gap.LEVEL_COLLAPSED)

    def test_cpc_expense_types_include_the_advertising_other_workaround(self):
        self.assertIn("advertising_other", gap.CPC_EXPENSE_TYPES)
        self.assertIn("advertising_clicks", gap.CPC_EXPENSE_TYPES)


if __name__ == "__main__":
    unittest.main()
