"""Реклама WB (adv/v1/upd): зерно (advertId, updTime), день по МСК, окно 31 день одним обращением,
429 повторяется и заканчивается именованным отказом, dry-run ничего не пишет, дубль пары — отказ.
"""
import unittest
from collections import Counter
from datetime import date
from decimal import Decimal
from unittest import mock

import loaders.wb_ads_loader as loader

ITEM = {"updNum": 0, "updTime": "2026-03-03T20:49:33.54322+03:00", "updSum": 24, "advertId": 3355881, "campName": "лук лучок",
        "advertType": 9, "paymentType": "Баланс", "advertStatus": 9, "currency": "RUB"}


class ParseTests(unittest.TestCase):
    def test_five_digit_fraction_and_moscow_day(self):
        self.assertEqual(loader.parse_ts("2026-03-03T20:49:33.54322+03:00").isoformat(), "2026-03-03T20:49:33.543220+03:00")
        self.assertEqual(loader.upd_day("2026-03-03T22:30:00Z"), "2026-03-04")           # 01:30 МСК
        self.assertEqual(loader.upd_day("2026-03-03T20:49:33.54322+03:00"), "2026-03-03")

    def test_build_row_maps_fields_and_keeps_the_sum_as_text(self):
        r = loader.build_row(ITEM, "2026-09-23T12:00:00+00:00")
        self.assertEqual((r["advert_id"], r["upd_day"], r["upd_sum"], r["payment_type"], r["advert_type"], r["upd_num"]), (3355881, "2026-03-03", "24", "Баланс", 9, 0))
        self.assertEqual(r["upd_time"], "2026-03-03T20:49:33.543220+03:00")
        self.assertEqual(r["observed_at"], "2026-09-23T12:00:00+00:00")

    def test_missing_required_field_refuses(self):
        with self.assertRaises(RuntimeError):
            loader.build_row({**ITEM, "updTime": None}, "t")
        with self.assertRaises(RuntimeError):
            loader.build_row({**ITEM, "updSum": ""}, "t")

    def test_duplicate_pair_refuses(self):
        with self.assertRaises(RuntimeError):
            loader.check_items([ITEM, dict(ITEM)])
        loader.check_items([ITEM, {**ITEM, "updTime": "2026-03-04T10:00:00+03:00"}])


class FakeResp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        return self._body


class RequestTests(unittest.TestCase):
    def test_429_is_retried_then_succeeds(self):
        sleeps = []
        with mock.patch.object(loader.requests, "get", side_effect=[FakeResp(429), FakeResp(200, [ITEM])]):
            counters = Counter()
            items = loader.request_upd("2026-08-23", "2026-09-22", counters, sleep_fn=sleeps.append)
        self.assertEqual(len(items), 1)
        self.assertEqual((counters["requests"], counters["429"]), (2, 1))
        self.assertEqual(sleeps, [loader.RETRY_SECONDS])

    def test_429_not_survived_is_a_named_error(self):
        with mock.patch.object(loader.requests, "get", side_effect=[FakeResp(429)] * loader.MAX_ATTEMPTS):
            with self.assertRaises(RuntimeError) as ctx:
                loader.request_upd("2026-08-23", "2026-09-22", Counter(), sleep_fn=lambda _s: None)
        self.assertIn("429", str(ctx.exception))

    def test_interval_longer_than_31_days_refuses_before_the_call(self):
        with mock.patch.object(loader.requests, "get") as get:
            with self.assertRaises(RuntimeError):
                loader.request_upd("2026-08-01", "2026-09-22", Counter())
        get.assert_not_called()


class RunTests(unittest.TestCase):
    def test_window_is_31_days_to_yesterday_in_one_call_and_dry_run_writes_nothing(self):
        sb = mock.MagicMock()
        with mock.patch.object(loader, "request_upd", return_value=[ITEM]) as req:
            result = loader.run(sb, days_back=31, today=date(2026, 9, 23), dry_run=True)
        req.assert_called_once()
        self.assertEqual(req.call_args[0][:2], ("2026-08-23", "2026-09-22"))
        self.assertEqual((result["rows"], result["written"]), (1, 0))
        sb.table.assert_not_called()

    def test_run_upserts_on_the_pair(self):
        sb = mock.MagicMock()
        with mock.patch.object(loader, "request_upd", return_value=[ITEM]):
            result = loader.run(sb, days_back=31, today=date(2026, 9, 23))
        self.assertEqual(result["written"], 1)
        self.assertEqual(sb.table.return_value.upsert.call_args[1]["on_conflict"], "advert_id,upd_time")

    def test_empty_window_is_not_an_error(self):
        sb = mock.MagicMock()
        with mock.patch.object(loader, "request_upd", return_value=[]):
            result = loader.run(sb, days_back=31, today=date(2026, 9, 23))
        self.assertEqual((result["rows"], result["written"]), (0, 0))
        sb.table.assert_not_called()

    def test_summarize_sums_with_vat_as_given(self):
        rows = [loader.build_row(ITEM, "t"), loader.build_row({**ITEM, "updTime": "2026-03-04T10:00:00+03:00", "updSum": 76}, "t")]
        text = loader.summarize(rows)
        self.assertIn("строк 2, дней 2", text)
        self.assertIn("100.00", text)
        self.assertEqual(Decimal(rows[1]["upd_sum"]), Decimal("76"))


if __name__ == "__main__":
    unittest.main()
