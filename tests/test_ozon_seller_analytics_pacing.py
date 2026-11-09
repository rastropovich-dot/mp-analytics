"""Аналитика заказов: ожидание по заголовкам 429, бюджет ожиданий прогона, строка шага (inbox 08.11, п. 11).

Ночь 08.11 KARATOV (лог Render): страница offset 0 — 429 и 200 со 2-й попытки, offset 1000 — с 3-й, offset 2000 — 429 на
всех шести попытках за 34 с, шаг упал с кодом 1. Спека /v1/analytics/data: «не больше 1 раза в минуту».
Сеть в тестах не нужна: requests.post подставной, часы идут только во сне.
"""
import unittest
from unittest import mock

import loaders.ozon_sku_total_analytics_loader as seller


RATE_LIMIT = {"code": 8, "message": "You have reached request rate limit per second"}


def _response(status, body=None, headers=None):
    r = mock.Mock(status_code=status, headers=headers or {}, text="")
    r.json.return_value = body if body is not None else {"result": {"data": []}}
    return r


def _page(n):
    return {"result": {"data": [{"dimensions": [], "metrics": []}] * n, "totals": []}}


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(round(seconds, 3))
        self.now += seconds


def _fetch(responses, page_size=2, **pacer_kw):
    clock = FakeClock()
    pacer = seller.SellerAnalyticsPacer(sleep_fn=clock.sleep, clock=clock, **pacer_kw)
    printed = []
    with mock.patch.object(seller.requests, "post", side_effect=responses) as post, \
            mock.patch.object(seller, "parse_rows", side_effect=lambda d: (d["result"]["data"], None)), \
            mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
        try:
            rows, meta = seller.fetch_total_orders("2026-11-07", "2026-11-07", page_size=page_size, pacer=pacer)
            error = None
        except RuntimeError as exc:
            rows, meta, error = None, None, exc
    return rows, meta, error, clock, post, printed, pacer


class HeaderWaits(unittest.TestCase):
    def test_wait_source_by_headers(self):
        self.assertEqual(seller.rate_limit_wait_seconds(_response(429, RATE_LIMIT, {"Retry-After": "90"}), 60),
                         (90, "retry_after=90s"))
        self.assertEqual(seller.rate_limit_wait_seconds(_response(429, RATE_LIMIT, {"Retry-After": "1"}), 60),
                         (60, "retry_after=1s"))
        self.assertEqual(seller.rate_limit_wait_seconds(_response(429, RATE_LIMIT, {"ratelimit-remaining": "0"}), 60),
                         (60, "ratelimit_remaining=0"))
        self.assertEqual(seller.rate_limit_wait_seconds(_response(429, RATE_LIMIT), 60), (60, "no_headers"))
        self.assertEqual(seller.rate_limit_wait_seconds(_response(429, RATE_LIMIT, {"Retry-After": "121"}), 60),
                         (None, "retry_after=121s"))

    def test_remaining_zero_on_success_delays_the_next_page_by_retry_after(self):
        responses = [_response(200, _page(2), {"ratelimit-remaining": "0", "Retry-After": "90"}),
                     _response(200, _page(1))]
        rows, meta, error, clock, _post, _printed, _pacer = _fetch(responses)
        self.assertIsNone(error)
        self.assertEqual(clock.slept, [90])

    def test_remaining_above_zero_keeps_the_plain_interval(self):
        responses = [_response(200, _page(2), {"ratelimit-remaining": "3", "Retry-After": "90"}),
                     _response(200, _page(1))]
        _rows, _meta, error, clock, _post, _printed, _pacer = _fetch(responses)
        self.assertIsNone(error)
        self.assertEqual(clock.slept, [60])


class Budget(unittest.TestCase):
    def test_budget_stops_before_the_wait_that_would_exceed_it(self):
        responses = [_response(429, RATE_LIMIT), _response(200, _page(2)),
                     _response(429, RATE_LIMIT), _response(200, _page(1))]
        _rows, _meta, error, clock, post, printed, pacer = _fetch(responses, budget=100)
        self.assertIsInstance(error, seller.SellerAnalyticsStop)
        self.assertIn("бюджет ожиданий по отказам исчерпан", str(error))
        self.assertEqual(post.call_count, 3)              # третий ответ — 429, второе ожидание 60 с уже не влезает в 100
        self.assertEqual(clock.slept, [60, 60])           # ожидание по 429 + интервал перед второй страницей (не в бюджете)
        self.assertEqual((pacer.retry_waits, pacer.interval_pauses), (1, 1))
        self.assertTrue(any(line.startswith("Ozon Seller Analytics ожидания:") for line in printed))

    def test_default_upper_bound_for_a_karatov_night(self):
        """Верхняя граница ожиданий шага: (страниц − 1) × интервал + бюджет. KARATOV — ~8 300 строк в день, 9 страниц."""
        pages = 9
        bound = (pages - 1) * seller.SELLER_PAGE_INTERVAL_SECONDS + seller.SELLER_RETRY_WAIT_BUDGET_SECONDS
        self.assertEqual((seller.SELLER_PAGE_INTERVAL_SECONDS, seller.SELLER_RETRY_WAIT_BUDGET_SECONDS), (60, 600))
        self.assertEqual(bound, 1080)                     # 18 мин ожиданий + время самих запросов


class NightReplay(unittest.TestCase):
    def test_night_0811_sequence_now_passes_and_counts_go_to_the_step_line(self):
        # те же ответы, что ночью 08.11 до отказа: 429 → 200; 429, 429 → 200; на третьей странице — 429, затем 200
        responses = [_response(429, RATE_LIMIT), _response(200, _page(2)),
                     _response(429, RATE_LIMIT), _response(429, RATE_LIMIT), _response(200, _page(2)),
                     _response(429, RATE_LIMIT), _response(200, _page(1))]
        rows, meta, error, clock, post, printed, _pacer = _fetch(responses)
        self.assertIsNone(error)
        self.assertEqual(len(rows), 5)
        self.assertEqual(post.call_count, 7)
        self.assertEqual(clock.slept, [60] * 6)          # 4 ожидания по 429 + 2 интервала перед страницами 2 и 3
        p = meta["pacing"]
        self.assertEqual((p["requests"], p["rate_limited_429"], p["retry_waits"], p["retry_wait_seconds"]), (7, 4, 4, 240))
        self.assertEqual((p["interval_pauses"], p["interval_pause_seconds"]), (2, 120))
        line = [l for l in printed if l.startswith("Ozon Seller Analytics ожидания:")][-1]
        self.assertIn("429 — 4", line)
        self.assertIn("пауз по отказам 4 (240 с из 600)", line)

    def test_another_429_code_is_still_not_retried(self):
        rows, meta, error, clock, post, _printed, _pacer = _fetch([_response(429, {"code": 3, "message": "x"})])
        self.assertIsInstance(error, RuntimeError)
        self.assertNotIsInstance(error, seller.SellerAnalyticsStop)
        self.assertIn("429_code_3", str(error))
        self.assertEqual((post.call_count, clock.slept), (1, []))


if __name__ == "__main__":
    unittest.main()
