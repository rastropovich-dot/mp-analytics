"""57-я: хранение Ozon читается по дням (без глубокого смещения), непрочитанный источник не затирает свою колонку нулём."""
import unittest
from decimal import Decimal
from unittest import mock

import reports_daily_sku_kpi as kpi


class _Q:
    CAP = 1000

    def __init__(self, rows, log):
        self.rows, self.log, self.f, self.o, self.span, self.lim = rows, log, [], [], None, None

    def select(self, *_): return self
    def eq(self, c, v): self.f.append((c, v)); return self
    def order(self, c, desc=False): self.o.append((c, desc)); return self
    def range(self, a, b): self.span = (a, b); return self
    def limit(self, n): self.lim = n; return self

    def execute(self):
        out = [r for r in self.rows if all(str(r[c]) == str(v) for c, v in self.f)]
        for c, d in reversed(self.o):
            out.sort(key=lambda r: r[c], reverse=d)
        if self.span:
            if self.span[0] > 50:      # «глубокое смещение» — живая база отвечает 57014
                raise AssertionError(f"глубокое смещение {self.span[0]}")
            out = out[self.span[0]:self.span[1] + 1]
        if self.lim:
            out = out[:self.lim]
        self.log.append((tuple(self.f), self.span))
        class R: pass
        r = R(); r.data = out[:self.CAP]
        return r


class _SB:
    def __init__(self, rows): self.rows, self.log = rows, []
    def table(self, _): return _Q(self.rows, self.log)


def _rows():
    out = []
    for d, n in (("2026-04-01", 25), ("2026-04-02", 0), ("2026-04-03", 7)):
        for i in range(n):
            out.append({"day": d, "sku": 1000 + i, "warehouse": "W" + str(i % 3), "amount": -0.5, "offer_id": f"A{i}"})
    return out


class PlacementByDayTests(unittest.TestCase):
    def test_reads_every_day_with_shallow_pages_and_folds_to_day_sku(self):
        rows = _rows() + [{"day": "2026-04-01", "sku": 1000, "warehouse": "Z", "amount": "-1.00", "offer_id": "A0"}]   # второй склад того же SKU
        sb = _SB(rows)
        got, totals, skipped = kpi.read_placement_by_day(sb=sb, page=10)
        self.assertEqual(len(got), 32)                                    # пары (день, SKU), не строки по складам
        by = {(r["day"], r["sku"]): r["amount"] for r in got}
        self.assertEqual(by[("2026-04-01", "1000")], Decimal("-1.50"))    # Σ по складам
        self.assertEqual(totals, {"2026-04-01": Decimal("13.50"), "2026-04-03": Decimal("3.50")})
        self.assertEqual(skipped, [0, Decimal(0)])
        day_queries = [f for f, span in sb.log if span is not None]
        self.assertTrue(all(f and f[0][0] == "day" for f in day_queries))
        self.assertEqual(sum(1 for f in day_queries if f[0][1] == "2026-04-01"), 3)   # 26 строк, страницы по 10

    def test_keep_filter_drops_pairs_but_day_totals_count_everything(self):
        got, totals, skipped = kpi.read_placement_by_day(sb=_SB(_rows()), page=10, keep=lambda d, sku: d == "2026-04-03" or sku == "1000")
        self.assertEqual(len(got), 8)                                     # 1 пара 01.04 + 7 пар 03.04
        self.assertEqual(totals["2026-04-01"], Decimal("12.50"))          # итог дня — по всем SKU
        self.assertEqual(skipped, [24, Decimal("12.00")])

    def test_empty_table(self):
        self.assertEqual(kpi.read_placement_by_day(sb=_SB([]), page=10), ([], {}, [0, Decimal(0)]))


def _kpi(mp, sku, storage, ad=1.0):
    row = kpi.empty_kpi_row("2026-10-25", mp, sku)
    row["storage_cost"], row["ad_spend"] = storage, ad
    return row


class KeepColumnsTests(unittest.TestCase):
    def test_unread_ozon_placement_drops_storage_only_from_ozon_rows(self):
        rows = [_kpi("ozon", "1", 0.0), _kpi("ozon", "2", 0.0), _kpi("wb", "9", 5.0)]
        batches = kpi.upsert_batches(rows, [kpi.PLACEMENT_TABLE])
        by_mp = {b[0][0]["marketplace_code"]: b for b in batches}
        self.assertNotIn("storage_cost", by_mp["ozon"][0][0]); self.assertEqual(by_mp["ozon"][1], {"storage_cost"})
        self.assertIn("ad_spend", by_mp["ozon"][0][0])
        self.assertEqual(by_mp["wb"][0][0]["storage_cost"], 5.0); self.assertEqual(by_mp["wb"][1], set())
        for batch, _ in batches:     # PostgREST: одинаковые ключи внутри пачки
            self.assertEqual(len({tuple(sorted(r)) for r in batch}), 1)

    def test_unread_wb_sources_keep_their_wb_columns(self):
        rows = [_kpi("ozon", "1", 3.0), _kpi("wb", "9", 5.0)]
        batches = kpi.upsert_batches(rows, [kpi.WB_ADS_TABLE, kpi.WB_REPORT_TABLE])
        wb = [b for b in batches if b[0][0]["marketplace_code"] == "wb"][0]
        self.assertEqual(wb[1], {"ad_spend", "other_expenses_amount"})
        oz = [b for b in batches if b[0][0]["marketplace_code"] == "ozon"][0]
        self.assertEqual(oz[1], set()); self.assertEqual(oz[0][0]["storage_cost"], 3.0)

    def test_no_failures_one_shape(self):
        rows = [_kpi("ozon", str(i), 1.0) for i in range(1200)]
        batches = kpi.upsert_batches(rows, [])
        self.assertEqual([len(b) for b, _ in batches], [500, 500, 200])

    def test_save_kpi_sends_batches_without_the_column(self):
        sent = []
        table = mock.MagicMock()
        table.upsert.side_effect = lambda batch, on_conflict: (sent.append(batch), mock.MagicMock())[1]
        with mock.patch.object(kpi.supabase, "table", return_value=table), mock.patch("builtins.print"):
            kpi.save_kpi([_kpi("ozon", "1", 0.0), _kpi("wb", "9", 5.0)], failures=[kpi.PLACEMENT_TABLE])
        shapes = {b[0]["marketplace_code"]: ("storage_cost" in b[0]) for b in sent}
        self.assertEqual(shapes, {"ozon": False, "wb": True})


if __name__ == "__main__":
    unittest.main()
