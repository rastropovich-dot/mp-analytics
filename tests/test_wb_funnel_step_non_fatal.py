"""Шаг Sales Funnel WB не должен уносить ночь обеих площадок.

Ночь 09-23: 429 на третьем дне, пауза 60 с, повтор — ReadTimeout, исключение
никто не ловил, шаг упал с кодом 1, а он фатальный и стоит третьим — ни одного
шага Ozon, ни KPI. Питает шаг только marketplace_orders_analytics (overlay
утреннего алерта). Здесь держим: шаг нефатальный; сетевой отказ — повтор, как
429, по исчерпании — RuntimeError, не голый Timeout; между запросами — пауза
под лимит 3 запроса в минуту.
"""
import types
import unittest
from unittest import mock

import requests

import run_daily_pipeline as pipeline
import loaders.wb_sales_funnel_orders_loader as funnel

STEP = "WB: загрузка заказов Analytics Sales Funnel"


def page(products, status=200):
    resp = mock.Mock(status_code=status)
    resp.json.return_value = {"data": {"products": products}}
    return resp


class StepTests(unittest.TestCase):
    def test_step_is_non_fatal(self):
        self.assertIn(STEP, pipeline.NON_FATAL_STEPS)

    def test_step_runs_before_wb_sales_ozon_and_kpi(self):
        titles = [title for title, _ in pipeline.build_steps()]
        for later in ("WB: загрузка продаж/выкупов", "Ozon: загрузка FBS заказов",
                      "Ozon: реклама Performance API", "KPI: расчет SKU"):
            self.assertLess(titles.index(STEP), titles.index(later))

    def test_failure_does_not_stop_the_pipeline(self):
        process = mock.MagicMock()
        process.stdout = mock.MagicMock()
        process.stdout.__iter__.return_value = iter(["requests.exceptions.ReadTimeout: Read timed out\n"])
        process.wait.return_value = 1
        with mock.patch.object(pipeline.subprocess, "Popen", return_value=process), \
             mock.patch.object(pipeline, "send_failure_alert") as alert:
            result = pipeline.run_step(STEP, "cmd", fatal=STEP not in pipeline.NON_FATAL_STEPS)
        self.assertTrue(result["failed"])
        alert.assert_called_once()

    def test_steps_after_the_failure_still_run(self):
        args = types.SimpleNamespace(
            skip_recovery=True, skip_organic=True, skip_excel=True, skip_decision=True, skip_telegram=True,
            ozon_recovery_current_day_only=False, ozon_campaign_selection=None, ozon_recent_activity_days=None,
            ozon_dormant_probe_size=None, ozon_max_daily_cpc_units=None, ozon_allow_staged_cpc_partial=False,
        )
        ran = []

        def fake_run_step(title, command, fatal=True, nonfatal_returncodes=()):
            ran.append(title)
            if title == STEP:
                return {"failed": True, "returncode": 1, "output_text": "", "recovery_result": None, "ozon_run_summary": None}
            return {"output_text": "", "recovery_result": None, "ozon_run_summary": None}

        with mock.patch.object(pipeline, "parse_args", return_value=args), \
             mock.patch.object(pipeline, "is_yesterday_cpc_loaded", return_value=False), \
             mock.patch.object(pipeline, "run_step", side_effect=fake_run_step):
            pipeline.main()

        for title in ("WB: загрузка продаж/выкупов", "Ozon: загрузка FBS заказов", "Ozon: расходы и комиссии",
                      "Ozon: реклама Performance API", "KPI: расчет SKU", "KPI: расчет маркетплейсов"):
            self.assertIn(title, ran, f"{title} не выполнился после падения funnel")


class LoaderTests(unittest.TestCase):
    def test_timeout_is_retried_like_429_and_then_succeeds(self):
        answers = [requests.exceptions.ReadTimeout("read timed out"), page([{"statistic": {"selected": {"orderCount": 2, "orderSum": 100}}}])]
        with mock.patch.object(funnel.requests, "post", side_effect=answers) as post, \
             mock.patch.object(funnel.time, "sleep") as sleep:
            row = funnel.fetch_wb_sales_funnel_day("2026-09-21")
        self.assertEqual(post.call_count, 2)
        self.assertEqual((row["orders_qty"], row["orders_amount"]), (2, 100))
        self.assertIn(mock.call(funnel.WB_RATE_LIMIT_SLEEP_SECONDS), sleep.call_args_list)

    def test_persistent_timeout_ends_in_a_named_error_not_a_bare_exception(self):
        with mock.patch.object(funnel.requests, "post", side_effect=requests.exceptions.ReadTimeout("x")), \
             mock.patch.object(funnel.time, "sleep"):
            with self.assertRaises(RuntimeError) as caught:
                funnel.fetch_wb_sales_funnel_day("2026-09-21")
        self.assertIn("сетевой отказ", str(caught.exception))

    def test_pages_are_paced_under_three_requests_per_minute(self):
        answers = [page([{"statistic": {"selected": {"orderCount": 1, "orderSum": 1}}}] * 1000), page([])]
        with mock.patch.object(funnel.requests, "post", side_effect=answers), \
             mock.patch.object(funnel.time, "sleep") as sleep:
            funnel.fetch_wb_sales_funnel_day("2026-09-21")
        self.assertGreaterEqual(funnel.PAGE_SLEEP_SECONDS, 20)
        self.assertIn(mock.call(funnel.PAGE_SLEEP_SECONDS), sleep.call_args_list)


if __name__ == "__main__":
    unittest.main()
