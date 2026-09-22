"""Запас по 429 у аналитики заказов: шесть попыток с паузами 1, 2, 4, 8, 16 с и пауза 1,5 с между страницами.

Ночь 09-22: три 429 rate_limit_per_second подряд, шаг прошёл с четвёртой попытки из четырёх — запаса не было.
"""
import unittest
from unittest import mock

import loaders.ozon_sku_total_analytics_loader as seller


def _response(status, body=None):
    r = mock.Mock(status_code=status, headers={}, text="")
    r.json.return_value = body if body is not None else {"result": {"data": []}}
    return r


RATE_LIMIT = {"code": 8, "message": "You have reached request rate limit per second"}


class Backoff(unittest.TestCase):
    def test_five_rate_limits_then_success_sleep_1_2_4_8_16(self):
        responses = [_response(429, RATE_LIMIT)] * 5 + [_response(200, {"result": {"data": [], "totals": []}})]
        slept = []
        with mock.patch.object(seller.requests, "post", side_effect=responses) as post, mock.patch("builtins.print"):
            seller.request_page("2026-09-21", "2026-09-21", 1000, 0, sleep_fn=slept.append)
        self.assertEqual(post.call_count, 6)
        self.assertEqual(slept, [1, 2, 4, 8, 16])

    def test_six_rate_limits_raise_after_the_sixth_attempt(self):
        responses = [_response(429, RATE_LIMIT)] * 6
        slept = []
        with mock.patch.object(seller.requests, "post", side_effect=responses) as post, mock.patch("builtins.print"), self.assertRaises(RuntimeError) as ctx:
            seller.request_page("2026-09-21", "2026-09-21", 1000, 0, sleep_fn=slept.append)
        self.assertEqual(post.call_count, 6)
        self.assertEqual(slept, [1, 2, 4, 8, 16])
        self.assertIn("attempts=6", str(ctx.exception))

    def test_backoff_is_capped_at_32(self):
        self.assertEqual(seller.seller_retry_sleep_seconds(_response(429, RATE_LIMIT), 7), 32)
        self.assertEqual((seller.SELLER_RETRY_MAX_ATTEMPTS, seller.SELLER_RETRY_CAP_SLEEP_SECONDS), (6, 32))

    def test_pages_are_separated_by_a_pause_but_the_first_is_not(self):
        pages = [({"result": {"data": [{"dimensions": [], "metrics": []}] * 2, "totals": []}}, {"offset": 0}),
                 ({"result": {"data": [{"dimensions": [], "metrics": []}] * 2, "totals": []}}, {"offset": 2}),
                 ({"result": {"data": [{"dimensions": [], "metrics": []}], "totals": []}}, {"offset": 4})]
        slept = []
        with mock.patch.object(seller, "request_page", side_effect=pages), mock.patch.object(seller, "parse_rows", side_effect=lambda d: (d["result"]["data"], None)), \
                mock.patch("builtins.print"):
            rows, meta = seller.fetch_total_orders("2026-09-21", "2026-09-21", page_size=2, sleep_fn=slept.append)
        self.assertEqual((len(rows), meta["page_count"]), (5, 3))
        self.assertEqual(slept, [1.5, 1.5])


if __name__ == "__main__":
    unittest.main()
