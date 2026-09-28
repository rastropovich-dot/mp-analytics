"""scripts/fetch_render_logs.py — чистые части: разбор наносекундного времени Render и шаг курсора на 1 нс.
Сети нет: fetch() не вызывается."""
import importlib.util
import unittest
from datetime import datetime, timezone

spec = importlib.util.spec_from_file_location("fetch_render_logs", "scripts/fetch_render_logs.py")
frl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frl)


class ParseTsTests(unittest.TestCase):
    def test_nanoseconds_are_truncated_to_microseconds(self):
        ts = frl.parse_ts("2026-09-28T00:23:40.185483203+00:00")
        self.assertEqual(ts, datetime(2026, 9, 28, 0, 23, 40, 185483, tzinfo=timezone.utc))

    def test_z_suffix_and_no_fraction(self):
        self.assertEqual(frl.parse_ts("2026-09-28T02:13:52Z"), datetime(2026, 9, 28, 2, 13, 52, tzinfo=timezone.utc))

    def test_garbage_is_named(self):
        with self.assertRaises(ValueError):
            frl.parse_ts("28.09.2026 02:13")


class BumpNsTests(unittest.TestCase):
    def test_plus_one_nanosecond_keeps_precision(self):
        self.assertEqual(frl.bump_ns("2026-09-28T00:23:40.185483203+00:00"), "2026-09-28T00:23:40.185483204+00:00")

    def test_rollover_to_next_second(self):
        self.assertEqual(frl.bump_ns("2026-09-28T00:23:40.999999999Z"), "2026-09-28T00:23:41.000000000Z")

    def test_short_fraction_is_padded(self):
        self.assertEqual(frl.bump_ns("2026-09-28T00:23:40.5Z"), "2026-09-28T00:23:40.500000001Z")


class DedupeTests(unittest.TestCase):
    def test_fetch_dedupes_by_id_and_walks_by_exact_timestamp(self):
        pages = [
            {"logs": [{"id": "a", "timestamp": "2026-09-28T00:00:01.000000001Z", "message": "1"},
                      {"id": "b", "timestamp": "2026-09-28T00:00:01.000000002Z", "message": "2"}], "hasMore": True},
            {"logs": [{"id": "b", "timestamp": "2026-09-28T00:00:01.000000002Z", "message": "2"},
                      {"id": "c", "timestamp": "2026-09-28T00:00:01.000000002Z", "message": "3"}], "hasMore": False},
            {"logs": [{"id": "c", "timestamp": "2026-09-28T00:00:01.000000002Z", "message": "3"}], "hasMore": False},
        ]
        calls = []

        def fake_get(url, key, stats):
            calls.append(url)
            stats["calls"] += 1
            return pages[len(calls) - 1]

        frl.get, frl.time.sleep = fake_get, lambda *_: None
        rows, stats = frl.fetch("crn-x", "2026-09-28T00:00:00Z", "2026-09-28T01:00:00Z", "key", limit=2)
        self.assertEqual([r["id"] for r in rows], ["a", "b", "c"])
        self.assertEqual(stats["dupes"], 2)
        self.assertIn("startTime=2026-09-28T00%3A00%3A01.000000002Z", calls[1])   # курсор — точное время последней строки


if __name__ == "__main__":
    unittest.main()
