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
