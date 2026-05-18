import unittest
from unittest import mock

import httpx

import reports_sku_decision_daily_input as decision
import reports_stock_data_quality_issues as stock_quality


class _Result:
    def __init__(self, data):
        self.data = data


class _RangeQuery:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return _Result(self._data)


class _Query:
    def __init__(self, data):
        self._data = data

    def select(self, _fields):
        return self

    def eq(self, _field, _value):
        return self

    def gte(self, _field, _value):
        return self

    def lte(self, _field, _value):
        return self

    def gt(self, _field, _value):
        return self

    def lt(self, _field, _value):
        return self

    def order(self, _field, desc=False):
        return self

    def range(self, _start, _end):
        return _RangeQuery(self._data)


class _Supabase:
    def __init__(self, data):
        self._data = data

    def table(self, _name):
        return _Query(self._data)


class ReadRetryTests(unittest.TestCase):
    def test_execute_read_with_retry_succeeds_without_retry(self):
        fn = mock.Mock(return_value="ok")

        result = stock_quality.execute_read_with_retry(fn, label="test")

        self.assertEqual(result, "ok")
        fn.assert_called_once_with()

    def test_execute_read_with_retry_retries_connect_timeout_then_succeeds(self):
        fn = mock.Mock(side_effect=[httpx.ConnectTimeout("boom"), "ok"])

        with mock.patch.object(stock_quality.time, "sleep") as sleep_mock:
            result = stock_quality.execute_read_with_retry(fn, label="test")

        self.assertEqual(result, "ok")
        self.assertEqual(fn.call_count, 2)
        sleep_mock.assert_called_once_with(2)

    def test_execute_read_with_retry_retries_read_error_then_succeeds(self):
        fn = mock.Mock(side_effect=[httpx.ReadError("boom"), "ok"])

        with mock.patch.object(stock_quality.time, "sleep") as sleep_mock:
            result = stock_quality.execute_read_with_retry(fn, label="test")

        self.assertEqual(result, "ok")
        self.assertEqual(fn.call_count, 2)
        sleep_mock.assert_called_once_with(2)

    def test_execute_read_with_retry_raises_after_max_attempts(self):
        fn = mock.Mock(side_effect=httpx.ConnectTimeout("boom"))

        with mock.patch.object(stock_quality.time, "sleep") as sleep_mock:
            with self.assertRaises(httpx.ConnectTimeout):
                stock_quality.execute_read_with_retry(fn, label="test", max_attempts=3)

        self.assertEqual(fn.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [mock.call(2), mock.call(5)])

    def test_decision_fetch_all_uses_retry_helper(self):
        fake_supabase = _Supabase([{"marketplace_sku": "1"}])

        with mock.patch.object(decision, "supabase", fake_supabase), mock.patch.object(
            decision, "execute_read_with_retry", side_effect=lambda execute_fn, label: execute_fn()
        ) as retry_mock:
            rows = decision.fetch_all("daily_sku_kpi")

        self.assertEqual(rows, [{"marketplace_sku": "1"}])
        retry_mock.assert_called_once()
        self.assertIn("decision:daily_sku_kpi:0", retry_mock.call_args.kwargs["label"])

    def test_stock_fetch_all_uses_retry_helper(self):
        fake_supabase = _Supabase([{"stock_date": "2026-05-16"}])

        with mock.patch.object(stock_quality, "supabase", fake_supabase), mock.patch.object(
            stock_quality, "execute_read_with_retry", side_effect=lambda execute_fn, label: execute_fn()
        ) as retry_mock:
            rows = stock_quality.fetch_all("stock_daily")

        self.assertEqual(rows, [{"stock_date": "2026-05-16"}])
        retry_mock.assert_called_once()
        self.assertIn("stock:stock_daily:0", retry_mock.call_args.kwargs["label"])

    def test_write_helpers_are_not_wrapped_by_read_retry(self):
        with mock.patch.object(decision.supabase, "table") as table_mock, mock.patch.object(
            decision, "execute_read_with_retry"
        ) as retry_mock:
            table_mock.return_value.upsert.return_value.execute.return_value = None
            decision.save_rows([{"marketplace_code": "ozon", "kpi_date": "2026-05-16", "marketplace_sku": "1"}])

        retry_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
