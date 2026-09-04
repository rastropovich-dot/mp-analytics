"""Сбой SKU-слоя не должен уносить витрину.

Ночи на 2026-09-03 и 2026-09-04 умерли одинаково: шаг «Ozon: total orders
analytics по SKU» получал 429 code 8 на ПЕРВОМ запросе и валил всё, что идёт
после — остатки, KPI, decision, excel. Ретраи (34ea2e1) отработали как задумано
и отказ не изжили: предыдущее обращение к api-seller было за 2 ч 39 мин до того,
то есть дело не в нашей частоте.

Шаг питает единственную таблицу ozon_daily_sku_total_orders, которую читает
только расчёт органики, выключенный флагом --skip-organic. Ронять из-за него
день нечем.
"""

import types
import unittest
from unittest import mock

import run_daily_pipeline as pipeline

STEP = "Ozon: total orders analytics по SKU"


class NonFatalStepsTests(unittest.TestCase):
    def test_total_orders_is_declared_non_fatal(self):
        self.assertIn(STEP, pipeline.NON_FATAL_STEPS)

    def test_the_list_is_narrow(self):
        """Нефатальным должен быть ровно тот шаг, который мы разобрали."""
        self.assertEqual(tuple(pipeline.NON_FATAL_STEPS), (STEP,))

    def test_steps_that_must_stay_fatal(self):
        for title in ("KPI: расчет SKU", "KPI: расчет маркетплейсов",
                      "WB: загрузка заказов", "Ozon: загрузка остатков"):
            self.assertNotIn(title, pipeline.NON_FATAL_STEPS, title)


class RunStepFatalityTests(unittest.TestCase):
    def _run(self, title, returncode):
        process = mock.MagicMock()
        process.stdout = mock.MagicMock()
        process.stdout.__iter__.return_value = iter(["line\n"])
        process.wait.return_value = returncode
        fatal = title != "Ozon: реклама Performance API" and title not in pipeline.NON_FATAL_STEPS
        with mock.patch.object(pipeline.subprocess, "Popen", return_value=process), \
             mock.patch.object(pipeline, "send_failure_alert"):
            return pipeline.run_step(title, "cmd", fatal=fatal)

    def test_total_orders_failure_does_not_stop_the_pipeline(self):
        result = self._run(STEP, 1)
        self.assertTrue(result["failed"])
        self.assertEqual(result["returncode"], 1)

    def test_kpi_failure_still_stops_the_pipeline(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run("KPI: расчет SKU", 1)
        self.assertEqual(ctx.exception.code, 1)

    def test_stocks_failure_still_stops_the_pipeline(self):
        with self.assertRaises(SystemExit):
            self._run("Ozon: загрузка остатков", 1)

    def test_failure_alert_still_fires_for_the_non_fatal_step(self):
        """Нефатальный не значит незамеченный."""
        process = mock.MagicMock()
        process.stdout = mock.MagicMock()
        process.stdout.__iter__.return_value = iter(["line\n"])
        process.wait.return_value = 1
        with mock.patch.object(pipeline.subprocess, "Popen", return_value=process), \
             mock.patch.object(pipeline, "send_failure_alert") as alert:
            pipeline.run_step(STEP, "cmd", fatal=False)
        alert.assert_called_once()


class PipelineContinuesTests(unittest.TestCase):
    """Шаги после упавшего SKU-слоя обязаны выполниться."""

    def test_steps_after_the_failure_still_run(self):
        args = types.SimpleNamespace(
            skip_recovery=True, skip_organic=True, skip_excel=True,
            skip_decision=True, skip_telegram=True,
            ozon_recovery_current_day_only=False,
            ozon_campaign_selection=None, ozon_recent_activity_days=None,
            ozon_dormant_probe_size=None, ozon_max_daily_cpc_units=None,
            ozon_allow_staged_cpc_partial=False,
        )
        ran = []

        def fake_run_step(title, command, fatal=True, nonfatal_returncodes=()):
            ran.append(title)
            if title == STEP:
                return {"failed": True, "returncode": 1, "output_text": "",
                        "recovery_result": None, "ozon_run_summary": None}
            return {"output_text": "", "recovery_result": None, "ozon_run_summary": None}

        with mock.patch.object(pipeline, "parse_args", return_value=args), \
             mock.patch.object(pipeline, "is_yesterday_cpc_loaded", return_value=False), \
             mock.patch.object(pipeline, "run_step", side_effect=fake_run_step):
            pipeline.main()

        self.assertIn(STEP, ran)
        for title in ("Ozon: загрузка остатков", "KPI: расчет SKU", "KPI: расчет маркетплейсов"):
            self.assertIn(title, ran, f"{title} не выполнился после падения SKU-слоя")


if __name__ == "__main__":
    unittest.main()
