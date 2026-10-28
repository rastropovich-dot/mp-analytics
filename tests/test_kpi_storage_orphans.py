"""59-я, вариант А: строки «только хранение» за дни, выпавшие из окна 30 дней, удаляются после пересчёта витрины — только Ozon,
только ключи, которых прогон не построил, и только если в строке ничего, кроме хранения (ночь 28.10: 27.09 — 2 739 строк, хранение
534,08 дважды). Окно, строка с продажами / расходами, день без построенных ключей, непрочитанный источник, --dry-run — не трогаются."""
import unittest
from datetime import date
from unittest import mock

import reports_daily_sku_kpi as kpi
from tests.test_stale_keys import FakeSupabase

TODAY = date(2026, 10, 28)          # окно с 2026-09-28; чистка «только хранение» — 2026-09-21 … 2026-09-27


def kpi_row(day, sku, id_, market="ozon", storage=0.0, logistics=None, **money):
    row = {"id": id_, "kpi_date": day, "marketplace_code": market, "marketplace_sku": sku,
           "storage_cost": storage, "logistics_amount": storage if logistics is None else logistics}
    row.update({c: 0 for c in kpi.STORAGE_ONLY_ZERO_COLUMNS})
    row.update(money)
    return row


def built(day, sku, market="ozon"):
    return {"kpi_date": day, "marketplace_code": market, "marketplace_sku": sku}


class StorageOnlyOrphans(unittest.TestCase):
    def setUp(self):
        kpi.SOURCE_READ_FAILURES.clear()

    def tearDown(self):
        kpi.SOURCE_READ_FAILURES.clear()

    def run_cleanup(self, table, rows, apply=True, days=None):
        sb = FakeSupabase({"daily_sku_kpi": table})
        with mock.patch("builtins.print"):
            n = kpi.cleanup_storage_only_orphans(rows, today=TODAY, apply=apply, sb=sb, days=days)
        return n, sb.deleted

    def test_day_that_left_the_window_loses_storage_only_rows(self):
        table = [kpi_row("2026-09-27", "100", 1, orders_qty=1, orders_amount_seller=900.5),   # построен — остаётся
                 kpi_row("2026-09-27", "200", 2, storage=4.72),                               # не построен, только хранение
                 kpi_row("2026-09-27", "300", 3),                                             # не построен, нули — тоже «только хранение»
                 kpi_row("2026-09-27", "", 4, logistics=1239.72)]                             # строка без SKU построена
        n, deleted = self.run_cleanup(table, [built("2026-09-27", "100"), built("2026-09-27", "")])
        self.assertEqual(n, 2)
        self.assertEqual(deleted, [("daily_sku_kpi", [("in_", "id", [2, 3])])])

    def test_unbuilt_row_with_money_besides_storage_is_kept(self):
        table = [kpi_row("2026-09-27", "100", 1),
                 kpi_row("2026-09-27", "200", 2, storage=1.0, buyouts_amount_seller=500),     # выкуп: это не «только хранение»
                 kpi_row("2026-09-27", "300", 3, storage=1.0, logistics=7.7),                 # логистика ≠ хранению
                 kpi_row("2026-09-27", "400", 4, storage=1.0, commission_amount=3.3)]
        n, deleted = self.run_cleanup(table, [built("2026-09-27", "100")])
        self.assertEqual((n, deleted), (0, []))

    def test_window_and_wb_are_never_read(self):
        # 09-28 — внутри окна (там «только хранение» законны), WB — свои правила
        table = [kpi_row("2026-09-28", "200", 1, storage=1.0), kpi_row("2026-09-27", "wb1", 2, market="wb"),
                 kpi_row("2026-09-27", "100", 3)]
        n, deleted = self.run_cleanup(table, [built("2026-09-27", "100")])
        self.assertEqual((n, deleted), (0, []))
        with self.assertRaises(ValueError):
            self.run_cleanup(table, [], days=("2026-09-27", "2026-09-28"))

    def test_older_than_lookback_is_not_touched(self):
        table = [kpi_row("2026-09-20", "200", 1, storage=1.0), kpi_row("2026-09-20", "100", 2)]
        n, deleted = self.run_cleanup(table, [built("2026-09-20", "100")])
        self.assertEqual((n, deleted), (0, []))

    def test_day_without_built_keys_is_skipped(self):
        table = [kpi_row("2026-09-26", "200", 1, storage=1.0), kpi_row("2026-09-27", "100", 2)]
        n, deleted = self.run_cleanup(table, [built("2026-09-27", "100")])
        self.assertEqual((n, deleted), (0, []))

    def test_unread_source_blocks_deletion(self):
        kpi.SOURCE_READ_FAILURES.append(kpi.PLACEMENT_TABLE)
        table = [kpi_row("2026-09-27", "200", 1, storage=1.0), kpi_row("2026-09-27", "100", 2)]
        n, deleted = self.run_cleanup(table, [built("2026-09-27", "100")])
        self.assertEqual((n, deleted), (0, []))

    def test_dry_run_does_not_delete(self):
        table = [kpi_row("2026-09-27", "200", 1, storage=1.0), kpi_row("2026-09-27", "100", 2)]
        n, deleted = self.run_cleanup(table, [built("2026-09-27", "100")], apply=False)
        self.assertEqual((n, deleted), (0, []))

    def test_explicit_one_day(self):
        table = [kpi_row("2026-09-27", "200", 1), kpi_row("2026-09-27", "100", 2), kpi_row("2026-09-26", "300", 3), kpi_row("2026-09-26", "400", 4)]
        n, deleted = self.run_cleanup(table, [built("2026-09-27", "100"), built("2026-09-26", "400")], days=("2026-09-27", "2026-09-27"))
        self.assertEqual(deleted, [("daily_sku_kpi", [("in_", "id", [1])])])


class IsStorageOnly(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(kpi.is_storage_only(kpi_row("2026-09-27", "1", 1, storage=0.004, logistics=0.0)))   # до копейки
        self.assertFalse(kpi.is_storage_only(kpi_row("2026-09-27", "", 1)))                                  # строка без SKU — никогда
        self.assertFalse(kpi.is_storage_only(kpi_row("2026-09-27", "1", 1, organic_orders_qty=1)))


if __name__ == "__main__":
    unittest.main()
