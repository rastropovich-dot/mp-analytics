"""Шестьдесят первая §3: история ozon_posting_items из сырья на диске — самое свежее наблюдение, полные дни, свежее в таблице не трогаем."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import ozon_posting_items_history as hist  # noqa: E402


def posting(number, day, status="delivered", sku=11, qty=1):
    return {"posting_number": number, "status": status, "created_at": day, "in_process_at": day,
            "products": [{"sku": sku, "offer_id": "A", "quantity": qty, "price": {"amount": "100", "currency": "RUB"}}]}


class History(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def put(self, name, fetched_at, postings):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
            json.dump({"schema": "fbo", "fetched_at": fetched_at, "postings": postings}, fh)

    def test_latest_observation_and_complete_days(self):
        # история снята 21.10 01:12 МСК — 19 и 20.10 полные; снимок днём 22.10 — 22.10 неполный, но статус свежее
        self.put("history_fbo_2026-10-19_2026-10-20.json", "2026-10-20T22:12:00+00:00",
                 [posting("A", "2026-10-19T10:00:00Z", "delivering"), posting("B", "2026-10-20T10:00:00Z")])
        self.put("fbo_20261022T091500Z.json", "2026-10-22T09:15:00+00:00",
                 [posting("A", "2026-10-19T10:00:00Z", "cancelled"), posting("C", "2026-10-22T08:00:00Z")])
        self.put("part_fbo_2026-10-19_2026-10-20.json", "2026-10-20T22:00:00+00:00", [posting("Z", "2026-10-19T10:00:00Z")])
        rows, counters, files, complete = hist.build_rows(self.dir, "fbo", "2026-10-19", "2026-10-22")
        by = {r["posting_number"]: r for r in rows}
        self.assertEqual(sorted(by), ["A", "B", "C"])                 # part_* не берётся
        self.assertEqual(by["A"]["status"], "cancelled")              # самое свежее наблюдение
        self.assertEqual(by["A"]["source"], "history:fbo_20261022T091500Z.json")
        self.assertEqual(by["A"]["observed_at"], "2026-10-22T09:15:00+00:00")
        self.assertEqual(sorted(complete), ["2026-10-19", "2026-10-20", "2026-10-21"])   # окно снимка — с 19.10; 22.10 снят днём — неполный
        self.assertEqual(len(files), 2)

    def test_newer_in_table_skipped(self):
        rows = [{"posting_number": "A", "sku": 11, "observed_at": "2026-10-21T22:12:00+00:00"},
                {"posting_number": "B", "sku": 11, "observed_at": "2026-10-21T22:12:00+00:00"}]
        keep, skip = hist.newer_in_table(rows, {("A", "11"): "2026-11-07T00:50:00+00:00", ("B", "11"): "2026-10-01T00:50:00+00:00"})
        self.assertEqual(([r["posting_number"] for r in keep], [r["posting_number"] for r in skip]), (["B"], ["A"]))
        self.assertEqual(hist.newer_in_table(rows, None), (rows, []))

    def test_ranges(self):
        self.assertEqual(hist._ranges(["2026-09-22", "2026-09-23", "2026-10-25"]), "2026-09-22 … 2026-09-23, 2026-10-25")


if __name__ == "__main__":
    unittest.main()
