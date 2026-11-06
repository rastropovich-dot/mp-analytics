"""Остатки Ozon: SKU из ответа /v4, товары из самого метода, артикул по SKU из ozon_products (RBH 2026-10-31, варианты (б) + (в)).

Было: stock_daily.decision_marketplace_sku угадывался по артикулу (ровно один SKU у артикула в заказах и атрибуции за
90 дней), и на 28.10 без SKU лежало РБХ-1 1 425, РБХ-2 242, KARATOV 33 218 шт — хотя `stocks[].sku` приходит в том же
ответе. Товары брались из sku_catalog (у KARATOV — с 25.04), а загрузчики рекламы и аналитики искали артикул по SKU в
каталоге, ключ которого — product_id. Фикстура — товары из ответов /v4 за 29.10 (выгрузка плана продаж).
"""
import json
import os
import unittest
from unittest import mock

import loaders.ozon_stocks_loader as stocks
import loaders.ozon_performance_ads_loader as perf
import loaders.ozon_sku_total_analytics_loader as analytics

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "ozon_v4_stocks_20261029_sample.json")


def sample(cab):
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["cabinets"][cab]


class Response:
    def __init__(self, data, status=200):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


class SkuFromApi(unittest.TestCase):
    def test_every_row_takes_sku_of_its_stock_entry_and_keeps_product_id_key(self):
        for cab in ("rbh1", "rbh2"):
            items = sample(cab)
            rows, counters = stocks.build_stock_rows(items, True, {}, "2026-10-29")
            by_pid = {str(i["product_id"]): {s["sku"] for s in i["stocks"]} for i in items}
            self.assertEqual(len(rows), sum(len(i["stocks"]) for i in items))
            for r in rows:
                self.assertEqual(r["marketplace_sku"], r["product_id"])
                self.assertEqual(r["stock_marketplace_sku"], r["product_id"])
                self.assertEqual({int(r["decision_marketplace_sku"])}, by_pid[r["product_id"]])
                self.assertEqual(r["stock_identity_status"], "sku_from_api")
            self.assertEqual(set(counters), {"sku_from_api"})

    def test_items_unresolved_on_28_10_get_the_card_sku(self):
        # 6092739 «Тени RELOADED»: у артикула в заказах два SKU (227239336 и 5948414665), догадка отказывала на 1 318 шт
        rows, _ = stocks.build_stock_rows(sample("rbh1"), True, {}, "2026-10-29")
        got = {r["product_id"]: r["decision_marketplace_sku"] for r in rows}
        self.assertEqual(got["53130305"], "227239336")
        rows, _ = stocks.build_stock_rows(sample("rbh2"), True, {}, "2026-10-29")
        got = {r["product_id"]: r["decision_marketplace_sku"] for r in rows}
        self.assertEqual(got["497061814"], "945083244")
        self.assertEqual(got["1255250157"], "1753271582")

    def test_quantities_are_summed_per_product_and_warehouse_type(self):
        items = sample("rbh1")[:1]
        rows, _ = stocks.build_stock_rows(items, True, {}, "2026-10-29")
        want = {s["type"]: (s["present"], s["reserved"]) for s in items[0]["stocks"]}
        self.assertEqual({r["warehouse_name"]: (r["stock_qty"], r["reserved_qty"]) for r in rows}, want)
        for r in rows:
            self.assertEqual(r["available_qty"], r["stock_qty"] - r["reserved_qty"])

    def test_article_guess_only_when_the_field_is_missing(self):
        items = [{"product_id": 1, "offer_id": "A", "stocks": [{"type": "fbo", "present": 3, "reserved": 0}]},
                 {"product_id": 2, "offer_id": "B", "stocks": [{"type": "fbo", "present": 1, "reserved": 0, "sku": 0}]},
                 {"product_id": 3, "offer_id": "C", "stocks": [{"type": "fbo", "present": 1, "reserved": 0, "sku": 77}]}]
        rows, counters = stocks.build_stock_rows(items, True, {"A": "555", "C": "999"}, "2026-10-29")
        got = {r["product_id"]: (r["decision_marketplace_sku"], r["stock_identity_status"]) for r in rows}
        self.assertEqual(got["1"], ("555", "mapped_by_article"))
        self.assertEqual(got["2"], (None, "unresolved"))
        self.assertEqual(got["3"], ("77", "sku_from_api"))      # поле есть — догадка не нужна, даже если она другая
        self.assertEqual(dict(counters), {"mapped_by_article": 1, "unresolved": 1, "sku_from_api": 1})
        self.assertTrue(stocks.needs_article_fallback(items))
        self.assertFalse(stocks.needs_article_fallback(sample("rbh1")))

    def test_without_identity_columns_rows_have_no_identity_fields(self):
        rows, _ = stocks.build_stock_rows(sample("rbh2"), False, {}, "2026-10-29")
        self.assertTrue(rows)
        self.assertFalse(any("decision_marketplace_sku" in r for r in rows))


class ProductsFromTheMethod(unittest.TestCase):
    def pages(self, script):
        calls = []

        def post(payload):
            calls.append(json.loads(json.dumps(payload)))
            return script[(payload["filter"]["visibility"], payload["cursor"])]
        return post, calls

    def test_cursor_is_walked_in_both_passes_and_archive_keeps_only_stocked_items(self):
        a = sample("rbh1")
        empty = {"product_id": 900, "offer_id": "X", "stocks": [{"type": "fbo", "present": 0, "reserved": 0, "sku": 9}]}
        stocked = {"product_id": 901, "offer_id": "Y", "stocks": [{"type": "fbo", "present": 0, "reserved": 2, "sku": 10}]}
        script = {
            ("ALL", ""): Response({"items": a[:2], "cursor": "c1"}),
            ("ALL", "c1"): Response({"items": a[2:], "cursor": ""}),
            ("ARCHIVED", ""): Response({"items": [empty, stocked, a[0]], "cursor": "z"}),
            ("ARCHIVED", "z"): Response({"items": [], "cursor": ""}),
        }
        post, calls = self.pages(script)
        items, summary = stocks.get_ozon_stocks(post=post, sleep=lambda s: None)
        pids = [str(i["product_id"]) for i in items]
        self.assertEqual(pids, [str(i["product_id"]) for i in a] + ["901"])   # пустой архивный — нет; дубль из ALL — нет
        self.assertEqual(summary["requests"], 4)
        self.assertEqual(summary["ALL"], {"pages": 2, "items_returned": len(a), "items_kept": len(a)})
        self.assertEqual(summary["ARCHIVED"], {"pages": 2, "items_returned": 3, "items_kept": 1})
        self.assertTrue(all(c["limit"] == 1000 and set(c["filter"]) == {"visibility"} for c in calls))

    def test_all_pass_keeps_items_without_stock(self):
        a = sample("rbh2")
        post, _ = self.pages({("ALL", ""): Response({"items": a, "cursor": ""}),
                              ("ARCHIVED", ""): Response({"items": [], "cursor": ""})})
        items, _ = stocks.get_ozon_stocks(post=post, sleep=lambda s: None)
        self.assertEqual(len(items), len(a))
        self.assertTrue(any(not stocks.item_has_stock(i) for i in items))

    def test_refusal_stops_the_step_instead_of_writing_a_partial_list(self):
        post, _ = self.pages({("ALL", ""): Response({"items": sample("rbh1"), "cursor": "c1"}),
                              ("ALL", "c1"): Response({"code": 8, "message": "limit"}, status=429)})
        with self.assertRaises(RuntimeError) as ctx:
            stocks.get_ozon_stocks(post=post, sleep=lambda s: None)
        self.assertIn("HTTP 429", str(ctx.exception))

    def test_endless_cursor_is_cut(self):
        post = lambda payload: Response({"items": sample("rbh1")[:1], "cursor": "same"})
        with mock.patch.object(stocks, "STOCKS_MAX_PAGES_PER_PASS", 3):
            with self.assertRaises(RuntimeError):
                stocks.get_ozon_stocks(post=post, sleep=lambda s: None)


class FakeQuery:
    def __init__(self, rows, log):
        self.rows, self.log, self.rng = rows, log, (0, 999)

    def select(self, cols):
        self.log.append(("select", cols)); return self

    def order(self, col):
        self.log.append(("order", col)); return self

    def range(self, a, b):
        self.rng = (a, b); return self

    def execute(self):
        a, b = self.rng
        return mock.Mock(data=self.rows[a:b + 1])


class FakeClient:
    def __init__(self, rows):
        self.rows, self.log = rows, []

    def table(self, name):
        self.log.append(("table", name))
        return FakeQuery(self.rows, self.log)


class ArticleBySku(unittest.TestCase):
    def test_both_loaders_read_ozon_products_by_sku_page_by_page_in_order(self):
        rows = [{"sku": str(1000 + i), "offer_id": f"A{i}", "name": f"N{i}"} for i in range(1500)]
        for module in (perf, analytics):
            client = FakeClient(rows)
            catalog = module.load_catalog(client)
            self.assertEqual(len(catalog), 1500)
            self.assertEqual(catalog["1001"], {"article": "A1", "product_name": "N1"})
            self.assertIn(("table", "ozon_products"), client.log)
            self.assertNotIn(("table", "sku_catalog"), client.log)
            self.assertEqual(client.log.count(("order", "sku")), 2)    # две страницы, обе с сортировкой

    def test_enrich_fills_article_of_order_and_promoted_sku(self):
        catalog = {"227239336": {"article": "6092739", "product_name": "Тени"}, "5": {"article": "P", "product_name": "x"}}
        rows = [{"marketplace_sku": "227239336", "article": "", "product_name": "", "promoted_sku": "5"},
                {"marketplace_sku": "53130305", "article": "", "product_name": ""}]   # product_id — не SKU, не найдётся
        perf.enrich_rows(rows, catalog)
        self.assertEqual((rows[0]["article"], rows[0]["promoted_article"]), ("6092739", "P"))
        self.assertEqual(rows[1]["article"], "")
        rows = [{"marketplace_sku": "227239336", "article": "", "product_name": ""}]
        analytics.enrich_rows(rows, catalog)
        self.assertEqual(rows[0]["article"], "6092739")


if __name__ == "__main__":
    unittest.main()
