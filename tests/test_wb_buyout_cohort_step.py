"""Шаг «WB: выкуп по когорте» — нефатальный, после отчёта реализации WB, до рекламы WB, Ozon и KPI; строка алерта (WB-10 §2)."""
import unittest
from unittest import mock

import alerts_telegram as alerts
import run_daily_pipeline as pipeline

STEP = "WB: выкуп по когорте"


class StepTests(unittest.TestCase):
    def test_step_is_declared_non_fatal_and_ordered(self):
        titles = [title for title, _ in pipeline.build_steps()]
        self.assertIn(STEP, titles)
        self.assertIn(STEP, pipeline.NON_FATAL_STEPS)
        self.assertEqual(dict(pipeline.build_steps())[STEP], "python3 scripts/wb_buyout_cohort_step.py --days-back 60 --apply --approve-wb-cohort-write")
        self.assertEqual(titles.index(STEP), titles.index("WB: отчёт реализации") + 1)
        for later in ("WB: реклама", "WB: загрузка остатков", "Ozon: загрузка FBS заказов", "KPI: расчет SKU"):
            self.assertLess(titles.index(STEP), titles.index(later))


ROWS = [  # как отдаёт таблица, свежие дни первыми
    {"day": "2026-09-24", "created_sum": 231418, "sold_sum": 0, "mature": False, "rate_sum": None, "forecast_rate_sum": "0.4179", "forecast_window": "2026-08-02 … 2026-08-31"},
    {"day": "2026-09-10", "created_sum": 100, "sold_sum": 10, "mature": False, "rate_sum": None, "forecast_rate_sum": "0.4179", "forecast_window": "2026-08-02 … 2026-08-31"},
] + [{"day": f"2026-08-{d:02d}", "created_sum": 1000, "sold_sum": 422, "mature": True, "rate_sum": "0.4220", "forecast_rate_sum": None, "forecast_window": None} for d in range(31, 0, -1)] \
  + [{"day": f"2026-07-{d:02d}", "created_sum": 1000, "sold_sum": 526, "mature": True, "rate_sum": "0.5260", "forecast_rate_sum": None, "forecast_window": None} for d in range(31, 0, -1)]


class AlertLineTests(unittest.TestCase):
    def test_line_names_the_last_full_mature_month_and_the_forecast(self):
        self.assertEqual(alerts.format_wb_buyout_cohort(ROWS), "Выкуп по когорте: август 42.2 % (зрелые дни ≥ 25 сут.), прогноз для последних 30 дней 41.8 % по зрелым 02.08 … 31.08.")
        partial = [r for r in ROWS if not (r["day"].startswith("2026-08") and r["day"] > "2026-08-20")]     # август неполный → июль
        self.assertTrue(alerts.format_wb_buyout_cohort(partial).startswith("Выкуп по когорте: июль 52.6 % (зрелые дни ≥ 25 сут.), прогноз"))
        only_partial = [r for r in ROWS if r["day"].startswith("2026-08") and r["day"] > "2026-08-20"]        # ни одного полного месяца
        self.assertEqual(alerts.format_wb_buyout_cohort(only_partial), "Выкуп по когорте: август 42.2 % по 11 зрелым дням (≥ 25 сут.).")
        self.assertEqual(alerts.format_wb_buyout_cohort([]), "Выкуп по когорте: нет данных (таблица wb_buyout_cohort_daily пуста).")

    def test_summary_never_raises_and_the_wb_block_carries_the_line(self):
        with mock.patch.object(alerts, "get_wb_buyout_cohort_rows", side_effect=RuntimeError("PGRST205 нет таблицы")):
            self.assertEqual(alerts.get_wb_buyout_cohort_summary(), "Выкуп по когорте: нет данных (PGRST205 нет таблицы).")
        kpi_rows = [{"marketplace_code": "wb", "kpi_date": "2026-05-20", "orders_qty": 10, "orders_amount_seller": 100000, "buyouts_qty": 8, "buyouts_amount_seller": 80000,
                     "commission_amount": 0, "logistics_amount": 0, "other_expenses_amount": 0, "ad_spend": 0, "ad_orders_revenue": 0, "organic_orders_revenue": 0, "ad_share_revenue": 0}]
        with mock.patch.object(alerts, "today_local", return_value=alerts.date(2026, 5, 21)), \
             mock.patch.object(alerts, "get_wb_buyout_cohort_summary", return_value="Выкуп по когорте: апрель 51.9 % (зрелые дни ≥ 25 сут.).") as line, \
             mock.patch.object(alerts, "get_ozon_report_completeness", return_value={"complete": False, "blockers": ["x"]}):
            text = "\n".join(alerts.build_executive_summary(kpi_rows))
        line.assert_called_once()
        self.assertIn("Отклонение заказов к 7дн: н/д.\nВыкуп по когорте: апрель 51.9 % (зрелые дни ≥ 25 сут.).", text)


if __name__ == "__main__":
    unittest.main()
