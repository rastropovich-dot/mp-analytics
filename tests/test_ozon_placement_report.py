"""Пятьдесят вторая §2: отчёт placement/by-products — разбор файла (заголовок как есть), путь create → info → скачивание (waiting → success,
failed, таймаут, пустое тело → повтор), строки таблицы (ключ day × sku × warehouse, знак леджера, дубли складываются), счётчик лимита,
storage_cost витрины, шаг пайплайна нефатален. Seller API и база в тестах — 0 (заглушки)."""
import io
import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal as D
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from loaders import ozon_placement_report as P  # noqa: E402
import reports_daily_sku_kpi as kpi  # noqa: E402
import run_daily_pipeline as pipeline  # noqa: E402

ROWS = [
    (datetime(2026, 10, 20), 2673631119, "F000293326-17,5", "Ювелирные украшения", "Кольцо ювелирное", "Тверь_Хаб", "", 1520, 2, 1520, 2, 3.8),
    (datetime(2026, 10, 20), 1017340949, "F000264544-20,5", "Ювелирные украшения", "Кольцо ювелирное", "СПБ_БУГРЫ_РФЦ_ЮВЕЛИРНЫЙ", "", 1000, 2, 1000, 2, 2.5),
    (datetime(2026, 10, 20), 1017340949, "F000264544-20,5", "Ювелирные украшения", "Кольцо ювелирное", "НОВОСИБИРСК_РФЦ_ЮВЕЛИРНЫЙ", "", 500, 1, 500, 1, 1.25),
    (datetime(2026, 10, 20), 1313888131, "F007755830-16-ОН", "Ювелирные украшения", "Браслет ювелирный", "Тверь_Хаб", "", 1520, 2, 0, 0, 0.0),
]


def xlsx_bytes(rows=ROWS, header=None):
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Страница #1"
    ws.append(header or P.COLUMNS)
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


class _R:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code, self._payload, self.content = status, payload, content
        self.text = str(payload)

    def json(self):
        return self._payload


class ParseTests(unittest.TestCase):
    def test_parse_rows_and_db_rows_with_ledger_sign(self):
        rows = P.parse_xlsx(xlsx_bytes())
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["day"], "2026-10-20"); self.assertEqual(rows[0]["sku"], 2673631119); self.assertEqual(rows[0]["amount"], D("3.80"))
        self.assertEqual(rows[3]["amount"], D("0.00"))
        db = P.to_db_rows(rows, "REPORT_x", loaded_at="2026-10-21T21:00:00+00:00")
        self.assertEqual(len(db), 4)
        self.assertEqual({(r["day"], r["sku"], r["warehouse"]) for r in db}, {("2026-10-20", 2673631119, "Тверь_Хаб"), ("2026-10-20", 1017340949, "СПБ_БУГРЫ_РФЦ_ЮВЕЛИРНЫЙ"),
                                                                              ("2026-10-20", 1017340949, "НОВОСИБИРСК_РФЦ_ЮВЕЛИРНЫЙ"), ("2026-10-20", 1313888131, "Тверь_Хаб")})
        self.assertEqual(db[0]["amount"], "-3.80"); self.assertEqual(db[0]["report_code"], "REPORT_x"); self.assertEqual(db[3]["amount"], "0.00")
        s = P.summary(rows)["2026-10-20"]
        self.assertEqual((s["rows"], s["paid_rows"], s["skus"], s["warehouses"], s["amount"]), (4, 3, 3, 3, D("7.55")))

    def test_duplicate_key_rows_are_summed(self):
        rows = P.parse_xlsx(xlsx_bytes(ROWS + [ROWS[0]]))
        db = {(r["sku"], r["warehouse"]): r for r in P.to_db_rows(rows, "c")}
        self.assertEqual(len(db), 4); self.assertEqual(db[(2673631119, "Тверь_Хаб")]["amount"], "-7.60"); self.assertEqual(db[(2673631119, "Тверь_Хаб")]["units"], 4)

    def test_foreign_header_is_an_error_with_both_lists(self):
        with self.assertRaises(P.ReportError) as cm:
            P.parse_xlsx(xlsx_bytes(header=P.COLUMNS[:-1] + ["Стоимость"]))
        self.assertIn("заголовок файла не тот", str(cm.exception)); self.assertIn("Начисленная стоимость размещения", str(cm.exception))

    def test_compare_text_against_ledger(self):
        lines = P.compare_text({"2026-10-20": {"amount": D("238.38")}, "2026-10-21": {"amount": D("10.00")}}, {"2026-10-20": D("-238.38")})
        self.assertIn("файл + леджер = +0.00 — сошлось", lines[0]); self.assertIn("ещё нет", lines[1])


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.content = xlsx_bytes()

    def post_factory(self, infos):
        infos = list(infos)

        def post(path, body):
            self.calls.append(("post", path))
            if path.endswith("/create"):
                self.assertEqual(body, {"date_from": "2026-10-20", "date_to": "2026-10-20"})
                return _R(200, {"code": "REPORT_seller_placement_by_products_1"})   # code на верхнем уровне, как у Ozon
            return _R(200, infos.pop(0))
        return post

    def test_waiting_then_success_and_empty_body_is_retried_once(self):
        gets = [_R(200, None, b""), _R(200, None, self.content)]
        get = lambda url: (self.calls.append(("get", url)), gets.pop(0))[1]  # noqa: E731
        sleeps = []
        rows, stats, content = P.fetch_report("2026-10-20", "2026-10-20", post=self.post_factory([{"result": {"status": "waiting"}}, {"result": {"status": "success", "file": "https://ir-20.ozone.ru/x.xlsx"}}]),
                                              get=get, sleep_fn=sleeps.append, clock=iter(range(0, 100)).__next__, save_dir=None, log=lambda *a: None)
        self.assertEqual(len(rows), 4); self.assertEqual((stats["create"], stats["info"], stats["download"], stats["rows"]), (1, 2, 2, 4))
        self.assertEqual([c[0] for c in self.calls], ["post", "post", "post", "get", "get"])

    def test_failed_status_is_a_named_error(self):
        with self.assertRaises(P.ReportError) as cm:
            P.fetch_report("2026-10-20", "2026-10-20", post=self.post_factory([{"result": {"status": "failed", "error": "boom"}}]), get=lambda u: _R(200, None, self.content),
                           sleep_fn=lambda s: None, clock=iter(range(100)).__next__, save_dir=None, log=lambda *a: None)
        self.assertIn("status=failed", str(cm.exception)); self.assertIn("boom", str(cm.exception))

    def test_timeout_names_the_status_and_polls(self):
        post = self.post_factory([{"result": {"status": "waiting"}}] * 50)
        with self.assertRaises(P.ReportError) as cm:
            P.fetch_report("2026-10-20", "2026-10-20", post=post, get=lambda u: _R(200), sleep_fn=lambda s: None, clock=iter(range(0, 10000, 200)).__next__, save_dir=None, log=lambda *a: None)
        self.assertIn("не готов за", str(cm.exception)); self.assertIn("status=waiting", str(cm.exception))

    def test_create_once_sends_create_exactly_once_and_names_the_429(self):
        """56-я §4: при --create-once create уходит одной попыткой; 429 — именованный отказ, без повторов и без info."""
        from unittest import mock
        sent = []

        def fake_request(method, url, **kw):
            sent.append((method, url, kw.get("json")))
            return _R(429, {"code": 8, "message": "You have reached request rate limit per second"})

        with mock.patch.object(P.http_retry.requests, "request", side_effect=fake_request), \
             mock.patch.object(P.http_retry.time, "sleep", side_effect=AssertionError("повтор create запрещён")):
            with self.assertRaises(P.ReportError) as cm:
                P.fetch_report("2026-04-01", "2026-04-30", get=lambda u: _R(200), sleep_fn=lambda s: None,
                               clock=iter(range(100)).__next__, save_dir=None, log=lambda *a: None, create_once=True)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][1].endswith("/v1/report/placement/by-products/create"))
        self.assertIn("HTTP 429", str(cm.exception)); self.assertIn("rate limit per second", str(cm.exception))

    def test_empty_body_twice_is_an_error(self):
        with self.assertRaises(P.ReportError) as cm:
            P.fetch_report("2026-10-20", "2026-10-20", post=self.post_factory([{"status": "success", "file": "https://x"}]), get=lambda u: _R(200, None, b""),
                           sleep_fn=lambda s: None, clock=iter(range(100)).__next__, save_dir=None, log=lambda *a: None)
        self.assertIn("после 2 попыток", str(cm.exception))

    def test_quota_counter_resets_on_a_new_utc_day(self):
        self.assertEqual(P.bump_quota({}, "2026-10-21"), {"day": "2026-10-21", "creates": 1})
        self.assertEqual(P.bump_quota({"day": "2026-10-21", "creates": 4}, "2026-10-21"), {"day": "2026-10-21", "creates": 5})
        self.assertEqual(P.bump_quota({"day": "2026-10-21", "creates": 5}, "2026-10-22"), {"day": "2026-10-22", "creates": 1})


class KpiTests(unittest.TestCase):
    def test_storage_cost_is_the_sum_over_warehouses_positive_and_does_not_touch_other_columns(self):
        placement = [{"day": "2026-10-20", "sku": 1017340949, "warehouse": "A", "offer_id": "F000264544-20,5", "amount": "-2.50"},
                     {"day": "2026-10-20", "sku": 1017340949, "warehouse": "B", "offer_id": "F000264544-20,5", "amount": "-1.25"},
                     {"day": "2026-10-20", "sku": 999, "warehouse": "A", "offer_id": "X", "amount": "0.00"}]
        expenses = [{"id": 1, "expense_date": "2026-10-20", "marketplace_code": "ozon", "marketplace_sku": "1017340949", "expense_type": "logistics", "expense_amount": 10}]
        with mock.patch.object(kpi, "load_orders", return_value=[]), mock.patch.object(kpi, "load_buyouts", return_value=[]), \
             mock.patch.object(kpi, "load_expenses", return_value=expenses), mock.patch.object(kpi, "load_ozon_organic", return_value=[]), \
             mock.patch.object(kpi, "load_placement", return_value=placement), mock.patch.object(kpi, "load_ledger_type46", return_value={}), mock.patch.object(kpi, "load_wb_costs", return_value={"storage": [], "ads": {}, "other": [], "articles": {}}), mock.patch.object(kpi, "load_wb_cohort", return_value=({}, {})), mock.patch("builtins.print"):
            rows = {(r["kpi_date"], r["marketplace_sku"]): r for r in kpi.build_kpi()}
        r = rows[("2026-10-20", "1017340949")]
        self.assertEqual(r["storage_cost"], 3.75); self.assertEqual(r["logistics_amount"], 10); self.assertEqual(r["other_expenses_amount"], 0)
        self.assertEqual(rows[("2026-10-20", "999")]["storage_cost"], 0.0); self.assertEqual(rows[("2026-10-20", "999")]["article"], "X")
        self.assertEqual(kpi.empty_kpi_row("2026-10-20", "ozon", "1")["storage_cost"], 0)

    def test_unreadable_placement_table_marks_the_source_and_yields_no_rows(self):
        with mock.patch.object(kpi, "read_placement_by_day", side_effect=RuntimeError("PGRST205 нет таблицы")), mock.patch("builtins.print"):
            before = len(kpi.SOURCE_READ_FAILURES)
            self.assertEqual(kpi.load_placement(), [])
            self.assertEqual(kpi.SOURCE_READ_FAILURES[before:], [kpi.PLACEMENT_TABLE])
        kpi.SOURCE_READ_FAILURES.remove(kpi.PLACEMENT_TABLE)


class FileModeTests(unittest.TestCase):
    def test_file_mode_takes_the_window_from_the_file_days_and_calls_no_api(self):
        import tempfile
        path = tempfile.mktemp(suffix=".xlsx")
        with open(path, "wb") as fh:
            fh.write(xlsx_bytes())
        printed = []
        with mock.patch.object(P.cabinet, "assert_env", return_value=P.cabinet.profile()), mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))), \
             mock.patch.object(P, "fetch_report", side_effect=AssertionError("API не должен зваться")):
            rc = P.main(["--file", path, "--dry-run", "--no-state"])
        self.assertEqual(rc, 0)
        self.assertTrue(any("дни 2026-10-20 … 2026-10-20" in l for l in printed), printed)
        last = next(l for l in printed if l.startswith(P.STATE_KEY))
        self.assertIn('"date_from": "2026-10-20", "date_to": "2026-10-20"', last); self.assertIn('"mode": "dry-run"', last)


class PipelineStepTests(unittest.TestCase):
    def test_step_registered_after_expenses_and_non_fatal(self):
        titles = [t for t, _c in pipeline.STEPS]
        self.assertIn("Ozon: хранение по товарам", titles)
        self.assertEqual(titles.index("Ozon: хранение по товарам"), titles.index("Ozon: расходы и комиссии") + 1)
        self.assertFalse(pipeline.is_fatal_step("Ozon: хранение по товарам"))
        cmd = dict(pipeline.STEPS)["Ozon: хранение по товарам"]
        self.assertEqual(cmd, "python3 loaders/ozon_placement_report.py --apply")


if __name__ == "__main__":
    unittest.main()
