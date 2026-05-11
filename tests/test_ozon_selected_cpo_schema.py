from pathlib import Path
import unittest


MIGRATION_PATH = Path(__file__).resolve().parents[1] / "sql" / "20260511_create_ozon_search_promo_selected_cpo_orders.sql"


class SelectedCpoSchemaTests(unittest.TestCase):
    def test_migration_artifact_exists(self):
        self.assertTrue(MIGRATION_PATH.exists())

    def test_migration_contains_expected_table_and_columns(self):
        text = MIGRATION_PATH.read_text(encoding="utf-8").lower()
        self.assertIn("create table if not exists public.ozon_search_promo_selected_cpo_orders", text)
        for needle in (
            "source_report text not null",
            "promotion_type text not null",
            "ordered_sku text not null",
            "promoted_sku text not null",
            "order_id text not null",
            "posting_number text not null",
            "spend numeric(14,2) not null",
            "raw_row jsonb",
        ):
            self.assertIn(needle, text)

    def test_unique_key_covers_idempotency_and_excludes_source_uuid(self):
        text = MIGRATION_PATH.read_text(encoding="utf-8").lower()
        self.assertIn("create unique index if not exists uq_ozon_search_promo_selected_cpo_orders_idem", text)
        for needle in (
            "sale_date",
            "marketplace_code",
            "source_report",
            "promotion_type",
            "order_id",
            "posting_number",
            "ordered_sku",
            "promoted_sku",
        ):
            self.assertIn(needle, text)
        self.assertNotIn("source_uuid text not null", text)
        unique_section = text.split("create unique index if not exists uq_ozon_search_promo_selected_cpo_orders_idem", 1)[1]
        self.assertNotIn("source_uuid", unique_section.split(");", 1)[0])

    def test_migration_does_not_touch_existing_tables(self):
        text = MIGRATION_PATH.read_text(encoding="utf-8").lower()
        self.assertNotIn("alter table if exists public.ozon_daily_sku_ad_attribution", text)
        self.assertNotIn("alter table if exists public.marketplace_expenses", text)
        self.assertNotIn("drop table", text)


if __name__ == "__main__":
    unittest.main()
