"""WB-18 §2: платное хранение WB по артикулам и дням — loaders/wb_paid_storage_loader.py: свёртка строк по ключу и суммы,
задание → статус (new / processing → done; purged / canceled / таймаут — отказ) → скачивание (204 — строк 0), 429 — пауза и повтор,
окно ≤ 8 дней, upsert по ключу (идемпотентно), ретенция сырья, шаг пайплайна нефатален. WB API в тестах — 0."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from unittest import mock

import loaders.wb_paid_storage_loader as ld
import run_daily_pipeline as pipeline

STEP = "WB: платное хранение"
D = Decimal


def item(day="2026-10-13", nm=926793547, chrt=1399395141, gi=0, calc="короба: товары свыше базы", barcode="2050087406101", price=0.1764, cnt=1, **extra):
    base = {"date": day, "logWarehouseCoef": 0, "officeId": 0, "warehouse": "Склад WB РФ", "warehouseCoef": 0, "giId": gi, "chrtId": chrt, "size": "0",
            "barcode": barcode, "subject": "Ювелирные серьги", "brand": "KARATOV", "vendorCode": "F007752858-ПРЛ", "nmId": nm, "volume": 1.26,
            "calcType": calc, "warehousePrice": price, "barcodesCount": cnt, "palletPlaceCode": 0, "palletCount": 0, "originalDate": day,
            "loyaltyDiscount": 0, "tariffFixDate": "", "tariffLowerDate": ""}
    base.update(extra)
    return base


ITEMS = [
    item(),                                                             # тарифные части одного ключа — две строки
    item(price=0.12348),
    item(price=0.12348),                                                # полный дубль — тоже в сумму (так сходится день)
    item(nm=17641481, chrt=207424128, gi=34440898, barcode="7002899503", price=1.1088, cnt=5, size="22,5", vendorCode="F007773989-ТП", tariffFixDate="2025-11-26"),
    item(day="2026-10-14", nm=17641481, chrt=207424128, gi=34440898, barcode="7002899503", price=0.22176, cnt=1),
    item(day="2026-10-14", calc="Сторнирование хранения по складу", price=-0.5, cnt=0),
]


def ITEMS_FOR(day):
    """ITEMS с датой day (строки окна истории)."""
    out = []
    for it in ITEMS:
        it = dict(it); it["date"] = day + it["date"][10:]; out.append(it)
    return out


class BuildRowsTests(unittest.TestCase):
    def test_rows_are_rolled_up_by_key_with_sums_and_counts(self):
        rows = ld.build_rows(ITEMS, "2026-10-15T00:30:00+00:00", "task-1", "2026-10-13", "2026-10-14")
        self.assertEqual(len(rows), 4)
        by = {tuple(r[k] for k in ld.KEY): r for r in rows}
        r = by[("2026-10-13", 926793547, 1399395141, 0, 0, "короба: товары свыше базы", "2050087406101")]
        self.assertEqual((r["warehouse_price"], r["barcodes_count"], r["rows_count"]), ("0.42336", 3, 3))   # 0,1764 + 0,12348 × 2
        self.assertEqual((r["vendor_code"], r["size"], r["volume"], r["warehouse"]), ("F007752858-ПРЛ", "0", "1.260", "Склад WB РФ"))
        self.assertEqual((r["tariff_fix_date"], r["original_date"], r["task_id"]), (None, "2026-10-13", "task-1"))
        r2 = by[("2026-10-13", 17641481, 207424128, 0, 34440898, "короба: товары свыше базы", "7002899503")]
        self.assertEqual((r2["warehouse_price"], r2["barcodes_count"], r2["tariff_fix_date"], r2["size"]), ("1.10880", 5, "2025-11-26", "22,5"))
        storno = by[("2026-10-14", 926793547, 1399395141, 0, 0, "Сторнирование хранения по складу", "2050087406101")]
        self.assertEqual(storno["warehouse_price"], "-0.50000")                                             # знак — смысл (how-we-work)
        self.assertEqual(ld.summarize(rows)["2026-10-13"], (D("1.53216"), 2, 2))
        self.assertEqual(ld.summarize(rows)["2026-10-14"], (D("-0.27824"), 2, 2))

    def test_rows_are_deterministic_whatever_the_input_order(self):
        a = ld.build_rows(ITEMS, "t", None, "2026-10-13", "2026-10-14")
        b = ld.build_rows(list(reversed(ITEMS)), "t", None, "2026-10-13", "2026-10-14")
        self.assertEqual(a, b)

    def test_missing_required_field_bad_number_and_date_outside_window_are_loud(self):
        with self.assertRaises(RuntimeError):
            ld.build_rows([item(nmId=None)], "t")
        with self.assertRaises(RuntimeError):
            ld.build_rows([item(warehousePrice="abc")], "t")
        with self.assertRaises(RuntimeError):
            ld.build_rows([item(day="2026-10-20")], "t", None, "2026-10-13", "2026-10-14")
        with self.assertRaises(RuntimeError):
            ld.build_rows([item(day="2026-13-01")], "t")


class Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        return self._payload


class ApiFlowTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.sleep = lambda s: self.sleeps.append(s)

    def test_task_status_download_with_waiting_statuses(self):
        answers = [Resp(200, {"data": {"taskId": "t1"}}), Resp(200, {"data": {"status": "new"}}), Resp(200, {"data": {"status": "processing"}}),
                   Resp(200, {"data": {"status": "done"}}), Resp(200, ITEMS)]
        calls = []
        def fake_get(url, headers=None, params=None, timeout=None):
            calls.append((url, params)); return answers.pop(0)
        counters = {"requests": 0, "429": 0, "transient": 0}
        with mock.patch.object(ld.requests, "get", side_effect=fake_get), redirect_stdout(io.StringIO()):
            task_id, items = ld.fetch_window("2026-10-13", "2026-10-14", counters, self.sleep)
        self.assertEqual((task_id, len(items), counters["requests"]), ("t1", 6, 5))
        self.assertEqual(calls[0][1], {"dateFrom": "2026-10-13", "dateTo": "2026-10-14"})
        self.assertTrue(calls[1][0].endswith("/tasks/t1/status") and calls[4][0].endswith("/tasks/t1/download"))
        self.assertEqual(self.sleeps, [5, ld.STATUS_POLL_SECONDS, ld.STATUS_POLL_SECONDS])              # пауза перед каждым опросом

    def test_canceled_timeout_and_204(self):
        counters = {"requests": 0, "429": 0, "transient": 0}
        with mock.patch.object(ld.requests, "get", return_value=Resp(200, {"data": {"status": "canceled"}})), redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as cm:
                ld.wait_done("t1", counters, self.sleep)
        self.assertIn("canceled", str(cm.exception))
        with mock.patch.object(ld.requests, "get", return_value=Resp(200, {"data": {"status": "processing"}})), redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as cm:
                ld.wait_done("t1", counters, self.sleep, max_polls=3)
        self.assertIn("не готов за 3", str(cm.exception))
        with mock.patch.object(ld.requests, "get", return_value=Resp(204)), redirect_stdout(io.StringIO()):
            self.assertEqual(ld.download("t1", counters, self.sleep), [])

    def test_429_pauses_and_gives_up_after_max_attempts(self):
        counters = {"requests": 0, "429": 0, "transient": 0}
        answers = [Resp(429), Resp(200, {"data": {"taskId": "t9"}})]
        with mock.patch.object(ld.requests, "get", side_effect=lambda *a, **k: answers.pop(0)), redirect_stdout(io.StringIO()):
            self.assertEqual(ld.create_task("2026-10-13", "2026-10-14", counters, self.sleep), "t9")
        self.assertEqual((counters["429"], self.sleeps), (1, [ld.SLEEP_SECONDS]))
        with mock.patch.object(ld.requests, "get", return_value=Resp(429)), redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as cm:
                ld.create_task("2026-10-13", "2026-10-14", counters, self.sleep)
        self.assertIn("429 не изжит", str(cm.exception))
        self.assertEqual(counters["429"], 1 + ld.MAX_ATTEMPTS)

    def test_window_and_limits(self):
        self.assertEqual(ld.window(date(2026, 10, 15)), ("2026-10-07", "2026-10-14"))
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                ld.run(None, dry_run=True, from_file=None, date_from="2026-10-01", date_to="2026-10-09", today=date(2026, 10, 15))


class FakeSB:
    def __init__(self):
        self.upserts = []

    def table(self, name):
        assert name == ld.TABLE, name
        sb = self

        class Q:
            def upsert(self, rows, on_conflict=None):
                sb.upserts.append((list(rows), on_conflict)); return self

            def execute(self):
                return type("R", (), {"data": []})()
        return Q()


class RunTests(unittest.TestCase):
    def test_run_from_file_writes_rolled_up_rows_by_key_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "2026-10-13_2026-10-14.json")
            json.dump({"taskId": "t1", "dateFrom": "2026-10-13", "dateTo": "2026-10-14", "rows": ITEMS}, open(path, "w"))
            sb = FakeSB()
            with redirect_stdout(io.StringIO()):
                p1 = ld.run(sb, from_file=path, today=date(2026, 10, 15), raw_dir=d)
                p2 = ld.run(sb, from_file=path, today=date(2026, 10, 15), raw_dir=d)
            self.assertEqual((p1["items"], p1["rows"], p1["written"], p1["requests"]), (6, 4, 4, 0))
            self.assertEqual(sb.upserts[0][1], ",".join(ld.KEY))
            k1 = [tuple(r[k] for k in ld.KEY) for r in sb.upserts[0][0]]
            k2 = [tuple(r[k] for k in ld.KEY) for r in sb.upserts[1][0]]
            self.assertEqual(k1, k2)                                                            # повтор окна — те же ключи
            self.assertEqual(p2["total"], p1["total"])
            self.assertEqual(p1["window"], ("2026-10-13", "2026-10-14"))

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "raw.json")
            json.dump(ITEMS, open(path, "w"))
            with redirect_stdout(io.StringIO()) as out:
                p = ld.run(None, from_file=path, dry_run=True, today=date(2026, 10, 15), raw_dir=d, date_from="2026-10-13", date_to="2026-10-14")
            self.assertEqual((p["written"], p["rows"]), (0, 4))
            self.assertIn("db_writes = 0", out.getvalue())

    def test_raw_retention_keeps_30_days_and_names_victims(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("2026-08-01_2026-08-08.json", "2026-09-01_2026-09-08.json", "2026-10-07_2026-10-14.json", "other.txt"):
                open(os.path.join(d, name), "w").write("{}")
            with redirect_stdout(io.StringIO()):
                victims = ld.cleanup_raw(date(2026, 10, 15), raw_dir=d)
            self.assertEqual(victims, ["2026-08-01_2026-08-08.json", "2026-09-01_2026-09-08.json"])
            self.assertEqual(sorted(os.listdir(d)), ["2026-10-07_2026-10-14.json", "other.txt"])

    def test_keep_raw_skips_retention_for_history_windows(self):
        # история по слову (22.10): окно 28.04 … 05.05 старше ретенции — без --keep-raw его сырьё стёрлось бы сразу после сохранения
        for keep in (False, True):
            with tempfile.TemporaryDirectory() as d:
                with mock.patch.object(ld, "fetch_window", return_value=("t9", ITEMS_FOR("2026-04-28"))), redirect_stdout(io.StringIO()):
                    p = ld.run(FakeSB(), today=date(2026, 10, 22), raw_dir=d, date_from="2026-04-28", date_to="2026-05-05", keep_raw=keep)
                self.assertEqual(os.path.exists(os.path.join(d, "2026-04-28_2026-05-05.json")), keep)
                self.assertEqual(p["written"], 3)                                          # все строки в одном дне — 3 ключа

    def test_save_and_load_raw(self):
        with tempfile.TemporaryDirectory() as d:
            with redirect_stdout(io.StringIO()):
                path = ld.save_raw("2026-10-13", "2026-10-14", "t1", ITEMS, raw_dir=d, downloaded_at="2026-10-15T00:30:00+00:00")
            raw = ld.load_raw(path)
            self.assertEqual((raw["taskId"], raw["dateFrom"], len(raw["rows"])), ("t1", "2026-10-13", 6))


class StepTests(unittest.TestCase):
    def test_step_is_declared_non_fatal_after_the_realisation_report(self):
        titles = [title for title, _ in pipeline.build_steps()]
        self.assertIn(STEP, titles)
        self.assertIn(STEP, pipeline.NON_FATAL_STEPS)
        self.assertFalse(pipeline.is_fatal_step(STEP))
        self.assertEqual(dict(pipeline.build_steps())[STEP], "python3 loaders/wb_paid_storage_loader.py")
        self.assertGreater(titles.index(STEP), titles.index("WB: отчёт реализации"))
        self.assertEqual(titles.index(STEP), titles.index("WB: реклама") + 1)
        for later in ("WB: загрузка остатков", "Ozon: загрузка FBS заказов", "KPI: расчет SKU"):
            self.assertLess(titles.index(STEP), titles.index(later))


if __name__ == "__main__":
    unittest.main()
