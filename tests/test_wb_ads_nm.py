"""Реклама WB по номенклатурам (adv/v3/fullstats): пачки по 50, окно, строки, застрявшие, шаг и бэкфилл.

Держим: ключ (день, кампания, appType, nmId); дни вне окна не пишутся; nms без nmId считаются; повтор ключа — отказ;
429 повторяется и кончается именованной ошибкой; без списаний в окне — 0 обращений; dry-run не пишет; чистка застрявших
только при полном сборе; шаг «WB: реклама» зовёт загрузчик по номенклатурам после списаний; бэкфилл — оценка без
API, файл есть — не идёт, запись только с одобрением.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from unittest import mock

import loaders.wb_ads_nm_loader as nm
import loaders.wb_ads_loader as ads
import scripts.wb_ads_nm_backfill as bf

D = Decimal


def nm_item(nm_id, s="10.19", **extra):
    base = {"nmId": nm_id, "name": "постер", "views": 21, "clicks": 1, "ctr": 4.76, "cpc": 10.19, "sum": s, "atbs": 0, "orders": 0, "cr": 0, "shks": 0, "sum_price": 0, "canceled": 0}
    base.update(extra)
    return base


def answer(advert_id=1, days=("2026-07-01",), nms=(1,), app_types=(1, 32)):
    return [{"advertId": advert_id, "days": [{"date": d, "sum": 1, "apps": [{"appType": a, "sum": 1, "nms": [nm_item(n) for n in nms]} for a in app_types]} for d in days]}]


class FakeSb:
    def __init__(self, spend=None, existing=None):
        self.spend, self.existing, self.upserts, self.deletes = spend or [], existing or [], [], []

    def table(self, name):
        sb = self

        class T:
            def __init__(self):
                self.kind = None

            def upsert(self, rows, on_conflict=None):
                sb.upserts.append((name, list(rows), on_conflict)); return self

            def select(self, *_a):
                return self

            def delete(self):
                self.kind = "delete"; return self

            def eq(self, *_a):
                return self

            def gte(self, *_a):
                return self

            def lte(self, *_a):
                return self

            def in_(self, col, values):
                if self.kind == "delete":
                    sb.deletes.append((col, list(values)))
                return self

            def order(self, *_a, **_k):
                return self

            def range(self, *_a):
                return self

            def execute(self):
                if self.kind == "delete":
                    return mock.Mock(data=[{"x": 1}] * len(sb.deletes[-1][1]))
                return mock.Mock(data=list(sb.spend) if name == nm.SPEND_TABLE else list(sb.existing))
        return T()


class RowTests(unittest.TestCase):
    def test_rows_keyed_by_day_campaign_app_and_nm_with_window_filter(self):
        counters = {}
        from collections import Counter
        counters = Counter()
        rows = nm.build_rows(answer(days=("2026-06-30", "2026-07-01"), nms=(5, 6)), "obs", "2026-07-01", "2026-07-31", counters)
        keys = sorted((r["day"], r["advert_id"], r["app_type"], r["nm_id"]) for r in rows)
        self.assertEqual(keys, [("2026-07-01", 1, 1, 5), ("2026-07-01", 1, 1, 6), ("2026-07-01", 1, 32, 5), ("2026-07-01", 1, 32, 6)])
        self.assertEqual((rows[0]["sum"], rows[0]["views"], rows[0]["nm_name"], rows[0]["observed_at"]), ("10.19", 21, "постер", "obs"))
        self.assertEqual(counters["days_outside"], 1)

    def test_nm_without_id_is_counted_and_duplicate_key_refused(self):
        from collections import Counter
        counters = Counter()
        a = answer(nms=(7,)); a[0]["days"][0]["apps"][0]["nms"].append(nm_item(None))
        rows = nm.build_rows(a, "obs", counters=counters)
        self.assertEqual((len(rows), counters["nm_without_id"]), (2, 1))
        dup = answer(nms=(7,)); dup[0]["days"][0]["apps"][0]["nms"].append(nm_item(7))
        with self.assertRaises(RuntimeError):
            nm.build_rows(dup, "obs")

    def test_batches_of_fifty(self):
        self.assertEqual([len(b) for b in nm.batches(range(1, 79))], [50, 28])
        self.assertEqual(nm.batches([]), [])


class RequestTests(unittest.TestCase):
    def test_429_is_retried_then_named(self):
        resp429 = mock.Mock(status_code=429, text="")
        ok = mock.Mock(status_code=200, content=b"[]"); ok.json.return_value = []
        from collections import Counter
        c = Counter()
        with mock.patch.object(nm.requests, "get", side_effect=[resp429, ok]) as get:
            out = nm.request_fullstats([1, 2], "2026-07-01", "2026-07-07", c, sleep_fn=lambda _s: None)
        self.assertEqual((out, get.call_count, c["429"], c["requests"]), ([], 2, 1, 2))
        self.assertEqual(get.call_args.kwargs["params"], {"ids": "1,2", "beginDate": "2026-07-01", "endDate": "2026-07-07"})
        with mock.patch.object(nm.requests, "get", return_value=resp429):
            with self.assertRaises(RuntimeError):
                nm.request_fullstats([1], "2026-07-01", "2026-07-07", Counter(), sleep_fn=lambda _s: None)

    def test_interval_and_batch_limits(self):
        with self.assertRaises(RuntimeError):
            nm.request_fullstats([1], "2026-07-01", "2026-08-05", sleep_fn=lambda _s: None)
        with self.assertRaises(RuntimeError):
            nm.request_fullstats(list(range(51)), "2026-07-01", "2026-07-07", sleep_fn=lambda _s: None)


class RunTests(unittest.TestCase):
    def test_no_spend_in_window_means_no_requests(self):
        sb = FakeSb(spend=[])
        with mock.patch.object(nm.requests, "get") as get, redirect_stdout(io.StringIO()):
            result = nm.run(sb, days_back=31, today=date(2026, 9, 25))
        get.assert_not_called()
        self.assertEqual((result["requests"], result["campaigns"], sb.upserts), (0, 0, []))

    def test_run_batches_campaigns_with_spend_and_cleans_stale_on_complete(self):
        spend = [{"advert_id": i, "upd_day": "2026-07-01", "upd_sum": "100"} for i in range(1, 53)]
        existing = [{"day": "2026-07-01", "advert_id": 1, "app_type": 1, "nm_id": 999, "sum": "5"}]
        sb = FakeSb(spend=spend, existing=existing)
        ok = mock.Mock(status_code=200, content=b"x"); ok.json.return_value = answer(advert_id=1, days=("2026-07-01",), nms=(5,), app_types=(1,))
        sleeps = []
        with mock.patch.object(nm.requests, "get", return_value=ok) as get, redirect_stdout(io.StringIO()):
            result = nm.run(sb, days_back=31, today=date(2026, 8, 1), sleep_fn=sleeps.append)
        self.assertEqual(get.call_count, 2)                                          # 52 кампании → 50 + 2
        self.assertEqual(sleeps, [nm.PAUSE_SECONDS])
        self.assertEqual((result["campaigns"], result["requests"]), (52, 2))
        self.assertEqual(sb.upserts[0][2], "day,advert_id,app_type,nm_id")
        self.assertEqual(sb.deletes, [("nm_id", [999])])                              # застрявший ключ снят

    def test_dry_run_writes_nothing(self):
        spend = [{"advert_id": 1, "upd_day": "2026-07-01", "upd_sum": "100"}]
        sb = FakeSb(spend=spend, existing=[{"day": "2026-07-01", "advert_id": 1, "app_type": 1, "nm_id": 999, "sum": "5"}])
        ok = mock.Mock(status_code=200, content=b"x"); ok.json.return_value = answer()
        with mock.patch.object(nm.requests, "get", return_value=ok), redirect_stdout(io.StringIO()):
            result = nm.run(sb, days_back=31, today=date(2026, 8, 1), dry_run=True, sleep_fn=lambda _s: None)
        self.assertEqual((sb.upserts, sb.deletes, result["written"]), ([], [], 0))


class StepTests(unittest.TestCase):
    def test_ads_step_calls_the_nm_loader_after_the_spend_loader(self):
        calls = []
        with mock.patch.object(ads, "_client", return_value="sb"), \
             mock.patch.object(ads, "run", side_effect=lambda sb, **kw: calls.append(("upd", kw["dry_run"]))), \
             mock.patch.object(nm, "run", side_effect=lambda sb, **kw: calls.append(("nm", kw["dry_run"], kw["days_back"]))):
            ads.main(["--dry-run"])
        self.assertEqual(calls, [("upd", True), ("nm", True, 31)])


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p1 = mock.patch.object(bf, "RAW_DIR", self.tmp.name); self.p2 = mock.patch.object(bf, "CALLS_PATH", os.path.join(self.tmp.name, "calls.json"))
        self.p1.start(); self.p2.start()

    def tearDown(self):
        self.p1.stop(); self.p2.stop(); self.tmp.cleanup()

    def test_months_and_estimate(self):
        self.assertEqual(bf.months_between("2026-02-01", "2026-03-15"), [("2026-02", "2026-02-01", "2026-02-28"), ("2026-03", "2026-03-01", "2026-03-15")])
        with mock.patch.object(nm, "campaigns_with_spend", side_effect=[({i: D(1) for i in range(125)}, {}), ({i: D(1) for i in range(78)}, {})]), redirect_stdout(io.StringIO()) as out:
            plan = bf.estimate(object(), "2026-02-01", "2026-03-31")
        self.assertEqual([(m, n) for m, _a, _b, _ids, n in plan], [("2026-02", 3), ("2026-03", 2)])
        self.assertIn("итого обращений 5", out.getvalue())

    def test_fetch_skips_existing_and_plan_reads_files_and_apply_needs_approval(self):
        json.dump({"month": "2026-07", "date_from": "2026-07-01", "date_to": "2026-07-31", "batch": 1, "ids": [1], "fetched_at_utc": "x",
                   "campaigns_in_answer": 1, "answer": answer(advert_id=1, days=("2026-07-01",), nms=(5,), app_types=(1,))},
                  open(os.path.join(self.tmp.name, "fullstats_2026-07_b1.json"), "w"))
        with mock.patch.object(nm, "campaigns_with_spend", return_value=({1: D("100")}, {"2026-07-01": D("100")})), \
             mock.patch.object(nm, "request_fullstats") as req, mock.patch.object(bf, "in_nightly_run_window", return_value=False), redirect_stdout(io.StringIO()):
            code = bf.fetch(object(), "2026-07-01", "2026-07-31")
        self.assertEqual(code, 0); req.assert_not_called()
        with mock.patch.object(nm, "campaigns_with_spend", return_value=({1: D("100")}, {"2026-07-01": D("100")})), redirect_stdout(io.StringIO()) as out:
            plans = bf.plan(object(), bf.load_files("2026-07-01", "2026-07-31"), "2026-07-01", "2026-07-31")
        self.assertEqual((len(plans["2026-07"]["rows"]), plans["2026-07"]["complete"]), (1, True))
        self.assertIn("db_writes = 0", out.getvalue())
        with mock.patch.object(nm, "_client", return_value=object()), mock.patch.object(nm, "campaigns_with_spend", return_value=({1: D("100")}, {})), \
             mock.patch.object(bf, "apply") as apply, redirect_stdout(io.StringIO()):
            code = bf.main(["--apply", "--date-from", "2026-07-01", "--date-to", "2026-07-31"])
        self.assertEqual(code, 2); apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
