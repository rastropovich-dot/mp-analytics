"""Сегменты по металлу и лист «Начисления - свод» в генераторе месяца (тридцать пятая задача, §6).

Металл — по названию товара: «серебр» / «925» → Серебро, «золот» / проба 585 / 375 / 750 → Золото, иначе — без признака.
Сегменты складываются в общий лист там же, где площадки. Свод — type_id → название ЛК, группа Ozon, Вид владельца, наша
статья, Σ за месяц со знаком Ozon (леджер хранит расход со знаком «+»).
"""
import importlib.util
import os
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("report_ozon_month", os.path.join(ROOT, "scripts", "report_ozon_month.py"))
rep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rep)
D = Decimal
DAY = "2026-08-01"


def buyout(sku, amount, commission="0", qty=1):
    return {"buyout_date": DAY, "marketplace_sku": sku, "buyouts_qty": qty, "buyouts_amount_seller": amount, "commission_amount": commission, "buyouts_units": None}


class MetalTests(unittest.TestCase):
    def test_metal_is_read_from_the_product_name(self):
        self.assertEqual(rep.metal_of("Серьги серебряные 925 пробы с фианитами"), "Серебро")
        self.assertEqual(rep.metal_of("Подвеска на шею женская 585 пробы с ониксом, KARATOV"), "Золото")   # «золот» нет, проба есть
        self.assertEqual(rep.metal_of("Кольцо золотое с бриллиантом"), "Золото")
        self.assertEqual(rep.metal_of("Цепочка 925"), "Серебро")
        self.assertEqual(rep.metal_of(""), rep.NO_METAL)
        self.assertEqual(rep.metal_of(None), rep.NO_METAL)

    def test_rows_land_on_their_metal_and_add_up_to_the_general_sheet(self):
        buyouts = [buyout("1", "1220", "220"), buyout("2", "2440", "440"), buyout("3", "610", "110")]
        expenses = [{"expense_date": DAY, "marketplace_sku": "1", "expense_type": "logistics", "expense_amount": "12.2"},
                    {"expense_date": DAY, "marketplace_sku": None, "expense_type": "other", "expense_amount": "6.1"}]
        kpi = [{"kpi_date": DAY, "marketplace_sku": "2", "ad_spend": "24.4"}]
        names = {"1": "Серьги серебряные 925", "2": "Кольцо золотое 585"}          # у SKU 3 названия нет
        metals = rep.build_metal_daily([DAY], buyouts, expenses, kpi, names, lambda sku: D("100"))
        self.assertEqual(set(metals), {"Серебро", "Золото", rep.NO_METAL})
        silver, gold, rest = metals["Серебро"][0], metals["Золото"][0], metals[rep.NO_METAL][0]
        self.assertEqual((silver["turnover"], silver["commission"], silver["logistics"]), (D("1220"), D("220"), D("12.2") / D("1.22")))
        self.assertEqual((gold["turnover"], gold["ads_perf"]), (D("2440"), D("24.4") / D("1.22")))
        self.assertEqual((rest["turnover"], rest["other_with_acq"]), (D("610"), D("6.1") / D("1.22")))   # без названия и без SKU — не теряются
        total = sum((m[0]["turnover"] for m in metals.values()), D(0))
        self.assertEqual(total, D("4270"))

    def test_platforms_still_work_without_classify(self):
        buyouts = [buyout("1", "100"), buyout("2", "200")]
        platforms = rep.build_platform_daily([DAY], buyouts, [], [], {"1": "F1", "2": "S2"}, lambda sku: None)
        self.assertEqual((platforms["Основная"][0]["turnover"], platforms["Селект"][0]["turnover"]), (D("100"), D("200")))


class SegmentCheckTests(unittest.TestCase):
    def test_check_counts_equal_days_and_lists_the_rest(self):
        prows = [{"date": DAY, "turnover": D("100"), "commission": D("40"), "logistics": D("5"), "ads_perf": D("3"), "other_with_acq": D("2"), "cogs": D("50")},
                 {"date": "2026-08-02", "turnover": D("120"), "commission": D("48"), "logistics": D("6"), "ads_perf": D("3"), "other_with_acq": D("2"), "cogs": D("60")}]
        manual = {DAY: {"turnover": D("100"), "commission": D("40"), "logistics": D("4"), "ads": D("1"), "acquiring": D("1"), "other": D("1"), "cogs": D("57.5")},
                  "2026-08-02": {"turnover": D("121"), "commission": D("48"), "logistics": D("6"), "ads": D("3"), "acquiring": D("2"), "other": D("0"), "cogs": D("69")}}
        table = {r["title"]: r for r in rep.check_segment(prows, manual)}
        self.assertEqual((table["Оборот (B)"]["equal_days"], table["Оборот (B)"]["bad"]), (1, [("2026-08-02", D("-1.00"))]))
        self.assertEqual(table["Комиссия (C)"]["equal_days"], 2)
        self.assertEqual(table["Эквайринг + Прочее (M + O) — у нас одной суммой"]["equal_days"], 2)   # 1 + 1 = 2 и 2 + 0 = 2
        self.assertEqual(table["Логистика (I)"]["bad"], [(DAY, D("1.00"))])

    def test_missing_manual_days_are_counted_not_zeroed(self):
        prows = [{"date": DAY, "turnover": D("1"), "commission": D("0"), "logistics": D("0"), "ads_perf": D("0"), "other_with_acq": D("0"), "cogs": D("0")}]
        table = rep.check_segment(prows, {})
        self.assertEqual((table[0]["days"], table[0]["missing"]), (0, [DAY]))


class TypesSummaryTests(unittest.TestCase):
    def test_summary_flips_the_ledger_sign_back_and_names_the_unknown(self):
        types_by_day = {DAY: {1: D("100"), 32: D("50"), 999: D("7")}, "2026-08-02": {1: D("20")}}
        rows = {r["type_id"]: r for r in rep.build_types_summary(types_by_day, [DAY, "2026-08-02"])}
        self.assertEqual((rows[1]["amount"], rows[1]["days"]), (D("-120"), 2))       # леджер: расход «+»; свод: знак Ozon
        self.assertEqual(rows[1]["name"], "Эквайринг")
        self.assertEqual((rows[1]["group"], rows[1]["kind"], rows[1]["article"]), ("Услуги агентов", "Эквайринг", "other"))
        self.assertEqual((rows[32]["group"], rows[32]["kind"]), ("Услуги доставки", "Логистика"))
        self.assertEqual((rows[999]["name"], rows[999]["group"], rows[999]["kind"], rows[999]["article"]),
                         ("type_id 999", "(нет в справочнике)", "(нет в справочнике)", "unknown_999"))


if __name__ == "__main__":
    unittest.main()
