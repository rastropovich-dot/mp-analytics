"""Ночное окно заказов начинается с локальной полуночи: первый день окна обязан быть целым.

До 2026-09-19 окно было «сейчас − 30 дней» в UTC, то есть начиналось в ~03:20 МСК даты D.
Upsert замещает ключ (D, sku) строкой только из отправлений окна, и ранние заказы того же
товара за D терялись навсегда. Предсказание на 08-20 (−147 065 подтверждённых) сбылось до
рубля в ночь 09-19.
"""
import unittest
from datetime import datetime, timezone
from unittest import mock

import loaders.ozon_fbo_orders_loader as fbo
import loaders.ozon_fbs_orders_loader as fbs
from loaders import ozon_orders_rows as rules

NIGHT = datetime(2026, 9, 19, 0, 15, tzinfo=timezone.utc)   # 03:15 МСК 09-19 — время ночного прогона


def _resp(payload, status=200):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload
    r.text = ""
    return r


class WindowRuleTests(unittest.TestCase):
    def test_nightly_window_starts_at_local_midnight_of_first_day(self):
        since, to = rules.nightly_window(30, NIGHT)
        self.assertEqual(since, datetime(2026, 8, 19, 21, 0, tzinfo=timezone.utc))   # 00:00 МСК 08-20
        self.assertEqual(to, NIGHT)

    def test_first_day_follows_the_local_date_not_the_utc_date(self):
        """21:30 UTC 09-19 — это уже 09-20 по Москве, и первый день окна — 08-21."""
        since, _to = rules.nightly_window(30, datetime(2026, 9, 19, 21, 30, tzinfo=timezone.utc))
        self.assertEqual(since, datetime(2026, 8, 20, 21, 0, tzinfo=timezone.utc))

    def test_window_is_wider_than_before_by_the_missing_hours_only(self):
        since, to = rules.nightly_window(30, NIGHT)
        old_since = datetime(2026, 8, 20, 0, 15, tzinfo=timezone.utc)
        self.assertEqual((old_since - since).total_seconds(), 3 * 3600 + 15 * 60)

    def test_utc_window_accepts_dates_and_strings(self):
        from datetime import date
        self.assertEqual(rules.utc_window("2026-08-20", "2026-08-20", NIGHT), rules.utc_window(date(2026, 8, 20), date(2026, 8, 20), NIGHT))
        start, end = rules.utc_window("2026-08-20", "2026-08-20", NIGHT)
        self.assertEqual((start, end), (datetime(2026, 8, 19, 21, 0, tzinfo=timezone.utc), datetime(2026, 8, 20, 21, 0, tzinfo=timezone.utc)))


class LoaderRequestTests(unittest.TestCase):
    def _captured_filter(self, module, call):
        captured = {}

        def fake_post(url, **kw):
            captured.update((kw.get("json") or {}).get("filter") or {})
            return _resp({"postings": [], "cursor": "", "has_next": False})

        with mock.patch.object(module.http_retry, "post", side_effect=fake_post), \
             mock.patch.object(module.time, "sleep"), \
             mock.patch.object(rules, "utc_now", return_value=NIGHT):
            call()
        return captured

    def test_fbo_request_starts_at_local_midnight(self):
        f = self._captured_filter(fbo, lambda: fbo.get_fbo_postings(days_back=30))
        self.assertEqual(f["since"], "2026-08-19T21:00:00.000Z")
        self.assertEqual(f["to"], "2026-09-19T00:15:00.000Z")

    def test_fbs_request_starts_at_local_midnight(self):
        f = self._captured_filter(fbs, lambda: fbs.get_ozon_fbs_postings(days_back=30))
        self.assertEqual(f["since"], "2026-08-19T21:00:00.000Z")
        self.assertEqual(f["to"], "2026-09-19T00:15:00.000Z")

    def test_default_fbs_window_is_thirty_days(self):
        f = self._captured_filter(fbs, lambda: fbs.get_ozon_fbs_postings())
        self.assertEqual(f["since"], "2026-08-19T21:00:00.000Z")

    def test_explicit_bounds_are_passed_through_untouched(self):
        since, to = datetime(2026, 4, 1, 5, 7, tzinfo=timezone.utc), datetime(2026, 5, 1, tzinfo=timezone.utc)
        for module, call in ((fbo, lambda: fbo.get_fbo_postings(since=since, to=to)), (fbs, lambda: fbs.get_ozon_fbs_postings(since=since, to=to))):
            f = self._captured_filter(module, call)
            self.assertEqual((f["since"], f["to"]), ("2026-04-01T05:07:00.000Z", "2026-05-01T00:00:00.000Z"))


if __name__ == "__main__":
    unittest.main()
