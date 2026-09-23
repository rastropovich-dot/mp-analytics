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

    def test_only_kpi_steps_are_fatal(self):
        """С 2026-09-23 (тридцать третья задача, §5) правило перевёрнуто: ни один загрузчик не фатален.

        Прежде нефатальными были ровно восемь шагов, внесённых по одному после падений (история —
        NON_FATAL_BEFORE_20260923 в run_daily_pipeline.py); ночь 09-23 девятый кандидат, Sales Funnel WB,
        успел уронить прогон до всех шагов Ozon и KPI. Фатальны только KPI — без них нет витрины.
        """
        self.assertEqual(set(pipeline.FATAL_STEPS), {"KPI: расчет SKU", "KPI: расчет маркетплейсов"})
        for reason in pipeline.FATAL_STEPS.values():
            self.assertTrue(reason.strip(), "фатальный шаг вносится только с причиной")
        titles = [title for title, _ in pipeline.build_steps()]
        self.assertEqual(set(pipeline.NON_FATAL_STEPS), set(titles) - set(pipeline.FATAL_STEPS))

    def test_every_earlier_non_fatal_step_is_still_non_fatal(self):
        for title in pipeline.NON_FATAL_BEFORE_20260923:
            self.assertIn(title, pipeline.NON_FATAL_STEPS, title)

    def test_loaders_that_used_to_be_fatal_are_not_anymore(self):
        for title in ("WB: загрузка продаж/выкупов", "WB: загрузка остатков", "Ozon: загрузка остатков",
                      "Ozon: расходы и комиссии", "Ozon: расчет organic sales по SKU"):
            self.assertFalse(pipeline.is_fatal_step(title), title)
        for title in ("KPI: расчет SKU", "KPI: расчет маркетплейсов"):
            self.assertTrue(pipeline.is_fatal_step(title), title)


class RunStepFatalityTests(unittest.TestCase):
    def _run(self, title, returncode):
        process = mock.MagicMock()
        process.stdout = mock.MagicMock()
        process.stdout.__iter__.return_value = iter(["line\n"])
        process.wait.return_value = returncode
        fatal = pipeline.is_fatal_step(title)
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

    def test_stocks_failure_no_longer_stops_the_pipeline(self):
        result = self._run("Ozon: загрузка остатков", 1)
        self.assertTrue(result["failed"])

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
             mock.patch.object(pipeline, "record_pipeline_run"), \
             mock.patch.object(pipeline, "is_yesterday_cpc_loaded", return_value=False), \
             mock.patch.object(pipeline, "run_step", side_effect=fake_run_step):
            pipeline.main()

        self.assertIn(STEP, ran)
        for title in ("Ozon: загрузка остатков", "KPI: расчет SKU", "KPI: расчет маркетплейсов"):
            self.assertIn(title, ran, f"{title} не выполнился после падения SKU-слоя")


if __name__ == "__main__":
    unittest.main()
