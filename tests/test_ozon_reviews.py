"""«Отзывы» Ozon по SKU до витрины (слово владельца 2026-10-26): статья reviews (типы 95 + 96), колонка reviews_spend, правило типа 96
без SKU — по участникам «Ускоренного сбора отзывов» (REVIEWS_PROMO) пропорционально заказам дня, иначе по доле 95, иначе без SKU (РБХ);
у KARATOV — без SKU. Сети в тестах нет."""
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal as D
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from loaders import ozon_finance_accrual as accrual, ozon_reviews as rv  # noqa: E402
import reports_daily_sku_kpi as kpi  # noqa: E402


class Classification(unittest.TestCase):
    def test_types_95_96_are_the_reviews_article_and_not_advertising(self):
        self.assertEqual((accrual.TYPE_TO_EXPENSE[95], accrual.TYPE_TO_EXPENSE[96]), ("reviews", "reviews"))
        self.assertFalse(accrual.REVIEWS_ARTICLE.startswith("advertising"))       # правило §2: пять мест берут рекламу по префиксу
        self.assertEqual(kpi.EXPENSE_TYPE_BUCKETS["reviews"], "reviews_spend")
        self.assertNotIn("reviews", [t for t, b in kpi.EXPENSE_TYPE_BUCKETS.items() if b == "ad_spend"])


class Split(unittest.TestCase):
    def test_split_to_kopeck_with_the_rest_on_the_biggest(self):
        parts = rv.split(D("100.00"), {"1": 1, "2": 1, "3": 1})
        self.assertEqual(sum(parts.values()), D("100.00"))
        self.assertEqual(sorted(parts.values()), [D("33.33"), D("33.33"), D("33.34")])
        self.assertEqual(rv.split(D("10"), {"1": 0, "2": D("-5")}), {})                  # нет положительных весов — нечего делить

    def test_participants_on_takes_the_last_snapshot_not_after_the_day_or_the_first(self):
        snaps = {"2026-10-21": {"1"}, "2026-10-27": {"2"}}
        self.assertEqual(rv.participants_on("2026-09-01", snaps), {"1"})                 # раньше первого — первый
        self.assertEqual(rv.participants_on("2026-10-26", snaps), {"1"})
        self.assertEqual(rv.participants_on("2026-10-27", snaps), {"2"})
        self.assertEqual(rv.participants_on("2026-10-27", {}), set())

    def test_allocate_participants_then_95_share_then_no_sku(self):
        parts, rule = rv.allocate_96(D("305"), {"1", "2"}, {"1": D("300"), "2": D("100"), "3": D("1000")}, {"3": D("50")})
        self.assertEqual((rule, parts), ("participants", {"1": D("228.75"), "2": D("76.25")}))   # 3 не участник — ему ничего
        parts, rule = rv.allocate_96(D("305"), {"1"}, {"1": D("0")}, {"3": D("10"), "4": D("30")})
        self.assertEqual((rule, parts), ("by_95_share", {"3": D("76.25"), "4": D("228.75")}))
        self.assertEqual(rv.allocate_96(D("305"), set(), {}, {}), ({}, "no_sku"))

    def test_promo_rows_from_info_list(self):
        items = [{"sku": 11, "offer_id": "A", "sources": [{"sku": 12}], "promotions": [{"type": "REVIEWS_PROMO", "is_enabled": True}]},
                 {"sku": 13, "offer_id": "B", "promotions": [{"type": "REVIEWS_PROMO", "is_enabled": False}]},
                 {"sku": 14, "offer_id": "C"}]
        rows = rv.promo_rows(items, "2026-10-26", "t")
        self.assertEqual([(r["sku"], r["is_enabled"]) for r in rows], [(11, True), (12, True), (13, False), (14, False)])
        self.assertEqual(rows[0]["observed_date"], "2026-10-26")


def kpi_row(day, sku, orders=0.0, reviews=0.0):
    r = kpi.empty_kpi_row(day, "ozon", sku)
    r["orders_amount_seller"], r["reviews_spend"] = orders, reviews
    return r


class Showcase(unittest.TestCase):
    def grouped(self):
        return {("2026-10-20", "ozon", ""): kpi_row("2026-10-20", "", reviews=305.0),
                ("2026-10-20", "ozon", "1"): kpi_row("2026-10-20", "1", orders=300.0, reviews=12.2),
                ("2026-10-20", "ozon", "3"): kpi_row("2026-10-20", "3", orders=900.0),
                ("2026-10-21", "ozon", ""): kpi_row("2026-10-21", "", reviews=10.0),
                ("2026-10-21", "ozon", "3"): kpi_row("2026-10-21", "3", reviews=5.0)}

    def total(self, g):
        return round(sum(r["reviews_spend"] for r in g.values()), 2)

    def test_participants_rule_moves_96_to_sku_and_keeps_the_day_sum(self):
        g = self.grouped()
        before = self.total(g)
        tally = kpi.allocate_reviews_96(g, participants={"2026-10-01": {"1", "2"}}, rule="participants")
        self.assertEqual(self.total(g), before)
        self.assertEqual(g[("2026-10-20", "ozon", "")]["reviews_spend"], 0.0)
        self.assertAlmostEqual(g[("2026-10-20", "ozon", "1")]["reviews_spend"], 12.2 + 305.0)     # 2 — участник без заказов, 3 — не участник
        self.assertEqual(g[("2026-10-20", "ozon", "3")]["reviews_spend"], 0.0)
        self.assertAlmostEqual(g[("2026-10-21", "ozon", "3")]["reviews_spend"], 15.0)              # 21.10 у участников заказов нет → по доле 95
        self.assertEqual({k: v[0] for k, v in tally.items()}, {"participants": 1, "by_95_share": 1})

    def test_no_sku_rule_leaves_96_without_sku(self):
        g = self.grouped()
        self.assertEqual(kpi.allocate_reviews_96(g, participants={"2026-10-01": {"1"}}, rule="no_sku"), {})
        self.assertEqual(g[("2026-10-20", "ozon", "")]["reviews_spend"], 305.0)

    def test_unknown_rule_stops(self):
        with self.assertRaises(SystemExit):
            kpi.allocate_reviews_96(self.grouped(), participants={}, rule="by_revenue")

    def test_profiles(self):
        import cabinet
        self.assertEqual(cabinet.profile("karatov").REVIEWS_96_RULE, "no_sku")
        self.assertEqual((cabinet.profile("rbh1").REVIEWS_96_RULE, cabinet.profile("rbh2").REVIEWS_96_RULE), ("participants", "participants"))


class PromoStep(unittest.TestCase):
    def test_karatov_skips_without_requests(self):
        import ozon_reviews_promo_step as step
        cat = mock.Mock()
        with mock.patch.object(step.CABINET, "REVIEWS_96_RULE", "no_sku"):
            self.assertEqual(step.run(mock.Mock(), catalog=cat), 0)
        cat.fetch_cards.assert_not_called()

    def test_night_snapshot_and_seed_write_rows(self):
        import ozon_reviews_promo_step as step
        sb, cat = mock.MagicMock(), mock.Mock(TABLE="ozon_products", BATCH=1000)
        cat.table_skus.return_value = {"12", "11"}
        cat.fetch_cards.return_value = [{"sku": 11, "promotions": [{"type": "REVIEWS_PROMO", "is_enabled": True}]}, {"sku": 12}]
        from datetime import datetime, timezone
        with mock.patch.object(step.CABINET, "REVIEWS_96_RULE", "participants"):
            self.assertEqual(step.run(sb, catalog=cat, now=datetime(2026, 10, 27, 1, tzinfo=timezone.utc)), 0)
        cat.fetch_cards.assert_called_once()
        self.assertEqual(cat.fetch_cards.call_args[0][0], ["11", "12"])
        rows = sb.table.return_value.upsert.call_args[0][0]
        self.assertEqual([(r["observed_date"], r["sku"], r["is_enabled"]) for r in rows], [("2026-10-27", 11, True), ("2026-10-27", 12, False)])
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "catalog.json")
            json.dump({"observed_at": "2026-10-21T22:38:17+00:00", "items": [{"sku": 5, "promotions": [{"type": "REVIEWS_PROMO", "is_enabled": True}]}]}, open(p, "w"))
            sb2 = mock.MagicMock()
            self.assertEqual(step.seed(sb2, p, apply=False), 0)
            sb2.table.assert_not_called()                                                     # план — db_writes 0
            self.assertEqual(step.seed(sb2, p, apply=True), 0)
            self.assertEqual(sb2.table.return_value.upsert.call_args[0][0][0]["observed_date"], "2026-10-21")


if __name__ == "__main__":
    unittest.main()
