"""Пятьдесят третья: бонусные (автосозданные) кампании Ozon — признак isAutocreated, строки расходов без них, флаг is_bonus в атрибуции,
строки алерта из атрибуции, план пересчёта окна. Сети нет: заглушки вместо Supabase."""
import os
import sys
import unittest
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import loaders.ozon_performance_ads_loader as L  # noqa: E402
from loaders import ozon_ad_bonus as B  # noqa: E402
import ozon_ad_bonus_rewrite as R  # noqa: E402

BONUS = {"id": "45128869", "title": "Кешбэк за продвижение товаров \"Только Селект\" (18.09 - 30.09)", "isAutocreated": True, "budget": "63433000000",
         "expenseStrategy": "ASAP", "PaymentType": "CPC", "state": "CAMPAIGN_STATE_RUNNING", "createdAt": "2026-10-09T14:56:41Z"}
REGULAR = {"id": "45313197", "title": "ОБЩАЯ - Серебро по списку клиента 12.10 - 28 SKU /\\ ПИР", "isAutocreated": False, "budget": "0",
           "expenseStrategy": "DAILY_BUDGET", "PaymentType": "CPC", "state": "CAMPAIGN_STATE_RUNNING", "createdAt": "2026-10-12T06:36:16Z"}
CAMPS = {"45128869": BONUS, "45313197": REGULAR}


def report(rows):
    return {"rows": rows}


def stat_row(cid, sku, spend, date="2026-10-13", clicks=3):
    return {"id": cid, "sku": sku, "date": date, "moneySpent": spend, "clicks": clicks, "views": 10, "orders": 0, "ordersMoney": 0}


class SignTests(unittest.TestCase):
    def test_is_bonus_campaign_by_isAutocreated_only(self):
        self.assertTrue(L.is_bonus_campaign(BONUS)); self.assertFalse(L.is_bonus_campaign(REGULAR))
        self.assertTrue(L.is_bonus_campaign({"isAutocreated": "true"})); self.assertFalse(L.is_bonus_campaign({})); self.assertFalse(L.is_bonus_campaign(None))
        self.assertFalse(L.is_bonus_campaign({"title": "Бонусы на продвижение", "isAutocreated": False}), "по названию не судим")

    def test_bonus_campaigns_summary_lists_autocreated_newest_first(self):
        out = L.bonus_campaigns_summary([REGULAR, BONUS, dict(BONUS, id="45369620", createdAt="2026-10-12T10:17:48Z", title="Бонусы на продвижение товаров-новинок FBS")])
        self.assertEqual([c["id"] for c in out], ["45369620", "45128869"]); self.assertEqual(out[1]["budget"], "63433000000")


class LoaderRowsTests(unittest.TestCase):
    def test_build_rows_skips_bonus_campaign_spend_and_counts_it(self):
        data = report([stat_row("45128869", "3119094700", 396.73), stat_row("45313197", "3119094700", 100.00), stat_row("45313197", "111", 50.00)])
        rows, counters = L.build_rows(data, CAMPS, "2026-10-13", source_expense_type="advertising_clicks")
        by = {(r["marketplace_sku"], r["expense_type"]): r["expense_amount"] for r in rows}
        self.assertEqual(by, {("3119094700", "advertising_clicks"): 100.0, ("111", "advertising_clicks"): 50.0})
        self.assertEqual((counters["bonus_rows_skipped"], counters["bonus_spend_skipped"]), (1, 396.73))

    def test_attribution_rows_carry_is_bonus_flag_and_aggregate_keeps_it(self):
        data = report([stat_row("45128869", "3119094700", 396.73), stat_row("45313197", "3119094700", 100.00)])
        rows, counters = L.build_cpc_attribution_rows(data, "2026-10-13", campaigns_by_id=CAMPS)
        flags = {r["campaign_id"]: r["is_bonus"] for r in rows}
        self.assertEqual(flags, {"45128869": True, "45313197": False}); self.assertEqual(counters["bonus_rows"], 1)
        agg = L.aggregate_ad_attribution_rows(rows)
        self.assertEqual({r["campaign_id"]: r["is_bonus"] for r in agg}, flags)
        rows2, _ = L.build_cpc_attribution_rows(data, "2026-10-13")
        self.assertEqual({r["is_bonus"] for r in rows2}, {False}, "без метаданных — не бонус")


class _Q:
    """Заглушка postgrest-запроса: фильтры запоминаются, execute отдаёт заранее заданные строки (одна страница)."""

    def __init__(self, rows, log):
        self.rows, self.log = rows, log

    def __getattr__(self, name):
        def f(*a, **k):
            self.log.append((name,) + a)
            return self
        return f

    def execute(self):
        class Res:
            data = self.rows
        return Res()


class _SB:
    def __init__(self, rows):
        self.rows, self.log = rows, []

    def table(self, name):
        self.log.append(("table", name))
        return _Q(self.rows, self.log)


ATTR = [{"sale_date": "2026-10-13", "marketplace_sku": "3119094700", "campaign_id": "45128869", "ad_source": "cpc", "ad_spend": "5055.51", "ad_clicks": 40, "ad_orders_qty": 1, "ad_orders_revenue": 1000},
        {"sale_date": "2026-10-13", "marketplace_sku": "5955353820", "campaign_id": "45369620", "ad_source": "cpc", "ad_spend": "793.16", "ad_clicks": 5, "ad_orders_qty": 0, "ad_orders_revenue": 0},
        {"sale_date": "2026-10-12", "marketplace_sku": "3119094700", "campaign_id": "45128869", "ad_source": "cpc", "ad_spend": "100.00", "ad_clicks": 1, "ad_orders_qty": 0, "ad_orders_revenue": 0}]


class ReaderTests(unittest.TestCase):
    def test_spend_by_day_and_new_campaigns(self):
        self.assertEqual(B.spend_by_day(ATTR), {"2026-10-13": D("5848.67"), "2026-10-12": D("100.00")})
        self.assertEqual(B.campaigns_by_day(ATTR)["2026-10-13"], {"45128869": D("5055.51"), "45369620": D("793.16")})
        self.assertEqual(B.new_campaigns(ATTR, "2026-10-13"), ["45369620"]); self.assertEqual(B.new_campaigns(ATTR, "2026-10-12"), ["45128869"])

    def test_read_bonus_rows_filters_is_bonus_and_alert_lines(self):
        sb = _SB(ATTR)
        rows = B.read_bonus_rows(sb, "2026-09-13", "2026-10-13")
        self.assertEqual(len(rows), 3)
        self.assertIn(("eq", "is_bonus", True), sb.log); self.assertIn(("order", "sale_date"), sb.log)
        fmt = lambda v: f"{float(v):,.2f}".replace(",", " ")  # noqa: E731
        lines = B.alert_lines(_SB(ATTR), "2026-10-13", fmt)
        self.assertEqual(lines[0], "Реклама за счёт бонусов Ozon (не расход): 5 848.67 руб. — кампаний 2")
        self.assertTrue(lines[1].startswith("⚠️ Новая бонусная (автосозданная) кампания Ozon: 45369620 (793.16 руб.)"))

    def test_alert_lines_survive_a_read_failure(self):
        class Broken:
            def table(self, name):
                raise RuntimeError("column is_bonus does not exist")
        lines = B.alert_lines(Broken(), "2026-10-13", str)
        self.assertEqual(len(lines), 1); self.assertIn("не прочитано", lines[0]); self.assertIn("is_bonus", lines[0])


class RewritePlanTests(unittest.TestCase):
    def test_plan_deletes_pure_bonus_rows_reduces_mixed_and_flags_attribution(self):
        attr = [{"sale_date": "2026-10-13", "marketplace_sku": "A", "campaign_id": "45128869", "ad_spend": "396.73", "is_bonus": False},
                {"sale_date": "2026-10-13", "marketplace_sku": "B", "campaign_id": "45369620", "ad_spend": "50.00", "is_bonus": False},
                {"sale_date": "2026-10-13", "marketplace_sku": "C", "campaign_id": "45369620", "ad_spend": "0", "is_bonus": True}]
        exp = [{"id": 1, "expense_date": "2026-10-13", "marketplace_sku": "A", "expense_type": "advertising_clicks", "expense_amount": 396.73},
               {"id": 2, "expense_date": "2026-10-13", "marketplace_sku": "B", "expense_type": "advertising_clicks", "expense_amount": 150.00},
               {"id": 3, "expense_date": "2026-10-13", "marketplace_sku": "D", "expense_type": "advertising_clicks", "expense_amount": 10.00}]
        to_flag, update, delete, errors, by_day = R.plan(attr, exp)
        self.assertEqual([r["marketplace_sku"] for r in to_flag], ["A", "B"])
        self.assertEqual(update, [(2, ("2026-10-13", "B"), D("150.00"), D("100.00"))]); self.assertEqual(delete, [(1, ("2026-10-13", "A"), D("396.73"))])
        self.assertEqual(errors, []); self.assertEqual(by_day["2026-10-13"], {"bonus": D("446.73"), "update": 1, "delete": 1, "flag": 2})

    def test_plan_stops_when_bonus_exceeds_the_expense_row_or_row_missing(self):
        attr = [{"sale_date": "2026-10-13", "marketplace_sku": "A", "campaign_id": "45128869", "ad_spend": "500", "is_bonus": False},
                {"sale_date": "2026-10-13", "marketplace_sku": "X", "campaign_id": "45128869", "ad_spend": "5", "is_bonus": False}]
        exp = [{"id": 1, "expense_date": "2026-10-13", "marketplace_sku": "A", "expense_type": "advertising_clicks", "expense_amount": 396.73}]
        _f, _u, _d, errors, _b = R.plan(attr, exp)
        self.assertEqual(len(errors), 2); self.assertIn("больше строки", errors[0]); self.assertIn("строки advertising_clicks нет", errors[1])

    def test_plan_after_apply_is_clean_flagged_rows_without_expense_row_are_not_errors(self):
        attr = [{"sale_date": "2026-10-13", "marketplace_sku": "A", "campaign_id": "45128869", "ad_spend": "396.73", "is_bonus": True},
                {"sale_date": "2026-10-13", "marketplace_sku": "B", "campaign_id": "45369620", "ad_spend": "50.00", "is_bonus": True}]
        exp = [{"id": 2, "expense_date": "2026-10-13", "marketplace_sku": "B", "expense_type": "advertising_clicks", "expense_amount": 100.00}]
        to_flag, update, delete, errors, by_day = R.plan(attr, exp)
        self.assertEqual((to_flag, update, delete, errors), ([], [], [], []), "после записи: строка A удалена, строка B уже уменьшена — всё чисто")
        self.assertEqual(by_day["2026-10-13"]["bonus"], D("446.73"))

    def test_bonus_campaign_ids_from_the_raw_list(self):
        import json, tempfile
        p = tempfile.mktemp(suffix=".json")
        json.dump([REGULAR, BONUS, dict(BONUS, id="45369620")], open(p, "w"))
        self.assertEqual(R.bonus_campaign_ids(p), (["45128869", "45369620"], 3))


if __name__ == "__main__":
    unittest.main()
