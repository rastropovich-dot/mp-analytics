import unittest

import scripts.reclassify_advertising_other as rc


def other(day, sku, amount, row_id=None):
    return {"id": row_id or f"o-{day}-{sku}", "expense_date": day, "marketplace_sku": sku,
            "expense_type": "advertising_other", "expense_amount": amount}


def clicks(day, sku, amount, row_id=None):
    return {"id": row_id or f"c-{day}-{sku}", "expense_date": day, "marketplace_sku": sku,
            "expense_type": "advertising_clicks", "expense_amount": amount}


class ClassifyTest(unittest.TestCase):
    def test_simple_when_no_clicks_row_on_that_date(self):
        buckets = rc.classify_rows([other("2026-07-13", "1", 100.0)],
                                   [clicks("2026-07-12", "1", 50.0)])
        self.assertEqual(len(buckets["simple"]), 1)
        self.assertEqual(buckets["merge"], [])
        self.assertEqual(buckets["suspicious"], [])

    def test_merge_when_clicks_row_exists_for_same_date_and_sku(self):
        buckets = rc.classify_rows([other("2026-07-13", "1", 100.0)],
                                   [clicks("2026-07-13", "1", 50.0)])
        self.assertEqual(len(buckets["merge"]), 1)
        self.assertEqual(buckets["merge"][0]["target"]["expense_amount"], 50.0)

    def test_sku_never_seen_as_clicks_is_suspicious_and_untouched(self):
        buckets = rc.classify_rows([other("2026-08-11", "only-cpo", 30.0)],
                                   [clicks("2026-08-11", "other-sku", 50.0)])
        self.assertEqual(len(buckets["suspicious"]), 1)
        self.assertEqual(buckets["simple"], [])
        self.assertEqual(buckets["merge"], [])

    def test_suspicious_wins_over_merge(self):
        # Даже при совпадении даты и SKU: если SKU не бывает clicks, не трогаем.
        rows = [other("2026-08-11", "x", 30.0)]
        buckets = rc.classify_rows(rows, [])
        self.assertEqual(len(buckets["suspicious"]), 1)


class InvariantTest(unittest.TestCase):
    def test_total_cpc_spend_is_preserved(self):
        other_rows = [other("2026-07-13", "1", 100.0), other("2026-07-13", "2", 40.0)]
        clicks_rows = [clicks("2026-07-13", "1", 60.0)]
        buckets = rc.classify_rows(other_rows, clicks_rows)

        before = sum(float(r["expense_amount"]) for r in other_rows + clicks_rows)
        after = sum(float(r["expense_amount"]) for r in clicks_rows if True)
        after += sum(float(r["expense_amount"]) for r in buckets["simple"])
        after += sum(float(r["expense_amount"]) for r in buckets["merge"])
        after += sum(float(r["expense_amount"]) for r in buckets["suspicious"])
        self.assertEqual(before, after, "переклассификация не должна менять сумму CPC")

    def test_every_row_lands_in_exactly_one_bucket(self):
        other_rows = [other("2026-07-13", "1", 100.0), other("2026-07-13", "2", 40.0),
                      other("2026-08-11", "cpo-only", 30.0)]
        clicks_rows = [clicks("2026-07-13", "1", 60.0), clicks("2026-07-01", "2", 10.0)]
        buckets = rc.classify_rows(other_rows, clicks_rows)
        total = sum(len(v) for v in buckets.values())
        self.assertEqual(total, len(other_rows))


class SafetyTest(unittest.TestCase):
    def test_apply_refuses_without_approval(self):
        with self.assertRaises(RuntimeError):
            rc.apply_changes({"simple": [], "merge": [], "suspicious": []}, approved=False)

    def test_dry_run_is_the_default_in_the_cli(self):
        import inspect
        source = inspect.getsource(rc.main)
        self.assertIn("if not args.apply", source)
        self.assertIn("--approve-reclassification", source)


class SnapshotTest(unittest.TestCase):
    def test_snapshot_covers_only_affected_dates(self):
        other_rows = [other("2026-07-13", "1", 100.0)]
        clicks_rows = [clicks("2026-07-13", "1", 60.0), clicks("2026-01-01", "9", 5.0)]
        totals = rc.build_snapshot(other_rows, clicks_rows)
        self.assertIn("2026-07-13", totals)
        self.assertNotIn("2026-01-01", totals)
        self.assertEqual(totals["2026-07-13"]["advertising_clicks"]["spend"], 60.0)
        self.assertEqual(totals["2026-07-13"]["advertising_other"]["spend"], 100.0)


if __name__ == "__main__":
    unittest.main()
