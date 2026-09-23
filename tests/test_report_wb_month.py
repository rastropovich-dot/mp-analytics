"""Лист «WB - месяц»: формулы владельца на строках отчёта реализации, СС по базовому артикулу, приёмка.

Держим: оборот = продажа − возврат по retailPriceWithDisc; комиссия факт = оборот − forPay;
НДС за возмещение = (ppvzReward + rebillLogisticCost) × 0,22/1,22; выручка по образцу — с 42 %;
эквайринг и прочее — с НДС / 1,22; Ebitda = фин. рез. − 74 002 с 09-01; СС — по базовому
артикулу через unit_cost_uniform, безразмерные — точной строкой; известные три дня в приёмке
печатаются и в отказ не считаются.
"""
import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal

import scripts.report_wb_month as rep

D = Decimal


def row(day, oper="Продажа", code="f000283615", size="17,5", **extra):
    base = {"rrd_id": extra.pop("rrd_id", 1), "rr_date": day, "seller_oper_name": oper, "doc_type": "Продажа" if oper == "Продажа" else "",
            "vendor_code": code, "tech_size": size, "nm_id": 1, "quantity": 1, "retail_price_with_disc": None, "retail_amount": None,
            "for_pay": None, "ppvz_reward": None, "rebill_logistic_cost": None, "delivery_service": None, "acquiring_fee": None,
            "paid_storage": None, "penalty": None, "cashback_discount": None}
    base.update(extra)
    return base


ROWS = [
    row("2026-09-01", retail_price_with_disc="1000", for_pay="556", ppvz_reward="18115.294", rebill_logistic_cost="8463.49", acquiring_fee="122", rrd_id=1),
    row("2026-09-01", oper="Возврат", retail_price_with_disc="100", for_pay="55.6", rrd_id=2),
    row("2026-09-01", oper="Доставка", delivery_service="244", rrd_id=3),
    row("2026-09-01", oper="Хранение", paid_storage="1220", rrd_id=4),
    row("2026-09-01", oper="Штраф", penalty="244", rrd_id=5),
]
EXACT = {"f000283615-17,5": D("100"), "f000283615-16": D("120"), "f000009483": D("50")}
UNIFORM = {"f000283615": D("110"), "f000009483": D("50")}


def cost_base(code, size):
    return rep.unit_cost_for(EXACT, UNIFORM, code, size, "base")


class FormulaTests(unittest.TestCase):
    def test_owner_formulas_on_one_day(self):
        [r] = rep.build_daily(ROWS, ["2026-09-01"], cost_base, {}, date(2026, 9, 10))
        self.assertEqual(r["turnover"], D("900"))                       # 1000 − 100
        self.assertEqual(r["commission"], D("900") - D("500.4"))        # оборот − forPay± (556 − 55.6)
        self.assertEqual(r["commission_manual"], D("378.00"))           # 0,42 × 900
        self.assertEqual(rep.q(r["vat_refund"]), rep.q((D("18115.294") + D("8463.49")) * 22 / 122))  # как колонка AB владельца
        self.assertEqual(rep.q(r["revenue_manual"]), rep.q((D("900") - D("378")) / D("1.22") - r["vat_refund"]))
        self.assertEqual(rep.q(r["revenue"]), rep.q((D("900") - r["commission"]) / D("1.22") - r["vat_refund"]))
        self.assertEqual(r["logistics"], D("200"))                      # 244 / 1,22
        self.assertEqual(r["acquiring"], D("100"))
        self.assertEqual(r["other"], D("1200"))                         # (1220 + 244) / 1,22
        self.assertEqual(r["cogs"], D("110") - D("110"))                # продажа + возврат одного артикула по единой СС
        self.assertEqual(r["overhead"], D("74002.00"))
        self.assertEqual(r["ebitda"], r["fin_result"] - D("74002.00"))
        self.assertEqual(r["positions"], 0)
        self.assertFalse(r["young"])

    def test_young_days_are_the_last_two(self):
        rows = rep.build_daily([], ["2026-09-08", "2026-09-09", "2026-09-10"], cost_base, {}, date(2026, 9, 10))
        self.assertEqual([r["young"] for r in rows], [False, True, True])

    def test_overhead_is_empty_before_its_start(self):
        [r] = rep.build_daily([], ["2026-08-31"], cost_base, {}, date(2026, 9, 10))
        self.assertIsNone(r["overhead"]); self.assertIsNone(r["ebitda"])

    def test_ads_unknown_stays_empty_not_zero(self):
        [r] = rep.build_daily([], ["2026-09-01"], cost_base, {}, date(2026, 9, 10), ads_known=False)
        self.assertIsNone(r["ads"]); self.assertIsNone(r["drr_pct"])


class CostTests(unittest.TestCase):
    def test_base_mode_uses_uniform_cost_and_plain_rows(self):
        self.assertEqual(rep.unit_cost_for(EXACT, UNIFORM, "F000283615", "17,5", "base"), (D("110"), "uniform"))
        self.assertEqual(rep.unit_cost_for(EXACT, UNIFORM, "f000009483", "0", "base"), (D("50"), "plain"))
        self.assertEqual(rep.unit_cost_for(EXACT, UNIFORM, "t000000001", "17", "base"), (None, "none"))

    def test_variant_mode_prefers_the_exact_size(self):
        self.assertEqual(rep.unit_cost_for(EXACT, UNIFORM, "f000283615", "17,5", "variant"), (D("100"), "variant"))
        self.assertEqual(rep.unit_cost_for(EXACT, UNIFORM, "f000283615", "19", "variant"), (D("110"), "uniform"))

    def test_missing_cost_is_counted_not_zeroed(self):
        rows = [row("2026-09-01", code="t000000001", retail_price_with_disc="10", for_pay="5")]
        [r] = rep.build_daily(rows, ["2026-09-01"], cost_base, {}, date(2026, 9, 10))
        self.assertEqual((r["no_cost_positions"], r["cogs"]), (1, D(0)))


class CheckTests(unittest.TestCase):
    def test_known_days_are_printed_but_not_failures(self):
        daily = rep.build_daily(ROWS, ["2026-09-01"], cost_base, {}, date(2026, 9, 10))
        manual = {"2026-09-01": {"turnover": "900", "revenue": str(rep.q(daily[0]["revenue_manual"])), "acquiring": "100", "other": "1200",
                                 "commission": "378", "logistics": "150", "cogs": "0", "fin_result": "0"}}
        table, failures = rep.check(daily, manual)
        self.assertEqual(failures, 0)
        by = {t["title"]: t for t in table}
        self.assertEqual(by["Оборот (с НДС)"]["equal_days"], 1)
        self.assertEqual(by["Логистика (deliveryService / НДС)"]["bad"], [("2026-09-01", D("50.00"))])
        rep.KNOWN_MANUAL_DIFFS.add(("acquiring", "2026-09-01"))
        try:
            manual["2026-09-01"]["acquiring"] = "90"
            table, failures = rep.check(daily, manual)
            self.assertEqual(failures, 0)
            self.assertEqual({t["title"]: t for t in table}["Эквайринг (acquiringFee / НДС)"]["known"], [("2026-09-01", D("10.00"))])
        finally:
            rep.KNOWN_MANUAL_DIFFS.discard(("acquiring", "2026-09-01"))

    def test_a_mandatory_mismatch_fails(self):
        daily = rep.build_daily(ROWS, ["2026-09-01"], cost_base, {}, date(2026, 9, 10))
        manual = {"2026-09-01": {"turnover": "901", "revenue": "1", "acquiring": "100", "other": "1200", "commission": "378"}}
        _table, failures = rep.check(daily, manual)
        self.assertGreaterEqual(failures, 1)

    def test_young_days_are_excluded_from_the_check(self):
        daily = rep.build_daily(ROWS, ["2026-09-01"], cost_base, {}, date(2026, 9, 1))
        _table, failures = rep.check(daily, {}, young_days={"2026-09-01"})
        self.assertEqual(failures, 0)


class BookTests(unittest.TestCase):
    def test_workbook_is_written_with_the_owner_columns(self):
        import openpyxl
        daily = rep.build_daily(ROWS, ["2026-09-01"], cost_base, {}, date(2026, 9, 10))
        total = rep.total_row(daily)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "wb.xlsx")
            rep.write_xlsx(path, "2026-09", daily, total, ["note"])
            ws = openpyxl.load_workbook(path)["WB - сентябрь"]
            heads = [ws.cell(row=3, column=j).value for j in range(1, 6)]
            self.assertEqual(heads[:3], ["Дата реализации", "Оборот (с НДС), руб.", "Комиссия факт (с НДС), руб."])
            self.assertEqual(ws.cell(row=5, column=1).value, "Итого")
            self.assertAlmostEqual(ws.cell(row=4, column=2).value, 900.0)


if __name__ == "__main__":
    unittest.main()
