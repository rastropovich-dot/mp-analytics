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
        """Нефатальны ровно те шаги, каждый из которых разобран отдельно.

        Список расширяется только осознанно: каждая запись должна иметь
        причину в коде и разбор, почему её падение не рушит витрину.
          - total orders: питает только выключенную органику
          - дневные финоперации: /v3/finance/transaction/list отключён
            Ozon 2026-09-08, загрузчик ждёт миграции
          - загрузка FBO заказов: с 2026-09-11 429 per-second, загрузчик на
            /v3 падает вместо частичной записи; одна ночь добирается окном,
            расходы/реклама/KPI — нет. Решение владельца 2026-09-14,
            tests/test_fbo_step_non_fatal.py
          - лог статусов отправлений: пишет переходы из уже полученного
            сырья; отказ — потеря одного наблюдения, не заказов (2026-09-15)
          - загрузка FBS заказов: с 2026-09-16 загрузчик на /v4 падает вместо
            частичного результата, как FBO; та же развилка, то же решение,
            блокер ozon_fbs_orders_missing. tests/test_fbs_step_non_fatal.py
          - штуки выкупов: дописывает колонку buyouts_units к уже записанным
            строкам выкупов; отказ оставляет штуки null («не измерено»), деньги
            и KPI от шага не зависят (2026-09-21). tests/test_buyout_units.py
          - загрузка заказов WB: с правилом Ozon разбор строк строгий (Decimal,
            пустая цена и не-bool isCancel роняют шаг), а шаг стоит перед
            расходами, рекламой и KPI обеих площадок; ночь добирается окном
            записи. Решение советника 2026-09-21,
            tests/test_wb_orders_step_non_fatal.py
          - Sales Funnel WB: питает только overlay утреннего алерта; ночь 09-23
            он упал на ReadTimeout после 429 и унёс прогон до KPI обеих площадок.
            Решение владельца 2026-09-23, tests/test_wb_funnel_step_non_fatal.py
          - отчёт реализации WB: новая таблица wb_sales_report_rows, читателей у
            неё пока нет; finance-api 1 запрос/мин; отказ — одна ночь, окно 21
            день доберёт (2026-09-23, WB-5). tests/test_wb_sales_report_step.py
        """
        self.assertEqual(
            tuple(pipeline.NON_FATAL_STEPS),
            (STEP, "Ozon: дневные финоперации", "Ozon: загрузка FBO заказов",
             "Ozon: загрузка FBS заказов", "Ozon: лог статусов отправлений",
             "Ozon: штуки выкупов", "WB: загрузка заказов",
             "WB: загрузка заказов Analytics Sales Funnel", "WB: отчёт реализации"),
        )

    def test_steps_that_must_stay_fatal(self):
        # «WB: загрузка заказов» стоял в этом списке до 2026-09-21: тогда загрузчик
        # падать не умел вовсе. Продажи WB остаются — их правило не менялось.
        for title in ("KPI: расчет SKU", "KPI: расчет маркетплейсов",
                      "WB: загрузка продаж/выкупов", "Ozon: загрузка остатков"):
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
