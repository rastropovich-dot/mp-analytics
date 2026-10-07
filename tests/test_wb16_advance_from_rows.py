"""WB-16 §1: аванс «Баллы за отзывы» берётся из строк отчёта, которые лист уже прочитал, — отдельного чтения всей истории
(seq scan, флапавший 57014) больше нет; возврат в окне без аванса дочитывается по индексу rr_date не глубже
ADVANCE_LOOKBACK_DAYS; без аванса нетто дня в «Прочее» не идёт и называется вслух; отказ чтения в сборке книги — оговорка
в finrez_nightly:last и «⚠️ … с оговоркой» в строке алерта."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal

import alerts_telegram as at
import scripts.finrez_nightly_status as st
import scripts.report_finrez_wb as fr
from loaders import wb_money_rules as rules
from tests.test_wb_money_rules import ADVANCE, ADVANCE_ROWS, COSTS, RAW, REFUND, SPLIT, rrow

wbm = fr.wbm
D = Decimal
NOW = datetime(2026, 10, 7, 7, 30, tzinfo=timezone.utc)
ADVANCES = [r for r in ADVANCE_ROWS if r["bonus_type_name"] == ADVANCE]
REFUNDS = [r for r in ADVANCE_ROWS if r["bonus_type_name"] == REFUND]


class FakeSB:
    """Supabase-клиент для дочитки: отдаёт строки аванса, запоминает фильтры (границы дат обязаны быть)."""

    def __init__(self, rows):
        self.rows, self.filters = rows, []

    def table(self, _name):
        return self

    def select(self, _cols):
        self.filters = []
        return self

    def __getattr__(self, name):
        if name in ("gte", "lte", "eq", "like", "order"):
            def f(*args):
                self.filters.append((name, *args))
                return self
            return f
        raise AttributeError(name)

    def range(self, _a, _b):
        return self

    def execute(self):
        return type("R", (), {"data": list(self.rows)})()


class RulesTests(unittest.TestCase):
    def test_review_points_rows_and_refunds_without_advance(self):
        rows = RAW + ADVANCES
        self.assertEqual(sorted(r["rrd_id"] for r in rules.review_points_rows(rows)), [4, 5, 10, 11])
        net, _open = rules.review_advance_net(REFUNDS, lambda r: r["rr_date"])
        self.assertEqual(rules.refunds_without_advance(net), {"2026-07-22": D("-6430620")})
        net, _open = rules.review_advance_net(ADVANCE_ROWS, lambda r: r["rr_date"])
        self.assertEqual(rules.refunds_without_advance(net), {})


class AdvanceFromRowsTests(unittest.TestCase):
    def test_pair_inside_the_window_needs_no_read(self):
        info = {}
        sb = FakeSB([])
        net, open_ = wbm.advance_net_by_day(sb, RAW + ADVANCES, "2026-07-01", info=info)
        self.assertEqual((net, open_), ({"2026-07-22": D("40260")}, D("0")))
        self.assertEqual(info["in_window"], 4)
        self.assertNotIn("lookback", info)
        self.assertEqual(info["missing"], {})
        self.assertEqual(sb.filters, [])                                          # в базу не ходили

    def test_refund_without_advance_reads_the_advance_before_the_window_by_rr_date(self):
        info = {}
        sb = FakeSB(ADVANCES)
        net, open_ = wbm.advance_net_by_day(sb, RAW, "2026-07-01", info=info)
        self.assertEqual((net, open_), ({"2026-07-22": D("40260")}, D("0")))
        self.assertEqual(info["lookback"], ("2026-03-03", "2026-06-30", 2))       # 120 дней до окна, по индексу rr_date
        self.assertEqual(info["missing"], {})
        kinds = [f[0] for f in sb.filters]
        self.assertEqual(kinds[:2], ["gte", "lte"])                                # границы дат стоят первыми
        self.assertIn(("gte", "rr_date", "2026-03-03"), sb.filters)
        self.assertIn(("lte", "rr_date", "2026-06-30"), sb.filters)

    def test_refund_without_advance_and_without_client_is_named_not_booked(self):
        info = {}
        net, _open = wbm.advance_net_by_day(None, RAW, "2026-07-01", info=info)
        self.assertEqual(net, {})                                                  # отрицательное нетто в «Прочее» не идёт
        self.assertEqual(info["missing"], {"2026-07-22": D("-6430620")})
        text = wbm.advance_info_text(info)
        self.assertIn("возврат без аванса в окне", text)
        self.assertIn("2026-07-22 -6,430,620.00", text)
        self.assertIn("не добавлено", text)

    def test_lookback_that_finds_nothing_keeps_the_day_named(self):
        info = {}
        net, _open = wbm.advance_net_by_day(FakeSB([]), RAW, "2026-07-01", info=info)
        self.assertEqual(net, {})
        self.assertEqual(info["lookback"], ("2026-03-03", "2026-06-30", 0))
        self.assertEqual(info["missing"], {"2026-07-22": D("-6430620")})

    def test_no_rows_in_window_is_an_explicit_line_not_a_silent_zero(self):
        info = {}
        net, open_ = wbm.advance_net_by_day(FakeSB([]), [rrow("2026-08-01")], "2026-08-01", info=info)
        self.assertEqual((net, open_), ({}, D("0")))
        self.assertEqual(wbm.advance_info_text(info), "аванс «Баллы за отзывы»: строк аванса/возврата в окне нет — нетто 0,00")
        self.assertEqual(wbm.advance_info_text({}), "аванс «Баллы за отзывы»: не считался")

    def test_rows_are_required(self):
        with self.assertRaises(ValueError):
            wbm.advance_net_by_day(FakeSB([]))

    def test_bounded_reader_puts_dates_first(self):
        sb = FakeSB([])
        wbm.load_review_advance_rows(sb, "2026-03-03", "2026-06-30")
        self.assertEqual(sb.filters[:2], [("gte", "rr_date", "2026-03-03"), ("lte", "rr_date", "2026-06-30")])
        self.assertIn(("eq", "seller_oper_name", "Удержание"), sb.filters)
        self.assertIn(("like", "bonus_type_name", rules.REVIEW_POINTS_LIKE), sb.filters)


class ModuleRowsTests(unittest.TestCase):
    def build(self, rows, sb, stats):
        with redirect_stdout(io.StringIO()):
            return fr.build_rows("2026-07", "2026-07", None, sb=sb, rows=rows, costs=COSTS, ads_by_day=SPLIT["balance"], products={},
                                 ads_nm_by_day={}, stats=stats, ads_cashback_by_day=SPLIT["other"], cashback_nm_by_day={})

    def test_net_comes_from_the_rows_already_read(self):
        stats = {}
        rows = self.build(RAW + [dict(r, rr_date="2026-06-30", sale_dt="2026-06-30T10:00:00Z") for r in ADVANCES], object(), stats)
        by = {(r["date"], r["nm_id"]): r for r in rows}
        self.assertEqual(by[("2026-07-22", None)]["other"], D("42990") + D("40260"))    # Джем + нетто аванса; чтения не было
        self.assertEqual(stats["excluded"]["advance_net"], {"2026-07-22": D("40260")})
        self.assertEqual(stats["excluded"]["advance_info"]["in_window"], 4)

    def test_refund_without_advance_and_without_client_leaves_other_without_net(self):
        stats = {}
        rows = self.build(RAW, object(), stats)                                    # sb без .table — дочитки нет
        by = {(r["date"], r["nm_id"]): r for r in rows}
        self.assertEqual(by[("2026-07-22", None)]["other"], D("42990"))            # нетто −6 430 620 в «Прочее» не ушло
        self.assertEqual(stats["excluded"]["advance_net"], {})
        self.assertEqual(stats["excluded"]["advance_info"]["missing"], {"2026-07-22": D("-6430620")})

    def test_refund_without_advance_with_client_reads_only_the_lookback(self):
        stats = {}
        sb = FakeSB([dict(r, rr_date="2026-06-30", sale_dt="2026-06-30T10:00:00Z") for r in ADVANCES])
        rows = self.build(RAW, sb, stats)
        by = {(r["date"], r["nm_id"]): r for r in rows}
        self.assertEqual(by[("2026-07-22", None)]["other"], D("42990") + D("40260"))
        self.assertEqual(stats["excluded"]["advance_info"]["lookback"], ("2026-03-03", "2026-06-30", 2))

    def test_failed_lookback_is_said_aloud_and_the_book_goes_on(self):
        class Broken(FakeSB):
            def execute(self):
                raise RuntimeError("canceling statement due to statement timeout (57014)")
        stats, out = {}, io.StringIO()
        with redirect_stdout(out):
            rows = fr.build_rows("2026-07", "2026-07", None, sb=Broken([]), rows=RAW, costs=COSTS, ads_by_day=SPLIT["balance"], products={},
                                 ads_nm_by_day={}, stats=stats, ads_cashback_by_day=SPLIT["other"], cashback_nm_by_day={})
        self.assertIn("аванс «Баллы за отзывы» не прочитан (дочитка раньше окна)", out.getvalue())
        self.assertEqual({(r["date"], r["nm_id"]): r for r in rows}[("2026-07-22", None)]["other"], D("42990"))


class CaveatTests(unittest.TestCase):
    def test_caveats_are_read_from_the_build_log(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "finrez.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write("словарь товаров: wb_funnel_products_current — строк 5206\n"
                        "аванс «Баллы за отзывы» не прочитан: {'message': 'canceling statement due to statement timeout', 'code': '57014'} — нетто аванса в «Прочее» не добавлено\n"
                        "аванс «Баллы за отзывы» не прочитан: {'message': 'canceling statement due to statement timeout', 'code': '57014'} — нетто аванса в «Прочее» не добавлено\n"
                        "книга: data/reports/finrez_2026-04_2026-09.xlsx — 88499269 байт\n"
                        "реклама WB не прочитана: APIError\n")
            caveats = st.caveats_from_log(path)
            self.assertEqual(len(caveats), 2)                                       # повтор склеен
            self.assertTrue(caveats[0].startswith("аванс «Баллы за отзывы» не прочитан"))
            self.assertEqual(caveats[1], "реклама WB не прочитана: APIError")
            self.assertEqual(st.caveats_from_log(os.path.join(d, "none.log")), [])
            self.assertEqual(st.caveats_from_log(""), [])

    def test_alert_line_carries_the_caveat(self):
        ok = {"finished_at": "2026-10-07T03:25:40+00:00", "seconds": 1452, "bytes": 86145579, "sha256": "7538072dacfa3b63", "rc": 0, "copied": None}
        clean = at.finrez_book_line(ok, NOW)
        self.assertTrue(clean.startswith("книга Фин рез: собрана 03:25 UTC"))
        self.assertEqual(at.finrez_book_line({**ok, "caveats": []}, NOW), clean)
        line = at.finrez_book_line({**ok, "caveats": ["аванс «Баллы за отзывы» не прочитан: 57014 — нетто аванса в «Прочее» не добавлено"]}, NOW)
        self.assertTrue(line.startswith("⚠️ книга Фин рез: собрана 03:25 UTC"))
        self.assertIn("; с оговоркой: аванс «Баллы за отзывы» не прочитан: 57014", line)
        self.assertEqual(at.finrez_book_line({**ok, "rc": 1, "error": "код 1", "caveats": ["x"]}, NOW), "⚠️ книга Фин рез: сборка не удалась (код 1)")


if __name__ == "__main__":
    unittest.main()
