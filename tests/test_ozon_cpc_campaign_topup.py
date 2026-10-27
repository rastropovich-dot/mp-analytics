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


if __name__ == "__main__":
    unittest.main()
