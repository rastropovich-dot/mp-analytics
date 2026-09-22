"""Съём сырья flag=1: ночное окно, пустые и чужие ответы, продолжение с места.

Ночной прогон ходит в тот же statistics-api с тем же лимитом, поэтому в общем
ночном окне (loaders/pipeline_window.py, 00:15…04:30 UTC) скрипт не стартует и
останавливается. Пустой или чужой ответ в
сырьё не кладётся: файл с таким именем потом читался бы как «день снят».
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import scripts.wb_flag1_raw_capture as capture


def at(hour, minute):
    return datetime(2026, 9, 22, hour, minute, tzinfo=timezone.utc)


def response(items, status=200):
    body = json.dumps(items).encode("utf-8")
    fake = mock.Mock(status_code=status, content=body)
    fake.json.return_value = items
    return fake


def row(day):
    return {"date": f"{day}T10:00:00", "nmId": 1}


class NightWindowTests(unittest.TestCase):
    def test_window_is_the_shared_nightly_window(self):
        """Не своё 00:15…03:15, а общее: ночь 09-21 шла до 04:04 UTC."""
        self.assertFalse(capture.in_night_window(at(0, 14)))
        self.assertTrue(capture.in_night_window(at(0, 15)))
        self.assertTrue(capture.in_night_window(at(3, 15)))
        self.assertTrue(capture.in_night_window(at(4, 30)))
        self.assertFalse(capture.in_night_window(at(4, 31)))
        self.assertFalse(capture.in_night_window(at(17, 30)))

    def test_no_request_is_made_inside_the_window(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(capture, "datetime") as clock, \
             mock.patch.object(capture.requests, "get") as http_get:
            clock.now.return_value = at(1, 0)
            code = capture.main(["--out", tmp, "--dates", "2026-03-20"])

        self.assertEqual(code, 3)
        http_get.assert_not_called()


class CaptureTests(unittest.TestCase):
    def run_capture(self, tmp, days, answers):
        with mock.patch.object(capture, "in_night_window", return_value=False), \
             mock.patch.object(capture.time, "sleep"), \
             mock.patch.object(capture.requests, "get", side_effect=answers) as http_get:
            code = capture.main(["--out", tmp, "--dates", *days])
        return code, http_get

    def test_clean_day_is_saved_and_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = self.run_capture(tmp, ["2026-03-20"], [response([row("2026-03-20")])])
            saved = os.path.exists(os.path.join(tmp, "orders_flag1_2026-03-20.json"))
            with open(os.path.join(tmp, "capture_calls.json"), encoding="utf-8") as handle:
                ledger = json.load(handle)

        self.assertEqual(code, 0)
        self.assertTrue(saved)
        self.assertEqual((ledger[0]["status"], ledger[0]["rows"], ledger[0]["foreign_dates"]), (200, 1, []))

    def test_empty_answer_is_a_refusal_and_the_run_goes_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, http_get = self.run_capture(tmp, ["2026-03-20", "2026-03-21"],
                                              [response([]), response([row("2026-03-21")])])
            files = sorted(name for name in os.listdir(tmp) if name.startswith("orders_flag1_"))

        self.assertEqual(code, 0)
        self.assertEqual(http_get.call_count, 2)
        self.assertEqual(files, ["orders_flag1_2026-03-21.json"])

    def test_foreign_dates_are_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_capture(tmp, ["2026-03-20"], [response([row("2026-03-20"), row("2026-03-21")])])
            files = [name for name in os.listdir(tmp) if name.startswith("orders_flag1_")]

        self.assertEqual(files, [])

    def test_first_non_200_stops_the_series_without_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, http_get = self.run_capture(tmp, ["2026-03-20", "2026-03-21"], [response([], status=429)])

        self.assertEqual(code, 1)
        self.assertEqual(http_get.call_count, 1)

    def test_captured_days_are_skipped_on_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "orders_flag1_2026-03-20.json"), "w", encoding="utf-8") as handle:
                json.dump([row("2026-03-20")], handle)
            _, http_get = self.run_capture(tmp, ["2026-03-20", "2026-03-21"], [response([row("2026-03-21")])])

        self.assertEqual(http_get.call_count, 1)
        self.assertEqual(http_get.call_args.kwargs["params"]["dateFrom"], "2026-03-21")


if __name__ == "__main__":
    unittest.main()
