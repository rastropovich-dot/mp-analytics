"""Шестьдесят вторая (седьмая задача RBH): ожидание по ratelimit-remaining в loaders/http_retry.py. Сеть не зовётся."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from loaders import http_retry  # noqa: E402

URL_P = "https://api-seller.ozon.ru/v1/finance/accrual/postings"
URL_D = "https://api-seller.ozon.ru/v1/finance/accrual/by-day"


class Resp:
    def __init__(self, status=200, headers=None, body=None):
        self.status_code, self.headers, self._body = status, headers or {}, body if body is not None else {}

    def json(self):
        return self._body


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.t += s


class Session:
    def __init__(self, responses, clock=None, step=0.0):
        self.responses, self.calls, self.clock, self.step = list(responses), [], clock, step

    def request(self, method, url, **kw):
        self.calls.append(url)
        if self.clock:
            self.clock.t += self.step       # время ответа
        return self.responses.pop(0)


class Gate(unittest.TestCase):
    def setUp(self):
        http_retry.reset_gate()
        self.clock = Clock()

    tearDown = setUp

    def call(self, session, url=URL_P, stats=None):
        return http_retry.post(url, label="t", session=session, sleep_fn=self.clock.sleep, clock=self.clock.now, stats=stats)

    def test_zero_on_200_waits_before_next_same_method(self):
        s = Session([Resp(200, {"Ratelimit-Remaining": "0"}), Resp(200, {"ratelimit-remaining": "3"})])
        stats = {}
        self.call(s, stats=stats)
        self.call(s, stats=stats)
        self.assertEqual(self.clock.sleeps, [http_retry.RATELIMIT_ZERO_WAIT_SECONDS])   # retry-after у 200 нет → умолчание
        self.assertEqual((stats["ratelimit_waits"], stats["ratelimit_zero"], stats["ratelimit_remaining_min"], stats["ratelimit_seen"]), (1, 1, 0, 2))
        self.assertNotIn("retries", stats)                                                # ожидание — не повтор и не 429

    def test_pause_of_caller_counts_in(self):
        s = Session([Resp(200, {"ratelimit-remaining": "0"}), Resp(200, {"ratelimit-remaining": "5"})])
        self.call(s)
        self.clock.t += 1.5                                                                # пауза вызывающего 1,5 с > 1 с
        self.call(s)
        self.assertEqual(self.clock.sleeps, [])

    def test_retry_after_used_and_capped(self):
        s = Session([Resp(200, {"ratelimit-remaining": "0", "Retry-After": "3"}), Resp(200, {}),
                     Resp(200, {"ratelimit-remaining": "0", "retry-after": "600"}), Resp(200, {})])
        self.call(s); self.call(s)
        self.call(s); self.call(s)
        self.assertEqual(self.clock.sleeps, [3.0, http_retry.RATELIMIT_MAX_WAIT_SECONDS])

    def test_no_headers_old_behaviour(self):
        s = Session([Resp(200), Resp(200), Resp(200)])
        stats = {}
        for _ in range(3):
            self.call(s, stats=stats)
        self.assertEqual(self.clock.sleeps, [])
        self.assertNotIn("ratelimit_seen", stats)

    def test_methods_have_separate_gates(self):
        s = Session([Resp(200, {"ratelimit-remaining": "0"}), Resp(200, {"ratelimit-remaining": "49"}), Resp(200, {})])
        self.call(s, URL_P)
        self.call(s, URL_D)            # by-day — своя квота, не ждём
        self.assertEqual(self.clock.sleeps, [])
        self.call(s, URL_P)            # postings — ждём
        self.assertEqual(self.clock.sleeps, [http_retry.RATELIMIT_ZERO_WAIT_SECONDS])

    def test_remaining_positive_clears_gate(self):
        s = Session([Resp(200, {"ratelimit-remaining": "0"}), Resp(200, {"ratelimit-remaining": "2"}), Resp(200, {})])
        self.call(s); self.call(s); self.call(s)
        self.assertEqual(self.clock.sleeps, [http_retry.RATELIMIT_ZERO_WAIT_SECONDS])

    def test_429_retry_waits_once(self):
        s = Session([Resp(429, {"ratelimit-remaining": "0", "retry-after": "1"}, {"code": 8}), Resp(200, {"ratelimit-remaining": "4"})])
        stats = {}
        r = self.call(s, stats=stats)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.clock.sleeps, [1])                                          # пауза повтора, без второго ожидания
        self.assertEqual((stats["retries"], stats.get("ratelimit_waits", 0)), (1, 0))
        self.assertEqual(http_retry.count_429(stats, r), 1)

    def test_bad_header_values(self):
        self.assertIsNone(http_retry.ratelimit_wait(Resp(200, {"ratelimit-remaining": "abc"})))
        self.assertEqual(http_retry.ratelimit_wait(Resp(200, {"ratelimit-remaining": "0", "retry-after": "x"})), http_retry.RATELIMIT_ZERO_WAIT_SECONDS)
        self.assertEqual(http_retry.ratelimit_wait(Resp(200, {"ratelimit-remaining": "7"})), 0.0)

    def test_absorb_into_step_counters(self):
        counters = {}
        http_retry.absorb_ratelimit(counters, {"ratelimit_waits": 2, "ratelimit_wait_seconds": 1.25, "ratelimit_remaining_min": 3, "ratelimit_seen": 4})
        http_retry.absorb_ratelimit(counters, {"ratelimit_zero": 1, "ratelimit_remaining_min": 0, "ratelimit_seen": 1})
        http_retry.absorb_ratelimit(counters, {})
        self.assertEqual(counters, {"ratelimit_waits": 2, "ratelimit_zero": 1, "ratelimit_seen": 5, "ratelimit_wait_seconds": 1.25, "ratelimit_remaining_min": 0})


class StepLine(unittest.TestCase):
    def setUp(self):
        http_retry.reset_gate()

    def test_fetch_postings_counts_waits_apart_from_429(self):
        from loaders import ozon_buyout_units as bu
        clock = Clock()
        responses = [Resp(200, {"ratelimit-remaining": "0"}, {"posting_accruals": [{"posting_number": "1"}]}),
                     Resp(200, {"ratelimit-remaining": "0"}, {"posting_accruals": [{"posting_number": "2"}]})]
        orig_post, orig_sleep, orig_headers = http_retry.post, bu.time.sleep, bu.accrual.headers
        sess = Session(responses)
        http_retry.post = lambda url, **kw: http_retry.request("POST", url, session=sess, sleep_fn=clock.sleep, clock=clock.now, **kw)
        bu.time.sleep = lambda s: None          # пауза шага 1,5 с — «мгновенная», ожидание по заголовку остаётся
        bu.accrual.headers = lambda: {}
        old_chunk = bu.CHUNK
        try:
            bu.CHUNK = 1
            counters = {}
            out = bu.fetch_postings(["1", "2"], counters)
        finally:
            http_retry.post, bu.time.sleep, bu.accrual.headers, bu.CHUNK = orig_post, orig_sleep, orig_headers, old_chunk
        self.assertEqual(len(out), 2)
        self.assertEqual((counters["429"], counters["ratelimit_waits"], counters["ratelimit_zero"], counters["ratelimit_remaining_min"]), (0, 1, 2, 0))


if __name__ == "__main__":
    unittest.main()
