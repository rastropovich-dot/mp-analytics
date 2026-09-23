"""Хранение сырья отправлений: три последних окна истории на схему, снимки не старше 30 дней, ссылки outbox — неприкосновенны."""
import importlib.util
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("raw_retention", os.path.join(ROOT, "scripts", "raw_retention.py"))
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
T = NOW.timestamp()


def decisions(files, refs=()):
    return {name: d for name, _s, d, _w in rr.plan([(n, 1, T) for n in files], set(refs), NOW)}


class Plan(unittest.TestCase):
    def test_three_latest_history_windows_per_scheme_are_kept(self):
        files = [f"history_fbo_2026-0{m}-01_2026-0{m}-28.json" for m in range(4, 9)] + ["history_fbs_2026-03-28_2026-09-18.json"]
        d = decisions(files)
        self.assertEqual(sorted(n for n, v in d.items() if v == "remove"),
                         ["history_fbo_2026-04-01_2026-04-28.json", "history_fbo_2026-05-01_2026-05-28.json"])
        self.assertEqual(d["history_fbs_2026-03-28_2026-09-18.json"], "keep")

    def test_snapshots_older_than_30_days_are_removed(self):
        d = decisions(["fbo_20260914T115900Z.json", "precheck_fbs_v3_20261020T200924Z.json", "fbs_20261002T000000Z.json"])
        self.assertEqual(d, {"fbo_20260914T115900Z.json": "remove", "precheck_fbs_v3_20261020T200924Z.json": "keep",
                             "fbs_20261002T000000Z.json": "keep"})

    def test_outbox_reference_protects_even_with_a_wildcard(self):
        files = ["fbo_20260914T115900Z.json"] + [f"history_fbo_2026-0{m}-01_2026-0{m}-28.json" for m in range(4, 8)]
        d = decisions(files, refs={"fbo_20260914T115900Z.json", "history_*_2026-04-01_2026-04-28.json"})
        self.assertEqual(d["fbo_20260914T115900Z.json"], "keep")
        self.assertEqual(d["history_fbo_2026-04-01_2026-04-28.json"], "keep")

    def test_foreign_files_are_kept(self):
        self.assertEqual(decisions(["notes.txt", "nightly_2026-09-01_fbo.json"]), {"notes.txt": "keep", "nightly_2026-09-01_fbo.json": "keep"})

    def test_outbox_references_only_from_recent_sections(self):
        text = ("## 2026-10-30, задача\nсырьё data/postings_raw/history_*_2026-03-28_2026-09-18.json и fbo_20260914T115900Z.json\n"
                "## 2026-09-01, старая\nprecheck_fbo_20260901T000000Z.json\n")
        self.assertEqual(rr.outbox_references(text, date(2026, 11, 1)),
                         {"history_*_2026-03-28_2026-09-18.json", "fbo_20260914T115900Z.json"})


class Apply(unittest.TestCase):
    def run_main(self, names, argv):
        with tempfile.TemporaryDirectory() as d:
            for n in names:
                open(os.path.join(d, n), "w").write("{}")
                os.utime(os.path.join(d, n), (T, T))
            with mock.patch.object(rr, "OUTBOX", os.path.join(d, "no-outbox.md")), \
                    mock.patch.object(rr, "datetime", wraps=datetime) as dt, mock.patch("builtins.print"):
                dt.now.return_value = NOW
                rr.main(argv + ["--dir", d])
            return sorted(os.listdir(d))

    def test_plan_without_apply_removes_nothing(self):
        names = ["fbo_20260101T000000Z.json", "notes.txt"]
        self.assertEqual(self.run_main(names, []), sorted(names))

    def test_apply_removes_exactly_the_plan(self):
        self.assertEqual(self.run_main(["fbo_20260101T000000Z.json", "fbs_20261030T000000Z.json", "notes.txt"], ["--apply"]),
                         ["fbs_20261030T000000Z.json", "notes.txt"])


if __name__ == "__main__":
    unittest.main()
