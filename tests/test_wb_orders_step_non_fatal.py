"""Шаг заказов WB не должен уносить с собой ночь обеих площадок.

С правилом Ozon (2026-09-21) разбор строк WB стал строгим: деньги в Decimal,
отсутствующая или пустая цена и не-bool isCancel роняют загрузчик вместо
молчаливого нуля (loaders/wb_orders_rows.py). До этого загрузчик падать не умел
вовсе, и шаг спокойно числился фатальным. А стоит он вторым в прогоне — перед
расходами, рекламой и KPI Ozon и WB: одна кривая строка WB уносила бы всё.

Цена отказа — одна ночь заказов WB, которую 30-дневное окно записи доберёт
следующей ночью. Отказ обязан быть громким: тревога «Шаг не выполнен, прогон
продолжен» с именем шага. Продажи WB остаются фатальными — их правило не менялось.
"""
import types
import unittest
from unittest import mock

import run_daily_pipeline as pipeline
from loaders import wb_orders_rows as rules

STEP = "WB: загрузка заказов"


class WbOrdersStepNonFatalTests(unittest.TestCase):
    def test_wb_orders_step_is_non_fatal(self):
        self.assertIn(STEP, pipeline.NON_FATAL_STEPS)

    def test_wb_sales_step_is_non_fatal_since_the_rule_flipped(self):
        # До 2026-09-23 продажи WB оставались фатальными; правило «загрузчики нефатальны» (§5 тридцать третьей)
        self.assertIn("WB: загрузка продаж/выкупов", pipeline.NON_FATAL_STEPS)

    def test_wb_orders_step_runs_before_expenses_ads_and_kpi(self):
        titles = [title for title, _ in pipeline.build_steps()]
        for later in ("Ozon: расходы и комиссии", "Ozon: реклама Performance API",
                      "KPI: расчет SKU", "KPI: расчет маркетплейсов"):
            self.assertLess(titles.index(STEP), titles.index(later),
                            "порядок изменился — обоснование нефатальности надо пересмотреть")

    def test_the_reason_is_real_the_loader_does_fail_on_a_broken_row(self):
        """Нефатальность оправдана только пока разбор действительно падает."""
        broken = {"date": "2026-09-20T10:00:00", "nmId": 1, "isCancel": False, "srid": "s",
                  "totalPrice": 1000, "finishedPrice": 900}  # priceWithDisc не пришёл
        with self.assertRaises(RuntimeError):
            rules.build_order_rows([broken])

    def test_failure_does_not_stop_the_pipeline(self):
        process = mock.MagicMock()
        process.stdout = mock.MagicMock()
        process.stdout.__iter__.return_value = iter(["RuntimeError: WB orders: в строке нет priceWithDisc\n"])
        process.wait.return_value = 1
        with mock.patch.object(pipeline.subprocess, "Popen", return_value=process), \
             mock.patch.object(pipeline, "send_failure_alert") as alert:
            result = pipeline.run_step(STEP, "cmd", fatal=STEP not in pipeline.NON_FATAL_STEPS)

        self.assertTrue(result["failed"])
        self.assertEqual(result["returncode"], 1)
        alert.assert_called_once()
        self.assertEqual(alert.call_args.kwargs.get(
            "fatal", alert.call_args.args[3] if len(alert.call_args.args) > 3 else None), False)

    def test_failure_alert_names_the_step_and_is_not_a_crash(self):
        sent = {}

        def fake_post(url, json=None, timeout=None, **kwargs):
            sent["text"] = (json or {}).get("text", "")
            return mock.Mock(status_code=200)

        with mock.patch.object(pipeline, "TELEGRAM_BOT_TOKEN", "t"), \
             mock.patch.object(pipeline, "TELEGRAM_CHAT_ID", "c"), \
             mock.patch.object(pipeline.requests, "post", side_effect=fake_post):
            pipeline.send_failure_alert(
                STEP, 1, ["RuntimeError: WB orders: isCancel='да' — ожидался true/false, srid=abc"], fatal=False)

        self.assertIn("Шаг не выполнен, прогон продолжен", sent["text"])
        self.assertIn(STEP, sent["text"])
        self.assertIn("isCancel", sent["text"])
        self.assertNotIn("упал", sent["text"])

    def test_alert_is_sent_even_with_skip_telegram(self):
        args = mock.Mock(skip_telegram=True, skip_organic=False, skip_recovery=False,
                         skip_excel=False, skip_decision=False)
        should_skip, _ = pipeline.should_skip_pipeline_step(STEP, args, None, True)
        self.assertFalse(should_skip)


class PipelineContinuesTests(unittest.TestCase):
    """Шаги после упавших заказов WB обязаны выполниться — обеих площадок."""

    def test_steps_after_the_failure_still_run(self):
        args = types.SimpleNamespace(
            skip_recovery=True, skip_organic=True, skip_excel=True,
            skip_decision=True, skip_telegram=True,
            ozon_recovery_current_day_only=False,
            ozon_campaign_selection=None, ozon_recent_activity_days=None,
            ozon_dormant_probe_size=None, ozon_max_daily_cpc_units=None,
            ozon_allow_staged_cpc_partial=False,
        )
        ran, fatal_flags = [], {}

        def fake_run_step(title, command, fatal=True, nonfatal_returncodes=()):
            ran.append(title)
            fatal_flags[title] = fatal
            if title == STEP:
                return {"failed": True, "returncode": 1, "output_text": "",
                        "recovery_result": None, "ozon_run_summary": None}
            return {"output_text": "", "recovery_result": None, "ozon_run_summary": None}

        with mock.patch.object(pipeline, "parse_args", return_value=args), \
             mock.patch.object(pipeline, "record_pipeline_run"), \
             mock.patch.object(pipeline, "is_yesterday_cpc_loaded", return_value=False), \
             mock.patch.object(pipeline, "run_step", side_effect=fake_run_step):
            pipeline.main()

        self.assertIs(fatal_flags[STEP], False)
        self.assertIs(fatal_flags["WB: загрузка продаж/выкупов"], False)
        self.assertIs(fatal_flags["KPI: расчет SKU"], True)
        for title in ("WB: загрузка продаж/выкупов", "Ozon: загрузка FBS заказов",
                      "Ozon: расходы и комиссии", "Ozon: реклама Performance API",
                      "KPI: расчет SKU", "KPI: расчет маркетплейсов"):
            self.assertIn(title, ran, f"{title} не выполнился после падения заказов WB")
            self.assertGreater(ran.index(title), ran.index(STEP))


if __name__ == "__main__":
    unittest.main()
