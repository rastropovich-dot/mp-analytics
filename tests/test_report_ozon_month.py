"""Генератор листа «Ozon - <месяц>»: формулы ручного листа на наших источниках.

Формулы сняты с ячеек листа владельца (data/manual_report_september.xlsx, 2026-09-19):
выручка = (оборот − комиссия) / НДС, ДРР = реклама / (оборот / НДС),
фин. рез. = маржа − логистика − реклама − эквайринг − прочее (у нас ещё − подписка).
Живая приёмка против листа — режим --check самого скрипта; здесь — арифметика и правила.
"""
import importlib.util
import os
import tempfile
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("report_ozon_month", os.path.join(ROOT, "scripts", "report_ozon_month.py"))
rep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rep)

D = Decimal
DAY = "2026-09-10"


def buyout(amount="1220", commission="0", qty=1, sku="11", day=DAY):
    return {"buyout_date": day, "marketplace_sku": sku, "buyouts_qty": qty, "buyouts_amount_seller": amount, "commission_amount": commission}


def expense(kind, amount, day=DAY):
    return {"expense_date": day, "expense_type": kind, "expense_amount": amount}


TYPES = {1: D("12.2"), 41: D("61"), 54: D("61"), 51: D("24.4"), 96: D("12.2"), 32: D("122")}
EXPENSES = [expense("logistics", "122"), expense("other", "36.6"), expense("subscription", "24.4"),
            expense("advertising_clicks", "999"), expense("commission", "777")]


def one_day(buyouts=None, expenses=None, types=TYPES, cost=lambda sku: D("300"), today="2026-09-19"):
    rows, unknown = rep.build_daily([DAY], buyouts or [buyout()], EXPENSES if expenses is None else expenses,
                                    {} if types is None else {DAY: types}, cost, today)
    return rep.add_ratios(rows[0]), unknown


class VatTests(unittest.TestCase):
    def test_rate_has_an_effective_date(self):
        self.assertEqual(rep.vat_for("2025-12-31"), D("1.20"))
        self.assertEqual(rep.vat_for("2026-01-01"), D("1.22"))
        self.assertEqual(rep.vat_for("2026-09-10"), D("1.22"))


class DailyFormulaTests(unittest.TestCase):
    def test_columns_follow_the_manual_sheet_formulas(self):
        r, unknown = one_day()
        self.assertEqual(unknown, {})
        self.assertEqual((r["turnover"], r["commission"], r["revenue"]), (D("1220"), D("0"), D("1000")))
        self.assertEqual((r["cogs"], r["margin"]), (D("300"), D("700")))
        self.assertEqual(r["logistics"], D("100"))                 # статья logistics / НДС
        self.assertEqual(r["acquiring"], D("10"))                  # тип 1 / НДС
        self.assertEqual(r["subscription"], D("20"))
        self.assertEqual(r["ads"], D("100"))                       # 41 + 54, без 51 и 96
        self.assertEqual(r["other"], D("20"))                      # (other − тип 1) / НДС
        self.assertEqual(r["fin_result"], D("700") - 100 - 10 - 20 - 100 - 20)
        self.assertEqual(r["drr_pct"], D("0.1"))                   # реклама / (оборот / НДС)
        self.assertEqual(r["margin_pct"], D("0.7"))

    def test_reference_columns_reproduce_the_manual_grouping(self):
        r, _ = one_day()
        self.assertEqual(r["ads_like_manual"], D("130"))           # (41 + 54 + 51 + 96) / НДС
        self.assertEqual(r["log_other_like_manual"], D("110"))     # (logistics + other − тип 1 − тип 96) / НДС

    def test_performance_ads_and_commission_expenses_stay_out_of_sheet_one(self):
        with_extra, _ = one_day()
        without, _ = one_day(expenses=[e for e in EXPENSES if e["expense_type"] not in ("advertising_clicks", "commission")])
        self.assertEqual(with_extra["fin_result"], without["fin_result"])

    def test_commission_comes_from_buyouts(self):
        r, _ = one_day(buyouts=[buyout("1220", "244")])
        self.assertEqual(r["revenue"], D("800"))
        self.assertEqual(r["commission_pct"], D("0.2"))

    def test_unknown_article_goes_to_other_and_is_named(self):
        r, unknown = one_day(expenses=EXPENSES + [expense("unknown_77", "12.2")])
        self.assertEqual(unknown, {"unknown_77": D("12.2")})
        self.assertEqual(r["other"], D("30"))

    def test_no_raw_means_empty_not_zero(self):
        r, _ = one_day(types=None)
        self.assertIsNone(r["acquiring"]); self.assertIsNone(r["ads"]); self.assertIsNone(r["fin_result"]); self.assertIsNone(r["drr_pct"])
        self.assertEqual(r["other"], D("30"))                      # эквайринг не отделить — остаётся внутри прочего
        self.assertIsNone(rep.total_row([r])["fin_result"])        # итог неполон, и это видно

    def test_position_without_cost_is_counted_not_priced_at_zero(self):
        r, _ = one_day(buyouts=[buyout(qty=2, sku="no-cost")], cost=lambda sku: None)
        self.assertEqual((r["cogs"], r["no_cost_positions"]), (D("0"), 2))

    def test_young_dates_are_flagged(self):
        self.assertTrue(one_day(today="2026-09-11")[0]["young"])
        self.assertFalse(one_day(today="2026-09-12")[0]["young"])

    def test_database_ahead_of_the_raw_file_is_named(self):
        """Статьи из базы, типы из файла сырья: если начисления доехали после сбора файла — сказать."""
        r, _ = one_day(expenses=[expense("logistics", "122"), expense("other", "86.6"), expense("subscription", "24.4")], types=TYPES)
        self.assertEqual(r["db_minus_raw"], {"other": D("62.20")})   # в сырье other = типы 1 + 96 = 24.4


class TotalsTests(unittest.TestCase):
    def test_total_percentages_are_computed_from_sums(self):
        days = ["2026-09-10", "2026-09-11"]
        rows, _ = rep.build_daily(days, [buyout("1220", day=days[0]), buyout("2440", day=days[1])], [],
                                  {d: {41: D("122")} for d in days}, lambda sku: D("0"), "2026-09-19")
        total = rep.total_row([rep.add_ratios(r) for r in rows])
        self.assertEqual((total["turnover"], total["ads"]), (D("3660"), D("200")))
        self.assertEqual(total["drr_pct"], D("200") / D("3000"))

    def test_month_days_respects_date_to(self):
        self.assertEqual(len(rep.month_days("2026-09")), 30)
        self.assertEqual(rep.month_days("2026-09", "2026-09-15")[-1], "2026-09-15")


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.row, _ = one_day()
        self.manual = {DAY: {"turnover": D("1220"), "commission": D("0"), "revenue": D("1000"), "acquiring": D("10"), "ads": D("130"),
                             "logistics": D("95"), "other": D("15"), "cogs": D("350")}}

    def test_exact_columns_pass_and_known_boundaries_do_not_fail_the_check(self):
        table, failures = rep.check([self.row], self.manual)
        self.assertEqual(failures, 0)
        by = {t["title"]: t for t in table}
        self.assertEqual(by["Логистика + Прочее по образцу = их Логистика + Прочее"]["diff"], D("0.00"))   # 110 против 95 + 15
        self.assertEqual(by["Себестоимость (наш снимок 1С против их цен)"]["diff"], D("-50.00"))
        self.assertEqual(by["Логистика (наша статья против их строки)"]["diff"], D("5.00"))               # границу не повторяем — не провал

    def test_a_kopeck_off_in_an_exact_column_fails(self):
        self.manual[DAY]["turnover"] = D("1220.01")
        _table, failures = rep.check([self.row], self.manual)
        self.assertEqual(failures, 1)

    def test_a_day_missing_from_the_manual_sheet_fails(self):
        _table, failures = rep.check([self.row], {})
        self.assertEqual(failures, len(rep.EXACT))


class WorkbookTests(unittest.TestCase):
    def test_workbook_has_both_sheets_total_row_and_notes(self):
        import openpyxl
        row, _ = one_day(today="2026-09-11")
        total = rep.total_row([row])
        sku_rows = rep.build_sku([{"marketplace_sku": "11", "article": "F11", "product_name": "товар", "buyouts_qty": 1, "buyouts_amount_seller": "1220",
                                   "commission_amount": "0", "ad_spend": "122", "logistics_amount": "122", "other_expenses_amount": "61"}],
                                 {"11": "F11"}, lambda sku: D("300"), D("1.22"))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "reports", "ozon_2026-09.xlsx")
            rep.write_xlsx(path, "2026-09", [row], total, sku_rows, ["источники"], ["про рекламу"])
            wb = openpyxl.load_workbook(path)
            self.assertEqual(wb.sheetnames, ["Ozon - сентябрь", "По SKU"])
            ws = wb["Ozon - сентябрь"]
            self.assertEqual(ws["A5"].value, "Итого")
            self.assertEqual(ws["B4"].value, 1220)
            self.assertIn("моложе двух суток", ws.cell(row=4, column=24).value)
            sku = wb["По SKU"]
            self.assertEqual((sku["A2"].value, sku["B2"].value, sku["K2"].value), ("11", "F11", 100))
            self.assertEqual(sku["A3"].value, "Итого")


if __name__ == "__main__":
    unittest.main()
