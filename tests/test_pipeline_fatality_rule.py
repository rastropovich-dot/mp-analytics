"""Правило с 2026-09-23: загрузчики нефатальны, фатальны только шаги KPI; пропущенные шаги — одной строкой утром.

Ночь 09-23 фатальный WB-шаг (третий по счёту) унёс все шаги Ozon и KPI обеих площадок. Теперь падение любого
загрузчика не останавливает KPI, падение KPI останавливает; итог прогона пишется в pipeline_runtime_state, и
утренний алерт называет пропущенные шаги или говорит, что итога нет (прогон умер на KPI или не запускался).
Все тесты — на подменённых run_step / клиенте базы: в боевую базу не ходит ни один.
"""
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import alerts_telegram as alerts
import run_daily_pipeline as pipeline

ARGS = types.SimpleNamespace(
    skip_recovery=True, skip_organic=True, skip_excel=True, skip_decision=True, skip_telegram=True,
    ozon_recovery_current_day_only=False, ozon_campaign_selection=None, ozon_recent_activity_days=None,
    ozon_dormant_probe_size=None, ozon_max_daily_cpc_units=None, ozon_allow_staged_cpc_partial=False,
)
OK = {"output_text": "", "recovery_result": None, "ozon_run_summary": None}


def run_main(fail_titles):
    ran, recorded = [], []

    def fake_run_step(title, command, fatal=True, nonfatal_returncodes=()):
        ran.append((title, fatal))
        if title in fail_titles:
            if fatal:
                raise SystemExit(1)                      # так делает настоящий run_step на фатальном шаге
            return {"failed": True, "returncode": 1, "output_text": "", "recovery_result": None, "ozon_run_summary": None}
        return dict(OK)

    with mock.patch.object(pipeline, "parse_args", return_value=ARGS), \
            mock.patch.object(pipeline, "is_yesterday_cpc_loaded", return_value=False), \
            mock.patch.object(pipeline, "run_step", side_effect=fake_run_step), \
            mock.patch.object(pipeline, "record_pipeline_run", side_effect=lambda s, f, failed: recorded.append(failed)), \
            mock.patch("builtins.print"):
        try:
            pipeline.main()
            exited = False
        except SystemExit:
            exited = True
    return ran, recorded, exited


class PipelineRule(unittest.TestCase):
    def test_any_loader_failure_leaves_kpi_running(self):
        loaders = [t for t, _ in pipeline.build_steps(ARGS) if not t.startswith(("KPI", "Decision", "Excel", "Telegram"))]
        for title in loaders:
            ran, recorded, exited = run_main({title})
            titles = [t for t, _ in ran]
            if title not in titles:
                continue                                  # шаг пропущен флагом — не упал
            self.assertFalse(exited, title)
            self.assertIn("KPI: расчет SKU", titles, title)
            self.assertIn("KPI: расчет маркетплейсов", titles, title)
            self.assertEqual(recorded, [[{"title": title, "returncode": 1}]], title)

    def test_kpi_failure_stops_the_run_and_records_nothing(self):
        ran, recorded, exited = run_main({"KPI: расчет SKU"})
        self.assertTrue(exited)
        self.assertNotIn("KPI: расчет маркетплейсов", [t for t, _ in ran])
        self.assertEqual(recorded, [])                   # итога нет — утром это и видно

    def test_only_kpi_steps_run_as_fatal(self):
        ran, _recorded, _exited = run_main(set())
        self.assertEqual({t for t, fatal in ran if fatal}, {"KPI: расчет SKU", "KPI: расчет маркетплейсов"})

    def test_steps_keep_their_order(self):
        titles = [t for t, _ in pipeline.build_steps()]
        self.assertLess(titles.index("WB: загрузка заказов Analytics Sales Funnel"), titles.index("Ozon: загрузка FBS заказов"))
        self.assertEqual(titles[-5:-3], ["KPI: расчет SKU", "KPI: расчет маркетплейсов"])

    def test_every_non_fatal_step_sends_step_not_done_alert(self):
        for title in pipeline.NON_FATAL_STEPS:
            process = mock.MagicMock()
            process.stdout.__iter__.return_value = iter(["boom\n"])
            process.wait.return_value = 1
            with mock.patch.object(pipeline.subprocess, "Popen", return_value=process), \
                    mock.patch.object(pipeline, "send_failure_alert") as alert, mock.patch("builtins.print"):
                result = pipeline.run_step(title, "cmd", fatal=pipeline.is_fatal_step(title))
            self.assertTrue(result["failed"], title)
            alert.assert_called_once()
            self.assertIs(alert.call_args[1]["fatal"], False, title)

    def test_failed_steps_line(self):
        self.assertEqual(pipeline.failed_steps_line([]), "Шаги не выполнены: нет")
        line = pipeline.failed_steps_line([{"title": "WB: загрузка остатков", "returncode": 1}, {"title": "Ozon: загрузка остатков", "returncode": 2}])
        self.assertEqual(line, "⚠️ Шаги не выполнены (2): WB: загрузка остатков (код 1); Ozon: загрузка остатков (код 2)")

    def test_record_upserts_one_row_by_key(self):
        client = mock.MagicMock()
        start = datetime(2026, 9, 24, 0, 15, tzinfo=timezone.utc)
        with mock.patch("builtins.print"):
            pipeline.record_pipeline_run(start, start + timedelta(hours=2), [{"title": "x", "returncode": 1}], client=client)
        client.table.assert_called_once_with("pipeline_runtime_state")
        row = client.table.return_value.upsert.call_args[0][0]
        self.assertEqual((row["state_key"], row["state_type"]), ("pipeline_run:last", "pipeline_run"))
        self.assertEqual(row["payload"]["failed_steps"], [{"title": "x", "returncode": 1}])
        self.assertEqual(client.table.return_value.upsert.call_args[1]["on_conflict"], "state_key")

    def test_record_failure_does_not_raise(self):
        client = mock.MagicMock()
        client.table.side_effect = RuntimeError("db down")
        now = datetime.now(timezone.utc)
        with mock.patch("builtins.print"):
            pipeline.record_pipeline_run(now, now, [], client=client)


class MorningLine(unittest.TestCase):
    NOW = datetime(2026, 9, 24, 7, 30, tzinfo=timezone.utc)

    def test_clean_night_says_nothing(self):
        self.assertEqual(alerts.pipeline_run_line({"finished_at": "2026-09-24T02:57:00+00:00", "failed_steps": []}, now=self.NOW), "")

    def test_failed_steps_are_listed_in_one_line(self):
        run = {"finished_at": "2026-09-24T02:57:00+00:00",
               "failed_steps": [{"title": "WB: загрузка заказов Analytics Sales Funnel", "returncode": 1}, {"title": "Ozon: загрузка остатков", "returncode": 1}]}
        line = alerts.pipeline_run_line(run, now=self.NOW)
        self.assertEqual(line, "⚠️ Ночью не выполнены шаги (2): WB: загрузка заказов Analytics Sales Funnel (код 1); Ozon: загрузка остатков (код 1)")
        self.assertNotIn("\n", line)

    def test_missing_record_is_loud(self):
        self.assertIn("итог не записан", alerts.pipeline_run_line(None, now=self.NOW))

    def test_stale_record_means_the_night_did_not_finish(self):
        line = alerts.pipeline_run_line({"finished_at": "2026-09-23T02:57:00+00:00", "failed_steps": []}, now=self.NOW)
        self.assertIn("сегодняшняя ночь не завершилась", line)

    def test_unreadable_record_is_said_not_raised(self):
        self.assertIn("не читается", alerts.pipeline_run_line({"finished_at": "вчера"}, now=self.NOW))


if __name__ == "__main__":
    unittest.main()
