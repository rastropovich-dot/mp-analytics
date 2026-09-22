"""Застрявшие ключи: после полного сбора окна удаляются ровно ключи окна, которых нет среди построенных строк.

Частичный сбор → удаления нет. Ключи вне окна не трогаются никогда (читатель фильтрует по окну). День без единой
построенной строки не чистится. Реклама в marketplace_expenses — чужие строки, их чистка не видит. --dry-run не пишет.
"""
import unittest
from unittest import mock

from loaders import http_retry, stale_keys
from loaders import ozon_expenses_loader as expenses
from loaders import ozon_finance_accrual as accrual
from loaders import ozon_finance_transactions_loader as buyouts

WINDOW = {"day_from": "2026-08-23", "day_to": "2026-09-22", "days": 31, "pages": 62, "requests": 62, "retries": 0, "failures": 0, "complete": True}


class FakeQuery:
    def __init__(self, sb, table, rows):
        self.sb, self.table_name, self.rows, self.filters, self.orders, self.op = sb, table, rows, [], [], "select"
        self.rng = None

    def select(self, cols):
        self.op = "select"; return self

    def delete(self):
        self.op = "delete"; return self

    def __getattr__(self, name):
        if name in ("eq", "gte", "lte", "in_", "neq", "like"):
            def f(*args):
                self.filters.append((name,) + args); return self
            return f
        raise AttributeError(name)

    def order(self, col):
        self.orders.append(col); return self

    def range(self, a, b):
        self.rng = (a, b); return self

    def execute(self):
        if self.op == "delete":
            self.sb.deleted.append((self.table_name, self.filters))
            return mock.Mock(data=[{} for _ in self._matching()])
        rows = self._matching()
        a, b = self.rng or (0, len(rows) - 1)
        return mock.Mock(data=rows[a:b + 1])

    def _matching(self):
        out = []
        for r in self.rows:
            ok = True
            for f in self.filters:
                op, col, val = f[0], f[1], f[2]
                v = r.get(col)
                if op == "eq":
                    ok &= v == val
                elif op == "gte":
                    ok &= str(v) >= str(val)
                elif op == "lte":
                    ok &= str(v) <= str(val)
                elif op == "in_":
                    ok &= v in val
                else:
                    raise AssertionError(op)
            if ok:
                out.append(r)
        return out


class FakeSupabase:
    def __init__(self, tables):
        self.tables, self.deleted = tables, []

    def table(self, name):
        return FakeQuery(self, name, self.tables.get(name, []))


def row(day, sku, kind, amount, id_):
    return {"id": id_, "expense_date": day, "marketplace_code": "ozon", "marketplace_sku": sku, "expense_type": kind, "expense_amount": amount}


class Plan(unittest.TestCase):
    def test_stale_is_existing_minus_built_on_days_with_built_rows(self):
        existing = [row("2026-09-11", "1", "other", 8.65, 1), row("2026-09-11", "2", "other", 5, 2), row("2026-09-12", "3", "logistics", 7, 3)]
        key = lambda r: (r["expense_date"], r["marketplace_sku"], r["expense_type"])  # noqa: E731
        stale, skipped = stale_keys.plan(existing, {key(existing[1])}, {"2026-09-11"}, key, lambda r: r["expense_date"])
        self.assertEqual([r["id"] for r in stale], [1])          # 09-11 sku 1 застрял
        self.assertEqual(skipped, ["2026-09-12"])                 # 09-12 без построенных строк — не трогаем

    def test_incomplete_window_deletes_nothing(self):
        delete_fn = mock.Mock(return_value=1)
        n = stale_keys.cleanup(None, "t", dict(WINDOW, complete=False, retries=1), [row("2026-09-11", "1", "other", 1, 1)], set(), {"2026-09-11"},
                               lambda r: r["id"], lambda r: r["expense_date"], str, delete_fn, apply=True)
        self.assertEqual(n, 0); delete_fn.assert_not_called()

    def test_dry_run_deletes_nothing_and_apply_deletes_exactly_stale(self):
        existing = [row("2026-09-11", "1", "other", 1, 1), row("2026-09-11", "2", "other", 1, 2)]
        key = lambda r: r["id"]  # noqa: E731
        delete_fn = mock.Mock(side_effect=lambda sb, stale: len(stale))
        with mock.patch("builtins.print"):
            self.assertEqual(stale_keys.cleanup(None, "t", WINDOW, existing, {2}, {"2026-09-11"}, key, lambda r: r["expense_date"], str, delete_fn, apply=False), 0)
            delete_fn.assert_not_called()
            self.assertEqual(stale_keys.cleanup(None, "t", WINDOW, existing, {2}, {"2026-09-11"}, key, lambda r: r["expense_date"], str, delete_fn, apply=True), 1)
        self.assertEqual([r["id"] for r in delete_fn.call_args[0][1]], [1])

    def test_too_many_stale_rows_is_refused(self):
        existing = [row("2026-09-11", str(i), "other", 1, i) for i in range(300)]
        delete_fn = mock.Mock()
        with mock.patch("builtins.print"):
            n = stale_keys.cleanup(None, "t", WINDOW, existing, set(), {"2026-09-11"}, lambda r: r["id"], lambda r: r["expense_date"], str, delete_fn, apply=True)
        self.assertEqual(n, 0); delete_fn.assert_not_called()

    def test_share_cap_with_floor(self):
        self.assertEqual((stale_keys.allowed_stale(100), stale_keys.allowed_stale(1000), stale_keys.allowed_stale(50000)), (10, 20, 200))
        existing = [row("2026-09-11", str(i), "other", 1, i) for i in range(100)]
        delete_fn = mock.Mock(side_effect=lambda sb, stale: len(stale))
        with mock.patch("builtins.print"):
            n = stale_keys.cleanup(None, "t", WINDOW, existing, {i for i in range(11, 100)}, {"2026-09-11"}, lambda r: r["id"], lambda r: r["expense_date"], str, delete_fn, apply=True)
        self.assertEqual(n, 0); delete_fn.assert_not_called()      # 11 из 100 — больше планки 10: отказ
        with mock.patch("builtins.print"):
            n = stale_keys.cleanup(None, "t", WINDOW, existing, {i for i in range(9, 100)}, {"2026-09-11"}, lambda r: r["id"], lambda r: r["expense_date"], str, delete_fn, apply=True)
        self.assertEqual(n, 9)                                     # 9 из 100 — в пределах планки

    def test_reader_filters_by_window_and_orders_by_full_key(self):
        sb = FakeSupabase({"t": [row("2026-08-01", "1", "other", 1, 1), row("2026-09-01", "1", "other", 1, 2)]})
        out = stale_keys.read_window_rows(sb, "t", "id", [("gte", "expense_date", "2026-08-23"), ("lte", "expense_date", "2026-09-22")], ["expense_date", "marketplace_sku"])
        self.assertEqual([r["id"] for r in out], [2])              # строка вне окна читателю не видна — трогать нечего


class RetryStats(unittest.TestCase):
    def response(self, status, code=None):
        r = mock.Mock(status_code=status, headers={}, text="")
        r.json.return_value = {"code": code} if code is not None else {}
        return r

    def test_retry_is_counted_and_makes_window_incomplete(self):
        session = mock.Mock()
        session.request.side_effect = [self.response(429, 8), self.response(200)]
        stats = {}
        with mock.patch("builtins.print"):
            r = http_retry.request("POST", "u", label="x", session=session, sleep_fn=lambda s: None, stats=stats)
        self.assertEqual(r.status_code, 200)
        self.assertEqual((stats["requests"], stats["retries"], stats.get("failures", 0)), (2, 1, 0))

    def test_window_passport(self):
        def fake_day(day, stats=None):
            stats["pages"] = stats.get("pages", 0) + 2; stats["requests"] = stats.get("requests", 0) + 2
            return [{"date": day}]
        with mock.patch.object(accrual, "fetch_day", side_effect=fake_day):
            from datetime import datetime, timezone
            rows, window = accrual.fetch_window_checked(2, datetime(2026, 9, 22, 0, 20, tzinfo=timezone.utc))
        self.assertEqual((window["day_from"], window["day_to"], window["days"], window["pages"], window["complete"]), ("2026-09-20", "2026-09-22", 3, 6, True))
        self.assertEqual(len(rows), 3)

        def retried_day(day, stats=None):
            stats["retries"] = 1; stats["requests"] = 3; stats["pages"] = 2
            return []
        with mock.patch.object(accrual, "fetch_day", side_effect=retried_day):
            _rows, window = accrual.fetch_window_checked(0)
        self.assertFalse(window["complete"])


class ExpensesIntegration(unittest.TestCase):
    def test_only_own_types_in_window_are_candidates(self):
        existing = [row("2026-09-11", "1", "other", 8.65, 1),          # застрял
                    row("2026-09-11", "2", "advertising_cpc", 100, 2),  # реклама — чужая строка, не трогать
                    row("2026-09-11", "3", "logistics", 5, 3),          # построена заново
                    row("2026-08-01", "4", "other", 9, 4)]              # вне окна
        sb = FakeSupabase({"marketplace_expenses": existing})
        built = [{"expense_date": "2026-09-11", "marketplace_code": "ozon", "marketplace_sku": "3", "expense_type": "logistics", "expense_amount": 5}]
        with mock.patch.object(expenses, "supabase", sb), mock.patch("builtins.print"):
            n = expenses.cleanup_stale_expenses(WINDOW, built, apply=True)
        self.assertEqual(n, 1)
        self.assertEqual(sb.deleted, [("marketplace_expenses", [("in_", "id", [1])])])

    def test_dry_run_never_touches_the_table(self):
        sb = FakeSupabase({"marketplace_expenses": [row("2026-09-11", "1", "other", 8.65, 1)]})
        with mock.patch.object(expenses, "supabase", sb), mock.patch("builtins.print"):
            n = expenses.cleanup_stale_expenses(WINDOW, [{"expense_date": "2026-09-11", "marketplace_sku": "9", "expense_type": "other"}], apply=False)
        self.assertEqual((n, sb.deleted), (0, []))

    def test_ledger_deletes_by_date_and_type(self):
        sb = FakeSupabase({"ozon_accrual_daily_types": [{"accrual_date": "2026-09-11", "type_id": 96, "amount": -1}, {"accrual_date": "2026-09-11", "type_id": 1, "amount": -2}]})
        with mock.patch.object(expenses, "supabase", sb), mock.patch("builtins.print"):
            n = expenses.cleanup_stale_ledger(WINDOW, [{"accrual_date": "2026-09-11", "type_id": 1}], apply=True)
        self.assertEqual(n, 1)
        self.assertEqual(sb.deleted, [("ozon_accrual_daily_types", [("eq", "accrual_date", "2026-09-11"), ("in_", "type_id", [96])])])

    def test_buyouts_key_is_date_and_sku(self):
        sb = FakeSupabase({"marketplace_buyouts": [{"id": 7, "buyout_date": "2026-09-11", "marketplace_code": "ozon", "marketplace_sku": "4453045714", "buyouts_amount_seller": 5930.5, "buyouts_units": None},
                                                   {"id": 8, "buyout_date": "2026-09-11", "marketplace_code": "ozon", "marketplace_sku": "1", "buyouts_amount_seller": 1, "buyouts_units": 1}]})
        with mock.patch.object(buyouts, "supabase", sb), mock.patch("builtins.print"):
            n = buyouts.cleanup_stale_buyouts(WINDOW, [{"buyout_date": "2026-09-11", "marketplace_sku": "1"}], apply=True)
        self.assertEqual((n, sb.deleted), (1, [("marketplace_buyouts", [("in_", "id", [7])])]))


if __name__ == "__main__":
    unittest.main()
