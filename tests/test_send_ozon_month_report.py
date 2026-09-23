"""Доставка книги «Ozon - <месяц>» в Telegram: какой месяц уходит, что сухой прогон ничего не шлёт, что отказ
шага громкий и алерт не роняет. Сеть и генератор здесь подменены — живой сухой прогон описан в docs/outbox.md.
"""
import importlib.util
import os
import subprocess
import unittest
from datetime import date, datetime, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("send_ozon_month_report", os.path.join(ROOT, "scripts", "send_ozon_month_report.py"))
send = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(send)

import alerts_telegram as alerts  # noqa: E402

SUMMARY = {"buyouts": {"turnover": "87054208.00", "revenue": "39531311.99", "fin_result": "9000000.00", "fin_result_index": "5500000.00"},
           "orders": {"created": "162480180.00", "forecast_confirmed": "98467177.97", "fin_result": "10011574.23", "drr_created": "0.0554",
                      "drr_forecast": "0.0918", "curve_nights": "2026-09-17 … 2026-09-21", "mature_days": 0, "forecast_days": 20},
           "warnings": []}
ENV = {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"}


class Window(unittest.TestCase):
    def test_month_is_the_month_of_yesterday(self):
        self.assertEqual(send.window_for(date(2026, 9, 21)), ("2026-09", "2026-09-21"))

    def test_on_the_first_the_previous_month_goes_whole(self):
        # 1 октября 07:30 UTC: вчера — 30 сентября, уходит сентябрь целиком; у октября ещё нет ни одного дня
        yesterday = send.yesterday_local(datetime(2026, 10, 1, 7, 30, tzinfo=timezone.utc))
        self.assertEqual(send.window_for(yesterday), ("2026-09", "2026-09-30"))

    def test_local_date_not_utc(self):
        # 21:30 UTC 30 сентября — в Москве уже 1 октября, вчера = 30 сентября
        self.assertEqual(send.yesterday_local(datetime(2026, 9, 30, 21, 30, tzinfo=timezone.utc)), date(2026, 9, 30))

    def test_foreign_month_without_date_to_goes_whole(self):
        self.assertEqual(send.month_end("2026-07"), "2026-07-31")


class Caption(unittest.TestCase):
    def test_caption_carries_measured_numbers(self):
        text = send.build_caption("2026-09", "2026-09-20", SUMMARY, False)
        self.assertIn("Ozon — сентябрь 2026, по 20 сентября", text)
        self.assertIn("оборот 87,1 млн", text)
        self.assertIn("фин. рез. по индексу СС 5,5 млн", text)
        self.assertIn("прогноз подтв. 98,5 млн", text)
        self.assertIn("ДРР 5,5 % от созданного / 9,2 % от прогноза", text)
        self.assertNotIn("⚠️", text)

    def test_empty_value_is_a_dash_not_a_zero_and_warnings_are_loud(self):
        summary = {"buyouts": {"turnover": "1.00", "revenue": "1.00", "fin_result": None}, "warnings": ["нет типов начислений за 2026-09-20"]}
        text = send.build_caption("2026-09", "2026-09-20", summary, True)
        self.assertIn("фин. рез. —", text)
        self.assertIn("Лист «Заказы» не собран", text)
        self.assertIn("нет типов начислений за 2026-09-20", text)
        self.assertLessEqual(len(text), 1024)


class GeneratorCall(unittest.TestCase):
    def test_book_is_built_into_data_reports_with_live_young_days(self):
        with mock.patch.object(send.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="")) as run, \
                mock.patch.object(send.os.path, "exists", return_value=False), mock.patch("builtins.print"):
            path, summary, code, _tail = send.generate("2026-09", "2026-09-21", send.REPORTS_DIR)
        cmd = run.call_args[0][0]
        self.assertIn("--fetch", cmd); self.assertEqual(cmd[cmd.index("--fetch") + 1], "young")
        self.assertNotIn("--no-fetch", cmd)
        self.assertTrue(cmd[cmd.index("--out") + 1].endswith(os.path.join("data", "reports", "ozon_2026-09_to_2026-09-21.xlsx")))
        self.assertTrue(send.REPORTS_DIR.endswith(os.path.join("data", "reports")))


class Delivery(unittest.TestCase):
    def run_main(self, argv, generated, sent=(True, "HTTP 200, ok=True")):
        with mock.patch.dict(os.environ, ENV), mock.patch.object(send, "generate", return_value=generated) as gen, \
                mock.patch.object(send, "telegram", return_value=sent) as tg, mock.patch.object(send, "send_document", return_value=sent) as doc, \
                mock.patch.object(send.os.path, "getsize", return_value=300000), mock.patch.object(send, "sha256_of", return_value="ab" * 32), \
                mock.patch.object(send, "record_delivery"), mock.patch("builtins.print"):
            return send.main(argv), gen, tg, doc

    def test_no_send_builds_the_book_and_sends_nothing(self):
        code, gen, tg, doc = self.run_main(["--no-send", "--month", "2026-09", "--date-to", "2026-09-20"], ("/tmp/x.xlsx", SUMMARY, 0, ""))
        self.assertEqual(code, 0)
        gen.assert_called_once(); self.assertEqual(gen.call_args[0][:2], ("2026-09", "2026-09-20"))
        tg.assert_not_called(); doc.assert_not_called()

    def test_send_posts_the_document_once(self):
        code, _gen, tg, doc = self.run_main(["--month", "2026-09", "--date-to", "2026-09-20"], ("/tmp/x.xlsx", SUMMARY, 0, ""))
        self.assertEqual(code, 0)
        doc.assert_called_once(); tg.assert_not_called()
        self.assertEqual(doc.call_args[0][0], "/tmp/x.xlsx")

    def test_generator_failure_is_said_in_telegram_and_returns_one(self):
        code, _gen, tg, doc = self.run_main(["--month", "2026-09", "--date-to", "2026-09-20"], (None, None, 1, "Traceback …"))
        self.assertEqual(code, 1)
        doc.assert_not_called(); tg.assert_called_once()
        self.assertEqual(tg.call_args[0][0], "sendMessage")
        self.assertIn("не отправлен", tg.call_args[1]["json"]["text"])

    def test_generator_failure_in_dry_run_stays_silent_in_telegram(self):
        code, _gen, tg, doc = self.run_main(["--no-send", "--month", "2026-09", "--date-to", "2026-09-20"], (None, None, 1, "Traceback …"))
        self.assertEqual(code, 1)
        tg.assert_not_called(); doc.assert_not_called()

    def test_refused_document_is_a_failure_not_a_success(self):
        code, _gen, tg, _doc = self.run_main(["--month", "2026-09", "--date-to", "2026-09-20"], ("/tmp/x.xlsx", SUMMARY, 0, ""), sent=(False, "HTTP 413, ok=False"))
        self.assertEqual(code, 1)
        tg.assert_called_once()

    def test_orders_sheet_failure_still_delivers_the_rest(self):
        code, _gen, _tg, doc = self.run_main(["--month", "2026-09", "--date-to", "2026-09-20"], ("/tmp/x.xlsx", SUMMARY, send.ORDERS_SHEET_FAILED, ""))
        self.assertEqual(code, 0)
        self.assertIn("Лист «Заказы» не собран", doc.call_args[0][1])

    def test_missing_credentials_stop_before_any_work(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}), mock.patch.object(send, "generate") as gen, mock.patch("builtins.print"):
            self.assertEqual(send.main([]), 1)
        gen.assert_not_called()


class AlertStep(unittest.TestCase):
    def alert(self, argv, sheet_outcome=0):
        with mock.patch.object(alerts, "build_message", return_value="message"), mock.patch.object(alerts, "send_telegram") as tg, \
                mock.patch.object(alerts.subprocess, "run", return_value=mock.Mock(returncode=sheet_outcome)) as run, mock.patch("builtins.print"), \
                mock.patch.object(alerts, "TELEGRAM_BOT_TOKEN", "token"), mock.patch.object(alerts, "TELEGRAM_CHAT_ID", "chat"):
            alerts.main(argv)
        return tg, run

    def test_morning_run_sends_the_alert_first_and_then_the_sheet(self):
        tg, run = self.alert([])
        tg.assert_called_once_with("message")
        run.assert_called_once()
        self.assertTrue(run.call_args[0][0][1].endswith("send_ozon_month_report.py"))
        self.assertNotIn("--no-send", run.call_args[0][0])

    def test_dry_run_does_not_build_the_sheet_unless_asked(self):
        _tg, run = self.alert(["--dry-run"])
        run.assert_not_called()
        tg, run = self.alert(["--dry-run", "--with-sheet"])
        self.assertIn("--no-send", run.call_args[0][0])
        tg.assert_not_called()

    def test_skip_sheet(self):
        _tg, run = self.alert(["--skip-sheet"])
        run.assert_not_called()

    def test_step_that_reported_itself_is_not_reported_twice(self):
        tg, _run = self.alert([], sheet_outcome=1)
        tg.assert_called_once_with("message")

    def test_step_that_died_silently_is_reported_and_alert_survives(self):
        tg, _run = self.alert([], sheet_outcome=-9)
        self.assertEqual(tg.call_count, 2)
        self.assertIn("Лист Ozon не отправлен", tg.call_args_list[1][0][0])
        with mock.patch.object(alerts, "build_message", return_value="message"), mock.patch.object(alerts, "send_telegram") as tg, \
                mock.patch.object(alerts.subprocess, "run", side_effect=subprocess.TimeoutExpired("cmd", 1200)), mock.patch("builtins.print"), \
                mock.patch.object(alerts, "TELEGRAM_BOT_TOKEN", "token"), mock.patch.object(alerts, "TELEGRAM_CHAT_ID", "chat"):
            self.assertEqual(alerts.main([]), "message")
        self.assertEqual(tg.call_count, 2)


if __name__ == "__main__":
    unittest.main()
