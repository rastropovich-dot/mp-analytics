"""Пересборка истории заказов: границы окна сбора и границы удаления.

Два дефекта, найденные чтением до первого запуска (2026-09-18):
сбор от 00:00 UTC терял первые три часа локального дня date_from, который
--apply переписывает целиком; delete не имел верхней границы, и --date-to в
прошлом стёр бы строки до сегодня, записав только до date_to.
"""
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("rebuild_orders", os.path.join(ROOT, "scripts", "rebuild_ozon_orders_history.py"))
rebuild = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rebuild)


class FakeQuery:
    def __init__(self, log, table):
        self.log, self.call = log, {"table": table, "op": "select", "filters": []}

    def select(self, *a, **kw):
        return self

    def delete(self):
        self.call["op"] = "delete"
        return self

    def upsert(self, rows, **kw):
        self.call["op"], self.call["rows"] = "upsert", rows
        return self

    def limit(self, n):
        return self

    def __getattr__(self, name):
        if name in ("eq", "gte", "lte"):
            def f(col, val):
                self.call["filters"].append((name, col, val))
                return self
            return f
        raise AttributeError(name)

    def execute(self):
        self.log.append(self.call)
        return type("R", (), {"count": 0, "data": []})()


class FakeClient:
    def __init__(self):
        self.log = []

    def table(self, name):
        return FakeQuery(self.log, name)


def posting(number, when, status="delivered"):
    return {"posting_number": number, "status": status, "created_at": when, "in_process_at": when,
            "products": [{"sku": 11, "offer_id": "F11", "name": "товар", "quantity": 1, "price": {"amount": "1000", "currency": "RUB"}}]}


class WindowTests(unittest.TestCase):
    def test_window_starts_at_local_midnight(self):
        """order_date — локальная дата: день 03-28 по Москве начинается в 03-27 21:00 UTC."""
        start, end = rebuild.utc_window("2026-03-28", "2026-09-10", datetime(2026, 9, 18, 13, 0, tzinfo=timezone.utc))
        self.assertEqual(start, datetime(2026, 3, 27, 21, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 9, 10, 21, 0, tzinfo=timezone.utc))

    def test_window_end_is_not_in_the_future(self):
        now = datetime(2026, 9, 18, 13, 40, 5, 999, tzinfo=timezone.utc)
        _start, end = rebuild.utc_window("2026-03-28", "2026-09-18", now)
        self.assertEqual(end, datetime(2026, 9, 18, 13, 40, 5, tzinfo=timezone.utc))


class ApplyBoundsTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.saved = (rebuild.sb, rebuild.fetch_history, rebuild.fetch_all, rebuild.SNAP_DIR)
        self.tmp = tempfile.TemporaryDirectory()
        rebuild.sb = lambda: self.client
        rebuild.SNAP_DIR = self.tmp.name
        self.table = [{"id": 1, "order_date": "2026-04-01", "order_schema": "fbo", "marketplace_sku": "11", "orders_qty": 1,
                       "orders_amount_seller": 1000, "article": "F11", "product_name": "товар", "observed_at": None}]
        rebuild.fetch_all = lambda table, filters, select="*", order="id": list(self.table)
        rebuild.fetch_history = lambda scheme, df, dt: [
            posting("in", "2026-04-01T08:00:00Z"), posting("before", "2026-03-20T08:00:00Z"), posting("after", "2026-04-20T08:00:00Z"),
        ] if scheme == "fbo" else []

    def tearDown(self):
        rebuild.sb, rebuild.fetch_history, rebuild.fetch_all, rebuild.SNAP_DIR = self.saved
        self.tmp.cleanup()

    def test_delete_and_write_share_both_bounds(self):
        rebuild.plan("2026-03-28", "2026-04-10", apply=True)
        deletes = [c for c in self.client.log if c["op"] == "delete"]
        self.assertEqual(len(deletes), 2)
        for c in deletes:
            self.assertIn(("gte", "order_date", "2026-03-28"), c["filters"])
            self.assertIn(("lte", "order_date", "2026-04-10"), c["filters"])
        written = [r["order_date"] for c in self.client.log if c["op"] == "upsert" for r in c["rows"]]
        self.assertEqual(written, ["2026-04-01"])

    def test_delete_by_month_covers_the_same_window_without_gaps(self):
        """Запасной путь при statement_timeout: те же границы, месяцы встык, ни дня мимо."""
        rebuild.plan("2026-03-28", "2026-05-10", apply=True, delete_by_month=True)
        fbo = [c for c in self.client.log if c["op"] == "delete" and ("eq", "order_schema", "fbo") in c["filters"]]
        spans = [(next(f[2] for f in c["filters"] if f[0] == "gte"), next(f[2] for f in c["filters"] if f[0] == "lte")) for c in fbo]
        self.assertEqual(spans, [("2026-03-28", "2026-03-31"), ("2026-04-01", "2026-04-30"), ("2026-05-01", "2026-05-10")])

    def test_observed_at_is_the_fetch_moment(self):
        rebuild.FETCHED_AT["fbo"] = "2026-09-18T13:51:25+00:00"
        try:
            rebuild.plan("2026-03-28", "2026-04-10", apply=True)
        finally:
            rebuild.FETCHED_AT.clear()
        written = [r for c in self.client.log if c["op"] == "upsert" for r in c["rows"]]
        self.assertEqual({r["observed_at"] for r in written}, {"2026-09-18T13:51:25+00:00"})

    def test_snapshot_keeps_full_rows(self):
        rebuild.plan("2026-03-28", "2026-04-10", apply=True)
        snaps = os.listdir(self.tmp.name)
        self.assertEqual(len(snaps), 1)
        rows = json.load(open(os.path.join(self.tmp.name, snaps[0])))["rows"]
        self.assertEqual(rows[0]["article"], "F11")

    def test_plan_only_writes_nothing(self):
        rebuild.plan("2026-03-28", "2026-04-10", apply=False)
        self.assertEqual([c for c in self.client.log if c["op"] in ("delete", "upsert")], [])
        self.assertEqual(os.listdir(self.tmp.name), [])


if __name__ == "__main__":
    unittest.main()
