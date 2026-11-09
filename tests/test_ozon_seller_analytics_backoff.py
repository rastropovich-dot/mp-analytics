"""Темп аналитики заказов (/v1/analytics/data): интервал между страницами и ожидания по отказам.

Было до 2026-11-08: шесть попыток с паузами 1, 2, 4, 8, 16 с и пауза 1,5 с между страницами (ночь 09-22: три 429 подряд,
прошло с четвёртой попытки из четырёх). Ночи 02, 05 и 08.11 упали: 429 на шести попытках за ~30 с. Спека метода —
«не больше 1 раза в минуту», мы ходили раз в ~2 с. Стало: между запросами не меньше 60 с, 5xx — backoff не меньше
интервала, все ожидания по отказам — в бюджете прогона.
"""
import unittest
from unittest import mock

import loaders.ozon_sku_total_analytics_loader as seller


def _response(status, body=None, headers=None):
    r = mock.Mock(status_code=status, headers=headers or {}, text="")
    r.json.return_value = body if body is not None else {"result": {"data": []}}
    return r


RATE_LIMIT = {"code": 8, "message": "You have reached request rate limit per second"}


class FakeClock:
    def __init__(self, start=1000.0, request_seconds=0.0):
        self.now = start
        self.slept = []
        self.request_seconds = request_seconds

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(round(seconds, 3))
        self.now += seconds


def _post_with_duration(clock, responses):
    it = iter(responses)

    def post(*_a, **_k):
        clock.now += clock.request_seconds
        return next(it)
    return post


def _page(n):
    return {"result": {"data": [{"dimensions": [], "metrics": []}] * n, "totals": []}}


class Backoff(unittest.TestCase):
    def test_server_errors_wait_at_least_the_interval(self):
        clock = FakeClock()
        responses = [_response(500)] * 2 + [_response(200, _page(0))]
        with mock.patch.object(seller.requests, "post", side_effect=responses) as post, mock.patch("builtins.print"):
            seller.request_page("2026-09-21", "2026-09-21", 1000, 0,
                                pacer=seller.SellerAnalyticsPacer(sleep_fn=clock.sleep, clock=clock))
        self.assertEqual(post.call_count, 3)
        self.assertEqual(clock.slept, [60, 60])

    def test_server_errors_keep_the_old_backoff_when_interval_is_short(self):
        clock = FakeClock()
        responses = [_response(500)] * 5 + [_response(200, _page(0))]
        with mock.patch.object(seller.requests, "post", side_effect=responses) as post, mock.patch("builtins.print"):
            seller.request_page("2026-09-21", "2026-09-21", 1000, 0,
                                pacer=seller.SellerAnalyticsPacer(sleep_fn=clock.sleep, clock=clock, interval=0))
        self.assertEqual(post.call_count, 6)
        self.assertEqual(clock.slept, [1, 2, 4, 8, 16])

    def test_six_server_errors_raise_after_the_sixth_attempt(self):
        clock = FakeClock()
        with mock.patch.object(seller.requests, "post", side_effect=[_response(502)] * 6) as post, \
                mock.patch("builtins.print"), self.assertRaises(RuntimeError) as ctx:
            seller.request_page("2026-09-21", "2026-09-21", 1000, 0,
                                pacer=seller.SellerAnalyticsPacer(sleep_fn=clock.sleep, clock=clock, interval=0))
        self.assertEqual(post.call_count, 6)
        self.assertIn("attempts=6", str(ctx.exception))

    def test_backoff_is_capped_at_32(self):
        self.assertEqual(seller.seller_retry_sleep_seconds(_response(500), 7), 32)
        self.assertEqual((seller.SELLER_RETRY_MAX_ATTEMPTS, seller.SELLER_RETRY_CAP_SLEEP_SECONDS), (6, 32))

    def test_total_equal_to_page_size_does_not_stop_paging(self):
        # analytics/data отдаёт total = размеру страницы (РБХ-1, 21.10: offset 1000 и 2000 — по 1 000 строк при total 1000);
        # конец — только короткая страница
        full = _page(2)
        short = _page(1)
        pages = [(full, {"offset": 0}), (full, {"offset": 2}), (full, {"offset": 4}), (short, {"offset": 6})]
        with mock.patch.object(seller, "request_page", side_effect=pages), \
                mock.patch.object(seller, "parse_rows", side_effect=lambda d: (d["result"]["data"], 2)), mock.patch("builtins.print"):
            rows, meta = seller.fetch_total_orders("2026-09-21", "2026-09-21", page_size=2, sleep_fn=lambda s: None)
        self.assertEqual((len(rows), meta["page_count"], meta["total_rows_reported"]), (7, 4, 2))

    def test_pages_are_one_interval_apart_but_the_first_waits_for_nothing(self):
        clock = FakeClock()
        responses = [_response(200, _page(2)), _response(200, _page(2)), _response(200, _page(1))]
        with mock.patch.object(seller.requests, "post", side_effect=responses), \
                mock.patch.object(seller, "parse_rows", side_effect=lambda d: (d["result"]["data"], None)), mock.patch("builtins.print"):
            rows, meta = seller.fetch_total_orders("2026-09-21", "2026-09-21", page_size=2,
                                                   sleep_fn=clock.sleep, clock=clock)
        self.assertEqual((len(rows), meta["page_count"]), (5, 3))
        self.assertEqual(clock.slept, [60, 60])
        self.assertEqual((meta["pacing"]["interval_pauses"], meta["pacing"]["retry_waits"]), (2, 0))

    def test_interval_counts_from_the_start_of_the_previous_request(self):
        clock = FakeClock(request_seconds=7)
        responses = [_response(200, _page(2)), _response(200, _page(1))]
        with mock.patch.object(seller.requests, "post", side_effect=_post_with_duration(clock, responses)), \
                mock.patch.object(seller, "parse_rows", side_effect=lambda d: (d["result"]["data"], None)), mock.patch("builtins.print"):
            seller.fetch_total_orders("2026-09-21", "2026-09-21", page_size=2, sleep_fn=clock.sleep, clock=clock)
        self.assertEqual(clock.slept, [53])   # 60 − 7 с самого запроса


if __name__ == "__main__":
    unittest.main()
