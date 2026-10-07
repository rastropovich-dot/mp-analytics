"""WB-17 §2: себестоимость WB по дате продажи через историю снимков 1С (loaders/unit_cost_history) без новых строк в
article_unit_costs — ключ WB (идентификатор без размера + tech_size) → Ozon-ключ той же строки 1С (база-размер,-суффикс);
лестница ключа exact → size → size_nosuffix → uniform_id → uniform_base; снимок ≤ даты, иначе ближайший более поздний, после
последнего — последний; источник — в строку «Данных» и заказов; старые пары карт (тесты, файлы) остаются одним снимком."""
import io
import re
import unittest
from contextlib import redirect_stdout
from decimal import Decimal

import scripts.report_finrez_wb as fr
from tests.test_report_finrez_wb import ADS, COSTS, PRODUCTS, ROWS

wbm = fr.wbm
D = Decimal

SNAPS = {
    "2026-04-20": {"f000078698-23,5": D("5000"), "f000078698-19,5": D("4000"), "f007753194-16,5-жм": D("11000"), "f000009483": D("50")},
    "2026-05-20": {"f000078698-23,5": D("5500"), "f007753194-16,5-жм": D("10000"), "f000009483": D("45"), "f000041046": D("700")},
}
UNI_ID = {"2026-04-20": {"f000078698": D("4500"), "f007753194-жм": D("10500"), "f000009483": D("50")},
          "2026-05-20": {"f000078698": D("5200"), "f007753194-жм": D("10100"), "f000009483": D("45"), "f000041046": D("700")}}
UNI_BASE = {"2026-04-20": {"f000078698": D("4500"), "f007753194": D("10500"), "f000009483": D("50")},
            "2026-05-20": {"f000078698": D("5200"), "f007753194": D("10100"), "f000009483": D("45"), "f000041046": D("700")}}


class KeyTests(unittest.TestCase):
    def test_wb_key_with_size_becomes_the_ozon_key_of_the_row(self):
        self.assertEqual(wbm.wb_size_key("f000078698", "23.5"), "f000078698-23,5")
        self.assertEqual(wbm.wb_size_key("f007753194-жм", "16,5"), "f007753194-16,5-жм")
        self.assertEqual(wbm.wb_size_key("f007753194-жм", "16.5", keep_suffix=False), "f007753194-16,5")
        self.assertEqual(wbm.wb_size_key("f000009483", "0"), "f000009483")
        self.assertEqual(wbm.wb_size_key("f000009483", None), "f000009483")

    def test_sizeless_key_drops_only_the_size_segment(self):
        self.assertEqual(wbm.sizeless_key("f000078698-23,5"), "f000078698")
        self.assertEqual(wbm.sizeless_key("f007753194-16,5-жм"), "f007753194-жм")
        self.assertEqual(wbm.sizeless_key("f000041046-хрл"), "f000041046-хрл")
        self.assertEqual(wbm.sizeless_key("f000009483"), "f000009483")


class LadderTests(unittest.TestCase):
    def setUp(self):
        self.h = wbm.WbCostHistory(SNAPS, UNI_ID, UNI_BASE)

    def test_exact_then_size_then_uniforms(self):
        self.assertEqual(self.h.lookup("F000009483", "0", "2026-06-01")[::3], (D("45"), "exact"))
        self.assertEqual(self.h.lookup("f000078698", "23.5", "2026-06-01")[::3], (D("5500"), "size"))
        self.assertEqual(self.h.lookup("F007753194-ЖМ", "16,5", "2026-06-01")[::3], (D("10000"), "size"))
        self.assertEqual(self.h.lookup("f000078698-тн", "23.5", "2026-06-01")[::3], (D("5500"), "size_nosuffix"))
        self.assertEqual(self.h.lookup("f000078698", "17", "2026-06-01")[::3], (D("5200"), "uniform_id"))       # размера 17 нет
        self.assertEqual(self.h.lookup("f007753194-жм", None, "2026-06-01")[::3], (D("10100"), "uniform_id"))
        self.assertEqual(self.h.lookup("f007753194-др", "99", "2026-06-01")[::3], (D("10100"), "uniform_base"))
        self.assertEqual(self.h.lookup("t000000001", "17", "2026-06-01"), (None, None, "none", "none"))
        self.assertEqual(self.h.lookup("", "17", "2026-06-01"), (None, None, "none", "none"))

    def test_snapshot_follows_the_sale_day(self):
        c, d, kind, step = self.h.lookup("f000078698", "23.5", "2026-04-25")
        self.assertEqual((c, d, kind, step), (D("5000"), "2026-04-20", "exact", "size"))     # снимок ≤ даты
        c, d, kind, step = self.h.lookup("f000078698", "19.5", "2026-05-25")
        self.assertEqual((c, d, kind, step), (D("4000"), "2026-04-20", "earlier", "size"))   # размер 19,5 только в раннем снимке
        c, d, kind, step = self.h.lookup("f000041046", None, "2026-04-25")
        self.assertEqual((c, d, kind, step), (D("700"), "2026-05-20", "later", "exact"))     # ключа нет в снимке ≤ даты — более поздний
        c, d, kind, step = self.h.lookup("f000078698", "23.5", "2026-09-15")
        self.assertEqual((c, d, kind, step), (D("5500"), "2026-05-20", "after", "size"))     # после последнего снимка — последний
        c, d, kind, step = self.h.lookup("f000078698", "23.5", None)
        self.assertEqual((c, d, kind, step), (D("5500"), "2026-05-20", "latest", "size"))

    def test_cost_source_counts_rows_and_rubles_and_labels(self):
        c, label, step = self.h.cost_source("f000078698", "23.5", "2026-04-25", qty=2)
        self.assertEqual((c, step), (D("5000"), "size"))
        self.assertEqual(label, "2026-04-20 · строка по размеру")
        c, label, step = self.h.cost_source("f000078698", "23.5", "2026-09-15", qty=1)
        self.assertEqual(label, "после последнего снимка 2026-05-20 · строка по размеру")
        self.assertEqual(self.h.cost_source("zzz", None, "2026-09-15"), (None, "нет снимка", "none"))
        self.assertEqual(self.h.counters, {"size": 2, "none": 1})
        self.assertEqual(self.h.rubles["size"], D("15500"))
        note = self.h.note()
        self.assertIn("снимки 2026-04-20, 2026-05-20", note)
        self.assertIn("строка по размеру: 2 строк, 15,500.00 ₽", note)
        self.assertIn("после последнего снимка", note)

    def test_cost_fn_contract_and_legacy_two_argument_callers(self):
        self.assertEqual(self.h.cost_fn("f000078698", "23.5", "2026-04-25"), (D("5000"), "size"))
        self.assertEqual(wbm._cost_call(self.h.cost_fn, "f000078698", "23.5", "2026-04-25"), (D("5000"), "size"))
        self.assertEqual(wbm._cost_call(lambda c, s: (D("1"), "plain"), "x", "1", "2026-04-25"), (D("1"), "plain"))

    def test_maps_of_one_snapshot_become_a_one_date_history(self):
        h = wbm.as_cost_history(({"f000283615-17,5": D("100")}, {"f000283615": D("110")}), snapshot="2026-05-20")
        self.assertEqual(h.dates, ["2026-05-20"])
        self.assertEqual(h.lookup("f000283615", "17,5", "2026-07-01")[::3], (D("100"), "size"))
        self.assertEqual(h.lookup("f000283615", "19", "2026-07-01")[::3], (D("110"), "uniform_id"))
        self.assertIs(wbm.as_cost_history(h), h)
        self.assertEqual(wbm.as_cost_history(None).lookup("f1", None, "2026-07-01"), (None, None, "none", "none"))


class FakeCosts:
    """PostgREST над строками article_unit_costs: select snapshot_date + gt/order/limit (даты снимков), eq snapshot_date + or_(eq/like по
    базам) + gt offer_id_norm + order + limit (страницы по ключу)."""

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def table(self, name):
        assert name == "article_unit_costs", name
        sb = self

        class Q:
            def __init__(self):
                self.sel, self.expr, self.eqs, self.gts, self.lim, self.rng, self.orders = None, None, [], [], None, None, []

            def select(self, cols): self.sel = cols; return self
            def eq(self, c, v): self.eqs.append((c, v)); return self
            def or_(self, expr): self.expr = expr; return self
            def gt(self, c, v): self.gts.append((c, v)); return self
            def order(self, c, **_k): self.orders.append(c); return self
            def limit(self, n): self.lim = n; return self
            def range(self, a, b): self.rng = (a, b); return self

            def execute(self):
                sb.calls.append({"select": self.sel, "eq": list(self.eqs), "or": self.expr, "gt": list(self.gts), "limit": self.lim, "order": list(self.orders)})
                rows = [r for r in sb.rows if all(str(r.get(c)) == str(v) for c, v in self.eqs) and all(str(r.get(c)) > str(v) for c, v in self.gts)]
                if self.expr is not None:
                    alts = re.findall(r'offer_id_norm\.(eq|like)\."([^"]*)"', self.expr)
                    rows = [r for r in rows if any((op == "eq" and r["offer_id_norm"] == pat) or (op == "like" and r["offer_id_norm"].startswith(pat[:-1]))
                                                   for op, pat in alts)]
                rows.sort(key=lambda r: tuple(str(r[c]) for c in self.orders))
                if self.rng is not None:
                    a, b = self.rng
                    rows = rows[a:b + 1]
                if self.lim is not None:
                    rows = rows[:self.lim]
                return type("R", (), {"data": [dict(r) for r in rows]})()
        return Q()


ROWS_DB = [
    {"offer_id_norm": "f000078698-23,5", "snapshot_date": "2026-04-20", "unit_cost": "5000", "unit_cost_uniform": "4500"},
    {"offer_id_norm": "f000078698-23,5", "snapshot_date": "2026-05-20", "unit_cost": "5500", "unit_cost_uniform": "5200"},
    {"offer_id_norm": "f000078698-19,5", "snapshot_date": "2026-04-20", "unit_cost": "4000", "unit_cost_uniform": "4500"},
    {"offer_id_norm": "f007753194-16,5-жм", "snapshot_date": "2026-05-20", "unit_cost": "10000", "unit_cost_uniform": "10100"},
    {"offer_id_norm": "f009999999", "snapshot_date": "2026-05-20", "unit_cost": "1", "unit_cost_uniform": "1"},
]


class LoadHistoryTests(unittest.TestCase):
    def test_all_snapshots_of_the_bases_are_read_with_full_key_order(self):
        sb = FakeCosts(ROWS_DB)
        stats = {}
        h = wbm.load_cost_history_for(sb, ["F000078698", "f007753194-жм"], stats=stats)
        self.assertEqual(stats["mode"], "narrow")
        self.assertEqual(stats["snapshots"], ["2026-04-20", "2026-05-20"])
        self.assertEqual(stats["rows"], 4)                                                # f009999999 — чужая база, не читалась
        self.assertEqual([c["select"] for c in sb.calls[:3]], ["snapshot_date"] * 3)           # даты снимков: 2 даты + пустой ответ
        self.assertEqual(sorted({c["eq"][0][1] for c in sb.calls if c["eq"]}), ["2026-04-20", "2026-05-20"])   # по одному снимку за запрос
        self.assertEqual(stats["requests"], 3 + 2)
        self.assertEqual(h.lookup("f000078698", "23.5", "2026-04-25")[::3], (D("5000"), "size"))
        self.assertEqual(h.lookup("f000078698", "23.5", "2026-06-01")[::3], (D("5500"), "size"))
        self.assertEqual(h.lookup("f000078698", "17", "2026-06-01")[::3], (D("5200"), "uniform_id"))
        self.assertEqual(h.lookup("f007753194-жм", "16.5", "2026-04-25")[1:], ("2026-05-20", "later", "size"))

    def test_pages_by_key_inside_a_snapshot(self):
        sb = FakeCosts(ROWS_DB)
        h = wbm.load_cost_history_for(sb, ["f000078698"], page=1, dates=["2026-04-20", "2026-05-20"])
        gts = [c["gt"] for c in sb.calls if c["gt"]]
        self.assertTrue(any(g[0][0] == "offer_id_norm" for g in gts))                       # страница по ключу внутри снимка
        self.assertEqual(sorted(h.exact.snaps["2026-04-20"]), ["f000078698-19,5", "f000078698-23,5"])
        self.assertEqual(h.exact.snaps["2026-05-20"], {"f000078698-23,5": D("5500")})


class ModuleRowsTests(unittest.TestCase):
    def test_data_rows_carry_cost_and_its_source_by_sale_day(self):
        h = wbm.WbCostHistory({"2026-05-20": {"f000283615-17,5": D("100"), "f000009483": D("50")}, "2026-09-02": {"f000283615-17,5": D("130"), "f000009483": D("60")}},
                              {"2026-05-20": {"f000283615": D("110")}, "2026-09-02": {"f000283615": D("140")}},
                              {"2026-05-20": {"f000283615": D("110"), "f000009483": D("50")}, "2026-09-02": {"f000283615": D("140"), "f000009483": D("60")}})
        with redirect_stdout(io.StringIO()):
            rows = fr.build_rows("2026-09", "2026-09", None, sb=object(), rows=ROWS, costs=h, ads_by_day=ADS, products=PRODUCTS)
        by = {(r["date"], r["nm_id"]): r for r in rows}
        r1 = by[("2026-09-01", 1)]
        self.assertEqual(r1["cost_source"], "2026-05-20 · строка по размеру")                      # 09-01 < 09-02 — снимок 05-20
        self.assertTrue(all("cost_source" in r for r in rows))
        self.assertEqual([c for c in fr.DATA_COLS if c[1] == "cost_source"][0][0], "СС источник")

    def test_legacy_maps_keep_one_snapshot_and_sum_as_before(self):
        with redirect_stdout(io.StringIO()):
            rows_new = fr.build_rows("2026-09", "2026-09", None, sb=object(), rows=ROWS, costs=COSTS, ads_by_day=ADS, products=PRODUCTS)
        total = sum((r["cogs"] for r in rows_new), D(0))
        self.assertGreater(total, 0)
        self.assertTrue(all(r["cost_source"] == "" or "2026-05-20" in r["cost_source"] for r in rows_new))

    def test_orders_rows_carry_source_and_db_maps_are_replaced_by_history_only_with_a_client(self):
        funnel = [{"day": "2026-09-01", "nm_id": 1, "vendor_code": "F000283615", "order_count": 2, "order_sum": "1000", "buyout_count": 0, "buyout_sum": "0",
                   "cancel_count": 0, "cancel_sum": "0", "subject_name": "Ювелирные серьги", "brand": "KARATOV"}]
        with redirect_stdout(io.StringIO()):
            rows = fr.orders_rows_for_finrez("2026-09-01", "2026-09-01", sb=object(), funnel_rows=funnel, costs=COSTS, ads_by_day={}, ads_nm_by_day={})
        self.assertEqual(rows[0]["cogs"], D("220"))                                                  # «единая» 110 × 2
        self.assertIn("2026-05-20", rows[0]["cost_source"])
        maps = wbm.SnapshotMaps(COSTS)
        maps.snapshot = "2026-05-20"
        with redirect_stdout(io.StringIO()):
            rows = fr.orders_rows_for_finrez("2026-09-01", "2026-09-01", sb=object(), funnel_rows=funnel, costs=maps, ads_by_day={}, ads_nm_by_day={})
        self.assertEqual(rows[0]["cogs"], D("220"))                                                  # без клиента — те же карты
        out = io.StringIO()
        sb = FakeCosts([{"offer_id_norm": "f000283615-17,5", "snapshot_date": "2026-08-01", "unit_cost": "300", "unit_cost_uniform": "250"}])
        with redirect_stdout(out):
            rows = fr.orders_rows_for_finrez("2026-09-01", "2026-09-01", sb=sb, funnel_rows=funnel, costs=maps, ads_by_day={}, ads_nm_by_day={})
        self.assertEqual(rows[0]["cogs"], D("500"))                                                  # история из базы: «единая» 250 × 2
        self.assertIn("заменены историей снимков", out.getvalue())
        self.assertEqual(rows[0]["cost_source"], "после последнего снимка 2026-08-01 · «единая» по идентификатору")


if __name__ == "__main__":
    unittest.main()
