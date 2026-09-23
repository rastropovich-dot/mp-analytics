"""--check-orders: лист «Заказы» против листа владельца — одна обязанная колонка (реклама по образцу, дни старше двух суток),
остальное печатается с отношением его/наша, фин. рез. раскладывается так, что сумма частей равна итогу."""
import importlib.util
import os
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("report_ozon_month", os.path.join(ROOT, "scripts", "report_ozon_month.py"))
rep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rep)
D = Decimal


def row(day, created="2440", cogs_created="600", ads="100", ads_manual="130", fc_a="1500", commission="600", revenue="700", cogs="300", other="60"):
    r = {"date": day, "vat": D("1.22"), "created_a": D(created), "cogs_created": D(cogs_created), "ads": D(ads), "ads_manual": D(ads_manual),
         "fc_a": D(fc_a), "commission": D(commission), "revenue": D(revenue), "cogs": D(cogs), "other": D(other)}
    r["margin"] = r["revenue"] - r["cogs"]
    r["fin_result"] = r["margin"] - r["ads"] - r["other"]
    r["owner_revenue"] = r["created_a"] * D("0.59") / D("1.22")
    r["owner_margin"] = r["owner_revenue"] - r["cogs_created"]
    r["owner_fin_result"] = r["owner_margin"] * D("0.65") - r["ads_manual"] - r["owner_revenue"] * D("0.65") * D("0.024")
    return r


def his(revenue, margin, ads, fin=None):
    m = {"revenue": D(revenue), "margin": D(margin), "ads": D(ads)}
    m["fin_result"] = D(fin) if fin is not None else m["margin"] * D("0.65") - m["ads"] - m["revenue"] * D("0.65") * D("0.024")
    return m


class CheckOrders(unittest.TestCase):
    def test_ads_must_match_only_on_days_older_than_two_days(self):
        rows = [row("2026-09-10"), row("2026-09-21", ads_manual="0")]
        manual = {"2026-09-10": his("1000", "400", "130"), "2026-09-21": his("1000", "400", "326887.47")}
        table, failures, _ = rep.check_orders(rows, manual, {}, {}, young_days={"2026-09-21"})
        ads_line = next(t for t in table if t["title"].startswith("Реклама E"))
        self.assertEqual(failures, 0)                                       # 09-21 моложе двух суток — не отказ
        self.assertEqual([d for d, _v in ads_line["bad"]], ["2026-09-21"])
        table, failures, _ = rep.check_orders([row("2026-09-10", ads_manual="129")], {"2026-09-10": his("1000", "400", "130")}, {}, {}, set())
        self.assertEqual(failures, 1)                                       # копейка на старом дне — отказ

    def test_decompositions_add_up_to_the_totals(self):
        rows = [row("2026-09-10"), row("2026-09-11", created="3660", cogs_created="900", ads_manual="200", fc_a="2000", revenue="900", cogs="400")]
        manual = {"2026-09-10": his("1000", "400", "130"), "2026-09-11": his("1600", "700", "190")}
        table, _f, dec = rep.check_orders(rows, manual, {}, {}, set())
        for key in ("owner", "forecast"):
            parts, n = dec[key]
            self.assertEqual(n, 2)
            self.assertEqual(sum((v for k, v in parts.items() if not k.startswith("итого")), D(0)), parts["итого наш − его"])
        owner_line = next(t for t in table if t["title"].startswith("Фин. рез. I vs Фин. рез. по формулам"))
        self.assertEqual(owner_line["diff"], dec["owner"][0]["итого наш − его"].quantize(D("0.01")))
        self.assertAlmostEqual(float(dec["implied"]["выкупаемость прогноза"]), 3500 / 6100, places=6)

    def test_revenue_line_carries_the_ratio_per_day_and_platform_lines(self):
        rows = [row("2026-09-10")]
        manual = {"2026-09-10": his("1000", "400", "130")}
        platform_rows = {"Основная": [dict(row("2026-09-10"), owner_revenue=D("900"))], "Селект": [dict(row("2026-09-10"), owner_revenue=D("100"))]}
        manual_platforms = {"Основная": {"2026-09-10": {"revenue": D("810"), "margin": D("300")}}, "Селект": {"2026-09-10": {"revenue": D("100"), "margin": D("20")}}}
        table, _f, _dec = rep.check_orders(rows, manual, platform_rows, manual_platforms, set())
        rev = next(t for t in table if t["title"].startswith("Выручка B"))
        self.assertEqual(rev["ratios"], [("2026-09-10", (D("1000") / rows[0]["owner_revenue"].quantize(D("0.01"))))])
        main = next(t for t in table if "Основная" in t["title"])
        self.assertEqual(main["ratios"][0][1], D("0.9"))
        sel = next(t for t in table if "Селект" in t["title"])
        self.assertEqual(sel["ratios"][0][1], D("1"))


if __name__ == "__main__":
    unittest.main()


class CostIndexAndUtc(unittest.TestCase):
    def test_cost_index_has_an_effective_date_and_reference_columns(self):
        self.assertIsNone(rep.cost_index_for("2026-08-31")); self.assertEqual(rep.cost_index_for("2026-09-01"), D("1.150"))
        day = "2026-09-10"
        rows, _ = rep.build_daily([day], [{"buyout_date": day, "marketplace_sku": "11", "buyouts_qty": 1, "buyouts_amount_seller": "1220", "commission_amount": "0"}], [],
                                  {day: {41: D("61"), 32: D("122")}}, lambda sku: D(300), "2026-09-25")
        r = rows[0]
        self.assertEqual(r["cogs_index"], D(300) * D("1.150"))
        self.assertEqual(r["fin_result_index"], r["fin_result"] - D(300) * D("0.150"))          # разница только в СС
        self.assertEqual(r["ebitda_index"], r["fin_result_index"] - D("311527.00"))
        old = rep.build_daily(["2026-08-31"], [{"buyout_date": "2026-08-31", "marketplace_sku": "11", "buyouts_qty": 1, "buyouts_amount_seller": "1220", "commission_amount": "0"}], [],
                              {"2026-08-31": {32: D("122")}}, lambda sku: D(300), "2026-09-25")[0][0]
        self.assertIsNone(old["cogs_index"]); self.assertIsNone(old["fin_result_index"])

    def test_check_reports_the_cost_index_by_day_and_flags_a_stale_parameter(self):
        rows = [dict(date="2026-09-10", cogs=D(1000), turnover=D(0), commission=D(0), revenue=D(0), acquiring=D(0), ads_like_manual=D(0), compensations=D(0),
                     log_other_like_manual=None, fin_result_with_comp=None, ebitda_with_comp=None, logistics=None, other=None, ads=None),
                dict(date="2026-09-11", cogs=D(1000), turnover=D(0), commission=D(0), revenue=D(0), acquiring=D(0), ads_like_manual=D(0), compensations=D(0),
                     log_other_like_manual=None, fin_result_with_comp=None, ebitda_with_comp=None, logistics=None, other=None, ads=None)]
        manual = {d: {"turnover": D(0), "commission": D(0), "revenue": D(0), "acquiring": D(0), "ads": D(0), "cogs": c, "logistics": None, "other": None, "fin_result": None, "ebitda": None}
                  for d, c in (("2026-09-10", D(1175)), ("2026-09-11", D(1125)))}
        table, _f = rep.check(rows, manual)
        cogs = next(t for t in table if t["title"].startswith("Себестоимость"))
        self.assertEqual(cogs["index_days"], [("2026-09-10", D("1.175")), ("2026-09-11", D("1.125"))])
        self.assertEqual((cogs["index_total"], cogs["index_param"], cogs["index_stale"]), (D("1.150"), D("1.150"), False))
        manual["2026-09-11"]["cogs"] = D(1300)                                                         # итог 1,2375 — дальше 3 % от 1,150
        cogs = next(t for t in rep.check(rows, manual)[0] if t["title"].startswith("Себестоимость"))
        self.assertTrue(cogs["index_stale"])

    def test_utc_day_table_uses_raw_postings_and_the_owner_multiplier(self):
        postings = {"fbo": [{"created_at": "2026-09-10T22:30:00Z", "products": [{"offer_id": "F1", "price": "1220", "quantity": 1}]},      # 22:30 UTC = 11-е по МСК, 10-е по UTC
                            {"created_at": "2026-09-10T10:00:00Z", "products": [{"offer_id": "T2", "price": "1000", "quantity": 2}]}],
                    "fbs": [{"in_process_at": "2026-09-10T12:00:00Z", "products": [{"offer_id": "S3", "price": "500", "quantity": 1}]}]}
        created = rep.created_by_utc_day(postings)
        self.assertEqual(created, {("2026-09-10", "Основная"): D(1220), ("2026-09-10", "Дискаунтер"): D(2000), ("2026-09-10", "Селект"): D(500)})
        manual = {"Основная": {"2026-09-10": {"revenue": D(1220) * D("0.53") / D("1.22")}}, "Селект": {"2026-09-10": {"revenue": D(500) * D("0.90") / D("1.22")}},
                  "Дискаунтер": {"2026-09-10": {"revenue": D(2000) * D("0.60") / D("1.22")}}}
        rows = rep.check_orders_utc(manual, created, ["2026-09-10"])
        by = {name: (r, exp) for name, _d, _b, _c, r, exp in rows}
        self.assertEqual(by["Основная"], (D("0.530"), D("0.53"))); self.assertEqual(by["Селект"], (D("0.900"), D("0.90")))
        self.assertEqual(by["Дискаунтер"], (D("0.600"), D("0.53")))                                       # отклонение — печатается как отклонение
