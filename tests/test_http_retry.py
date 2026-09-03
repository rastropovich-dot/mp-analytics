"""loaders/http_retry: повтор транзиентных отказов, без подмены контракта.

Помощник не решает за загрузчик, что делать с не-200 — он лишь даёт отказу
шанс не быть транзиентным. Поэтому проверяем и то, что он возвращает последний
ответ, а не бросает.
"""

import unittest
from unittest import mock

from loaders import http_retry


def _response(status, body=None, headers=None):
    resp = mock.Mock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.text = ""
    if body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = body
    return resp


OZON_RATE_LIMIT = {"code": 8, "message": "You have reached request rate limit per second"}


class ClassifyTests(unittest.TestCase):
    def test_ozon_rate_limit_code_8_is_retryable(self):
        retryable, reason = http_retry.classify(_response(429, OZON_RATE_LIMIT))
        self.assertTrue(retryable)
        self.assertEqual(reason, "rate_limit_per_second")

    def test_ozon_429_with_other_code_is_not_retryable(self):
        retryable, reason = http_retry.classify(_response(429, {"code": 3}))
        self.assertFalse(retryable)
        self.assertEqual(reason, "429_code_3")

    def test_wb_policy_retries_any_429(self):
        retryable, reason = http_retry.classify(
            _response(429, None), retry_429=http_retry.RETRY_429_ANY
        )
        self.assertTrue(retryable)
        self.assertEqual(reason, "rate_limit")

    def test_server_errors_are_retryable_under_every_policy(self):
        for policy in (http_retry.RETRY_429_ANY, http_retry.RETRY_429_OZON_RATE_LIMIT,
                       http_retry.RETRY_429_NEVER):
            self.assertTrue(http_retry.classify(_response(503), retry_429=policy)[0], policy)

    def test_client_errors_are_not_retryable(self):
        self.assertFalse(http_retry.classify(_response(400, {"code": 1}))[0])
        self.assertFalse(http_retry.classify(_response(401))[0])

    def test_never_policy_refuses_even_code_8(self):
        retryable, _ = http_retry.classify(
            _response(429, OZON_RATE_LIMIT), retry_429=http_retry.RETRY_429_NEVER
        )
        self.assertFalse(retryable)


class SleepTests(unittest.TestCase):
    def test_backoff_doubles_and_caps(self):
        seq = [http_retry.sleep_seconds(_response(429), attempt) for attempt in range(1, 7)]
        self.assertEqual(seq, [1, 2, 4, 8, 10, 10])

    def test_retry_after_wins(self):
        self.assertEqual(http_retry.sleep_seconds(_response(429, headers={"Retry-After": "4"}), 1), 4)

    def test_retry_after_is_capped(self):
        self.assertEqual(
            http_retry.sleep_seconds(_response(429, headers={"Retry-After": "900"}), 1),
            http_retry.CAP_SLEEP_SECONDS,
        )

    def test_unparsable_retry_after_falls_back_to_backoff(self):
        self.assertEqual(
            http_retry.sleep_seconds(_response(429, headers={"Retry-After": "Wed, 21 Oct"}), 2), 2
        )


class RequestTests(unittest.TestCase):
    def _run(self, responses, **kwargs):
        session = mock.Mock()
        session.request.side_effect = responses
        slept = []
        resp = http_retry.request(
            "POST", "https://example/x", label="тест",
            session=session, sleep_fn=slept.append, **kwargs
        )
        return resp, slept, session

    def test_retries_until_success(self):
        resp, slept, session = self._run([
            _response(429, OZON_RATE_LIMIT), _response(500), _response(200, {"ok": True}),
        ])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(session.request.call_count, 3)
        self.assertEqual(slept, [1, 2])

    def test_returns_last_response_instead_of_raising(self):
        """Контракт вызывающего не меняется: решение о не-200 остаётся за ним."""
        responses = [_response(429, OZON_RATE_LIMIT)] * http_retry.MAX_ATTEMPTS
        resp, slept, session = self._run(responses)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(session.request.call_count, http_retry.MAX_ATTEMPTS)
        self.assertEqual(len(slept), http_retry.MAX_ATTEMPTS - 1)

    def test_non_retryable_returns_immediately(self):
        resp, slept, session = self._run([_response(400, {"code": 1})])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(slept, [])

    def test_success_does_not_sleep(self):
        resp, slept, session = self._run([_response(200, {"ok": True})])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(slept, [])


class WiringTests(unittest.TestCase):
    """Каждый шаг пайплайна, ходящий по HTTP, должен звать помощника."""

    def test_pipeline_loaders_use_the_helper(self):
        import loaders.ozon_expenses_loader as expenses
        import loaders.ozon_fbo_orders_loader as fbo
        import loaders.ozon_fbs_orders_loader as fbs
        import loaders.ozon_finance_transactions_loader as finance
        import loaders.ozon_stocks_loader as stocks
        import loaders.wb_orders_loader as wb_orders
        import loaders.wb_sales_loader as wb_sales
        import loaders.wb_stocks_loader as wb_stocks

        for module in (expenses, fbo, fbs, finance, stocks, wb_orders, wb_sales, wb_stocks):
            self.assertTrue(hasattr(module, "http_retry"), module.__name__)

    def test_wb_loaders_retry_any_429(self):
        """У WB нет кода 8 — там любой 429 это частота запросов."""
        import inspect
        import loaders.wb_orders_loader as wb_orders
        source = inspect.getsource(wb_orders)
        self.assertIn("RETRY_429_ANY", source)

    def test_sales_funnel_rate_limit_loop_is_bounded(self):
        import loaders.wb_sales_funnel_orders_loader as funnel
        self.assertGreater(funnel.WB_RATE_LIMIT_MAX_ATTEMPTS, 0)
        self.assertLessEqual(funnel.WB_RATE_LIMIT_MAX_ATTEMPTS, 10)


if __name__ == "__main__":
    unittest.main()
