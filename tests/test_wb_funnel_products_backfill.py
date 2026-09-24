"""Бэкфилл воронки по товарам: файл есть — в API не идёт; план из файлов; запись только с одобрением."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest import mock

import scripts.wb_funnel_products_backfill as bf


def card(nm, vendor, orders, order_sum, buyout_sum=0):
    return {"product": {"nmId": nm, "vendorCode": vendor, "title": "x", "brandName": "b", "subjectId": 1, "subjectName": "s",
                        "stocks": {"wb": 1, "mp": 0, "balanceSum": 10}},
            "statistic": {"selected": {"orderCount": orders, "orderSum": order_sum, "buyoutCount": 0, "buyoutSum": buyout_sum,
                                       "cancelCount": 0, "cancelSum": 0, "avgPrice": 1, "openCount": 1, "cartCount": 1, "addToWishlist": 0}}}


def payload(day, products, complete=True):
    return {"day": day, "fetched_at_utc": "2026-09-24T09:10:00+00:00", "pages": 1, "complete": complete, "dup_nm_ids": 0,
            "orders_qty": sum(p["statistic"]["selected"]["orderCount"] for p in products),
            "orders_amount": sum(p["statistic"]["selected"]["orderSum"] for p in products), "products": products}


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = self.tmp.name
        self.p_raw = mock.patch.object(bf, "RAW_DIR", self.raw)
        self.p_calls = mock.patch.object(bf, "CALLS_PATH", os.path.join(self.raw, "calls.json"))
        self.p_recheck = mock.patch.object(bf, "RECHECK_DIR", os.path.join(self.raw, "none"))
        self.p_raw.start(); self.p_calls.start(); self.p_recheck.start()

    def tearDown(self):
        self.p_raw.stop(); self.p_calls.stop(); self.p_recheck.stop(); self.tmp.cleanup()

    def test_fetch_skips_existing_files_and_writes_new_ones_with_a_ledger(self):
        json.dump(payload("2026-09-01", [card(1, "f1", 1, 100)]), open(os.path.join(self.raw, "funnel_2026-09-01.json"), "w"))
        fetched = {"order_date": "2026-09-02", "marketplace_code": "wb", "orders_qty": 2.0, "orders_amount": 300.0, "source": "wb_sales_funnel",
                   "products_count": 2, "products": [card(1, "f1", 1, 100), card(2, "t1", 1, 200)], "pages": 1, "complete": True, "dup_nm_ids": 0}
        sleeps = []
        with mock.patch.object(bf.funnel, "fetch_wb_sales_funnel_day", return_value=fetched) as fetch, \
             mock.patch.object(bf, "in_nightly_run_window", return_value=False), redirect_stdout(io.StringIO()):
            code = bf.fetch("2026-09-01", "2026-09-02", sleep_fn=sleeps.append)
        self.assertEqual(code, 0)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args[0][0], "2026-09-02")
        self.assertEqual(sleeps, [])                       # первый настоящий запрос — без паузы
        saved = json.load(open(os.path.join(self.raw, "funnel_2026-09-02.json")))
        self.assertEqual((saved["day"], saved["pages"], saved["complete"], len(saved["products"])), ("2026-09-02", 1, True, 2))
        ledger = json.load(open(os.path.join(self.raw, "calls.json")))
        self.assertEqual([(e["day"], e["cards"]) for e in ledger], [("2026-09-02", 2)])

    def test_fetch_stops_in_the_nightly_window(self):
        with mock.patch.object(bf, "in_nightly_run_window", return_value=True), \
             mock.patch.object(bf.funnel, "fetch_wb_sales_funnel_day") as fetch, redirect_stdout(io.StringIO()):
            code = bf.fetch("2026-09-01", "2026-09-01")
        self.assertEqual(code, 3); fetch.assert_not_called()

    def test_plan_reads_files_and_splits_by_letter(self):
        json.dump(payload("2026-09-01", [card(1, "F000000001", 3, 300), card(2, "t000000002", 1, 50), card(3, "ц1", 0, 0)]),
                  open(os.path.join(self.raw, "funnel_2026-09-01.json"), "w"))
        s = bf.day_summary(bf.load_files("2026-09-01", "2026-09-01")["2026-09-01"]["products"])
        self.assertEqual((s["cards"], s["qty"], s["sum"]), (3, Decimal(4), Decimal(350)))
        self.assertEqual((s["by_letter"]["t"]["sum"], s["by_letter"]["f"]["sum"], s["by_letter"]["ц"]["cards"]), (Decimal(50), Decimal(300), 1))
        out = io.StringIO()
        with mock.patch.object(bf, "read_summaries", return_value={"2026-09-01": {"order_date": "2026-09-01", "orders_qty": 4, "orders_amount": 400}}), redirect_stdout(out):
            days = bf.plan(object(), bf.load_files("2026-09-01", "2026-09-01"))
        self.assertEqual(days, ["2026-09-01"])
        self.assertIn("db_writes = 0", out.getvalue())
        self.assertIn("-50", out.getvalue())                # сводка в базе 400 против сырья 350 — пересмотр виден

    def test_apply_requires_approval(self):
        json.dump(payload("2026-09-01", [card(1, "f1", 1, 100)]), open(os.path.join(self.raw, "funnel_2026-09-01.json"), "w"))
        with mock.patch.object(bf, "read_summaries", return_value={}), mock.patch.object(bf, "apply") as apply, \
             mock.patch.object(bf.funnel, "supabase", object()), redirect_stdout(io.StringIO()):
            code = bf.main(["--apply", "--date-from", "2026-09-01", "--date-to", "2026-09-01"])
        self.assertEqual(code, 2); apply.assert_not_called()

    def test_apply_writes_summary_then_products_per_day(self):
        files = {"2026-09-01": payload("2026-09-01", [card(1, "f1", 1, 100)]), "2026-09-02": payload("2026-09-02", [card(2, "t1", 2, 200)], complete=False)}
        calls = []
        with mock.patch.object(bf.funnel, "save_day", side_effect=lambda row, sb: calls.append(("day", row["order_date"]))), \
             mock.patch.object(bf.funnel, "save_products", side_effect=lambda sb, day, rows, complete, apply: (calls.append(("products", day, len(rows), complete)), (len(rows), 0))[1]), \
             redirect_stdout(io.StringIO()):
            written, deleted = bf.apply(object(), files)
        self.assertEqual((written, deleted), (2, 0))
        self.assertEqual(calls, [("day", "2026-09-01"), ("products", "2026-09-01", 1, True), ("day", "2026-09-02"), ("products", "2026-09-02", 1, False)])


if __name__ == "__main__":
    unittest.main()
