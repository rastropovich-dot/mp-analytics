"""WB-часть книги «Фин рез» повторяется на транзиентном отказе сети (дополнение RBH 24.10: ночью книга rbh1 потеряла WB-листы на одном 503)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import test_000_no_network as _guard  # noqa: E402,F401
import report_finrez as rf  # noqa: E402

ARGS = ("2026-08", "2026-10", "2026-10-23", "2026-08-01", "2026-10-23", ["2026-10-23"])


class WbPartRetry(unittest.TestCase):
    def run_with(self, side_effect, tries=3):
        slept = []
        with mock.patch.object(rf, "_build_wb_parts_once", side_effect=side_effect) as once, \
                mock.patch.object(rf, "wbfin", object()):
            out = rf.build_wb_parts(*ARGS, tries=tries, pause=20, sleep_fn=slept.append)
        return out, once.call_count, slept

    def test_transient_proxy_503_is_retried_then_succeeds(self):
        out, calls, slept = self.run_with([Exception("ProxyError: 503 Service Unavailable"), {"rows": [1]}])
        self.assertEqual((out, calls, slept), ({"rows": [1]}, 2, [20]))

    def test_transient_failure_on_every_attempt_becomes_the_book_note(self):
        out, calls, slept = self.run_with([Exception("ProxyError: 503 Service Unavailable")] * 3)
        self.assertEqual((calls, slept), (3, [20, 20]))
        self.assertIn("ProxyError: 503 Service Unavailable", out["error"])
        self.assertIn("попыток 3, отказ транзиентный", out["error"])

    def test_non_transient_failure_is_not_retried(self):
        out, calls, slept = self.run_with([TypeError("unsupported operand type(s) for *: 'decimal.Decimal' and 'NoneType'")])
        self.assertEqual((calls, slept), (1, []))
        self.assertEqual(out["error"], "TypeError: unsupported operand type(s) for *: 'decimal.Decimal' and 'NoneType'")

    def test_amount_like_1503_is_not_a_network_marker(self):
        out, calls, _slept = self.run_with([ValueError("сумма 1 503,00 не сходится")])
        self.assertEqual(calls, 1)

    def test_missing_module_short_circuits(self):
        with mock.patch.object(rf, "wbfin", None):
            out = rf.build_wb_parts(*ARGS)
        self.assertIn("не импортирован", out["error"])


if __name__ == "__main__":
    unittest.main()
