"""Ключи витрины без источника: после пересчёта KPI удаляются ключи окна (Ozon, 30 дней), которых этот прогон
не построил ни из одной из четырёх таблиц. Вне окна, WB, при непрочитанном источнике, сверх порога и в --dry-run
— ничего не удаляется.
"""
import unittest
from datetime import date
from unittest import mock

import reports_daily_sku_kpi as kpi
from tests.test_stale_keys import FakeSupabase

TODAY = date(2026, 9, 23)


def kpi_row(day, sku, id_, market="ozon", buy=0):
    return {"id": id_, "kpi_date": day, "marketplace_code": market, "marketplace_sku": sku,
            "orders_amount_seller": 0, "buyouts_amount_seller": buy, "ad_spend": 0, "other_expenses_amount": 0}


def built(day, sku, market="ozon"):
    return {"kpi_date": day, "marketplace_code": market, "marketplace_sku": sku}


class KpiStaleKeysTests(unittest.TestCase):
    def setUp(self):
        kpi.SOURCE_READ_FAILURES.clear()

    def tearDown(self):
        kpi.SOURCE_READ_FAILURES.clear()

    def run_cleanup(self, table, rows, apply=True):
        sb = FakeSupabase({"daily_sku_kpi": table})
        with mock.patch("builtins.print"):
            n = kpi.cleanup_stale_kpi_keys(rows, today=TODAY, apply=apply, sb=sb)
        return n, sb.deleted

    def test_key_without_source_in_window_is_deleted(self):
        table = [kpi_row("2026-08-25", "1621630016", 1, buy=171227), kpi_row("2026-08-25", "111", 2)]
        n, deleted = self.run_cleanup(table, [built("2026-08-25", "111")])
        self.assertEqual(n, 1)
        self.assertEqual(deleted, [("daily_sku_kpi", [("in_", "id", [1])])])

    def test_key_with_source_is_kept(self):
        table = [kpi_row("2026-09-01", "111", 1), kpi_row("2026-09-01", "222", 2)]
        n, deleted = self.run_cleanup(table, [built("2026-09-01", "111"), built("2026-09-01", "222")])
        self.assertEqual((n, deleted), (0, []))

    def test_keys_outside_window_are_never_touched(self):
        # 08-23 = сегодня − 31 день: вне окна, хотя источника нет
        table = [kpi_row("2026-08-23", "999", 1), kpi_row("2026-08-24", "111", 2)]
        n, deleted = self.run_cleanup(table, [built("2026-08-24", "111")])
        self.assertEqual((n, deleted), (0, []))

    def test_wb_keys_are_not_read(self):
        table = [kpi_row("2026-09-01", "wb1", 1, market="wb"), kpi_row("2026-09-01", "111", 2)]
        n, deleted = self.run_cleanup(table, [built("2026-09-01", "111")])
        self.assertEqual((n, deleted), (0, []))

    def test_unread_source_blocks_deletion(self):
        kpi.SOURCE_READ_FAILURES.append("ozon_daily_sku_organic")
        table = [kpi_row("2026-09-01", "999", 1), kpi_row("2026-09-01", "111", 2)]
        n, deleted = self.run_cleanup(table, [built("2026-09-01", "111")])
        self.assertEqual((n, deleted), (0, []))

    def test_day_without_built_keys_is_skipped(self):
        table = [kpi_row("2026-09-02", "999", 1), kpi_row("2026-09-01", "111", 2)]
        n, deleted = self.run_cleanup(table, [built("2026-09-01", "111")])
        self.assertEqual((n, deleted), (0, []))

    def test_threshold_refuses_mass_deletion(self):
        table = [kpi_row("2026-09-01", str(i), i) for i in range(1, 30)]
        n, deleted = self.run_cleanup(table, [built("2026-09-01", "1")])
        self.assertEqual((n, deleted), (0, []))       # 28 к удалению > порога 10 на окне в 29 строк

    def test_dry_run_does_not_delete(self):
        table = [kpi_row("2026-08-25", "1621630016", 1), kpi_row("2026-08-25", "111", 2)]
        n, deleted = self.run_cleanup(table, [built("2026-08-25", "111")], apply=False)
        self.assertEqual((n, deleted), (0, []))

    def test_organic_read_failure_is_recorded(self):
        with mock.patch.object(kpi, "read_all_by_key", side_effect=Exception("relation does not exist")), mock.patch("builtins.print"):
            self.assertEqual(kpi.load_ozon_organic(), [])
        self.assertEqual(kpi.SOURCE_READ_FAILURES, ["ozon_daily_sku_organic"])


if __name__ == "__main__":
    unittest.main()
