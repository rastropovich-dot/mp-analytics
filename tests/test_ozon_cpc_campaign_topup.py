"""Клики по кампаниям: арбитр «тип 41 × атрибуция» и добор прибавлением (RBH 27.10, B / C). Сети нет."""
import os
import sys
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from loaders import ozon_click_campaign_arbiter as arb  # noqa: E402
import ozon_cpc_campaign_topup as topup  # noqa: E402


def acc41(day, campaign, amount):
    return {"date": day, "unit_number": campaign, "accrued_category": "NON_ITEM",
            "non_item_fee": {"type_id": 41, "accrued": {"amount": str(-amount), "currency": "RUB"}}}


def stat(day, sku, spend, clicks=10):
    return {"date": day, "sku": sku, "moneySpent": spend, "clicks": clicks, "views": 100, "orders": 0, "ordersMoney": 0}


class Arbiter(unittest.TestCase):
    def test_spend_from_accruals_takes_type_41_by_unit_number(self):
        rows = [acc41("2026-10-08", "42573900", 1164.65), acc41("2026-10-08", "30580019", 1676.17), acc41("2026-10-08", "30580019", 0.01),
                {"date": "2026-10-08", "unit_number": "29745999", "non_item_fee": {"type_id": 54, "accrued": {"amount": "-46287.12"}}}]
        got = arb.campaign_spend_from_accruals(rows)
        self.assertEqual(got, {("2026-10-08", "42573900"): Decimal("1164.65"), ("2026-10-08", "30580019"): Decimal("1676.18")})

    def test_compare_missing_extra_mismatch(self):
        acc = {("d", "1"): Decimal("10"), ("d", "2"): Decimal("5"), ("d", "3"): Decimal("7")}
        att = {("d", "1"): Decimal("10.00"), ("d", "3"): Decimal("9"), ("d", "4"): Decimal("1")}
        r = arb.compare(acc, att)
        self.assertEqual(r["missing"], {("d", "2"): Decimal("5")})
        self.assertEqual(r["extra"], {("d", "4"): Decimal("1")})
        self.assertEqual(r["mismatch"], {("d", "3"): (Decimal("7"), Decimal("9"))})
        self.assertEqual(r["equal"], 1)

    def test_zero_row_in_attribution_is_not_missing(self):
        r = arb.compare({("d", "1"): Decimal("10")}, {("d", "1"): Decimal("0")})
        self.assertEqual(r["missing"], {})
        self.assertEqual(r["mismatch"], {("d", "1"): (Decimal("10"), Decimal("0"))})

    def test_summary_line(self):
        self.assertIsNone(arb.summary_line({"missing": {}}))
        line = arb.summary_line({"missing": {("2026-10-08", "36692482"): Decimal("1899.70"), ("2026-10-09", "36692482"): Decimal("1000"),
                                             ("2026-10-08", "30580019"): Decimal("1676.17")}})
        self.assertIn("кампании без атрибуции — 2 (3 пар", line)
        self.assertIn("Σ 4 575,87 ₽", line)
        self.assertTrue(line.index("36692482") < line.index("30580019"))


class Batches(unittest.TestCase):
    def test_ten_per_batch_window_from_pairs(self):
        missing = {(f"2026-10-{d:02d}", str(100 + c)): Decimal(1) for c in range(12) for d in (1, 2)}
        b = topup.plan_batches(missing)
        self.assertEqual([len(x["campaigns"]) for x in b], [10, 2])
        self.assertEqual((b[0]["date_from"], b[0]["date_to"]), ("2026-10-01", "2026-10-02"))

    def test_span_over_62_days_splits_or_stops(self):
        missing = {("2026-08-01", "1"): Decimal(1), ("2026-10-15", "2"): Decimal(1)}
        b = topup.plan_batches(missing)
        self.assertEqual([x["campaigns"] for x in b], [["1"], ["2"]])
        with self.assertRaises(SystemExit):
            topup.plan_batches({("2026-08-01", "1"): Decimal(1), ("2026-10-15", "1"): Decimal(1)})


class RowsAndTargets(unittest.TestCase):
    def test_rows_only_for_missing_pairs(self):
        report = {"36692482": {"report": {"rows": [stat("2026-10-08", "227239319", 1899.70), stat("2026-10-23", "227239319", 5.0)]}},
                  "30580019": {"report": {"rows": [stat("2026-10-08", "863885383", 1676.17)]}}}
        batch = {"campaigns": ["30580019", "36692482"], "date_from": "2026-10-08", "date_to": "2026-10-23"}
        missing = {("2026-10-08", "36692482"): Decimal("1899.70"), ("2026-10-08", "30580019"): Decimal("1676.17")}
        exp, attr = topup.rows_for_missing(report, {}, batch, missing)
        self.assertEqual(sorted((r["expense_date"], r["marketplace_sku"], r["expense_amount"]) for r in exp),
                         [("2026-10-08", "227239319", 1899.70), ("2026-10-08", "863885383", 1676.17)])
        self.assertEqual(topup.built_pair_spend(attr), {("2026-10-08", "36692482"): Decimal("1899.7"), ("2026-10-08", "30580019"): Decimal("1676.17")})

    def test_target_is_before_plus_add_not_replacement(self):
        exp = [{"expense_date": "2026-10-08", "marketplace_sku": "863885383", "expense_amount": 1676.17, "article": "7930575"}]
        current = {("2026-10-08", "863885383"): (Decimal("1164.65"), "7930575")}
        t = topup.expense_targets(exp, current)[("2026-10-08", "863885383")]
        self.assertEqual((t["before"], t["add"], t["target"]), (Decimal("1164.65"), Decimal("1676.17"), Decimal("2840.82")))
        new = topup.expense_targets([{"expense_date": "2026-10-22", "marketplace_sku": "5", "expense_amount": 3.333, "article": ""}], {})
        self.assertEqual(new[("2026-10-22", "5")]["target"], Decimal("3.33"))

    def test_apply_action(self):
        item = {"before": Decimal("1164.65"), "target": Decimal("2840.82")}
        self.assertEqual(topup.apply_action(item, Decimal("1164.65")), "write")
        self.assertEqual(topup.apply_action(item, Decimal("2840.82")), "done")
        self.assertEqual(topup.apply_action(item, Decimal("1200")), "stop")

    def test_write_needs_word(self):
        with self.assertRaises(SystemExit):
            topup.main(["--date-from", "2026-10-22", "--date-to", "2026-10-22", "--write"])


class FakeSb:
    """Таблица атрибуции по дням (select … eq … execute) и upsert итога."""
    def __init__(self, attribution):
        self.attribution, self.upserts, self.filters = attribution, [], {}

    def table(self, name):
        self.name, self.filters = name, {}; return self

    def select(self, *_a, **_k):
        return self

    def eq(self, field, value):
        self.filters[field] = value; return self

    def limit(self, _n):
        return self

    def upsert(self, row, on_conflict=None):
        self.upserts.append((self.name, row, on_conflict)); return self

    def execute(self):
        day = self.filters.get("sale_date")
        self.data = [r for r in self.attribution if r["sale_date"] == day] if day else []
        return self


class NightlyArbiter(unittest.TestCase):
    def setUp(self):
        from loaders import ozon_expenses_loader as exp
        self.exp = exp
        self.window = {"day_from": "2026-10-20", "day_to": "2026-10-23"}

    def test_last_two_days_are_not_judged(self):
        self.assertEqual(self.exp.click_arbiter_days(self.window), ["2026-10-20", "2026-10-21"])
        self.assertEqual(self.exp.click_arbiter_days({"day_from": "2026-10-23", "day_to": "2026-10-23"}), [])

    def test_missing_campaign_named_and_state_written_only_on_apply(self):
        accruals = [acc41("2026-10-20", "42573900", 1164.65), acc41("2026-10-20", "30580019", 1676.17), acc41("2026-10-22", "999", 5)]
        attr = [{"sale_date": "2026-10-20", "campaign_id": "42573900", "ad_spend": 1164.65}]
        sb = FakeSb(attr)
        p = self.exp.run_click_arbiter(accruals, self.window, apply=False, sb=sb)
        self.assertEqual((p["equal"], p["missing_pairs"], p["missing_campaigns"]), (1, 1, ["30580019"]))
        self.assertIn("30580019 1 676,17", p["line"])
        self.assertEqual(sb.upserts, [])
        self.exp.run_click_arbiter(accruals, self.window, apply=True, sb=sb)
        self.assertEqual(sb.upserts[0][1]["state_key"], "ozon_click_campaign_arbiter:last")

    def test_alert_line(self):
        import alerts_telegram as at
        from datetime import datetime, timezone
        now = datetime(2026, 10, 28, 7, 30, tzinfo=timezone.utc)
        self.assertEqual(at.click_arbiter_line(None, now), "")
        self.assertEqual(at.click_arbiter_line({"finished_at": "2026-10-28T01:10:00+00:00", "line": None}, now), "")
        self.assertTrue(at.click_arbiter_line({"finished_at": "2026-10-28T01:10:00+00:00", "line": "клики: …"}, now).startswith("⚠️ клики"))
        self.assertIn("записи за эту ночь нет", at.click_arbiter_line({"finished_at": "2026-10-26T01:10:00+00:00", "line": None}, now))


class ScopeByAccruals(unittest.TestCase):
    def test_files_to_ids_and_absent_days(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            json.dump({"date": "2026-10-20", "accruals": [acc41("2026-10-20", "30580019", 10), acc41("2026-10-20", "1", 0)]},
                      open(os.path.join(d, "2026-10-20.json"), "w"))
            ids, absent = arb.campaigns_from_accrual_files(d, "2026-10-20", "2026-10-21")
        self.assertEqual((ids, absent), ({"30580019"}, ["2026-10-21"]))

    def test_selection_takes_stopped_campaign_updated_after_window(self):
        from loaders import ozon_performance_ads_loader as ads
        camps = [{"id": "30580019", "state": "CAMPAIGN_STATE_INACTIVE", "PaymentType": "CPC", "updatedAt": "2026-10-27T06:28:23Z", "fromDate": "2026-06-28"},
                 {"id": "42573900", "state": "CAMPAIGN_STATE_RUNNING", "PaymentType": "CPC"},
                 {"id": "777", "state": "CAMPAIGN_STATE_INACTIVE", "PaymentType": "CPM"}]
        recent = [c["id"] for c in ads.filter_cpc_campaigns(camps, "2026-08-23", "2026-10-21", "recent")]
        self.assertNotIn("30580019", recent)                       # так его потерял O11
        selected, not_listed = ads.select_cpc_campaigns_by_accruals(camps, {"30580019", "777", "555"})
        self.assertEqual([c["id"] for c in selected], ["30580019"])
        self.assertEqual(selected[0]["_cpc_activity_markers"], ["accrual_type41"])
        self.assertEqual(not_listed, ["555"])


class RecoveryIncompleteList(unittest.TestCase):
    def test_foreign_campaign_on_sku_is_named(self):
        from loaders import ozon_performance_ads_loader as ads

        class Sb(FakeSb):
            def in_(self, field, values):
                self.filters[field] = set(values); return self

            def execute(self):
                self.data = [r for r in self.attribution if r["sale_date"] == self.filters.get("sale_date")
                             and r["marketplace_sku"] in self.filters.get("marketplace_sku", set())]
                return self

        attr = [{"sale_date": "2026-10-08", "marketplace_sku": "863885383", "campaign_id": "42573900", "ad_spend": 1164.65},
                {"sale_date": "2026-10-08", "marketplace_sku": "863885383", "campaign_id": "30580019", "ad_spend": 0},
                {"sale_date": "2026-10-08", "marketplace_sku": "1", "campaign_id": "9", "ad_spend": 5}]
        rows = [{"expense_date": "2026-10-08", "marketplace_sku": "863885383", "expense_amount": 1676.17}]
        self.assertEqual(ads.foreign_campaigns_on_skus(Sb(attr), "2026-10-08", rows, ["30580019"]), {"863885383": ["42573900"]})
        self.assertEqual(ads.foreign_campaigns_on_skus(Sb(attr), "2026-10-08", rows, ["30580019", "42573900"]), {})


if __name__ == "__main__":
    unittest.main()
