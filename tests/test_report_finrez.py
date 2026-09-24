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
        self.assertEqual(order, ["авг", "сен", "01.сен", "02.сен", "Общий итог"])
        self.assertEqual(of("2026-08-30"), ["авг", "Общий итог"])
        self.assertEqual(of("2026-09-02"), ["сен", "02.сен", "Общий итог"])


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

    def test_coefficients_and_pivot(self):
        rows, _ = self.data()
        coef = fr.coefficients(rows, [], ["2026-08-01", "2026-09-01"])
        self.assertEqual(coef["buyout"]["все"], D(800) / D(1000))                    # только дозревший 08-01: подтверждено 800 из 1000
        self.assertEqual(coef["buyout"]["Основная"], D(800) / D(1000))
        coef["commission"] = {"сен": D("0.4"), "авг": D("0.4"), "все": D("0.4")}
        piv, order_ = fr.pivot_orders(rows, ["2026-08-01", "2026-09-01"], coef, [daily("2026-08-01", ads="0"), daily("2026-09-01")])
        s = piv["сен"]
        self.assertEqual((s["created_a"], s["created_q"], s["price"]), (D(1900), D(3), D(1900) / D(3)))
        self.assertEqual(rep.q(s["revenue"]), rep.q(D(1900) / D("1.22") * D("0.8") * D("0.6")))   # Σ по дням без НДС × выкуп × (1 − комиссия)
        self.assertEqual(s["ads_net"], D(122))
        self.assertEqual(s["coinvest_pct"], D("0.4"))                                   # по измеренным строкам: (1500 − 900) / 1500; SKU без цены не входит
        self.assertEqual(s["coinvest_cover"], D(1500) / D(1900))                        # измерено 1 500 из 1 900 созданного
        self.assertIsNone(piv["авг"]["coinvest_pct"])                                   # дубль цены продавца — не измерено, база 0
        self.assertEqual(s["gross_fin"], s["margin"] - D(122))
        self.assertEqual(order_, ["авг", "сен", "01.сен", "Общий итог"])


if __name__ == "__main__":
    unittest.main()
