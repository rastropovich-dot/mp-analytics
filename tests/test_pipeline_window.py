"""Окно ночного прогона — одно место (loaders/pipeline_window.py). Ночь 09-21 шла до 04:04 UTC, верх — 04:30."""
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from loaders import pipeline_window as pw
from scripts import fetch_accrual_postings_raw, measure_buyouts_history_vs_accrual, rebuild_ozon_orders_history


def utc(h, m):
    return datetime(2026, 9, 22, h, m, tzinfo=timezone.utc)


class NightlyWindow(unittest.TestCase):
    def test_boundaries(self):
        self.assertFalse(pw.in_nightly_run_window(utc(0, 14)))
        self.assertTrue(pw.in_nightly_run_window(utc(0, 15)))
        self.assertTrue(pw.in_nightly_run_window(utc(4, 4)))      # финиш ночи 09-21
        self.assertTrue(pw.in_nightly_run_window(utc(4, 30)))
        self.assertFalse(pw.in_nightly_run_window(utc(4, 31)))
        self.assertFalse(pw.in_nightly_run_window(utc(19, 0)))

    def test_local_time_is_converted_to_utc(self):
        # 03:20 МСК = 00:20 UTC — внутри окна; 06:00 МСК = 03:00 UTC — тоже
        self.assertTrue(pw.in_nightly_run_window(datetime(2026, 9, 22, 3, 20, tzinfo=ZoneInfo("Europe/Moscow"))))
        self.assertFalse(pw.in_nightly_run_window(datetime(2026, 9, 22, 0, 10, tzinfo=ZoneInfo("Europe/Moscow"))))

    def test_morning_alert_window(self):
        self.assertTrue(pw.in_morning_alert_window(utc(7, 30)))
        self.assertFalse(pw.in_morning_alert_window(utc(7, 46)))

    def test_text(self):
        self.assertEqual(pw.window_text(), "00:15…04:30 UTC")

    def test_scripts_share_the_one_constant(self):
        for mod in (fetch_accrual_postings_raw, measure_buyouts_history_vs_accrual, rebuild_ozon_orders_history):
            self.assertIs(mod.in_night_window, pw.in_nightly_run_window)
            self.assertTrue(mod.in_night_window(utc(4, 20)))


if __name__ == "__main__":
    unittest.main()
