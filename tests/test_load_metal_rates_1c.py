"""Сорок восьмая §1: загрузчик курса 1С — план из csv истории (правка задним числом → последний по картинке, прежний в примечании), seen_to из
ll_rates.csv, сверка таблицы с csv; без --apply ничего не пишет."""
import os
import sys
import tempfile
import unittest
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import load_metal_rates_1c as L  # noqa: E402
from loaders import unit_cost_history as uch  # noqa: E402

HISTORY = """rate,set_date,value_1c,first_seen_img
gold,2026-05-20,6072.85,2026-05-22
gold,2026-09-29,6600.00,2026-09-30
silver,2026-05-20,162.00,2026-05-22
usd,2026-05-20,75.00,2026-05-22
usd,2026-05-20,74.00,2026-06-18
usd,2026-09-10,86.00,2026-09-11
kzt,2026-05-20,1900.00,2026-05-22
scrap,2026-05-20,10000.00,2026-05-22
"""
PICTURES = """img_date,gold_date_1c,gold_c1,silver_date_1c,silver_c1,usd_date_1c,usd_c1
2026-05-22,20.05.2026,6072.85,20.05.2026,162.00,20.05.2026,75.00
2026-06-18,20.05.2026,6072.85,20.05.2026,162.00,20.05.2026,74.00
2026-09-29,20.05.2026,6072.85,20.05.2026,162.00,10.09.2026,86.00
2026-10-06,29.09.2026,6600.00,20.05.2026,162.00,10.09.2026,86.00
"""


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.h = os.path.join(self.dir, "h.csv"); open(self.h, "w").write(HISTORY)
        self.p = os.path.join(self.dir, "p.csv"); open(self.p, "w").write(PICTURES)

    def test_plan_maps_metals_dedupes_by_last_picture_and_skips_kzt_scrap(self):
        rows, raw = L.plan_rows(self.h, self.p)
        self.assertEqual(raw, {"gold": 2, "silver": 1, "usd": 3, "kzt": 1, "scrap": 1})
        self.assertEqual([(r["metal"], r["set_date"], r["rate"]) for r in rows],
                         [("gold585", "2026-05-20", D("6072.85")), ("gold585", "2026-09-29", D("6600.00")), ("silver925", "2026-05-20", D("162.00")),
                          ("usd", "2026-05-20", D("74.00")), ("usd", "2026-09-10", D("86.00"))])
        usd = next(r for r in rows if r["metal"] == "usd" and r["set_date"] == "2026-05-20")
        self.assertEqual(usd["replaced"], [(D("75.00"), "2026-05-22")]); self.assertEqual(usd["seen_from"], "2026-06-18")
        text = L.plan_text(rows, raw, self.p)
        self.assertIn("2026-05-20: пишется 74,00 (картинка 2026-06-18), прежний 75,00 (картинка 2026-05-22) — правка задним числом", text.replace(".", ","))
        self.assertIn("kzt, scrap не грузятся", text)

    def test_seen_to_comes_from_the_last_picture_showing_the_rate(self):
        rows, _ = L.plan_rows(self.h, self.p)
        by = {(r["metal"], r["set_date"]): r for r in rows}
        self.assertEqual(by[("gold585", "2026-05-20")]["seen_to"], "2026-09-29")
        self.assertEqual(by[("gold585", "2026-09-29")]["seen_to"], "2026-10-06")
        self.assertEqual(by[("silver925", "2026-05-20")]["seen_to"], "2026-10-06")
        self.assertEqual(by[("usd", "2026-05-20")]["seen_to"], "2026-06-18")   # правка 74 видна только 18.06; 75 — 22.05
        rates = uch.MetalRates(rows)
        self.assertEqual(rates.last_picture, "2026-10-06")

    def test_without_pictures_seen_to_is_the_day_before_the_next_setting(self):
        rows, _ = L.plan_rows(self.h, None)
        by = {(r["metal"], r["set_date"]): r for r in rows}
        self.assertEqual(by[("gold585", "2026-05-20")]["seen_to"], "2026-09-29")   # первая картинка 29.09 = 30.09 − 1
        self.assertEqual(by[("gold585", "2026-09-29")]["seen_to"], "2026-09-30")   # последняя установка — её первая картинка
        self.assertIn("ll_rates.csv нет", L.plan_text(rows, {}, None))

    def test_compare_names_missing_differing_and_extra(self):
        rows, _ = L.plan_rows(self.h, self.p)
        table = {("gold585", "2026-05-20"): D("6072.85"), ("gold585", "2026-09-29"): D("6500.00"), ("usd", "2026-01-01"): D("1")}
        missing, differ, extra = L.compare(rows, table)
        self.assertEqual(missing, [("silver925", "2026-05-20"), ("usd", "2026-05-20"), ("usd", "2026-09-10")])
        self.assertEqual(differ, [(("gold585", "2026-09-29"), D("6500.00"), D("6600.00"))])
        self.assertEqual(extra, [("usd", "2026-01-01")])

    def test_db_payload_is_strings_and_keys_only(self):
        rows, _ = L.plan_rows(self.h, self.p)
        self.assertEqual(L.to_db(rows)[0], {"metal": "gold585", "set_date": "2026-05-20", "rate": "6072.85", "source": "ll_bot",
                                            "seen_from": "2026-05-22", "seen_to": "2026-09-29"})

    def test_plan_mode_without_apply_writes_nothing(self):
        calls = []
        L.make_client = lambda: calls.append("client")
        rc = L.main(["--history", self.h, "--pictures", self.p])
        self.assertEqual((rc, calls), (0, []))


if __name__ == "__main__":
    unittest.main()
