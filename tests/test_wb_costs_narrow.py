"""WB-12 §4: report_wb_month.load_costs_for — снимок 1С адресно по базам артикулов: тот же (exact, uniform), что у load_costs
для этих баз; пачки баз, страницы по ключу внутри пачки; фильтр or=(eq база, like база-*) на строчных базах."""
import re
import unittest
from decimal import Decimal

import scripts.report_finrez_wb as fr

wbm = fr.wbm   # тот же report_wb_month, что у модуля книги (scripts/ в sys.path добавляет он)


class FakeCosts:
    """PostgREST над списком строк article_unit_costs: eq / or_ (eq."x" и like."x-*") / gt / order / limit / range."""

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def table(self, name):
        assert name == "article_unit_costs", name
        sb = self

        class Q:
            def __init__(self):
                self.conds, self.expr, self.gt_v, self.lim, self.rng, self.orders = [], None, None, None, None, []

            def select(self, *_a): return self
            def eq(self, c, v): self.conds.append((c, v)); return self
            def or_(self, expr): self.expr = expr; return self
            def gt(self, c, v): self.gt_v = (c, v); return self
            def order(self, c, **_k): self.orders.append(c); return self
            def limit(self, n): self.lim = n; return self
            def range(self, a, b): self.rng = (a, b); return self

            def execute(self):
                sb.calls.append({"eq": list(self.conds), "or": self.expr, "gt": self.gt_v, "limit": self.lim, "range": self.rng})
                rows = [r for r in sb.rows if all(r.get(c) == v for c, v in self.conds)]
                if self.expr is not None:
                    alts = re.findall(r'offer_id_norm\.(eq|like)\."([^"]*)"', self.expr)
                    assert len(alts) * len('offer_id_norm.eq."",') <= len(self.expr) + 40   # ничего, кроме этих условий
                    def ok(v):
                        for op, pat in alts:
                            if op == "eq" and v == pat:
                                return True
                            if op == "like" and pat.endswith("*") and v.startswith(pat[:-1]):
                                return True
                        return False
                    rows = [r for r in rows if ok(r["offer_id_norm"])]
                if self.gt_v:
                    rows = [r for r in rows if r[self.gt_v[0]] > self.gt_v[1]]
                for c in reversed(self.orders):
                    rows.sort(key=lambda r: r[c])
                if self.rng is not None:
                    rows = rows[self.rng[0]: self.rng[1] + 1]
                elif self.lim is not None:
                    rows = rows[: self.lim]
                return type("R", (), {"data": rows})()
        return Q()


def row(code, cost, uni, snap="2026-05-20"):
    return {"offer_id_norm": code, "unit_cost": cost, "unit_cost_uniform": uni, "snapshot_date": snap}


ROWS = [
    row("f000000001", "10.00", "11.00"),
    row("f000000001-16", "10.50", "11.00"),
    row("f000000002-17", "20.00", "22.00"),
    row("f000000002-18", "20.50", "23.00"),          # у базы 2 «единая» разнится — первая по ключу побеждает (22.00)
    row("f0000000029", "99.00", "99.00"),            # чужая база с тем же префиксом — like база-* её не задевает
    row("t000000003", "30.00", None),                # без единой
    row("f000000004", "40.00", "44.00", snap="2026-01-01"),   # другой снимок — мимо
    row("f000000005", "50.00", "55.00"),             # база, которой в кодах нет
]


class LoadCostsForTests(unittest.TestCase):
    def test_bases_from_codes_lowercase_without_size(self):
        self.assertEqual(wbm.cost_bases(["F000000001-16", " f000000002 ", "", None, "T000000003-ЖМ"]), {"f000000001", "f000000002", "t000000003"})

    def test_same_result_as_full_read_for_the_asked_bases(self):
        sb = FakeCosts(ROWS)
        full_exact, full_uniform = wbm.load_costs(FakeCosts(ROWS))
        stats = {}
        exact, uniform = wbm.load_costs_for(sb, ["F000000001-16", "f000000002", "t000000003"], stats=stats)
        self.assertEqual(exact, {"f000000001": Decimal("10.00"), "f000000001-16": Decimal("10.50"), "f000000002-17": Decimal("20.00"),
                                 "f000000002-18": Decimal("20.50"), "t000000003": Decimal("30.00")})
        self.assertEqual(uniform, {"f000000001": Decimal("11.00"), "f000000002": Decimal("22.00")})
        for k, v in exact.items():
            self.assertEqual(full_exact[k], v)
        for k, v in uniform.items():
            self.assertEqual(full_uniform[k], v)
        self.assertNotIn("f0000000029", exact); self.assertNotIn("f000000005", exact); self.assertNotIn("f000000004", exact)
        self.assertEqual(stats, {"bases": 3, "rows": 5, "requests": 1, "mode": "narrow"})
        self.assertEqual(sb.calls[0]["eq"], [("snapshot_date", "2026-05-20")])
        self.assertEqual(sb.calls[0]["or"], 'offer_id_norm.eq."f000000001",offer_id_norm.like."f000000001-*",offer_id_norm.eq."f000000002",'
                                            'offer_id_norm.like."f000000002-*",offer_id_norm.eq."t000000003",offer_id_norm.like."t000000003-*"')

    def test_batches_of_bases_and_pages_inside_a_batch(self):
        sb = FakeCosts(ROWS)
        stats = {}
        exact, uniform = wbm.load_costs_for(sb, ["f000000001", "f000000002", "t000000003"], batch=2, page=2, stats=stats)
        self.assertEqual(len(exact), 5)
        self.assertEqual(stats["requests"], 4)                  # пачка 1: 4 строки → 2 + 2 + 0-я пустая? нет: 2, 2 (<page? нет) , затем 0 → 3; пачка 2: 1
        self.assertEqual([c["gt"] for c in sb.calls], [None, ("offer_id_norm", "f000000001-16"), ("offer_id_norm", "f000000002-18"), None])
        self.assertEqual(uniform["f000000002"], Decimal("22.00"))

    def test_unit_cost_for_is_unchanged_on_the_narrow_result(self):
        exact, uniform = wbm.load_costs_for(FakeCosts(ROWS), ["F000000002-18"])
        self.assertEqual(wbm.unit_cost_for(exact, uniform, "F000000002", None, "base"), (Decimal("22.00"), "uniform"))
        self.assertEqual(wbm.unit_cost_for(exact, uniform, "F000000002-18", "18", "variant"), (Decimal("20.50"), "plain"))

    def test_too_many_bases_read_the_snapshot_whole(self):
        sb = FakeCosts(ROWS)
        stats = {}
        exact, uniform = wbm.load_costs_for(sb, ["f000000001", "f000000002", "t000000003"], max_bases=2, stats=stats)
        self.assertEqual(stats["mode"], "full")
        self.assertEqual((stats["bases"], stats["rows"]), (3, 7))                       # весь снимок 05-20: 7 строк, включая чужие базы
        self.assertIn("f000000005", exact)
        self.assertEqual([c["or"] for c in sb.calls], [None])                          # одно чтение без or-фильтра, страницами range
        self.assertIsNotNone(sb.calls[0]["range"])
        full_exact, full_uniform = wbm.load_costs(FakeCosts(ROWS))
        self.assertEqual((exact, uniform), (full_exact, full_uniform))

    def test_no_codes_reads_nothing(self):
        sb = FakeCosts(ROWS)
        self.assertEqual(wbm.load_costs_for(sb, []), ({}, {}))
        self.assertEqual(sb.calls, [])


if __name__ == "__main__":
    unittest.main()
