"""Сорок третья §4, §5: ночной шаг добора каталога и строки утреннего алерта (каталог, книга «Фин рез»)."""
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import alerts_telegram as at  # noqa: E402
import ozon_catalog_topup_step as step  # noqa: E402
import run_daily_pipeline as pipe  # noqa: E402

NOW = datetime(2026, 9, 28, 7, 30, tzinfo=timezone.utc)


class FakeCatalog:
    TABLE = "ozon_products"; BATCH = 1000; OUT_DIR = "/nonexistent"

    def __init__(self, info, in_table, fail=None):
        self.info, self.in_table, self.fail = info, set(in_table), fail
        self.fetched, self.applied, self.tree_calls = [], [], 0

    def sku_universe(self, sb):
        return dict(self.info), [], []

    def table_exists(self, sb):
        return True

    def table_skus(self, sb):
        return set(self.in_table)

    def fetch_cards(self, skus, stats):
        if self.fail:
            raise SystemExit(self.fail)
        stats["requests"] = stats.get("requests", 0) + 1
        self.fetched = list(skus)
        return [{"sku": int(s)} for s in skus]

    def fetch_tree(self, stats):
        stats["requests"] = stats.get("requests", 0) + 1; self.tree_calls += 1
        return {"result": []}

    def build_rows(self, info, items, tree, observed_at):
        return [{"sku": s, "offer_id": info[s]["article"]} for s in info], {"Кольца": len(info)}, {}

    def apply(self, sb, rows, allow_window=False):
        assert allow_window
        self.applied = rows; self.in_table |= {r["sku"] for r in rows}


class FakeSb:
    def __init__(self):
        self.upserts = []

    def table(self, name):
        self.name = name; return self

    def upsert(self, row, on_conflict=None):
        self.upserts.append((self.name, row, on_conflict)); return self

    def execute(self):
        return self


class CatalogTopupStepTests(unittest.TestCase):
    def info(self):
        return {"1": {"article": "A1", "source": "orders_buyouts"}, "2": {"article": "A2", "source": "orders_buyouts"}, "3": {"article": "", "source": "ads_only"}}

    def test_dry_run_counts_and_lists_without_api_or_writes(self):
        cat = FakeCatalog(self.info(), {"1"}); sb = FakeSb()
        payload, code = step.run(sb, dry_run=True, tree_file="/nonexistent/tree.json", catalog=cat, now=NOW)
        self.assertEqual((code, payload["missing_before"], payload["missing"], payload["missing_after"], payload["written"]), (0, 2, ["2", "3"], 2, 0))
        self.assertEqual((cat.fetched, cat.applied, sb.upserts), ([], [], []))            # ни API, ни записи, ни итога

    def test_night_mode_collects_missing_writes_and_records_state(self):
        cat = FakeCatalog(self.info(), {"1"}); sb = FakeSb()
        payload, code = step.run(sb, dry_run=False, tree_file="/nonexistent/tree.json", catalog=cat, now=NOW)
        self.assertEqual(code, 0)
        self.assertEqual(cat.fetched, ["2", "3"]); self.assertEqual(cat.tree_calls, 1)     # дерево — обращением, файла нет
        self.assertEqual([r["sku"] for r in cat.applied], ["2", "3"])
        self.assertEqual((payload["collected"], payload["written"], payload["requests"], payload["missing_after"]), (2, 2, 2, 0))
        self.assertEqual(sb.upserts[-1][1]["state_key"], step.STATE_KEY)                   # итог записан

    def test_nothing_missing_writes_state_only(self):
        cat = FakeCatalog(self.info(), {"1", "2", "3"}); sb = FakeSb()
        payload, code = step.run(sb, dry_run=False, catalog=cat, now=NOW)
        self.assertEqual((code, payload["missing_before"], cat.fetched), (0, 0, []))
        self.assertEqual(len(sb.upserts), 1)

    def test_api_failure_is_recorded_and_nonzero(self):
        cat = FakeCatalog(self.info(), {"1"}, fail="429 rate_limit"); sb = FakeSb()
        payload, code = step.run(sb, dry_run=False, catalog=cat, now=NOW)
        self.assertEqual((code, payload["error"], payload["written"], payload["missing_after"]), (1, "429 rate_limit", 0, 2))
        self.assertEqual(sb.upserts[-1][1]["payload"]["error"], "429 rate_limit")

    def test_step_is_in_pipeline_after_fbo_and_nonfatal(self):
        titles = [t for t, _c in pipe.STEPS]
        i = titles.index("Ozon: загрузка FBO заказов")
        self.assertEqual(titles[i + 1], "Ozon: каталог, добор недостающих")
        self.assertFalse(pipe.is_fatal_step("Ozon: каталог, добор недостающих"))


class AlertLinesTests(unittest.TestCase):
    def test_catalog_line(self):
        self.assertEqual(at.catalog_line(None, NOW), "")
        fresh = {"finished_at": "2026-09-28T00:36:10+00:00", "missing_after": 0, "written": 9}
        self.assertEqual(at.catalog_line(fresh, NOW), "каталог: без карточки 0 SKU (добрано 9)")
        self.assertTrue(at.catalog_line({**fresh, "error": "429"}, NOW).startswith("⚠️ каталог: без карточки 0 SKU (добрано 9); ошибка шага: 429"))
        self.assertIn("записи за эту ночь нет", at.catalog_line({**fresh, "finished_at": "2026-09-26T00:36:10+00:00"}, NOW))

    def test_finrez_book_line(self):
        self.assertEqual(at.finrez_book_line(None, NOW), "")
        ok = {"finished_at": "2026-09-28T03:25:40+00:00", "seconds": 1452, "bytes": 86145579, "sha256": "7538072dacfa3b63", "rc": 0, "copied": None}
        self.assertEqual(at.finrez_book_line(ok, NOW), "книга Фин рез: собрана 03:25 UTC за 24 мин, 86 145 579 байт, sha256 7538072dacfa…; копия не делалась")
        self.assertIn("копия /Users/x/Фин рез", at.finrez_book_line({**ok, "copied": "/Users/x/Фин рез/finrez.xlsx"}, NOW))
        self.assertEqual(at.finrez_book_line({**ok, "rc": 1, "error": "report_finrez.py завершился с кодом 1"}, NOW),
                         "⚠️ книга Фин рез: сборка не удалась (report_finrez.py завершился с кодом 1)")
        self.assertIn("сегодняшней сборки нет", at.finrez_book_line({**ok, "finished_at": "2026-09-26T03:25:40+00:00"}, NOW))

    def test_build_message_includes_lines_when_states_exist(self):
        with mock.patch.object(at, "get_state_payload", side_effect=lambda key, client=None: {
                at.CATALOG_STATE_KEY: {"finished_at": NOW.isoformat(), "missing_after": 2, "written": 7},
                at.FINREZ_STATE_KEY: None}.get(key)):
            self.assertEqual(at.catalog_line(at.get_state_payload(at.CATALOG_STATE_KEY), NOW), "каталог: без карточки 2 SKU (добрано 7)")
            self.assertEqual(at.finrez_book_line(at.get_state_payload(at.FINREZ_STATE_KEY), NOW), "")


if __name__ == "__main__":
    unittest.main()
