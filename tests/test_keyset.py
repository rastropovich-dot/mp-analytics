"""loaders.keyset: страницы по (день, id) только индексными запросами — диапазон по дню и «тот же день, id >»."""
import unittest

from loaders import keyset


class FakeSb:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def table(self, _name):
        sb = self

        class Q:
            def __init__(self): self.conds, self.lim = [], None
            def select(self, *_a): return self
            def gte(self, c, v): self.conds.append(("gte", c, v)); return self
            def lte(self, c, v): self.conds.append(("lte", c, v)); return self
            def eq(self, c, v): self.conds.append(("eq", c, v)); return self
            def gt(self, c, v): self.conds.append(("gt", c, v)); return self
            def in_(self, c, v): self.conds.append(("in", c, list(v))); return self
            def order(self, *_a, **_k): return self
            def limit(self, n): self.lim = n; return self
            def execute(self):
                sb.calls.append(list(self.conds))
                rows = sb.rows
                for op, c, v in self.conds:
                    test = {"gte": lambda x: x >= v, "lte": lambda x: x <= v, "eq": lambda x: x == v, "gt": lambda x: x > v, "in": lambda x: x in v}[op]
                    rows = [r for r in rows if test(r[c])]
                rows = sorted(rows, key=lambda r: (r["day"], r["id"]))[: self.lim]
                return type("R", (), {"data": rows})()
        return Q()


class KeysetTests(unittest.TestCase):
    def test_full_page_continues_the_day_then_next_range_and_all_rows_come_once(self):
        rows = [{"day": "2026-09-01", "id": i, "op": "a"} for i in range(1, 13)] + [{"day": "2026-09-02", "id": i, "op": "a"} for i in range(1, 11)] + [{"day": "2026-09-03", "id": 1, "op": "b"}]
        sb = FakeSb(rows)
        out = keyset.read_keyset(sb, "t", "*", "2026-09-01", "2026-09-03", day_col="day", id_col="id", page=10)
        self.assertEqual([(r["day"], r["id"]) for r in out], [(r["day"], r["id"]) for r in rows])
        self.assertEqual(sb.calls, [
            [("gte", "day", "2026-09-01"), ("lte", "day", "2026-09-03")],       # диапазон: 10 строк дня 1
            [("eq", "day", "2026-09-01"), ("gt", "id", 10)],                    # тот же день: 2 строки — день исчерпан
            [("gte", "day", "2026-09-02"), ("lte", "day", "2026-09-03")],       # диапазон: ровно 10 строк дня 2 (полная страница)
            [("eq", "day", "2026-09-02"), ("gt", "id", 10)],                    # тот же день: 0 строк
            [("gte", "day", "2026-09-03"), ("lte", "day", "2026-09-03")],       # диапазон: 1 строка — конец
        ])

    def test_filters_apply_to_every_query_and_window_end_stops_before_the_next_day(self):
        rows = [{"day": "2026-09-01", "id": i, "op": "a" if i % 2 else "b"} for i in range(1, 8)] + [{"day": "2026-09-02", "id": 1, "op": "a"}]
        sb = FakeSb(rows)
        out = keyset.read_keyset(sb, "t", "*", "2026-09-01", "2026-09-01", day_col="day", id_col="id", page=2, filters=[("in_", "op", ["a"])])
        self.assertEqual([r["id"] for r in out], [1, 3, 5, 7])
        self.assertTrue(all(("in", "op", ["a"]) in c for c in sb.calls))
        self.assertEqual(sb.calls[-1], [("in", "op", ["a"]), ("eq", "day", "2026-09-01"), ("gt", "id", 7)])   # день исчерпан, следующий день за окном — стоп
        self.assertEqual(keyset.read_keyset(FakeSb([]), "t", "*", "2026-09-01", "2026-09-30", day_col="day", id_col="id"), [])


if __name__ == "__main__":
    unittest.main()
