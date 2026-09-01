import unittest

import loaders.ozon_performance_ads_loader as loader


# Фактический уникальный индекс marketplace_expenses (проверен в БД):
#   marketplace_expenses_unique_idx (expense_date, marketplace_code, marketplace_sku, expense_type)
CONFLICT_KEY = ("expense_date", "marketplace_code", "marketplace_sku", "expense_type")


def upsert(existing, incoming):
    """Модель upsert по фактическому ключу конфликта."""
    table = {tuple(row[field] for field in CONFLICT_KEY): dict(row) for row in existing}
    for row in incoming:
        table[tuple(row[field] for field in CONFLICT_KEY)] = dict(row)
    return list(table.values())


def row(sku, spend, expense_type):
    return {"expense_date": "2026-07-13", "marketplace_code": "ozon",
            "marketplace_sku": sku, "expense_type": expense_type, "expense_amount": spend}


def cpc_total(rows):
    """Как считают расход детектор и reports_ozon_ad_diagnostic_rule.py:459."""
    return round(sum(r["expense_amount"] for r in rows
                     if r["expense_type"] in ("advertising_clicks", "advertising_other")), 2)


class ConflictKeyTest(unittest.TestCase):
    def test_expense_type_is_part_of_the_conflict_key(self):
        self.assertIn("expense_type", CONFLICT_KEY)
        self.assertIn("expense_type", loader.UPSERT_KEY_FIELDS)


class ReloadOverAdvertisingOtherTest(unittest.TestCase):
    """Блокирующий случай: дата, испорченная дефектом A, перезагружается после 6967f4e."""

    def test_reload_doubles_the_spend(self):
        existing = [row("1", 14216.52, "advertising_other")]      # что лежит сейчас
        incoming = [row("1", 14216.52, "advertising_clicks")]     # что положит новый код

        after = upsert(existing, incoming)

        self.assertEqual(len(after), 2, "старая строка не перетирается — тип входит в ключ")
        self.assertEqual(cpc_total(existing), 14216.52)
        self.assertEqual(cpc_total(after), 28433.04, "расход по дате удваивается")

    def test_deleting_the_stale_rows_first_makes_reload_safe(self):
        existing = [row("1", 14216.52, "advertising_other")]
        incoming = [row("1", 14216.52, "advertising_clicks")]

        cleaned = [r for r in existing if r["expense_type"] != "advertising_other"]
        after = upsert(cleaned, incoming)

        self.assertEqual(len(after), 1)
        self.assertEqual(cpc_total(after), 14216.52)

    def test_reclassifying_in_place_also_makes_reload_safe(self):
        # Переклассификация: та же сумма переезжает в advertising_clicks.
        existing = [row("1", 14216.52, "advertising_clicks")]
        incoming = [row("1", 14216.52, "advertising_clicks")]
        after = upsert(existing, incoming)
        self.assertEqual(len(after), 1)
        self.assertEqual(cpc_total(after), 14216.52)


class ReloadOverCleanDateTest(unittest.TestCase):
    def test_reload_of_a_clean_date_is_idempotent(self):
        existing = [row("1", 100.0, "advertising_clicks"), row("2", 50.0, "advertising_clicks")]
        after = upsert(existing, list(existing))
        self.assertEqual(len(after), 2)
        self.assertEqual(cpc_total(after), 150.0)

    def test_partial_reload_leaves_untouched_skus_alone(self):
        # Бэкфилл берёт часть кампаний: не тронутые SKU остаются как были.
        existing = [row("1", 100.0, "advertising_clicks"), row("2", 50.0, "advertising_clicks")]
        after = upsert(existing, [row("1", 120.0, "advertising_clicks")])
        by_sku = {r["marketplace_sku"]: r["expense_amount"] for r in after}
        self.assertEqual(by_sku, {"1": 120.0, "2": 50.0})

    def test_stale_sku_from_an_earlier_load_is_never_removed(self):
        # Upsert не удаляет: SKU, которого нет в новой выгрузке, останется навсегда.
        existing = [row("gone", 999.0, "advertising_clicks")]
        after = upsert(existing, [row("1", 100.0, "advertising_clicks")])
        self.assertEqual(len(after), 2)
        self.assertEqual(cpc_total(after), 1099.0)


if __name__ == "__main__":
    unittest.main()
