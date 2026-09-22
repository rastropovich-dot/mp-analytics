"""Лист «Заказы»: кривая дозревания из лога статусов и прогноз подтверждённых без зашитых долей.

Правила, которые закрепляют тесты: ночью считается только сбор до 06:00 UTC (дневной посев несёт состояние, но
срезом не служит); доля считается по парам СОСЕДНИХ ночей внутри когорты; дозревший день (≥ 21 суток) — факт,
прогноз обязан равняться подтверждённому; комиссия — измеренная доля площадки товара; незнакомое называется вслух.
"""
import importlib.util
import os
import sys
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ozon_orders_forecast as fc  # noqa: E402

_spec = importlib.util.spec_from_file_location("report_ozon_month", os.path.join(ROOT, "scripts", "report_ozon_month.py"))
rep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rep)

D = Decimal


def log(number, observed_at, status, order_date, amount="1000", schema="fbo"):
    return {"posting_number": number, "observed_at": observed_at, "schema": schema, "order_date": order_date, "status": status, "amount": amount}


def order(day, schema="fbo", conf_q=1, conf_a="1000", canc_q=0, canc_a="0", sku="11", article="F1"):
    return {"order_date": day, "order_schema": schema, "marketplace_sku": sku, "article": article, "orders_qty": conf_q,
            "orders_amount_seller": conf_a, "cancelled_orders_qty": canc_q, "cancelled_orders_amount_seller": canc_a}


class ParseAndSmooth(unittest.TestCase):
    def test_timestamp_with_five_digit_fraction_is_parsed(self):
        # PostgREST отдаёт дробную часть любой длины; python 3.9 читает только 3 или 6 знаков
        ts = fc.parse_ts("2026-09-17T00:20:52.40194+00:00")
        self.assertEqual((ts.hour, ts.minute, ts.microsecond), (0, 20, 401940))
        self.assertEqual(fc.parse_ts("2026-09-14 11:59:00+00").hour, 11)

    def test_garbage_timestamp_fails_loudly(self):
        with self.assertRaises(ValueError):
            fc.parse_ts("вчера")

    def test_pava_merges_violators_and_fills_unobserved_points(self):
        self.assertEqual(fc.pava([D(1), D(3), D(2), D(4)], [D(1), D(1), D(1), D(1)]), [D(1), D("2.5"), D("2.5"), D(4)])
        # точка без наблюдений берёт значение соседа, а не ноль: ноль посреди кривой раздул бы прогноз отмен
        self.assertEqual(fc.pava([D(1), D(0), D(2)], [D(1), D(0), D(1)]), [D(1), D(1), D(2)])


class Curve(unittest.TestCase):
    def rows(self):
        # две соседние ночи 09-18 и 09-19; заказы 09-17 (возраст 1 → 2). Из четырёх живых на 09-18 одно отменено к 09-19.
        rows = [log(f"p{i}", "2026-09-18T00:23:00+00:00", "awaiting_deliver", "2026-09-17", amount=a) for i, a in enumerate(("1000", "1000", "1000", "3000"))]
        rows.append(log("p0", "2026-09-19T00:23:00+00:00", "cancelled", "2026-09-17", amount="1000"))
        rows.append(log("p1", "2026-09-19T00:23:00+00:00", "delivering", "2026-09-17", amount="1000"))
        return rows

    def test_hazard_is_cancelled_by_next_night_over_alive(self):
        c = fc.build_curve(self.rows())["fbo"]
        self.assertEqual((c["nights"], c["pairs"]), (["2026-09-18", "2026-09-19"], 1))
        self.assertEqual(c["alive"][1], (4, D(6000)))
        self.assertEqual(c["events"][1], (1, D(1000)))
        self.assertEqual(c["h_cnt"][1], D("0.25"))
        self.assertEqual(c["h_amt"][1], D(1000) / D(6000))           # рублёвая доля ниже штучной: уцелел дорогой заказ
        self.assertEqual(c["r_cnt"][1], D("0.25"))                    # других переходов нет — всё, что ещё отменится, это он
        self.assertEqual(c["r_cnt"][2], 0)
        self.assertEqual(c["biggest_event"][:2], (1, D(1000)))

    def test_already_cancelled_posting_is_not_alive(self):
        rows = self.rows() + [log("dead", "2026-09-18T00:23:00+00:00", "cancelled", "2026-09-17")]
        self.assertEqual(fc.build_curve(rows)["fbo"]["alive"][1][0], 4)

    def test_daytime_seed_carries_state_but_is_not_a_night(self):
        # посев снят днём 09-15: ночью не считается, но отправление из него живо на ночь 09-18
        rows = self.rows() + [log("seed", "2026-09-15T18:59:11+00:00", "delivering", "2026-09-10")]
        c = fc.build_curve(rows)["fbo"]
        self.assertEqual(c["nights"], ["2026-09-18", "2026-09-19"])
        self.assertEqual(c["alive"][8], (1, D(1000)))

    def test_fbs_nights_start_later_and_gap_between_nights_gives_no_pair(self):
        rows = [log("s1", "2026-09-17T00:18:00+00:00", "awaiting_deliver", "2026-09-16", schema="fbs"),
                log("s2", "2026-09-18T00:20:00+00:00", "awaiting_deliver", "2026-09-17", schema="fbs"),
                log("s3", "2026-09-20T00:20:00+00:00", "awaiting_deliver", "2026-09-19", schema="fbs")]
        # 09-17 у FBS — старое окно 14 дней, не ночь; 09-18 и 09-20 не соседи — пары нет, кривой по схеме нет
        self.assertNotIn("fbs", fc.build_curve(rows))

    def test_remaining_share_rules(self):
        curve = fc.build_curve(self.rows())
        self.assertEqual(fc.remaining_share(curve, "fbo", 21, "amt"), 0)      # дозревший день — факт
        self.assertEqual(fc.remaining_share(curve, "fbo", 400, "cnt"), 0)
        self.assertIsNone(fc.remaining_share(curve, "fbs", 3, "amt"))          # кривой нет — не ноль, а «неизвестно»
        self.assertEqual(fc.remaining_share(curve, "fbo", 1, "cnt"), D("0.25"))


class OrdersSheet(unittest.TestCase):
    CURVE = {"fbo": {"r_cnt": {a: D("0.2") for a in range(21)}, "r_amt": {a: D("0.1") for a in range(21)}}}
    VAT = staticmethod(lambda d: D("1.22"))

    def build(self, orders, days, curve=None, shares=None, other=D("0.05"), ads=None, cost=lambda sku: D(300)):
        return fc.build_orders_daily(days, orders, self.CURVE if curve is None else curve, "2026-09-21", shares or {"Основная": D("0.4"), "все": D("0.3")},
                                     other, ads or {}, cost, self.VAT, lambda r: "Основная" if str(r.get("article") or "").startswith("F") else "Селект")

    def test_mature_day_is_fact_and_young_day_is_forecast(self):
        blocks, said = self.build([order("2026-08-31", conf_q=2, conf_a="2000", canc_q=1, canc_a="500"), order("2026-09-20", conf_q=10, conf_a="10000")],
                                  ["2026-08-31", "2026-09-20"], ads={"2026-08-31": D(100), "2026-09-20": D(200)})
        old, young = blocks["all"]
        self.assertTrue(old["mature"]); self.assertEqual((old["age"], old["fc_a"], old["fc_q"], old["expected_cancels_a"]), (21, D(2000), D(2), D(0)))
        self.assertEqual((old["created_a"], old["created_q"]), (D(2500), D(3)))
        self.assertFalse(young["mature"])
        self.assertEqual((young["fc_a"], young["fc_q"], young["expected_cancels_a"]), (D(9000), D(8), D(1000)))   # рубли — рублёвой долей, штуки — штучной
        self.assertEqual(young["commission"], D(3600))
        self.assertEqual(young["revenue"], D(9000) * D("0.6") / D("1.22"))
        self.assertEqual(young["cogs"], D(8) * D(300))
        self.assertEqual(young["other"], D(9000) * D("0.05") / D("1.22"))
        self.assertEqual(young["fin_result"], young["revenue"] - young["cogs"] - D(200) - young["other"])
        self.assertEqual(fc.mature_check(blocks["all"]), (1, 1, []))
        self.assertEqual(said, [])

    def test_owner_formulas_on_his_constants(self):
        blocks, _ = self.build([order("2026-08-31", conf_q=1, conf_a="610", canc_q=1, canc_a="610")], ["2026-08-31"], ads={"2026-08-31": D(10)})
        r = fc.add_order_ratios(blocks["all"][0])
        self.assertEqual(r["owner_revenue"], D(1220) * D("0.59") / D("1.22"))                                    # создано × 0,59 / НДС
        self.assertEqual(r["owner_margin"], r["owner_revenue"] - D(600))                                           # СС — по созданным штукам
        self.assertEqual(r["owner_fin_result"], r["owner_margin"] * D("0.65") - D(10) - r["owner_revenue"] * D("0.65") * D("0.024"))
        self.assertEqual(r["owner_drr_pct"], D(10) / (D(1220) * D("0.65")))                                       # = Реклама / (создано × 0,65)

    def test_platform_without_buyouts_takes_common_share_and_says_so(self):
        blocks, said = self.build([order("2026-09-20", article="S9")], ["2026-09-20"])
        self.assertEqual(blocks["all"][0]["commission"], D(900) * D("0.3"))
        self.assertTrue(any("Селект" in x and "общая доля" in x for x in said))

    def test_schema_without_curve_gives_empty_forecast_not_zero(self):
        blocks, said = self.build([order("2026-09-20", schema="fbs")], ["2026-09-20"])
        self.assertIsNone(blocks["fbs"][0]["fc_a"]); self.assertIsNone(blocks["all"][0]["fc_a"]); self.assertIsNone(blocks["all"][0]["fin_result"])
        self.assertEqual(blocks["fbs"][0]["conf_a"], D(1000))                 # факт при этом на месте
        self.assertTrue(any("fbs" in x and "кривой нет" in x for x in said))
        self.assertIsNone(fc.orders_total(blocks["all"])["fc_a"])             # пустая колонка в итоге пуста, а не занижена

    def test_unknown_schema_is_named_not_swallowed(self):
        blocks, said = self.build([order("2026-09-20", schema="rfbs")], ["2026-09-20"])
        self.assertEqual(blocks["all"][0]["created_a"], 0)
        self.assertTrue(any("rfbs" in x for x in said))

    def test_ads_and_fin_result_live_only_on_the_common_sheet(self):
        blocks, _ = self.build([order("2026-09-20")], ["2026-09-20"], ads={"2026-09-20": D(50)})
        self.assertEqual(blocks["all"][0]["ads"], D(50)); self.assertIsNone(blocks["fbo"][0]["ads"]); self.assertIsNone(blocks["fbo"][0]["fin_result"])

    def test_stale_schema_gets_its_own_age_and_is_named(self):
        # ночью упал шаг FBS: его состояние на сутки старше, день 08-31 для него ещё не дозрел (20 суток), а для FBO дозрел
        curve = {**self.CURVE, "fbs": self.CURVE["fbo"]}
        blocks, said = fc.build_orders_daily(["2026-08-31"], [order("2026-08-31"), order("2026-08-31", schema="fbs")], curve,
                                             {"fbo": "2026-09-21", "fbs": "2026-09-20"}, {"Основная": D("0.4"), "все": D("0.3")}, D("0.05"), {},
                                             lambda sku: D(300), self.VAT, lambda r: "Основная")
        self.assertEqual((blocks["fbo"][0]["age"], blocks["fbo"][0]["fc_a"]), (21, D(1000)))
        self.assertEqual((blocks["fbs"][0]["age"], blocks["fbs"][0]["fc_a"]), (20, D(900)))
        self.assertTrue(any("разной свежести" in x for x in said))
        self.assertFalse(blocks["all"][0]["mature"])                          # дозрел не у обеих — на общем листе это ещё прогноз
        self.assertEqual(fc.mature_check(blocks["all"]), (0, 0, []))

    def test_total_sums_money_and_takes_percents_from_sums(self):
        blocks, _ = self.build([order("2026-09-19", conf_a="1000"), order("2026-09-20", conf_a="3000")], ["2026-09-19", "2026-09-20"],
                               ads={"2026-09-19": D(10), "2026-09-20": D(30)})
        rows = [fc.add_order_ratios(r) for r in blocks["all"]]
        t = fc.orders_total(rows)
        self.assertEqual((t["created_a"], t["fc_a"], t["ads"]), (D(4000), D(3600), D(40)))
        self.assertEqual(t["drr_created_pct"], D(40) / (D(4000) / D("1.22")))
        self.assertEqual(t["drr_fc_pct"], D(40) / (D(3600) / D("1.22")))


class Shares(unittest.TestCase):
    def test_commission_share_by_platform_with_common_fallback(self):
        rows = [{"marketplace_sku": "1", "commission_amount": "400", "buyouts_amount_seller": "1000"},
                {"marketplace_sku": "2", "commission_amount": "80", "buyouts_amount_seller": "1000"}]
        share, base = fc.shares_by_platform(rows, lambda sku: "Основная" if sku == "1" else "Селект")
        self.assertEqual((share["Основная"], share["Селект"], share["все"]), (D("0.4"), D("0.08"), D("0.24")))
        self.assertEqual(base["все"], D(2000))

    def test_other_share_skips_ads_and_compensations_names_unknown_and_matches_days(self):
        ledger = {"2026-09-10": {32: D(30), 1: D(10), 51: D(5), 41: D(999), 25: D(-777), 12345: D(5)}}
        buyouts = [{"buyout_date": "2026-09-10", "buyouts_amount_seller": "1000"}, {"buyout_date": "2026-09-11", "buyouts_amount_seller": "9000"}]
        share, expense, turnover, unknown = rep.other_share_of(ledger, buyouts)
        self.assertEqual((expense, turnover, share), (D(50), D(1000), D("0.05")))      # 09-11 нет в леджере — его оборот в базу не идёт
        self.assertEqual(unknown, [12345])

    def test_window_is_thirty_days_inclusive(self):
        self.assertEqual(fc.window_before("2026-09-20"), ("2026-08-22", "2026-09-20"))


if __name__ == "__main__":
    unittest.main()


class OwnerAdsAndPlatforms(unittest.TestCase):
    """Тридцать первая: формулы владельца — на его рекламе («по образцу»), блоки по площадкам — те же колонки без рекламы."""
    CURVE = {"fbo": {"r_cnt": {a: D("0.2") for a in range(21)}, "r_amt": {a: D("0.1") for a in range(21)}}}
    VAT = staticmethod(lambda d: D("1.22"))

    def build(self, orders, ads=None, ads_manual=None):
        return fc.build_orders_daily(["2026-09-20"], orders, self.CURVE, "2026-09-21", {"Основная": D("0.4"), "Селект": D("0.1"), "все": D("0.3")},
                                     D("0.05"), ads or {}, lambda sku: D(300), self.VAT,
                                     lambda r: "Селект" if str(r.get("article") or "").startswith("S") else "Основная", ads_manual)

    def test_owner_formulas_use_the_owner_style_ads_and_the_model_keeps_41_54(self):
        blocks, _ = self.build([order("2026-09-20", conf_a="1220", canc_a="1220")], ads={"2026-09-20": D(100)}, ads_manual={"2026-09-20": D(130)})
        r = fc.add_order_ratios(blocks["all"][0])
        self.assertEqual((r["ads"], r["ads_manual"]), (D(100), D(130)))
        self.assertEqual(r["fin_result"], r["margin"] - D(100) - r["other"])                                   # модель — 41 + 54
        self.assertEqual(r["owner_fin_result"], r["owner_margin"] * D("0.65") - D(130) - r["owner_revenue"] * D("0.65") * D("0.024"))
        self.assertEqual(r["owner_drr_pct"], D(130) / (D(2440) * D("0.65")))                                    # ДРР владельца — от его рекламы

    def test_without_owner_ads_the_model_ads_are_used(self):
        blocks, _ = self.build([order("2026-09-20")], ads={"2026-09-20": D(100)})
        self.assertEqual(blocks["all"][0]["ads_manual"], D(100))

    def test_platform_blocks_split_the_common_sheet_without_ads(self):
        blocks, _ = self.build([order("2026-09-20", conf_a="1000", article="F1"), order("2026-09-20", conf_a="500", article="S2", sku="22")],
                               ads={"2026-09-20": D(50)})
        main, sel = blocks["platform:Основная"][0], blocks["platform:Селект"][0]
        self.assertEqual((main["created_a"], sel["created_a"]), (D(1000), D(500)))
        self.assertEqual(main["created_a"] + sel["created_a"], blocks["all"][0]["created_a"])
        self.assertEqual(sel["commission"], D(450) * D("0.1"))                                                   # своя доля комиссии
        self.assertIsNone(main["ads"]); self.assertIsNone(main["fin_result"])
        self.assertEqual(main["owner_revenue"], D(1000) * D("0.59") / D("1.22"))
        self.assertIn("ads_manual", fc.ORDER_MONEY)
