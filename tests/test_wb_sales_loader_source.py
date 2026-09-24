"""Шаг «WB: загрузка продаж/выкупов»: источник — отчёт реализации, запасной путь — supplier/sales.

Держим: строка шага в run_daily_pipeline.py не менялась; auto — сначала сбор отчёта и выкупы из него, в логе
«источник: wb_sales_report_rows»; отчёт не собрался — старый путь с «источник: supplier/sales (отчёт не
собран: …)»; --source sales — только старый путь; --source report при отказе падает; dry-run не пишет.
"""
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

import run_daily_pipeline as pipeline
import loaders.wb_sales_loader as sales

STEP = "WB: загрузка продаж/выкупов"
REPORT_OK = {"window": ("2026-09-04", "2026-09-24"), "rows": 12011, "written": 12011, "deleted": 0, "requests": 1, "429": 0,
             "skipped": False, "complete": True}


class StepLineTests(unittest.TestCase):
    def test_step_command_is_unchanged_and_precedes_the_report_step(self):
        steps = dict(pipeline.build_steps())
        self.assertEqual(steps[STEP], "python3 loaders/wb_sales_loader.py")
        titles = [t for t, _ in pipeline.build_steps()]
        self.assertLess(titles.index(STEP), titles.index("WB: отчёт реализации"))


class SourceTests(unittest.TestCase):
    def run_main(self, argv, report_side_effect, sales_items=()):
        out = io.StringIO()
        with mock.patch.object(sales.report_loader, "run", side_effect=report_side_effect) as report_run, \
             mock.patch.object(sales.buyouts_from_report, "run", return_value={"source": "wb_sales_report_rows", "rows": 3, "written": 2, "deleted": 0, "window": ("2026-09-04", "2026-09-24")}) as buyouts_run, \
             mock.patch.object(sales, "get_wb_sales", return_value=list(sales_items)) as old_fetch, \
             mock.patch.object(sales, "save_wb_sales", return_value={"rows_written": 0, "held_rows": 0, "window_start": "x"}) as old_save, \
             redirect_stdout(out):
            code = sales.main(argv)
        return code, out.getvalue(), report_run, buyouts_run, old_fetch, old_save

    def test_auto_takes_the_report_when_it_is_collected(self):
        code, log, report_run, buyouts_run, old_fetch, old_save = self.run_main([], [REPORT_OK])
        self.assertEqual(code, 0)
        self.assertIn("источник: wb_sales_report_rows (отчёт собран этой ночью: строк 12011, обращений 1, 429 — 0; окно 2026-09-04 … 2026-09-24)", log)
        report_run.assert_called_once()
        buyouts_run.assert_called_once()
        self.assertEqual(buyouts_run.call_args.kwargs["complete"], True)
        old_fetch.assert_not_called(); old_save.assert_not_called()

    def test_auto_uses_a_collection_already_made_this_night(self):
        from datetime import datetime, timezone
        skipped = dict(REPORT_OK, rows=None, requests=0, skipped=True, collected_at=datetime(2026, 9, 25, 0, 19, 20, tzinfo=timezone.utc))
        code, log, _r, buyouts_run, old_fetch, _s = self.run_main([], [skipped])
        self.assertEqual(code, 0)
        self.assertIn("отчёт уже собран этой ночью в 00:19:20 UTC", log)
        buyouts_run.assert_called_once(); old_fetch.assert_not_called()

    def test_auto_falls_back_to_supplier_sales_when_the_report_fails(self):
        code, log, _r, buyouts_run, old_fetch, old_save = self.run_main([], [RuntimeError("WB sales report: 429 не изжит за 3 попыток")])
        self.assertEqual(code, 0)
        self.assertIn("источник: supplier/sales (отчёт не собран: RuntimeError: WB sales report: 429 не изжит за 3 попыток)", log)
        buyouts_run.assert_not_called()
        old_fetch.assert_called_once(); old_save.assert_called_once()
        self.assertFalse(old_save.call_args.kwargs["dry_run"])

    def test_source_report_raises_instead_of_falling_back(self):
        with mock.patch.object(sales.report_loader, "run", side_effect=RuntimeError("HTTP 500")), \
             mock.patch.object(sales, "get_wb_sales") as old_fetch, redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                sales.main(["--source", "report"])
        old_fetch.assert_not_called()

    def test_source_sales_takes_only_the_old_path(self):
        code, log, report_run, buyouts_run, old_fetch, old_save = self.run_main(["--source", "sales"], [REPORT_OK])
        self.assertEqual(code, 0)
        self.assertIn("источник: supplier/sales (по флагу --source sales)", log)
        report_run.assert_not_called(); buyouts_run.assert_not_called()
        old_fetch.assert_called_once(); old_save.assert_called_once()

    def test_dry_run_is_passed_to_both_paths(self):
        _c, _l, report_run, buyouts_run, _f, _s = self.run_main(["--dry-run"], [REPORT_OK])
        self.assertTrue(report_run.call_args.kwargs["dry_run"])
        self.assertTrue(buyouts_run.call_args.kwargs["dry_run"])
        _c, _l, _r, _b, _f, old_save = self.run_main(["--dry-run"], [RuntimeError("x")])
        self.assertTrue(old_save.call_args.kwargs["dry_run"])


class OldPathDryRunTests(unittest.TestCase):
    def test_save_wb_sales_dry_run_writes_nothing(self):
        items = [{"nmId": 1, "date": "2026-09-24T10:00:00", "totalPrice": 100, "finishedPrice": 80, "priceWithDisc": 90, "supplierArticle": "F1", "subject": "Кольца"}]
        with mock.patch.object(sales, "supabase") as sb, redirect_stdout(io.StringIO()):
            from datetime import date
            summary = sales.save_wb_sales(items, days_back=30, today=date(2026, 9, 25), dry_run=True)
        sb.table.assert_not_called()
        self.assertEqual(summary["rows_written"], 0)


if __name__ == "__main__":
    unittest.main()
