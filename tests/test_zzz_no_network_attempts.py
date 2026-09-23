"""Последний модуль набора: за весь прогон ни одной попытки открыть сеть (правило test_000_no_network.py).

Блокировка превращает соединение в ошибку, но код проекта местами её ловит и идёт дальше — тест проходит, а
попытка была (2026-09-23: так 20 тестов «читали» боевую базу молча, по 7 с на повторы). Здесь такие попытки
перечисляются: что, куда и из какого места проекта.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_000_no_network as guard  # noqa: E402


class NoNetworkAttempts(unittest.TestCase):
    @unittest.skipIf(guard.LIVE, "MP_TESTS_LIVE=1: сеть разрешена")
    def test_no_test_tried_to_open_the_network(self):
        lines = [f"{what} {target} ← " + " ← ".join(reversed(frames)) for what, target, frames in guard.ATTEMPTS]
        self.assertEqual(lines, [], f"попыток открыть сеть: {len(lines)}\n" + "\n".join(lines[:40]))


if __name__ == "__main__":
    unittest.main()
