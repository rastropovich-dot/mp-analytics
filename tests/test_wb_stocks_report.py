"""Разбор нового отчёта об остатках WB под stock_daily.

Форма ответа взята с живого запроса 2026-09-03: data.items[] со строкой на
(warehouseId, nmId, chrtId), без артикула и названия, с агрегатным складом
warehouseId = -999999 «Склад WB».
"""

import unittest
from datetime import date
from unittest import mock

import loaders.wb_stocks_loader as wb_stocks

# Колонки stock_daily, которые заполняет загрузчик. Остальные — служебные
# (id, created_at) и колонки идентичности, их пишет другой слой.
STOCK_DAILY_COLUMNS = {
    "stock_date", "marketplace_code", "marketplace_sku", "article",
    "product_name", "warehouse_name", "stock_qty", "reserved_qty", "available_qty",
}


def item(nm_id, chrt_id, quantity, warehouse="Склад WB", warehouse_id=-999999):
    return {
        "nmId": nm_id,
        "chrtId": chrt_id,
        "warehouseId": warehouse_id,
        "warehouseName": warehouse,
        "regionName": warehouse,
        "quantity": quantity,
        "inWayToClient": 0,
        "inWayFromClient": 0,
    }


class Response:
    def __init__(self, payload, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self.payload


def page(items):
    return Response({"data": {"items": items}})


class AggregateTests(unittest.TestCase):
    def test_sizes_are_collapsed_into_one_sku_row(self):
        """Ключ stock_daily не знает про размер: chrtId надо схлопнуть, иначе
        строки затрут друг друга по одному ключу."""
        rows = wb_stocks.aggregate_stocks(
            [item(100, 1, 3), item(100, 2, 4), item(200, 3, 5)],
            today=date(2026, 9, 3),
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[("2026-09-03", "wb", "100", "Склад WB")]["stock_qty"], 7)
        self.assertEqual(rows[("2026-09-03", "wb", "100", "Склад WB")]["available_qty"], 7)

    def test_zero_stock_rows_are_kept(self):
        """«Остаток кончился» — тоже факт, и старый загрузчик их писал."""
        rows = wb_stocks.aggregate_stocks([item(100, 1, 0)], today=date(2026, 9, 3))
        self.assertEqual(len(rows), 1)
        self.assertEqual(list(rows.values())[0]["stock_qty"], 0)

    def test_rows_match_stock_daily_columns(self):
        rows = wb_stocks.aggregate_stocks([item(100, 1, 3)], today=date(2026, 9, 3))
        self.assertEqual(set(list(rows.values())[0]), STOCK_DAILY_COLUMNS)

    def test_article_and_name_come_from_the_catalog(self):
        catalog = {"100": {"article": "F000283615", "product_name": "Серьги"}}
        rows = wb_stocks.aggregate_stocks([item(100, 1, 3)], catalog=catalog, today=date(2026, 9, 3))
        row = list(rows.values())[0]
        self.assertEqual(row["article"], "F000283615")
        self.assertEqual(row["product_name"], "Серьги")

    def test_sku_missing_from_catalog_gets_empty_article_not_a_crash(self):
        rows = wb_stocks.aggregate_stocks([item(100, 1, 3)], catalog={}, today=date(2026, 9, 3))
        self.assertEqual(list(rows.values())[0]["article"], "")

    def test_real_warehouses_split_into_separate_rows(self):
        """Если WB когда-нибудь начнёт отдавать разбивку, менять код не придётся."""
        rows = wb_stocks.aggregate_stocks(
            [item(100, 1, 3, warehouse="Коледино", warehouse_id=507),
             item(100, 2, 4, warehouse="Казань", warehouse_id=117501)],
            today=date(2026, 9, 3),
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual({key[3] for key in rows}, {"Коледино", "Казань"})

    def test_item_without_nm_id_is_dropped(self):
        rows = wb_stocks.aggregate_stocks([{"chrtId": 1, "quantity": 5}], today=date(2026, 9, 3))
        self.assertEqual(rows, {})


class PaginationTests(unittest.TestCase):
    def test_pages_are_walked_until_a_short_page(self):
        pages = [page([item(i, i, 1) for i in range(2)]), page([item(9, 9, 1)])]

        with mock.patch.object(wb_stocks.http_retry, "post", side_effect=pages) as post, \
             mock.patch.object(wb_stocks.time, "sleep"):
            items = wb_stocks.get_wb_stocks(page_limit=2)

        self.assertEqual(len(items), 3)
        self.assertEqual([call.kwargs["json"]["offset"] for call in post.call_args_list], [0, 2])

    def test_error_returns_what_was_collected_so_far(self):
        pages = [page([item(i, i, 1) for i in range(2)]), Response(None, status_code=429, text="rate limit")]

        with mock.patch.object(wb_stocks.http_retry, "post", side_effect=pages), \
             mock.patch.object(wb_stocks.time, "sleep"):
            items = wb_stocks.get_wb_stocks(page_limit=2)

        self.assertEqual(len(items), 2)

    def test_deprecated_endpoint_body_would_yield_nothing(self):
        """Ровно то, что 51 ночь подряд выглядело как «✅ Готово»."""
        dead = Response(None, status_code=404, text='{"detail":"This method is deprecated."}')

        with mock.patch.object(wb_stocks.http_retry, "post", return_value=dead):
            self.assertEqual(wb_stocks.get_wb_stocks(), [])


class DescribeTests(unittest.TestCase):
    def test_aggregate_warehouse_is_called_out(self):
        rows = list(wb_stocks.aggregate_stocks([item(100, 1, 3)], today=date(2026, 9, 3)).values())
        self.assertIn("Разбивки по складам нет", wb_stocks.describe_rows(rows))

    def test_missing_articles_are_called_out(self):
        rows = list(wb_stocks.aggregate_stocks([item(100, 1, 3)], catalog={}, today=date(2026, 9, 3)).values())
        self.assertIn("Без артикула", wb_stocks.describe_rows(rows))

    def test_real_warehouses_do_not_trigger_the_warning(self):
        rows = list(wb_stocks.aggregate_stocks(
            [item(100, 1, 3, warehouse="Коледино", warehouse_id=507)],
            catalog={"100": {"article": "F1", "product_name": "x"}},
            today=date(2026, 9, 3),
        ).values())
        self.assertNotIn("Разбивки по складам нет", wb_stocks.describe_rows(rows))


class DryRunTests(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        with mock.patch.object(wb_stocks, "get_wb_stocks", return_value=[item(100, 1, 3)]), \
             mock.patch.object(wb_stocks, "load_sku_catalog", return_value={}), \
             mock.patch.object(wb_stocks, "save_wb_stocks") as save:
            code = wb_stocks.main(["--dry-run"])

        self.assertEqual(code, 0)
        save.assert_not_called()

    def test_empty_response_is_a_failure_not_a_green_step(self):
        with mock.patch.object(wb_stocks, "get_wb_stocks", return_value=[]), \
             mock.patch.object(wb_stocks, "save_wb_stocks") as save:
            code = wb_stocks.main([])

        self.assertEqual(code, 1)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
