"""Артикул в витрине KPI: дописывается по карте sku → article из заказов там, где строка его не принесла.

Артикул приносит только строка заказа; у выкупов и расходов он пуст. Выкуп приходит не в день заказа,
поэтому его ключ оставался без артикула. Суммы витрины правка не меняет — только заполненность article.
"""
import unittest
from unittest import mock

import reports_daily_sku_kpi as kpi


def order(day, sku, article, qty=1, cancelled=0, mp="ozon", name="товар"):
    return {"order_date": day, "marketplace_code": mp, "marketplace_sku": sku, "article": article, "product_name": name,
            "orders_qty": qty, "orders_amount_seller": 1000 * qty, "cancelled_orders_qty": cancelled}


def buyout(day, sku, amount=900, mp="ozon"):
    return {"buyout_date": day, "marketplace_code": mp, "marketplace_sku": sku, "article": "", "product_name": None,
            "buyouts_qty": 1, "buyouts_amount_seller": amount}


def expense(day, sku, kind="logistics", amount=50, mp="ozon"):
    return {"expense_date": day, "marketplace_code": mp, "marketplace_sku": sku, "article": "", "expense_type": kind, "expense_amount": amount}


class ArticleMapTests(unittest.TestCase):
    def build(self, orders, buyouts=(), expenses=()):
        with mock.patch.object(kpi, "load_orders", return_value=list(orders)), mock.patch.object(kpi, "load_buyouts", return_value=list(buyouts)), \
             mock.patch.object(kpi, "load_expenses", return_value=list(expenses)), mock.patch.object(kpi, "load_ozon_organic", return_value=[]), \
             mock.patch("builtins.print"):
            return {(r["kpi_date"], r["marketplace_code"], r["marketplace_sku"]): r for r in kpi.build_kpi()}

    def test_buyout_and_expense_days_get_the_article_of_their_sku(self):
        rows = self.build([order("2026-09-01", "11", "F11")], [buyout("2026-09-08", "11")], [expense("2026-09-09", "11")])
        self.assertEqual({k[0]: r["article"] for k, r in rows.items()}, {"2026-09-01": "F11", "2026-09-08": "F11", "2026-09-09": "F11"})
        self.assertEqual(rows[("2026-09-08", "ozon", "11")]["product_name"], "товар")

    def test_sums_do_not_change(self):
        rows = self.build([order("2026-09-01", "11", "F11")], [buyout("2026-09-08", "11", 900)], [expense("2026-09-08", "11", "logistics", 50)])
        r = rows[("2026-09-08", "ozon", "11")]
        self.assertEqual((r["orders_amount_seller"], r["buyouts_amount_seller"], r["logistics_amount"]), (0, 900, 50))

    def test_article_brought_by_the_row_is_not_overwritten(self):
        rows = self.build([order("2026-09-01", "11", "F-OLD", qty=1), order("2026-09-02", "11", "F-NEW", qty=5)])
        self.assertEqual(rows[("2026-09-01", "ozon", "11")]["article"], "F-OLD")

    def test_several_articles_pick_the_most_ordered_then_alphabetical(self):
        orders = [order("2026-09-01", "11", "F-B", qty=2), order("2026-09-02", "11", "F-A", qty=1, cancelled=1), order("2026-09-03", "11", "F-C", qty=1)]
        self.assertEqual(kpi.build_article_map(orders)[("ozon", "11")][0], "F-A")       # F-A и F-B по 2 созданных — меньший по алфавиту
        self.assertEqual(kpi.build_article_map(list(reversed(orders)))[("ozon", "11")][0], "F-A")   # порядок чтения не влияет

    def test_cancelled_only_keys_still_teach_the_map(self):
        rows = self.build([order("2026-09-01", "11", "F11", qty=0, cancelled=1)], [buyout("2026-09-08", "11")])
        self.assertEqual(rows[("2026-09-08", "ozon", "11")]["article"], "F11")

    def test_marketplaces_do_not_lend_articles_to_each_other(self):
        rows = self.build([order("2026-09-01", "11", "WB-ART", mp="wb")], [buyout("2026-09-08", "11", mp="ozon")])
        self.assertEqual(rows[("2026-09-08", "ozon", "11")]["article"], "")

    def test_sku_unknown_to_orders_stays_empty_and_rows_without_sku_too(self):
        rows = self.build([order("2026-09-01", "11", "F11")], [buyout("2026-09-08", "99")], [expense("2026-09-08", "")])
        self.assertEqual((rows[("2026-09-08", "ozon", "99")]["article"], rows[("2026-09-08", "ozon", "")]["article"]), ("", ""))


if __name__ == "__main__":
    unittest.main()
