"""Тридцать седьмая §2, §4: книга «Фин рез» в форме владельца — Ozon-часть.

Правила, которые закрепляют тесты: знак как в сводной владельца (списания < 0, товарооборот и СС > 0); строка «(без SKU)» на день
несёт остаток леджера, чтобы Σ листа по статье равнялась листу месяца; метки строк — месяцы, дни последнего месяца, «Общий итог»;
метрики сводной считаются от сумм без НДС по дате; Σ по брендам = Σ по категориям = общий; коэффициенты заказов — измеренные,
соинвест пуст там, где цена покупателя не измерена; приёмка против build_daily сходится на общих входах. Сети нет.
"""
import os
import sys
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import report_finrez as fr  # noqa: E402
import report_ozon_month as rep  # noqa: E402

D = Decimal
DAYS = ["2026-08-30", "2026-08-31", "2026-09-01", "2026-09-02"]
CATALOG = {"11": {"category": "кольца", "brand": "KARATOV", "name": "Кольцо", "offer_id": "F1"},
           "22": {"category": "серьги", "brand": "Топаз", "name": "Серьги", "offer_id": "T2"}}
UNIT_COST = {"11": D(100), "22": None}.get


def buyout(day, sku, seller, commission, units=1, bonus="10", coinv="1"):
    return {"buyout_date": day, "marketplace_sku": sku, "buyouts_qty": 1, "buyouts_amount_seller": seller, "commission_amount": commission,
            "buyouts_units": units, "bonus_amount": bonus, "coinvestment_amount": coinv}


def expense(day, sku, kind, amount):
    return {"expense_date": day, "marketplace_sku": sku, "expense_type": kind, "expense_amount": amount}


def daily(day, ads="122", acq="61", logistics="10", other="5", subscription="0", cogs="0"):
    """Строка build_daily листа месяца: суммы БЕЗ НДС (как отдаёт генератор)."""
    return {"date": day, "vat": D("1.22"), "ads": D(ads), "acquiring": D(acq), "logistics": D(logistics), "other": D(other), "subscription": D(subscription),
            "turnover": D(0), "commission": D(0), "revenue": D(0), "cogs": D(cogs)}


class Labels(unittest.TestCase):
    def test_month_and_day_labels(self):
        self.assertEqual((fr.month_label("2026-09-05"), fr.day_label("2026-09-05")), ("сен", "05.сен"))
        self.assertEqual(fr.days_between("2026-08", "2026-09", "2026-09-02")[0], "2026-08-01")
        self.assertEqual(fr.days_between("2026-08", "2026-09", "2026-09-02")[-1], "2026-09-02")

    def test_row_order_months_days_of_last_month_total(self):
        order, of = fr.labels_for(DAYS)
        self.assertEqual(order, ["авг", "30.авг", "31.авг", "сен", "01.сен", "02.сен", "Общий итог"])     # дни у каждого месяца (сороковая §3)
        self.assertEqual(of("2026-08-30"), ["авг", "30.авг", "Общий итог"])
        self.assertEqual(of("2026-09-02"), ["сен", "02.сен", "Общий итог"])
        self.assertEqual([fr.label_kind(l) for l in order], ["month", "day", "day", "month", "day", "day", "total"])


class BuyoutRows(unittest.TestCase):
    def rows(self):
        buyouts = [buyout("2026-09-01", "11", "1000", "400"), buyout("2026-09-01", "22", "500", "200", units=2)]
        expenses = [expense("2026-09-01", "11", "logistics", "8"), expense("2026-09-01", "", "other", "3"), expense("2026-09-01", "11", "commission", "400"),
                    expense("2026-09-01", "22", "advertising_clicks", "50")]
        kpi = [{"kpi_date": "2026-09-01", "marketplace_sku": "11", "ad_spend": "100"}]
        # лист месяца за день: реклама 41 + 54 = 122 × 1,22 = 148,84 с НДС; эквайринг 61 → 74,42; логистика 10 → 12,20; прочее 5 + подписка 0 → 6,10
        return fr.build_buyout_rows(["2026-09-01"], buyouts, expenses, kpi, [daily("2026-09-01")], UNIT_COST, {"11": "F1", "22": "T2"}, CATALOG, {})

    def test_signs_sources_and_residual(self):
        rows, stats = self.rows()
        by = {r["sku"]: r for r in rows}
        r11 = by["11"]
        self.assertEqual((r11["turnover"], r11["commission"], r11["logistics"], r11["ads"]), (D(1000), D(-400), D(-8), D(-100)))
        self.assertEqual((r11["cogs"], r11["coinvest"], r11["units"], r11["brand"], r11["category"]), (D(100) * fr.cogs_index("2026-09-01"), D(11), D(1), "KARATOV", "кольца"))
        self.assertEqual(by["22"]["cogs"], D(0))                      # СС нет — ноль в строке и счётчик
        self.assertEqual(stats["positions_without_cost"], 1)
        res = by[fr.NO_SKU]
        self.assertEqual(res["ads"], D("-48.84"))                     # 148,84 леджера − 100 Performance по SKU
        self.assertEqual(res["acquiring"], D("-74.42"))
        self.assertEqual(res["logistics"], D("-4.20"))                # 12,20 типов − 8 по SKU
        self.assertEqual(res["other"], D("-6.10"))                    # остаток = типы − Σ по SKU (3,00 без SKU входит в остаток, не удваивается)
        self.assertEqual(res["article"], fr.NO_SKU)
        # тождество: Σ статьи листа = лист месяца
        self.assertEqual(sum(r["ads"] for r in rows), D("-148.84"))
        self.assertEqual(sum(r["logistics"] for r in rows), D("-12.20"))
        self.assertEqual(sum(r["other"] for r in rows), D("-6.10"))

    def test_pivot_metrics_and_totals(self):
        rows, _ = self.rows()
        piv, order = fr.pivot_buyouts(rows, ["2026-09-01"])
        t = piv["Ozon"]["Общий итог"]
        self.assertEqual(order, ["сен", "01.сен", "Общий итог"])
        self.assertEqual((t["turnover"], t["commission"]), (D(1500), D(-600)))
        self.assertEqual(t["revenue_net"], (D(1500) - D(600)) / D("1.22"))
        self.assertEqual(t["ads_net"], D("148.84") / D("1.22"))
        self.assertEqual(t["commission_pct"], D(600) / D(1500))
        fin = t["revenue_net"] - t["cogs"] - t["logistics_net"] - t["ads_net"] - t["acquiring_net"] - t["other_net"]
        self.assertEqual(t["fin_result"], fin)
        by_brand, _ = fr.pivot_buyouts(rows, ["2026-09-01"], lambda r: r["brand"])
        for k in ("turnover", "commission", "logistics", "ads", "acquiring", "other", "cogs"):
            self.assertEqual(sum(by_brand[b]["Общий итог"][k] for b in by_brand), t[k])

    def test_check_against_month_is_zero_on_shared_inputs(self):
        rows, _ = self.rows()
        d = daily("2026-09-01")
        d.update({"turnover": D(1500), "commission": D(600), "revenue": (D(1500) - D(600)) / D("1.22"), "cogs": D(100), "cogs_index": D(100) * fr.cogs_index("2026-09-01")})
        table = fr.check_against_month(rows, [d], ["2026-09-01"])
        self.assertEqual([t[3] for t in table], [D(0)] * len(table), table)


def order(day, sku, conf_a, canc_a="0", conf_q=1, canc_q=0, buyer=None, canc_buyer="0", article="F1"):
    return {"order_date": day, "marketplace_sku": sku, "article": article, "orders_qty": conf_q, "cancelled_orders_qty": canc_q,
            "orders_amount_seller": conf_a, "cancelled_orders_amount_seller": canc_a, "orders_amount_buyer": buyer, "cancelled_orders_amount_buyer": canc_buyer}


class Orders(unittest.TestCase):
    def data(self):
        orders = [order("2026-09-01", "11", "1000", "500", canc_q=1, buyer="600", canc_buyer="300"), order("2026-09-01", "22", "400", buyer=None, article="T2"),
                  order("2026-08-01", "11", "800", "200", canc_q=1, buyer="800", canc_buyer="200")]          # дозревший день; покупатель = продавец (дубль)
        kpi = [{"kpi_date": "2026-09-01", "marketplace_sku": "11", "ad_spend": "100"}]
        dl = [daily("2026-08-01", ads="0"), daily("2026-09-01")]
        return fr.build_order_rows(["2026-08-01", "2026-09-01"], orders, kpi, dl, UNIT_COST, {"11": "F1", "22": "T2"}, CATALOG, {}, "2026-09-10")  # 09-01 ещё не дозрел (9 сут.)

    def test_rows_buyer_and_residual(self):
        rows, stats = self.data()
        by = {(r["date"], r["sku"]): r for r in rows}
        r = by[("2026-09-01", "11")]
        self.assertEqual((r["created_a"], r["created_q"], r["confirmed_a"], r["buyer_a"]), (D(1500), D(2), D(1000), D(900)))
        self.assertIsNone(by[("2026-09-01", "22")]["buyer_a"])                        # цены нет — пусто
        self.assertIsNone(by[("2026-08-01", "11")]["buyer_a"])                        # дубль цены продавца — не измерено
        self.assertEqual(by[("2026-09-01", fr.NO_SKU)]["ads"], D("48.84"))            # 148,84 леджера − 100 по SKU
        self.assertEqual(r["cogs_created"], D(2) * D(100) * fr.cogs_index("2026-09-01"))
        self.assertEqual(stats["qty_without_cost"], 1)

    def test_coefficients_and_svod(self):
        rows, _ = self.data()
        days = ["2026-08-01", "2026-09-01"]
        coef = fr.coefficients(rows, [], days)
        self.assertEqual(coef["buyout"]["все"], D(800) / D(1000))                    # только дозревший 08-01: подтверждено 800 из 1000
        self.assertEqual(coef["buyout"]["Основная"], D(800) / D(1000))
        coef["commission"] = {"сен": D("0.4"), "авг": D("0.4"), "все": D("0.4")}
        data, _ = fr.build_order_data_rows(rows, None, coef, days)
        piv, order_ = fr.svod_from_data(data, days, "Ozon")
        s = piv["сен"]
        net = D(1900) / D("1.22")
        self.assertEqual((s["created_a"], s["created_q"], s["price"]), (D(1900), D(3), D(1900) / D(3)))
        self.assertEqual(rep.q(s["revenue"]), rep.q(net * D("0.6")))                     # по ВСЕМ созданным, без коэффициента выкупа
        self.assertEqual(rep.q(s["buyout_rub"]), rep.q(net * D("0.6") * D("0.8")))       # «Выкуплено» — рубли = Выручка × коэффициент
        self.assertEqual(s["buyout"], D("0.8"))
        self.assertEqual(s["commission_pct"], D("0.4"))                                   # Σ Комиссия руб / Σ ₽
        self.assertEqual(s["ads_net"], D(122))
        self.assertEqual(s["drr_pct"], D(122) / s["buyout_rub"])                          # ДДР = Реклама без НДС / Выкуплено
        self.assertEqual(s["coinvest_pct"], D("0.4"))                                     # по измеренным строкам: (1500 − 900) / 1500 без НДС — та же доля
        self.assertEqual(s["coinvest_cover"], D(1500) / D(1900))
        self.assertIsNone(piv["авг"]["coinvest_pct"])
        cogs = D(2) * D(100) * fr.cogs_index("2026-09-01")
        self.assertEqual(s["cogs"], cogs)
        self.assertEqual(s["margin"], s["revenue"] - cogs)
        self.assertEqual(rep.q(s["gross_fin"]), rep.q(s["buyout_rub"] - cogs * D("0.8") - D(122)))   # = 0,8 × (Выручка − СС) − Реклама, как прежняя формула
        self.assertEqual(order_, ["авг", "01.авг", "сен", "01.сен", "Общий итог"])


if __name__ == "__main__":
    unittest.main()


# ---------- §7: длинный формат из сырья, «Данные заказы» в полях владельца, раскладка книги ----------

import json
import tempfile

RAW_DAY = "2026-09-01"


def raw_accruals(d=RAW_DAY):
    """Синтетическое сырьё by-day: отправление с продажей (sku 11, услуга доставки) и возвратом (sku 22); эквайринг по товару; подписка без SKU;
    реклама и компенсация (в лист не входят); начисление чужой даты."""
    m = lambda v: {"amount": v, "currency": "RUB"}  # noqa: E731
    return [
        {"accrual_id": 1, "date": d, "accrued_category": "POSTING", "posting": {"products": [
            {"sku": 11, "commission": {"sale_amount": m("1000.00"), "sale_commission": m("-400.00"), "bonus": m("10.00"), "coinvestment": m("1.00")},
             "delivery": {"services": [{"type_id": 32, "accrued": m("-50.00")}]}},
            {"sku": 22, "commission": {"sale_amount": m("-500.00"), "sale_commission": m("200.00"), "bonus": m("0"), "coinvestment": m("0")}}]}},
        {"accrual_id": 2, "date": d, "accrued_category": "ITEM", "item_fees": {"fees": [{"sku": 11, "fees": [{"type_id": 1, "accrued": m("-20.00")}]}]}},
        {"accrual_id": 3, "date": d, "accrued_category": "NON_ITEM", "non_item_fee": {"fees": [{"type_id": 51, "accrued": m("-100.00")}]}},
        {"accrual_id": 4, "date": d, "accrued_category": "NON_ITEM", "non_item_fee": {"fees": [{"type_id": 41, "accrued": m("-300.00")}, {"type_id": 25, "accrued": m("10.00")}]}},
        {"accrual_id": 5, "date": "2026-08-31", "posting": {"products": [{"sku": 11, "commission": {"sale_amount": m("7.00"), "sale_commission": m("-1.00")}}]}},
    ]


def daily_for_raw(d=RAW_DAY):
    """Строка build_daily того же дня (без НДС): оборот 500, комиссия 200, логистика 50, эквайринг 20, подписка 100, реклама 300, прочее 0."""
    vat = D("1.22")
    row = {"date": d, "vat": vat, "turnover": D(500), "commission": D(200), "revenue": (D(500) - D(200)) / vat, "cogs": D(200),
           "logistics": D(50) / vat, "acquiring": D(20) / vat, "subscription": D(100) / vat, "other": D(0), "ads": D(300) / vat}
    row["cogs_index"] = D(200) * fr.cogs_index(d)
    return row


class LongRows(unittest.TestCase):
    def build(self, tmp):
        with open(os.path.join(tmp, f"{RAW_DAY}.json"), "w", encoding="utf-8") as fh:
            json.dump({"date": RAW_DAY, "accruals": raw_accruals()}, fh)
        buyouts = [dict(buyout(RAW_DAY, "11", "1000", "400", units=2), buyouts_qty=1), dict(buyout(RAW_DAY, "22", "-500", "-200"), buyouts_units=None, buyouts_qty=-1)]
        kpi = [{"kpi_date": RAW_DAY, "marketplace_sku": "11", "ad_spend": "100"}]
        return fr.build_buyout_long_rows([RAW_DAY], tmp, buyouts, [], kpi, [daily_for_raw()], UNIT_COST, {"11": "F1", "22": "T2"}, CATALOG, {})

    def test_raw_day_rows_signs_ids_qty_and_residual(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows, wide, stats = self.build(tmp)
        by = {(r.accrual_id, r.sku, r.kind): r for r in rows}
        self.assertEqual(len(rows), 9, [(r.accrual_id, r.sku, r.kind, r.amount) for r in rows])
        t11 = by[(1, "11", "Товарооборот")]
        self.assertEqual((t11.amount, t11.qty, t11.coinvest, t11.cogs), (D(1000), D(2), D(11), D(2) * D(100) * fr.cogs_index(RAW_DAY)))   # штуки 2 измерены → на строку продажи
        self.assertEqual((t11.article, t11.brand, t11.brand_cc, t11.category, t11.month, t11.day_num, t11.month_num, t11.platform), ("F1", "KARATOV", "KARATOV", "кольца", "сен", 1, 9, "Ozon"))
        self.assertEqual(by[(1, "11", "Комиссия")].amount, D(-400))
        self.assertEqual((by[(1, "22", "Товарооборот")].amount, by[(1, "22", "Товарооборот")].qty, by[(1, "22", "Комиссия")].amount), (D(-500), D(-1), D(200)))
        self.assertEqual(by[(1, "11", "Логистика")].amount, D(-50))
        self.assertEqual(by[(2, "11", "Эквайринг")].amount, D(-20))
        self.assertEqual((by[(3, "", "Прочее")].amount, by[(3, "", "Прочее")].article), (D(-100), fr.NO_SKU))
        self.assertEqual(by[("", "11", "Реклама")].amount, D(-100))                          # Performance по SKU — без ID
        self.assertEqual(by[("", "", "Реклама")].amount, D(-200))                            # остаток дня: 300 леджера − 100 по SKU
        self.assertEqual((stats["raw_ads_lines_skipped"], stats["raw_compensation_lines_skipped"], stats["raw_foreign_date"]), (1, 1, 1))
        self.assertEqual((stats["units_reallocated_keys"], stats["positions_without_cost"], stats["days_from_raw"]), (1, 1, 1))
        self.assertFalse([k for k in stats if k.startswith("residual_") and k != "residual_ads_days" and k != "residual_ads_sum"], stats)
        w = {r["sku"]: r for r in wide}
        self.assertEqual((w["11"]["turnover"], w["11"]["commission"], w["11"]["logistics"], w["11"]["acquiring"], w["11"]["ads"], w["11"]["units"]), (D(1000), D(-400), D(-50), D(-20), D(-100), D(2)))
        self.assertEqual((w[fr.NO_SKU]["other"], w[fr.NO_SKU]["ads"]), (D(-100), D(-200)))
        table = fr.check_against_month(wide, [daily_for_raw()], [RAW_DAY])
        self.assertEqual([t[3] for t in table], [D(0)] * len(table), table)
        piv, order = fr.pivot_buyouts(wide, [RAW_DAY])
        self.assertEqual(fr.long_identity(rows, piv["Ozon"], order), [])

    def test_day_without_raw_falls_back_to_tables_without_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            buyouts = [buyout(RAW_DAY, "11", "1000", "400")]
            kpi = [{"kpi_date": RAW_DAY, "marketplace_sku": "11", "ad_spend": "100"}]
            rows, wide, stats = fr.build_buyout_long_rows([RAW_DAY], tmp, buyouts, [expense(RAW_DAY, "11", "logistics", "8")], kpi, [daily(RAW_DAY)], UNIT_COST, {"11": "F1"}, CATALOG, {})
        self.assertEqual(stats["days_from_tables"], 1)
        self.assertTrue(all(r.accrual_id == "" for r in rows))
        kinds = {(r.sku, r.kind): r.amount for r in rows}
        self.assertEqual((kinds[("11", "Товарооборот")], kinds[("11", "Комиссия")], kinds[("11", "Логистика")], kinds[("11", "Реклама")]), (D(1000), D(-400), D(-8), D(-100)))
        self.assertEqual(kinds[("", "Реклама")], D("-48.84"))
        self.assertEqual([r for r in rows if r.kind == "Товарооборот"][0].qty, D(1))


class OrdersData(unittest.TestCase):
    def parts(self):
        rows, _ = Orders().data()
        days = ["2026-08-01", "2026-09-01"]
        coef = fr.coefficients(rows, [], days)
        coef["commission"] = {"сен": D("0.4"), "авг": D("0.3"), "все": D("0.35")}
        dl = [daily("2026-08-01", ads="0"), daily("2026-09-01")]
        data, _ = fr.build_order_data_rows(rows, None, coef, days)
        return rows, days, coef, dl, fr.svod_from_data(data, days, "Ozon")

    def test_row_formulas_are_the_pivot_formulas(self):
        rows, days, coef, dl, (piv, order) = self.parts()
        data, stats = fr.build_order_data_rows(rows, None, coef, days)
        r = next(x for x in data if x["date"] == "2026-09-01" and x["sku"] == "11")
        net = D(1500) / D("1.22")
        self.assertEqual((r["mp"], r["shop"], r["article"], r["created_a"], r["created_q"], r["created_net"]), ("Ozon", "KARATOV", "F1", D(1500), D(2), net))
        self.assertEqual((r["commission"], r["commission_avg"], r["buyout_rate"]), (D("0.4"), D("0.4"), D("0.8")))   # Комиссия сред = Комиссия руб / Заказы ₽
        self.assertEqual(r["revenue"], net * D("0.6"))                                        # без коэффициента выкупа
        self.assertEqual(r["commission_rub"], D(1500) * D("0.4"))                              # на базе с НДС
        self.assertEqual(r["buyout_rub"], net * D("0.6") * D("0.8"))                          # рубли
        self.assertEqual(r["margin"], r["revenue"] - r["cogs_real"])
        self.assertEqual(r["fin"], r["buyout_rub"] - r["cogs_real"] * (r["buyout_rub"] / r["revenue"]) - r["ads_net"])
        self.assertEqual(r["fin_pct"], r["fin"] / r["buyout_rub"])
        self.assertEqual((r["spp"], r["coinvest_pct"], r["with_spp"]), (D("0.4"), D("0.4"), D(900) / D("1.22")))   # c СПП — без НДС
        self.assertEqual((r["week"], r["day_num"], r["month_num"]), (36, 1, 9))
        self.assertEqual(r["drr_pct"], r["ads_net"] / r["buyout_rub"])
        self.assertEqual([h for h, _k, _f in fr.ORDER_DATA_COLS][:4], ["Дата", "МП", "Магазин", "Артикул поставщика"])
        self.assertEqual(len(fr.ORDER_DATA_COLS), 33)
        self.assertEqual([h for h, _k, _f in fr.ORDER_DATA_COLS][28:31], ["Фин.рез %", "Месяцы", "SKU/nmId"])      # «Месяцы» — после 29 полей владельца
        self.assertEqual(stats, {})

    def test_svod_equals_sum_of_data_rows_all_13_columns(self):
        rows, days, coef, dl, (piv, order) = self.parts()
        data, _ = fr.build_order_data_rows(rows, None, coef, days)
        table = fr.check_orders_data(data, days, piv, None)
        self.assertEqual({t[1] for t in table}, {"авг", "01.авг", "сен", "01.сен", "Общий итог"})
        self.assertEqual([t for t in table if t[5]], [])
        # «Общий итог» — комиссия взвешенная, месяцы с разной комиссией складываются построчно
        self.assertNotEqual(piv["Общий итог"]["commission_pct"], D("0.35"))
        self.assertEqual(rep.q(piv["Общий итог"]["revenue"]), rep.q(piv["авг"]["revenue"] + piv["сен"]["revenue"]))

    def test_zero_rows_are_dropped_and_counted(self):
        rows, days, coef, dl, _ = self.parts()
        zero = {"date": "2026-09-01", "month": "сен", "brand": "", "category": "", "article": "X", "sku": "99", "name": "", "created_a": D(0), "created_q": D(0),
                "confirmed_a": D(5), "buyer_a": None, "ads": D(0), "cogs_created": D(0), "vat": D("1.22"), "platform_name": "Основная"}
        data, stats = fr.build_order_data_rows(rows + [zero], None, coef, days)
        self.assertEqual((stats["dropped_zero_Ozon"], stats["dropped_confirmed_Ozon"]), (1, D(5)))
        self.assertFalse([r for r in data if r["sku"] == "99"])


class Book(unittest.TestCase):
    def test_layout_rows_and_column_order(self):
        import openpyxl
        rows, _ = BuyoutRows().rows()
        long_rows = [x for w in rows for x in fr.explode_wide(w)]
        days = ["2026-09-01"]
        pivots = {"all": fr.pivot_buyouts(rows, days), "brand": fr.pivot_buyouts(rows, days, lambda r: r["brand"]), "category": fr.pivot_buyouts(rows, days, lambda r: r["category"])}
        orows, _ = Orders().data()
        odays = ["2026-08-01", "2026-09-01"]
        coef = fr.coefficients(orows, rows, odays)
        coef["commission"] = {"сен": D("0.4"), "все": D("0.4")}
        data, _ = fr.build_order_data_rows(orows, None, coef, odays)
        piv = fr.svod_from_data(data, odays, "Ozon")
        notes = {k: [f"примечание {k}"] for k in ("buyouts", "buyout_data", "orders", "coef", "order_data", "wb")}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "book.xlsx")
            timings = {}
            counts = fr.write_book(path, days, long_rows, pivots, orows, piv, coef, notes, "test", "2026-09-10", wb={"error": "нет"}, order_data_rows=data, timings=timings)
            full = openpyxl.load_workbook(path)
            wf = full["Выкупы Ozon"]
            self.assertEqual([(r, wf.row_dimensions[r].outline_level, bool(wf.row_dimensions[r].hidden)) for r in (4, 5, 6)], [(4, 0, False), (5, 1, True), (6, 0, False)])
            self.assertFalse(full["Выкупы Ozon"].sheet_properties.outlinePr.summaryBelow)
            wz = full["Заказы"]
            self.assertEqual([(wz.cell(row=r, column=1).value, wz.row_dimensions[r].outline_level) for r in (4, 5, 6, 7, 8)],
                             [("авг", 0), ("01.авг", 1), ("сен", 0), ("01.сен", 1), ("Общий итог", 0)])
            wb = openpyxl.load_workbook(path, read_only=True)
            self.assertEqual(wb.sheetnames, ["Выкупы Ozon", "Выкупы Ozon × бренд", "Выкупы Ozon × категория", "Данные Ozon выкупы", "Выкупы WB", "Заказы", "Коэффициенты", "Данные заказы", "Списки", "Артикул", "Примечания"])
            wa = wb["Артикул"]
            rows_a = list(wa.iter_rows(min_row=1, max_row=8, values_only=True))
            self.assertEqual((rows_a[0][0][:7], rows_a[3][0], rows_a[3][1]), ("Артикул", "Показатель", "сен"))
            self.assertTrue(str(rows_a[5][1]).startswith("=SUMIFS('Данные Ozon выкупы'!$L:$L"))
            self.assertEqual(wb["Списки"].sheet_state, "hidden")
            v = lambda ws, r, c: ws.cell(row=r, column=c).value  # noqa: E731
            ws = wb["Выкупы Ozon"]
            cells = {(r, c): x for r, row in enumerate(ws.iter_rows(min_row=1, max_row=14, values_only=True), 1) for c, x in enumerate(row, 1)}
            self.assertEqual((cells[(2, 1)], cells[(2, 2)], cells[(3, 1)], cells[(3, 2)], cells[(3, 14)], cells[(4, 1)], cells[(5, 1)], cells[(6, 1)]),
                             ("Названия столбцов", "Комиссия", "Названия строк", " Начисления", "Оборот (с НДС)", "сен", "01.сен", "Общий итог"))   # компактно, без пустых колонок
            wo = wb["Заказы"]
            cells = {(r, c): x for r, row in enumerate(wo.iter_rows(min_row=1, max_row=12, values_only=True), 1) for c, x in enumerate(row, 1)}
            self.assertEqual((cells[(2, 1)], cells[(2, 2)], cells[(2, 15)], cells[(3, 1)], cells[(3, 2)], cells[(4, 1)]), ("Названия столбцов", "Ozon", "WB", "Названия строк", "Оборот (с НДС)", "авг"))
            self.assertEqual(list(next(wb["Данные Ozon выкупы"].iter_rows(min_row=1, max_row=1, values_only=True))), [h for h, _k, _f in fr.LONG_COLS])
            self.assertEqual(list(next(wb["Данные заказы"].iter_rows(min_row=1, max_row=1, values_only=True))), [h for h, _k, _f in fr.ORDER_DATA_COLS])
            self.assertEqual(list(next(wb["Коэффициенты"].iter_rows(min_row=1, max_row=1, values_only=True)))[:5], ["МП", "% выкупа", None, "МП", "Комиссия"])
            self.assertEqual(counts, {"long_rows": len(long_rows), "order_rows": len(data)})
            self.assertIn("Данные заказы", timings)


class WbCoefficient(unittest.TestCase):
    """Тридцать восьмая §3: блок WB «Свода» и строки WB «Данных заказы» считаются формулами Ozon с коэффициентом выкупа модуля; «Свод» = Σ строк."""

    def parts(self):
        days = ["2026-08-01", "2026-09-01"]
        orders = [{"date": "2026-08-01", "month": "авг", "platform": "WB", "shop": "KARATOV", "article": "F1", "nm_id": 11, "title": "Кольцо", "brand": "KARATOV", "category": "кольца",
                   "orders_qty": 2, "orders_sum": D(1220), "revenue": D(1220) * D("0.58") / D("1.22"), "cogs": D(300), "ads": D("61"), "funnel_buyouts_sum": D(610)},
                  {"date": "2026-09-01", "month": "сен", "platform": "WB", "shop": "KARATOV", "article": "T2", "nm_id": 22, "title": "Серьги", "brand": "Топаз", "category": "серьги",
                   "orders_qty": 1, "orders_sum": D(2440), "revenue": D(2440) * D("0.58") / D("1.22"), "cogs": None, "ads": D(0), "funnel_buyouts_sum": D(0)}]
        ads_by_day = {"2026-08-01": D("122"), "2026-09-01": D("244")}      # 08-01: 61 в строке + 61 остаток дня; 09-01: всё в остаток
        rate = {"авг": D("0.5"), "итого": D("0.5")}
        wb_parts = {"orders": orders, "ads_by_day": ads_by_day, "buyout": rate}
        coef = {"buyout": {"все": D("0.6")}, "commission": {"все": D("0.4"), "авг": D("0.4"), "сен": D("0.4")}}
        data, _ = fr.build_order_data_rows([], wb_parts, coef, days)
        piv, _order = fr.svod_from_data(data, days, "WB")
        return days, orders, ads_by_day, rate, piv, wb_parts, data

    def test_wb_svod_uses_the_owner_formulas_with_the_coefficient_in_rubles(self):
        days, orders, ads_by_day, rate, piv, _wb, _data = self.parts()
        a = piv["авг"]
        net = D(1220) / D("1.22")
        self.assertEqual((a["buyout"], a["commission_pct"]), (D("0.5"), D("0.42")))
        self.assertEqual(a["revenue"], net * D("0.58"))                                      # по всем созданным
        self.assertEqual(a["buyout_rub"], net * D("0.58") * D("0.5"))                        # «Выкуплено» — рубли
        self.assertEqual(a["cogs"], D(300))
        self.assertEqual(a["margin"], a["revenue"] - D(300))
        self.assertEqual(a["ads_net"], D(122) / D("1.22"))
        self.assertEqual(a["drr_pct"], a["ads_net"] / a["buyout_rub"])
        self.assertEqual(rep.q(a["gross_fin"]), rep.q(D("0.5") * (a["revenue"] - D(300)) - a["ads_net"]))   # = k × (Выручка − СС) − Реклама

    def test_wb_data_rows_carry_the_coefficient_and_sum_to_the_svod(self):
        days, orders, ads_by_day, rate, piv, wb_parts, data = self.parts()
        wb = [r for r in data if r["mp"] == "WB"]
        self.assertEqual(len(wb), 4)                                                        # 2 строки товаров + 2 строки рекламы дня «(без nmId)»
        self.assertTrue(all(r["buyout_rate"] == D("0.5") and r["commission"] == D("0.42") for r in wb))
        remainder = {r["date"]: r["ads"] for r in wb if r["article"] == "(без nmId)"}
        self.assertEqual(remainder, {"2026-08-01": D(61), "2026-09-01": D(244)})
        table = fr.check_orders_data(data, days, None, piv)
        self.assertEqual([t for t in table if t[5]], [])
        bad, n = fr.check_days_sum(piv, fr.labels_for(days)[0], ("created_a", "created_q", "revenue", "cogs", "ads_net", "buyout_rub"))
        self.assertEqual((bad, n > 0), ([], True))


class LabelsCheck(unittest.TestCase):
    def test_label_sets_by_platform_and_brandless_rows(self):
        rows = [{"mp": "Ozon", "brand": "KARATOV", "category": "кольца", "article": "F1", "created_a": D(1)}, {"mp": "WB", "brand": "KARATOV", "category": "кольца", "article": "F2", "created_a": D(1)},
                {"mp": "Ozon", "brand": "", "category": "", "article": fr.NO_SKU, "created_a": D(0)}, {"mp": "WB", "brand": "Топаз", "category": "серьги", "article": "T1", "created_a": D(1)}]
        res = fr.check_labels(rows)
        self.assertEqual(res["brands"], {"Ozon": {"KARATOV"}, "WB": {"KARATOV", "Топаз"}})
        self.assertEqual(res["categories"], {"Ozon": {"кольца"}, "WB": {"кольца", "серьги"}})
        self.assertEqual(res["brandless_ozon"], {fr.NO_SKU: 1})
        self.assertFalse(res["equal"])


class WbForm(unittest.TestCase):
    """Тридцать девятая §2: листы WB в форме владельца из дневных строк модуля — месяцы, дни последнего месяца, «Общий итог»; = build_month_sheet / build_split."""

    def rows(self):
        def r(day, brand, cat, sales, comm, cogs, ads, storage, logi, other, acq, qty):
            return {"date": day, "brand": brand, "category": cat, "sales": D(sales), "commission": D(comm), "cogs": D(cogs), "ads": D(ads), "storage": D(storage),
                    "logistics": D(logi), "other": D(other), "acquiring": D(acq), "coinvest": D(0), "qty": qty}
        return [r("2026-08-30", "KARATOV", "кольца", 1000, 400, 300, 10, 5, 50, 7, 3, 1), r("2026-09-01", "KARATOV", "кольца", 2000, 800, 600, 20, 6, 60, 8, 4, 2),
                r("2026-09-01", "Топаз", "серьги", 500, 200, 100, 0, 1, 10, 2, 1, 1), r("2026-09-02", "Топаз", "серьги", 700, 280, 200, 5, 2, 20, 3, 2, 1)]

    def test_months_and_total_equal_the_module_days_sum_to_month(self):
        rows, days = self.rows(), ["2026-08-30", "2026-08-31", "2026-09-01", "2026-09-02"]
        piv, order = fr.wb_pivot(rows, days)
        self.assertEqual(order, ["авг", "30.авг", "31.авг", "сен", "01.сен", "02.сен", "Общий итог"])
        bad, n = fr.check_wb_pivot_against_module(piv, order, fr.wbfin.build_month_sheet(rows), days)
        self.assertEqual(bad, [])
        self.assertGreater(n, 40)
        self.assertEqual(piv["сен"]["sales"], D(3200))
        self.assertEqual(piv["01.сен"]["sales"] + piv["02.сен"]["sales"], piv["сен"]["sales"])
        self.assertEqual(piv["Общий итог"]["y_fin"], fr.wbfin.build_month_sheet(rows)[-1]["y_fin"])
        self.assertEqual(piv["01.сен"]["n_revenue"], (D(2500) - D(1000)) / D("1.22"))
        by_brand = fr.wb_pivots_by_group(rows, days, "brand")
        self.assertEqual(set(by_brand), {"KARATOV", "Топаз"})
        mod = {}
        for m in fr.wbfin.build_split(rows, "brand"):
            mod.setdefault(m["group"], []).append(m)
        for g, (p_, o_) in by_brand.items():
            b_, _n = fr.check_wb_pivot_against_module(p_, o_, mod[g], days)
            self.assertEqual(b_, [], g)

    def test_block_layout(self):
        rows, days = self.rows(), ["2026-08-30", "2026-09-01", "2026-09-02"]
        piv, order = fr.wb_pivot(rows, days)
        g = fr.Grid()
        nxt = fr.write_wb_pivot_block(g, 9, "Топаз", piv, order)
        self.assertEqual((g.cells[(9, 1)][0], g.cells[(10, 1)][0], g.cells[(10, 2)][0], g.cells[(11, 1)][0], g.cells[(12, 1)][0]),
                         ("Топаз", "Названия строк", "Продажи, ₽", "авг", "30.авг"))
        self.assertEqual((g.cells[(11, 2)][0], g.outline.get(11), g.outline.get(12)), (1000.0, None, 1))
        self.assertEqual(nxt, 9 + 1 + 1 + len(order))
        self.assertGreaterEqual(g.widths()[2], len("1,000.00") + 1)
