"""«Выкупы WB» книги «Фин рез» (форма второго кабинета) и «Данные WB выкупы».

Держим: строка «Данных» на (день продажи МСК, nmId) со знаком возврата; операции без nmId — «(без товара)»; реклама дня
разнесена по продажам дня и Σ по дню = списаниям дня; тождества формы образца (N = (K − L)/НДС, R = (G + F)/НДС, X = H/НДС,
Y = P − R − T − V − X); Σ брендов = Σ категорий = общий; категория по списку владельца, остальное — «прочее»; договорные
сигнатуры и колонки на месте; заказы для общего листа — выручка × 0,58 / НДС.
"""
import unittest
from decimal import Decimal
from unittest import mock

import scripts.report_finrez_wb as fr

D = Decimal


def rep(day_ts, nm, oper="Продажа", price="1000", amount="600", pct="42", vendor="f000283615", size="17,5", rrd=1, **extra):
    base = {"rrd_id": rrd, "rr_date": day_ts[:10], "sale_dt": day_ts, "seller_oper_name": oper, "doc_type": "", "vendor_code": vendor, "tech_size": size,
            "nm_id": nm, "quantity": 1, "retail_price_with_disc": price, "retail_amount": amount, "for_pay": "500", "commission_percent": pct,
            "ppvz_reward": None, "rebill_logistic_cost": None, "delivery_service": None, "acquiring_fee": None, "paid_storage": None,
            "penalty": None, "deduction": None, "cashback_discount": None, "additional_payment": None}
    base.update(extra)
    return base


ROWS = [
    rep("2026-09-01T10:00:00Z", 1, price="1000", amount="600", acquiring_fee="12.2", rrd=1),
    rep("2026-09-01T11:00:00Z", 1, oper="Возврат", price="100", amount="60", acquiring_fee="1.22", rrd=2),
    rep("2026-09-01T12:00:00Z", 2, price="500", amount="300", pct="38", vendor="t000000001", size="0", rrd=3),
    rep("2026-09-01T13:00:00Z", 1, oper="Доставка", price=None, amount=None, delivery_service="122", rebill_logistic_cost="24.4", rrd=4),
    rep("2026-09-01T14:00:00Z", 0, oper="Хранение", price=None, amount=None, paid_storage="61", rrd=5),                       # без nmId
    rep("2026-09-01T15:00:00Z", 1, oper="Штраф", price=None, amount=None, penalty="30", deduction="12.2", additional_payment="2", rrd=6),
    rep("2026-09-01T16:00:00Z", None, oper="Возмещение за выдачу и возврат товаров на ПВЗ", price=None, amount=None, ppvz_reward="10", rrd=7),
    rep("2026-08-31T21:30:00Z", 2, price="700", amount="400", vendor="t000000001", size="0", rrd=8),                            # 00:30 МСК 09-01
    rep("2026-09-02T10:00:00Z", 3, price="300", amount="200", vendor="f000009483", size="0", rrd=9),
]
COSTS = ({"f000283615-17,5": D("100"), "f000009483": D("50")}, {"f000283615": D("110"), "f000009483": D("50")})
PRODUCTS = {1: {"title": "Серьги", "brand": "KARATOV", "subject": "Ювелирные серьги"}, 2: {"title": "Кольцо", "brand": None, "subject": "Упаковки для украшений"}}
ADS = {"2026-09-01": D("244"), "2026-09-02": D("50"), "2026-09-05": D("10")}


class DataRowsTests(unittest.TestCase):
    def rows(self):
        return fr.build_rows("2026-09", "2026-09", None, sb=object(), rows=ROWS, costs=COSTS, ads_by_day=ADS, products=PRODUCTS)

    def test_row_per_sale_day_and_nm_with_return_sign_and_no_product_row(self):
        rows = self.rows()
        by = {(r["date"], r["nm_id"]): r for r in rows}
        self.assertEqual(sorted(by, key=lambda k: (k[0], k[1] or 0)), [("2026-09-01", None), ("2026-09-01", 1), ("2026-09-01", 2), ("2026-09-02", 3), ("2026-09-05", None)])
        self.assertEqual((by[("2026-09-05", None)]["ads"], by[("2026-09-05", None)]["sales"]), (D("10"), D("0")))   # списания без строк отчёта — не теряются
        r1 = by[("2026-09-01", 1)]
        self.assertEqual((r1["sales"], r1["commission"], r1["coinvest"], r1["acquiring"], r1["qty"]), (D("900"), D("378.00"), D("360"), D("10.98"), 0))
        self.assertEqual((r1["logistics"], r1["rebill"], r1["other"], r1["cogs"], r1["storage"]), (D("122"), D("24.4"), D("40.2"), D("0"), D("0")))   # rebill — не берём
        self.assertEqual((r1["article"], r1["brand"], r1["category"], r1["title"], r1["month"], r1["month_no"], r1["platform"]), ("F000283615", "KARATOV", "Серьги", "Серьги", "сен", 9, "WB"))
        r2 = by[("2026-09-01", 2)]
        self.assertEqual((r2["sales"], r2["qty"], r2["brand"], r2["category"], r2["no_cost_qty"], r2["cogs"]), (D("1200"), 2, "КОЮЗ Топаз", "прочее", 2, D("0")))
        none = by[("2026-09-01", None)]
        self.assertEqual((none["article"], none["storage"], none["ppvz_reward"], none["sales"]), ("(без товара)", D("61"), D("10"), D("0")))
        r3 = by[("2026-09-02", 3)]
        self.assertEqual((r3["category"], r3["cogs"], r3["brand"]), ("прочее (нет предмета)", D("50"), "KARATOV"))

    def test_ads_are_allocated_by_positive_sales_and_sum_to_the_day(self):
        rows = self.rows()
        day1 = [r for r in rows if r["date"] == "2026-09-01"]
        self.assertEqual(sum((r["ads"] for r in day1), D(0)), D("244"))
        by = {r["nm_id"]: r["ads"] for r in day1}
        self.assertEqual(by[1], D("104.57"))          # 244 × 900 / 2100
        self.assertEqual(by[2], D("139.43"))          # остаток копеек — последнему
        self.assertEqual(by[None], D("0"))
        self.assertEqual([r for r in day1 if r["nm_id"] == 1][0]["ads_allocated"], "по продажам")
        self.assertEqual([r["ads"] for r in rows if r["date"] == "2026-09-02"], [D("50")])

    def test_ads_without_sales_go_to_the_no_product_row_and_none_when_unknown(self):
        rows = fr.build_rows("2026-09", "2026-09", None, sb=object(), rows=ROWS, costs=COSTS, ads_by_day={"2026-09-03": D("70")}, products=PRODUCTS)
        extra = [r for r in rows if r["date"] == "2026-09-03"]
        self.assertEqual((len(extra), extra[0]["nm_id"], extra[0]["ads"]), (1, None, D("70")))
        rows = fr.build_rows("2026-09", "2026-09", None, sb=object(), rows=ROWS, costs=COSTS, ads_by_day=None, products=PRODUCTS)
        self.assertTrue(all(r["ads"] is None for r in rows))

    def test_ads_shares_from_fullstats_are_normalized_to_the_day_write_offs(self):
        nm_shares = {"2026-09-01": {1: D("30"), 2: D("10"), 9: D("10")}}      # Σ 50 против списаний 244 — нормируем к 244
        rows = fr.build_rows("2026-09", "2026-09", None, sb=object(), rows=ROWS, costs=COSTS, ads_by_day=ADS, products=PRODUCTS, ads_nm_by_day=nm_shares)
        day1 = {r["nm_id"]: r for r in rows if r["date"] == "2026-09-01"}
        self.assertEqual((day1[1]["ads"], day1[2]["ads"], day1[9]["ads"]), (D("146.40"), D("48.80"), D("48.80")))
        self.assertEqual(sum((r["ads"] for r in day1.values()), D(0)), D("244"))
        self.assertEqual((day1[9]["sales"], day1[9]["qty"], day1[9]["ads_allocated"], day1[1]["ads_allocated"]), (D("0"), 0, "fullstats", "fullstats"))
        self.assertEqual([r["ads_allocated"] for r in rows if r["date"] == "2026-09-02"], ["по продажам"])   # за 09-02 статистики нет

    def test_category_and_brand_come_from_the_report_row_first(self):
        rows_with = [dict(r, subject_name="Ювелирные кольца", brand_name="КОЮЗ Топаз") if r["nm_id"] == 1 else r for r in ROWS]
        rows = fr.build_rows("2026-09", "2026-09", None, sb=object(), rows=rows_with, costs=COSTS, ads_by_day=None, products=PRODUCTS)
        r1 = [r for r in rows if r["nm_id"] == 1 and r["date"] == "2026-09-01"][0]
        self.assertEqual((r1["category"], r1["brand"]), ("Кольца", "КОЮЗ Топаз"))          # строка отчёта важнее словаря карточек
        r3 = [r for r in rows if r["nm_id"] == 3][0]
        self.assertEqual(r3["category"], "прочее (нет предмета)")

    def test_subject_falls_back_to_other_rows_of_the_same_nm_then_to_a_targeted_fetch(self):
        rows_with = [dict(r, subject_name="Ювелирные кольца", brand_name="KARATOV") if (r["nm_id"] == 1 and r["rrd_id"] == 2) else r for r in ROWS]
        rows = fr.build_rows("2026-09", "2026-09", None, sb=object(), rows=rows_with, costs=COSTS, ads_by_day=None, products={})
        r1 = [r for r in rows if r["nm_id"] == 1 and r["date"] == "2026-09-01"][0]
        self.assertEqual((r1["category"], r1["brand"]), ("Кольца", "KARATOV"))            # предмет взят с другой строки того же nmId
        with mock.patch.object(fr, "fetch_subjects_for", return_value={3: {"subject": "Ювелирные броши", "brand": "КОЮЗ Топаз"}}) as fetch:
            rows = fr.build_rows("2026-09", "2026-09", None, sb=mock.Mock(), rows=ROWS, costs=COSTS, ads_by_day=None, products={})
        fetch.assert_called_once()
        self.assertEqual(sorted(fetch.call_args[0][1]), [1, 2, 3])
        r3 = [r for r in rows if r["nm_id"] == 3][0]
        self.assertEqual((r3["category"], r3["brand"]), ("Броши", "КОЮЗ Топаз"))

    def test_date_to_cuts_the_period(self):
        rows = fr.build_rows("2026-09", "2026-09", "2026-09-01", sb=object(), rows=ROWS, costs=COSTS, ads_by_day=ADS, products=PRODUCTS)
        self.assertEqual({r["date"] for r in rows}, {"2026-09-01"})


class FormTests(unittest.TestCase):
    def test_month_sheet_identities_of_the_sample(self):
        rows = fr.build_rows("2026-09", "2026-09", None, sb=object(), rows=ROWS, costs=COSTS, ads_by_day=ADS, products=PRODUCTS)
        sheet = fr.build_month_sheet(rows)
        self.assertEqual([r["label"] for r in sheet], ["сен", "Итого"])
        t = sheet[-1]
        vat = t["vat"]
        self.assertEqual((t["k_turnover"], t["l_commission"], t["o_cogs"]), (t["sales"], t["commission"], t["cogs"]))
        self.assertEqual(t["n_revenue"], (t["sales"] - t["commission"]) / vat)
        self.assertEqual(t["p_margin"], t["n_revenue"] - t["cogs"])
        self.assertEqual(t["r_logistics"], (t["logistics"] + t["storage"]) / vat)
        self.assertEqual(t["t_ads"], t["ads"] / vat)
        self.assertEqual(t["x_other"], t["other"] / vat)
        self.assertEqual(t["v_acquiring"], t["acquiring"] / vat)
        self.assertEqual(t["y_fin"], t["p_margin"] - t["r_logistics"] - t["t_ads"] - t["v_acquiring"] - t["x_other"])
        self.assertEqual(t["m_commission_pct"], fr.ratio(t["commission"], t["sales"]))
        self.assertEqual(t["z_fin_pct"], fr.ratio(t["y_fin"], t["n_revenue"]))
        self.assertEqual((t["sales"], t["storage"], t["ads"]), (D("2400"), D("61"), D("304")))

    def test_splits_sum_to_the_total(self):
        rows = fr.build_rows("2026-09", "2026-09", None, sb=object(), rows=ROWS, costs=COSTS, ads_by_day=ADS, products=PRODUCTS)
        total = fr.build_month_sheet(rows)[-1]
        for by in ("brand", "category"):
            split = fr.build_split(rows, by)
            groups = {r["group"] for r in split}
            totals = [r for r in split if r["label"] == "Итого"]
            self.assertEqual(len(totals), len(groups))
            self.assertEqual(sum((r["sales"] for r in totals), D(0)), total["sales"])
            self.assertEqual(fr.q(sum((r["y_fin"] for r in totals), D(0))), fr.q(total["y_fin"]))
        self.assertEqual({r["group"] for r in fr.build_split(rows, "brand")}, {"KARATOV", "КОЮЗ Топаз", "(без товара)"})
        with self.assertRaises(ValueError):
            fr.build_split(rows, "shop")

    def test_contract_columns_present(self):
        keys = {k for _h, k, _f in fr.SHEET_COLS if k}
        for k in ("sales", "commission", "cogs", "ads", "storage", "logistics", "other", "k_turnover", "l_commission", "m_commission_pct", "n_revenue",
                  "o_cogs", "p_margin", "q_margin_pct", "r_logistics", "s_logistics_pct", "t_ads", "u_drr_pct", "v_acquiring", "w_acquiring_pct", "x_other", "y_fin", "z_fin_pct"):
            self.assertIn(k, keys)
        self.assertEqual(tuple(h for h, _k, _f in fr.DATA_COLS[:15]), fr.OWNER_DATA_FIELDS)     # 15 полей кэша владельца слово в слово
        self.assertEqual(fr.OWNER_DATA_FIELDS, ("Дата", "Месяц", "Магазин", "Артикул поставщика", "Продажи, ₽", "Реклама, ₽", "Комиссия, ₽", "Логистика, ₽",
                                                "Себес-ть, ₽", "Хранение, ₽", "Ост.расходы и компенсации МП, ₽", "Наименование", "Бренд", "Статус", "Месяцы"))
        self.assertEqual([k for _h, k, _f in fr.DATA_COLS[:15]], ["date", "month", "shop", "article", "sales", "ads", "commission", "logistics", "cogs", "storage", "other", "title", "brand", "category", "month_no"])
        self.assertEqual([h for h, _k, _f in fr.DATA_COLS[15:19]], ["nmId", "Эквайринг, ₽", "Соинвест, ₽", "Штуки"])


class OrdersRowsTests(unittest.TestCase):
    def test_orders_rows_use_the_owner_multiplier_and_snapshot_cost(self):
        funnel = [{"day": "2026-09-01", "nm_id": 1, "vendor_code": "F000283615", "title": "Серьги", "brand": "KARATOV", "subject_name": "Ювелирные серьги",
                   "order_count": 2, "order_sum": 2000, "buyout_count": 1, "buyout_sum": 900, "cancel_count": 0, "cancel_sum": 0},
                  {"day": "2026-09-01", "nm_id": 2, "vendor_code": "t000000001", "title": "К", "brand": "КОЮЗ Топаз", "subject_name": "Ювелирные кольца",
                   "order_count": 1, "order_sum": 500, "buyout_count": 0, "buyout_sum": 0, "cancel_count": 1, "cancel_sum": 500}]
        rows = fr.orders_rows_for_finrez("2026-09-01", "2026-09-01", sb=object(), funnel_rows=funnel, costs=COSTS)
        r1, r2 = rows
        vat = fr.wbm.vat_for("2026-09-01")
        self.assertEqual((r1["orders_qty"], r1["orders_sum"], r1["revenue"], r1["cogs"], r1["category"], r1["brand"]), (2, D("2000"), D("2000") * D("0.58") / vat, D("220"), "Серьги", "KARATOV"))
        self.assertEqual((r2["cogs"], r2["no_cost"], r2["category"], r2["funnel_cancel_qty"]), (None, True, "Кольца", 1))


if __name__ == "__main__":
    unittest.main()


class KeysetAndOrdersTests(unittest.TestCase):
    class Sb:
        """Таблица из 25 строк (day, nm_id); отдаёт страницы по ключу, как PostgREST с фильтром or(day.gt, and(day.eq, nm_id.gt))."""
        def __init__(self):
            self.rows = [{"day": d, "nm_id": n, "vendor_code": f"F{n}", "title": "t", "brand": "KARATOV", "subject_name": "Ювелирные кольца"} for d in ("2026-09-01", "2026-09-02") for n in range(1, 13)] + [{"day": "2026-09-03", "nm_id": 1, "vendor_code": "F1", "title": "t3", "brand": "KARATOV", "subject_name": "Ювелирные серьги"}]
            self.calls = []

        def table(self, _name):
            sb = self

            class Q:
                def __init__(self):
                    self.after, self.limit_n, self.d1, self.d2 = None, None, None, None

                def select(self, *_a): return self
                def gte(self, _c, v): self.d1 = v; return self
                def lte(self, _c, v): self.d2 = v; return self
                def order(self, *_a): return self
                def limit(self, n): self.limit_n = n; return self
                def or_(self, expr):
                    import re
                    m = re.match(r"day\.gt\.(\S+?),and\(day\.eq\.(\S+?),nm_id\.gt\.(\d+)\)", expr)
                    self.after = (m.group(1), int(m.group(3))); return self
                def execute(self):
                    rows = [r for r in sb.rows if self.d1 <= r["day"] <= self.d2]
                    if self.after:
                        rows = [r for r in rows if (r["day"], r["nm_id"]) > self.after]
                    rows = sorted(rows, key=lambda r: (r["day"], r["nm_id"]))[: self.limit_n]
                    sb.calls.append((self.after, len(rows)))
                    return type("R", (), {"data": rows})()
            return Q()

    def test_keyset_pages_walk_the_window_by_primary_key(self):
        sb = self.Sb()
        rows = fr.read_keyset(sb, "t", "day,nm_id", "2026-09-01", "2026-09-02", page=10)
        self.assertEqual(len(rows), 24)
        self.assertEqual([c[1] for c in sb.calls], [10, 10, 4])
        self.assertEqual(sb.calls[1][0], ("2026-09-01", 10))
        with mock.patch.object(fr, "DICT_PAGE", 10):
            products = fr.load_product_dictionary(sb, "2026-09-01", "2026-09-03")
        self.assertEqual(len(products), 12)
        self.assertEqual(products[1]["subject"], "Ювелирные серьги")            # более поздний день побеждает
        self.assertEqual(fr.load_product_dictionary(sb), {})                      # без окна словарь не читается

    def test_orders_rows_drop_empty_cards_keep_sums_and_carry_nm_ads(self):
        funnel = [{"day": "2026-09-01", "nm_id": n, "vendor_code": f"F00000948{n}", "title": "t", "brand": "KARATOV", "subject_name": "Ювелирные кольца",
                   "order_count": (2 if n == 1 else 0), "order_sum": (2000 if n == 1 else 0), "buyout_count": 0, "buyout_sum": 0, "cancel_count": 0, "cancel_sum": 0} for n in (1, 2, 3)]
        stats = {}
        rows = fr.orders_rows_for_finrez("2026-09-01", "2026-09-01", sb=object(), funnel_rows=funnel, costs=COSTS,
                                         ads_by_day={"2026-09-01": D("100")}, ads_nm_by_day={"2026-09-01": {1: D("30"), 3: D("10")}}, stats=stats)
        self.assertEqual([r["nm_id"] for r in rows], [1, 3])                     # 2 — пустая карточка, 3 — без заказов, но с рекламой
        self.assertEqual((rows[0]["ads"], rows[1]["ads"], rows[1]["orders_qty"], rows[1]["orders_sum"]), (D("75.00"), D("25.00"), 0, D("0")))
        self.assertEqual((stats["rows_before"], stats["rows_after"], stats["equal"]), (3, 2, True))
        self.assertEqual(stats["sum_before"], {"2026-09": D("2000")}); self.assertEqual(stats["sum_after"], {"2026-09": D("2000")})
        self.assertEqual(rows[0]["month_no"], 9)
        all_rows = fr.orders_rows_for_finrez("2026-09-01", "2026-09-01", sb=object(), funnel_rows=funnel, costs=COSTS, ads_by_day={}, ads_nm_by_day={}, keep_empty=True)
        self.assertEqual(len(all_rows), 3)
