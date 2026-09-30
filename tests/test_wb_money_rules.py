"""WB-14: правила денег WB (loaders/wb_money_rules) и их применение в «WB - месяц» (report_wb_month) и в строках модуля
«Фин рез» (report_finrez_wb): удержания по виду, аванс «Баллы за отзывы» нетто в день возврата, реклама только «Баланс»,
упаковка исключена, мост к отчёту реализации — остаток 0,00."""
import io
import unittest
from contextlib import redirect_stdout
from decimal import Decimal

import scripts.report_finrez_wb as fr
from loaders import wb_money_rules as rules

wbm = fr.wbm
D = Decimal
PROMO = "Оказание услуг «WB Продвижение», документ №304137708"
ADVANCE = 'Аванс за услугу "Баллы за отзывы"'
REFUND = 'Возврат неиспользованного остатка аванса за услугу "Баллы за отзывы"'
JAM = "Предоставление услуг по подписке «Джем», документ №304074652"
VITRINA = "Витрина Магазина, документ №311953134"
REVIEW = "Списание за отзыв pYSerzoDhMulsR5Cj77W: акция №1622923, товар 561740756"


class ClassifyTests(unittest.TestCase):
    def test_kinds_by_prefix_whatever_the_document_number(self):
        self.assertEqual(rules.classify_deduction(PROMO), rules.ADS_WITHHELD)
        self.assertEqual(rules.classify_deduction("Оказание услуг «WB Продвижение», документ №1"), rules.ADS_WITHHELD)
        self.assertEqual(rules.classify_deduction(ADVANCE), rules.REVIEW_ADVANCE)
        self.assertEqual(rules.classify_deduction(REFUND), rules.REVIEW_REFUND)
        for name in (JAM, VITRINA, REVIEW, "Новое удержание", None, ""):
            self.assertEqual(rules.classify_deduction(name), rules.OTHER)

    def test_known_other_and_labels(self):
        self.assertTrue(rules.is_known_other(JAM) and rules.is_known_other(VITRINA) and rules.is_known_other(REVIEW))
        self.assertFalse(rules.is_known_other("Новое удержание"))
        self.assertEqual(rules.deduction_label(PROMO), "Оказание услуг «WB Продвижение»")
        self.assertEqual(rules.deduction_label(REVIEW), "Списание за отзыв")

    def test_other_amount_takes_penalty_other_deductions_minus_additional_payment(self):
        self.assertEqual(rules.other_amount({"penalty": "30", "deduction": "12.2", "additional_payment": "2", "bonus_type_name": JAM}), D("40.2"))
        self.assertEqual(rules.other_amount({"penalty": None, "deduction": "519190", "bonus_type_name": PROMO}), D("0"))
        self.assertEqual(rules.other_amount({"deduction": "6470880", "bonus_type_name": ADVANCE}), D("0"))
        self.assertEqual(rules.other_amount({"penalty": "3000", "deduction": None}), D("3000"))

    def test_tally_names_unknown_kinds_with_sums(self):
        t = rules.DeductionTally()
        for name, v in ((PROMO, "100"), (JAM, "42990"), ("Новое удержание, документ №7", "5"), ("Новое удержание, документ №8", "6")):
            t.add({"deduction": v, "bonus_type_name": name})
        self.assertEqual(t.by_kind[rules.ADS_WITHHELD], D("100"))
        self.assertEqual(dict(t.unknown), {"Новое удержание": D("11")})
        self.assertIn("незнакомые виды «прочего»: «Новое удержание» 11.00", t.text())

    def test_advance_net_is_the_real_june_july_pair(self):
        rows = [{"rrd_id": 3, "rr_date": "2026-06-30", "bonus_type_name": ADVANCE, "deduction": "2298480"},
                {"rrd_id": 4, "rr_date": "2026-06-30", "bonus_type_name": ADVANCE, "deduction": "4172400"},
                {"rrd_id": 6, "rr_date": "2026-07-22", "bonus_type_name": REFUND, "deduction": "-2258220"},
                {"rrd_id": 7, "rr_date": "2026-07-22", "bonus_type_name": REFUND, "deduction": "-4172400"}]
        net, open_ = rules.review_advance_net(rows, lambda r: r["rr_date"])
        self.assertEqual((net, open_), ({"2026-07-22": D("40260")}, D("0")))
        net, open_ = rules.review_advance_net(rows[:2], lambda r: r["rr_date"])
        self.assertEqual((net, open_), ({}, D("6470880")))                     # аванс без возврата — не расход до возврата

    def test_packaging_and_payment(self):
        self.assertTrue(rules.is_packaging("Упаковки для украшений"))
        self.assertFalse(rules.is_packaging("Ювелирные кольца"))
        self.assertTrue(rules.is_balance_payment("Баланс"))
        self.assertFalse(rules.is_balance_payment("Кэшбэк"))


def rrow(day, oper="Продажа", nm=1, subject="Ювелирные серьги", **extra):
    base = {"rrd_id": extra.pop("rrd_id", 1), "rr_date": day, "sale_dt": f"{day}T10:00:00Z", "seller_oper_name": oper, "doc_type": "",
            "vendor_code": "f000283615", "tech_size": "0", "nm_id": nm, "quantity": 1, "retail_price_with_disc": None, "retail_amount": None,
            "for_pay": None, "commission_percent": "40", "ppvz_reward": None, "rebill_logistic_cost": None, "delivery_service": None,
            "acquiring_fee": None, "paid_storage": None, "penalty": None, "deduction": None, "cashback_discount": None, "additional_payment": None,
            "bonus_type_name": None, "subject_name": subject, "brand_name": "KARATOV"}
    base.update(extra)
    return base


RAW = [
    rrow("2026-07-22", retail_price_with_disc="1000", retail_amount="600", acquiring_fee="30", rrd_id=1),
    rrow("2026-07-22", nm=334613161, subject="Упаковки для украшений", retail_price_with_disc="246", retail_amount="150", acquiring_fee="6", rrd_id=2),
    rrow("2026-07-22", oper="Логистика", nm=334613161, subject="Упаковки для украшений", delivery_service="12.2", rrd_id=3),
    rrow("2026-07-22", oper="Удержание", nm=0, subject=None, deduction="-2258220", bonus_type_name=REFUND, rrd_id=4),
    rrow("2026-07-22", oper="Удержание", nm=0, subject=None, deduction="-4172400", bonus_type_name=REFUND, rrd_id=5),
    rrow("2026-07-22", oper="Удержание", nm=0, subject=None, deduction="472678", bonus_type_name=PROMO, rrd_id=6),
    rrow("2026-07-22", oper="Удержание", nm=0, subject=None, deduction="42990", bonus_type_name=JAM, rrd_id=7),
    rrow("2026-07-22", oper="Штраф", penalty="300", rrd_id=8),
    rrow("2026-07-22", oper="Хранение", nm=0, subject=None, paid_storage="122", rrd_id=9),
]
ADVANCE_ROWS = [{"rrd_id": 10, "rr_date": "2026-06-30", "sale_dt": "2026-06-30T10:00:00Z", "bonus_type_name": ADVANCE, "deduction": "2298480"},
                {"rrd_id": 11, "rr_date": "2026-06-30", "sale_dt": "2026-06-30T10:00:00Z", "bonus_type_name": ADVANCE, "deduction": "4172400"},
                {"rrd_id": 4, "rr_date": "2026-07-22", "sale_dt": "2026-07-22T10:00:00Z", "bonus_type_name": REFUND, "deduction": "-2258220"},
                {"rrd_id": 5, "rr_date": "2026-07-22", "sale_dt": "2026-07-22T10:00:00Z", "bonus_type_name": REFUND, "deduction": "-4172400"}]
COSTS = ({"f000283615": D("400")}, {})
SPLIT = wbm.ads_split_of([
    {"upd_day": "2026-07-22", "advert_id": 1, "upd_sum": "1220", "payment_type": "Баланс"},
    {"upd_day": "2026-07-22", "advert_id": 2, "upd_sum": "244", "payment_type": "Кэшбэк"},
    {"upd_day": "2026-07-22", "advert_id": 3, "upd_sum": "100", "payment_type": "Баланс"},
    {"upd_day": "2026-07-22", "advert_id": 3, "upd_sum": "100", "payment_type": "Кэшбэк"},
])


class MonthSheetRulesTests(unittest.TestCase):
    def test_ads_split_by_payment_type(self):
        self.assertEqual(SPLIT["balance"], {"2026-07-22": D("1320")})
        self.assertEqual(SPLIT["other"], {"2026-07-22": D("344")})
        self.assertEqual(SPLIT["advert_day"][("2026-07-22", 3)], (D("100"), D("200")))
        self.assertEqual(SPLIT["by_type"], {"Баланс": D("1320"), "Кэшбэк": D("344")})

    def test_build_daily_other_without_ads_and_advance_but_with_net_and_without_packaging(self):
        excluded = {}
        net, _open = wbm.advance_net_by_day(rows=ADVANCE_ROWS)
        daily = wbm.build_daily(RAW, ["2026-07-22"], lambda c, s: (D("400"), "plain"), SPLIT["balance"], wbm.date(2026, 9, 29), True, None,
                                advance_net=net, excluded=excluded)
        d = daily[0]
        vat = D("1.22")
        self.assertEqual(d["turnover"], D("1000"))                                   # коробка 246 не в обороте
        self.assertEqual(excluded["packaging"]["turnover"], D("246"))
        self.assertEqual(excluded["packaging"]["delivery"], D("12.2"))
        self.assertEqual(d["deduction"], D("42990") + D("40260"))                    # Джем + нетто аванса
        self.assertEqual(d["deduction_ads"], D("472678"))
        self.assertEqual(d["deduction_advance"], D("-6430620"))
        self.assertEqual(d["other"], D("122") / vat + D("300") + (D("42990") + D("40260")) / vat)
        self.assertEqual(d["ads"], D("1320") / vat)                                   # только «Баланс»


class ModuleRowsRulesTests(unittest.TestCase):
    def build(self, stats):
        nm_rows = [{"day": "2026-07-22", "advert_id": 1, "nm_id": 1, "sum": "600"}, {"day": "2026-07-22", "advert_id": 2, "nm_id": 5, "sum": "200"},
                   {"day": "2026-07-22", "advert_id": 3, "nm_id": 5, "sum": "400"}]
        balance_w = fr.nm_weights(nm_rows, SPLIT["advert_day"], "balance")
        cashback_w = fr.nm_weights(nm_rows, SPLIT["advert_day"], "other")
        with redirect_stdout(io.StringIO()):
            return fr.build_rows("2026-07", "2026-07", None, sb=object(), rows=RAW, costs=COSTS, ads_by_day=SPLIT["balance"], products={},
                                 ads_nm_by_day=balance_w, stats=stats, ads_cashback_by_day=SPLIT["other"], cashback_nm_by_day=cashback_w,
                                 advance_rows=ADVANCE_ROWS), balance_w, cashback_w

    def test_weights_follow_the_campaign_payment_share(self):
        _rows, balance_w, cashback_w = self.build({})
        self.assertEqual(balance_w, {"2026-07-22": {1: D("600"), 5: D("200")}})       # кампания 2 — кэшбэк целиком, 3 — наполовину
        self.assertEqual(cashback_w, {"2026-07-22": {5: D("400")}})

    def test_rows_exclude_packaging_and_ads_deductions_and_carry_the_advance_net(self):
        stats = {}
        rows, _b, _c = self.build(stats)
        by = {(r["date"], r["nm_id"]): r for r in rows}
        self.assertNotIn(("2026-07-22", 334613161), by)
        self.assertEqual(stats["excluded"]["packaging"]["sales"], D("246"))
        self.assertEqual(stats["excluded"]["packaging"]["nm_ids"], [334613161])
        no_product = by[("2026-07-22", None)]
        self.assertEqual(no_product["other"], D("42990") + D("40260"))                # Джем + нетто аванса; «WB Продвижение» и аванс — нет
        self.assertEqual(by[("2026-07-22", 1)]["other"], D("300"))                   # штраф
        self.assertEqual(sum((r["ads"] for r in rows), D(0)), D("1320"))             # только «Баланс»
        self.assertEqual(sum((r["ads_cashback"] for r in rows), D(0)), D("344"))     # кэшбэк — справочно
        self.assertEqual(stats["excluded"]["deductions"][rules.ADS_WITHHELD], D("472678"))
        t = fr.build_month_sheet(rows)[-1]
        self.assertEqual(t["t_ads"], D("1320") / D("1.22"))                           # в форму — без кэшбэка

    def test_bridge_to_report_rests_are_zero(self):
        stats = {}
        rows, _b, _c = self.build(stats)
        table = fr.bridge_to_report(rows, RAW, "2026-07-01", "2026-07-31", stats["excluded"], SPLIT)
        self.assertEqual([t["rest"] for t in table], [D("0.00")] * len(table))
        other = [t for t in table if t["title"].startswith("Ост.")][0]
        self.assertEqual(dict(other["terms"])["удержания за рекламу «WB Продвижение» (исключены)"], D("-472678.00"))
        self.assertEqual(dict(other["terms"])["нетто аванса в день возврата"], D("40260.00"))

    def test_orders_rows_skip_packaging(self):
        funnel = [{"day": "2026-07-22", "nm_id": 1, "vendor_code": "f000283615", "subject_name": "Ювелирные серьги", "order_count": 1, "order_sum": 1000},
                  {"day": "2026-07-22", "nm_id": 334613161, "vendor_code": "коробка karatov 75*75*65", "subject_name": "Упаковки для украшений", "order_count": 2, "order_sum": 492}]
        stats = {}
        out = fr.orders_rows_for_finrez("2026-07-22", "2026-07-22", sb=object(), funnel_rows=funnel, costs=COSTS, ads_by_day={}, ads_nm_by_day={}, stats=stats)
        self.assertEqual([r["nm_id"] for r in out], [1])
        self.assertEqual(stats["packaging_excluded"]["orders_sum"], D("492"))


if __name__ == "__main__":
    unittest.main()
