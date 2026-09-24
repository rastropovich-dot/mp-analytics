"""Тридцать шестая §3: соинвест Ozon — в базу и в книгу.

Правила, которые закрепляют тесты: у выкупов оплачено покупателем = Σ sale_price (за строку), баллы и зелёные цены — свои
колонки, тождество seller − buyer = bonus + coinvestment считается построчно; у заказов цена покупателя — customer_price списка
FBS или отчёт ЛК для FBO, неизвестна хоть у одного товара ключа — null, не ноль и не цена продавца; запись без колонок
миграции не падает, а пишет без них и говорит об этом; отчёт ЛК опрашивается не чаще раза в 5 с и не дольше 3 минут; в
листах доля соинвеста пуста там, где цена не измерена. Сети нет ни в одном тесте.
"""
import importlib.util
import itertools
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from loaders import ozon_finance_accrual as accrual  # noqa: E402
from loaders import ozon_finance_transactions_loader as tx  # noqa: E402
from loaders import ozon_orders_rows as rules  # noqa: E402
from loaders import ozon_postings_report as report  # noqa: E402
import ozon_orders_forecast as fc  # noqa: E402

_spec = importlib.util.spec_from_file_location("report_ozon_month", os.path.join(ROOT, "scripts", "report_ozon_month.py"))
rep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rep)

D = Decimal


def m(v):
    return {"amount": str(v), "currency": "RUB"}


def accr(day, sale_amount, sale_price, bonus=None, coinv=None, commission="-100", sku="11"):
    c = {"sale_amount": m(sale_amount), "sale_commission": m(commission), "sale_price": m(sale_price)}
    if bonus is not None:
        c["bonus"] = m(bonus)
    if coinv is not None:
        c["coinvestment"] = m(coinv)
    return {"date": f"{day}T00:00:00Z", "posting": {"products": [{"sku": sku, "commission": c}]}}


class BuyoutRows(unittest.TestCase):
    def test_buyer_is_sale_price_and_bonus_coinvest_have_their_own_columns(self):
        rows, c = accrual.build_buyout_rows([accr("2026-09-20", "53398", "19843", "33356.57", "198.43"),
                                             accr("2026-09-20", "-2272", "-1000", "-1200", "-72", commission="50")])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["buyouts_amount_seller"], r["buyouts_amount_buyer"]), (51126.0, 18843.0))
        self.assertEqual((r["bonus_amount"], r["coinvestment_amount"]), (32156.57, 126.43))
        self.assertEqual(c.get("coinvest_identity_broken", 0), 0)
        self.assertAlmostEqual(r["buyouts_amount_seller"] - r["buyouts_amount_buyer"], r["bonus_amount"] + r["coinvestment_amount"], places=2)

    def test_broken_identity_is_counted_not_hidden(self):
        rows, c = accrual.build_buyout_rows([accr("2026-09-20", "100", "50", "10", "10")])
        self.assertEqual(c["coinvest_identity_broken"], 1)
        self.assertEqual(rows[0]["buyouts_amount_buyer"], 50.0)

    def test_old_shape_without_bonus_keys_gives_zero_bonus(self):
        rows, _c = accrual.build_buyout_rows([accr("2026-09-20", "100", "100")])
        self.assertEqual((rows[0]["bonus_amount"], rows[0]["coinvestment_amount"]), (0.0, 0.0))


class SaveWithoutColumns(unittest.TestCase):
    class FakeSb:
        def __init__(self, fail_first):
            self.calls, self.fail_first = [], fail_first

        def table(self, name):
            return self

        def upsert(self, rows, on_conflict):
            self.calls.append([dict(r) for r in rows]); return self

        def execute(self):
            if self.fail_first and len(self.calls) == 1:
                raise Exception('{"code":"PGRST204","message":"Could not find the \'bonus_amount\' column of \'marketplace_buyouts\' in the schema cache"}')
            return self

    def rows(self, n):
        return [{"buyout_date": "2026-09-20", "marketplace_code": "ozon", "marketplace_sku": str(i), "buyouts_amount_seller": 1.0,
                 "buyouts_amount_buyer": 0.5, "bonus_amount": 0.3, "coinvestment_amount": 0.2} for i in range(n)]

    def test_missing_columns_fall_back_to_rows_without_them_and_say_so(self):
        fake = self.FakeSb(fail_first=True)
        with mock.patch.object(tx, "supabase", fake), mock.patch("builtins.print") as out:
            tx.save_buyout_rows(self.rows(600))
        self.assertEqual(len(fake.calls), 3)                                   # упавшая первая пачка, она же без колонок, вторая пачка
        self.assertIn("bonus_amount", fake.calls[0][0])
        self.assertTrue(all("bonus_amount" not in r and "coinvestment_amount" not in r for call in fake.calls[1:] for r in call))
        self.assertEqual(sum(len(c) for c in fake.calls[1:]), 600)
        self.assertTrue(any("нет колонок" in str(c.args[0]) for c in out.call_args_list))

    def test_other_errors_are_not_swallowed(self):
        class Boom(self.FakeSb):
            def execute(self):
                raise Exception("statement timeout")
        with mock.patch.object(tx, "supabase", Boom(fail_first=False)), mock.patch("builtins.print"):
            with self.assertRaises(Exception):
                tx.save_buyout_rows(self.rows(3))


def posting(number, status, schema="fbo", when="2026-09-10T08:00:00Z", price="1000", qty=1, sku=11, customer_price=None):
    p = {"posting_number": number, "status": status, "in_process_at": when,
         "products": [{"sku": sku, "offer_id": f"F{sku}", "name": "товар", "quantity": qty,
                       "price": {"amount": price, "currency": "RUB"} if schema == "fbo" else price}]}
    if schema == "fbo":
        p["created_at"] = when
    else:
        p["shipment_date"] = "2026-09-11T19:00:00Z"
    if customer_price is not None:
        p["financial_data"] = {"products": [{"product_id": sku, "customer_price": customer_price, "price": float(price)}]}
    return p


class OrderRowsBuyerPrice(unittest.TestCase):
    def test_fbs_buyer_amount_is_customer_price_times_qty(self):
        rows, c = rules.build_order_rows([posting("A", "delivering", schema="fbs", qty=2, customer_price=600.5)], "fbs")
        self.assertEqual((rows[0]["orders_amount_seller"], rows[0]["orders_amount_buyer"]), (2000.0, 1201.0))
        self.assertEqual((c["buyer_price_known_products"], c.get("buyer_price_unknown_products", 0)), (1, 0))

    def test_fbo_takes_prices_from_the_report_map_and_null_when_unknown(self):
        prices = {("A", "11"): D("724.94")}
        rows, c = rules.build_order_rows([posting("A", "delivered"), posting("B", "cancelled", sku=12, price="500")], "fbo", buyer_prices=prices)
        by_sku = {r["marketplace_sku"]: r for r in rows}
        self.assertEqual(by_sku["11"]["orders_amount_buyer"], 724.94)
        self.assertEqual(by_sku["11"]["orders_amount_seller"], 1000.0)
        self.assertIsNone(by_sku["12"]["cancelled_orders_amount_buyer"])           # цены нет — null, не ноль и не цена продавца
        self.assertEqual(by_sku["12"]["cancelled_orders_amount_seller"], 500.0)
        self.assertEqual(by_sku["12"]["orders_amount_buyer"], 0.0)                 # подтверждённых товаров у ключа нет — измеренный ноль
        self.assertEqual((c["buyer_price_known_products"], c["buyer_price_unknown_products"]), (1, 1))

    def test_one_unknown_product_makes_the_whole_key_null(self):
        rows, _c = rules.build_order_rows([posting("A", "delivered", schema="fbs", customer_price=700), posting("B", "delivered", schema="fbs")], "fbs")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["orders_amount_buyer"])
        self.assertEqual(rows[0]["orders_amount_seller"], 2000.0)

    def test_buyer_unit_price_forms(self):
        p = {"posting_number": "A", "financial_data": {"products": [{"product_id": 11, "customer_price": {"amount": "12.5"}}]}}
        self.assertEqual(rules.buyer_unit_price(p, {"sku": 11}), D("12.5"))
        self.assertEqual(rules.buyer_unit_price({"posting_number": "A"}, {"sku": 11}, {("A", "11"): 3}), D(3))
        self.assertIsNone(rules.buyer_unit_price({"posting_number": "A"}, {"sku": 11}, {("A", "12"): 3}))

    def test_seller_amounts_and_other_columns_did_not_move(self):
        rows, _c = rules.build_order_rows([posting("A", "delivered"), posting("B", "cancelled", price="500")], "fbo", observed_at="t")
        r = rows[0]
        self.assertEqual((r["orders_qty"], r["orders_amount_seller"], r["cancelled_orders_qty"], r["cancelled_orders_amount_seller"], r["observed_at"]),
                         (1.0, 1000.0, 1.0, 500.0, "t"))


CSV = ("﻿\"Номер заказа\";\"Номер отправления\";\"SKU\";\"Предельная цена\";\"Оплачено покупателем\";\"Количество\"\n"
       "\"1\";\"A-1\";\"11\";\"1555.00\";\"724.94\";\"1\"\n"
       "\"2\";\"B-1\";\"12\";\"900.00\";\"\";\"1\"\n"
       "\"3\";\"C-1\";\"13\";\"100,00\";\"90,50\";\"2\"\n")


class Resp:
    def __init__(self, status, payload=None, content=b""):
        self.status_code, self._payload, self.content, self.text = status, payload, content, str(payload)

    def json(self):
        return self._payload


class PostingsReport(unittest.TestCase):
    def test_parse_report_reads_bom_semicolons_and_skips_rows_without_price(self):
        prices, skipped = report.parse_report(CSV)
        self.assertEqual(prices, {("A-1", "11"): D("724.94"), ("C-1", "13"): D("90.50")})
        self.assertEqual(skipped, 1)

    def fake_post(self, statuses, bodies):
        infos = iter(statuses)

        def post(path, body):
            bodies.append((path, body))
            if path == "/v1/report/postings/create":
                return Resp(200, {"result": {"code": "REPORT_x"}})
            st = next(infos)
            return Resp(200, {"result": {"status": st, "error": "", "file": "https://ir.ozone.ru/x.csv" if st == "success" else ""}})
        return post

    def test_fetch_polls_every_five_seconds_and_returns_prices_and_stats(self):
        bodies, sleeps = [], []
        since, to = datetime(2026, 8, 24, 21, tzinfo=timezone.utc), datetime(2026, 9, 23, 21, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            prices, stats = report.fetch_buyer_prices("fbo", since, to, sleep_fn=sleeps.append, post=self.fake_post(["waiting", "waiting", "success"], bodies),
                                                      get=lambda url: Resp(200, content=CSV.encode("utf-8")), save_dir=tmp, clock=itertools.count(0, 5).__next__)
            self.assertEqual(len(os.listdir(tmp)), 1)
        self.assertEqual(prices[("A-1", "11")], D("724.94"))
        self.assertEqual((stats["create"], stats["info"], stats["download"], stats["rows"], stats["skipped"]), (1, 3, 1, 3, 1))
        self.assertEqual(sleeps, [report.POLL_SECONDS] * 3)
        self.assertEqual(report.POLL_SECONDS, 5)
        create = bodies[0][1]
        self.assertEqual(create["filter"]["delivery_schema"], ["fbo"])
        self.assertEqual(create["with"], {})                                            # у fbo additional_data → 400
        self.assertEqual(create["filter"]["processed_at_from"], "2026-08-24T21:00:00Z")

    def test_fbs_asks_additional_data(self):
        bodies = []
        with tempfile.TemporaryDirectory() as tmp:
            report.fetch_buyer_prices("fbs", datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 2, tzinfo=timezone.utc), sleep_fn=lambda s: None,
                                      post=self.fake_post(["success"], bodies), get=lambda url: Resp(200, content=CSV.encode("utf-8")), save_dir=tmp,
                                      clock=itertools.count(0, 5).__next__)
        self.assertEqual(bodies[0][1]["with"], {"additional_data": True})

    def test_not_ready_in_three_minutes_is_a_named_refusal(self):
        self.assertEqual(report.MAX_WAIT_SECONDS, 180)
        with self.assertRaises(RuntimeError) as ctx:
            report.fetch_buyer_prices("fbo", datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 2, tzinfo=timezone.utc), sleep_fn=lambda s: None,
                                      post=self.fake_post(["waiting"] * 100, []), get=lambda url: Resp(200), save_dir=tempfile.gettempdir(),
                                      clock=itertools.count(0, 50).__next__)
        self.assertIn("не готов", str(ctx.exception))

    def test_failed_report_and_bad_http_are_named(self):
        with self.assertRaises(RuntimeError) as ctx:
            report.fetch_buyer_prices("fbo", datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 2, tzinfo=timezone.utc), sleep_fn=lambda s: None,
                                      post=self.fake_post(["failed"], []), get=lambda url: Resp(200), save_dir=tempfile.gettempdir(), clock=itertools.count(0, 5).__next__)
        self.assertIn("status=failed", str(ctx.exception))
        with self.assertRaises(RuntimeError) as ctx:
            report.fetch_buyer_prices("fbo", datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 2, tzinfo=timezone.utc), sleep_fn=lambda s: None,
                                      post=lambda path, body: Resp(429, {"message": "rate"}), get=lambda url: Resp(200), save_dir=tempfile.gettempdir(),
                                      clock=itertools.count(0, 5).__next__)
        self.assertIn("HTTP 429", str(ctx.exception))


def order(day, conf_a="1000", canc_a="0", buyer=None, canc_buyer=None, schema="fbo", sku="11", article="F1", canc_q=0):
    return {"order_date": day, "order_schema": schema, "marketplace_sku": sku, "article": article, "orders_qty": 1, "orders_amount_seller": conf_a,
            "cancelled_orders_qty": canc_q, "cancelled_orders_amount_seller": canc_a, "orders_amount_buyer": buyer, "cancelled_orders_amount_buyer": canc_buyer}


class OrdersSheetCoinvest(unittest.TestCase):
    CURVE = {"fbo": {"r_cnt": {a: D("0.2") for a in range(21)}, "r_amt": {a: D("0.1") for a in range(21)}}}

    def build(self, orders, days=("2026-09-20",)):
        blocks, said = fc.build_orders_daily(list(days), orders, self.CURVE, "2026-09-21", {"Основная": D("0.4"), "все": D("0.3")}, D("0.05"), {},
                                             lambda sku: D(300), lambda d: D("1.22"), lambda r: "Основная")
        for rows in blocks.values():
            for r in rows:
                fc.add_order_ratios(r)
        return blocks, said

    def test_share_is_created_minus_buyer_over_created_including_cancelled(self):
        blocks, said = self.build([order("2026-09-20", "1000", buyer=800), order("2026-09-20", "500", "500", buyer=400, canc_buyer=450, canc_q=1, sku="12")])
        r = blocks["all"][0]
        self.assertEqual(r["created_buyer_a"], D(1650))
        self.assertEqual(r["coinvest_pct"], D(350) / D(2000))
        self.assertEqual(blocks["platform:Основная"][0]["coinvest_pct"], r["coinvest_pct"])
        self.assertFalse(any("не измерен" in x for x in said))

    def test_unknown_price_on_one_row_empties_the_day_and_the_total(self):
        blocks, _ = self.build([order("2026-09-20", "1000", buyer=800), order("2026-09-20", "500", buyer=None, sku="12"), order("2026-09-21", "100", buyer=90, sku="13")],
                               days=("2026-09-20", "2026-09-21"))
        self.assertIsNone(blocks["all"][0]["coinvest_pct"])
        self.assertEqual(blocks["all"][1]["coinvest_pct"], D("0.1"))
        self.assertIsNone(fc.orders_total(blocks["all"])["coinvest_pct"])       # пустой день — пустой итог, а не занижение

    def test_day_where_every_row_has_buyer_equal_seller_is_not_zero_but_unmeasured(self):
        blocks, said = self.build([order("2026-09-20", "1000", buyer=1000), order("2026-09-20", "500", buyer=500, sku="12")])
        self.assertIsNone(blocks["all"][0]["coinvest_pct"])
        self.assertTrue(any("2026-09-20" in x and "не измерен" in x for x in said))


class MonthSheetCoinvest(unittest.TestCase):
    def buyout(self, seller, bonus, coinv, sku="1"):
        return {"buyout_date": "2026-09-01", "marketplace_sku": sku, "buyouts_qty": 1, "buyouts_amount_seller": seller, "commission_amount": "400",
                "bonus_amount": bonus, "coinvestment_amount": coinv}

    def test_coinvest_is_bonus_plus_green_prices_and_share_of_turnover(self):
        rows, _u = rep.build_daily(["2026-09-01"], [self.buyout("1000", "100", "50"), self.buyout("1000", "20", "0", sku="2")], [], {}, lambda sku: None, "2026-09-24")
        r = rep.add_ratios(rows[0])
        self.assertEqual((r["coinvest"], r["coinvest_pct"]), (D(170), D(170) / D(2000)))

    def test_row_without_columns_empties_the_day(self):
        rows, _u = rep.build_daily(["2026-09-01"], [self.buyout("1000", "100", "50"), self.buyout("1000", None, None, sku="2")], [], {}, lambda sku: None, "2026-09-24")
        r = rep.add_ratios(rows[0])
        self.assertIsNone(r["coinvest"]); self.assertIsNone(r["coinvest_pct"])
        self.assertIn("coinvest", rep.MONEY)

    def test_check_coinvest_tolerance(self):
        ok, cmp, off, missing = rep.check_coinvest({"d1": D("0.20"), "d2": D("0.25"), "d3": None},
                                                    {"d1": {"coinvest_pct": D("0.205")}, "d2": {"coinvest_pct": D("0.27")}}, ["d1", "d2", "d3"])
        self.assertEqual((ok, cmp, off, missing), (1, 2, [("d2", D("0.25"), D("0.27"))], ["d3"]))


class UpsertOrdersNotNullFallback(unittest.TestCase):
    ERR = ('{"code":"23502","details":"Failing row contains (...)","message":"null value in column \\"cancelled_orders_amount_buyer\\" '
           'of relation \\"marketplace_orders\\" violates not-null constraint"}')

    class FakeSb:
        def __init__(self, fail_times):
            self.calls, self.fail_times = [], fail_times

        def table(self, name):
            return self

        def upsert(self, rows, on_conflict):
            self.calls.append([dict(r) for r in rows]); self.on_conflict = on_conflict; return self

        def execute(self):
            if self.fail_times:
                self.fail_times -= 1
                raise Exception(UpsertOrdersNotNullFallback.ERR)
            return self

    def rows(self):
        return [{"order_date": "2026-09-23", "marketplace_code": "ozon", "marketplace_sku": "1", "order_schema": "fbo", "orders_amount_seller": 10.0,
                 "orders_amount_buyer": 7.0, "cancelled_orders_amount_buyer": 0.0},
                {"order_date": "2026-09-23", "marketplace_code": "ozon", "marketplace_sku": "2", "order_schema": "fbo", "orders_amount_seller": 5.0,
                 "orders_amount_buyer": None, "cancelled_orders_amount_buyer": None}]

    def test_not_null_refusal_writes_orders_without_buyer_columns_and_says_so(self):
        fake = self.FakeSb(fail_times=1)
        with mock.patch("builtins.print") as out:
            rules.upsert_orders(fake, self.rows(), "Ozon FBO")
        self.assertEqual(len(fake.calls), 3)                                                # отказ, целые строки, строки без колонок покупателя
        self.assertEqual(fake.calls[1][0]["marketplace_sku"], "1"); self.assertEqual(fake.calls[1][0]["orders_amount_buyer"], 7.0)
        self.assertEqual(fake.calls[2][0]["marketplace_sku"], "2")
        self.assertNotIn("orders_amount_buyer", fake.calls[2][0]); self.assertNotIn("cancelled_orders_amount_buyer", fake.calls[2][0])
        self.assertEqual(fake.calls[2][0]["orders_amount_seller"], 5.0)
        self.assertEqual(fake.on_conflict, rules.ORDERS_KEY)
        self.assertTrue(any("NOT NULL" in str(c.args[0]) and "1 строк записаны БЕЗ" in str(c.args[0]) for c in out.call_args_list))

    def test_other_errors_go_up(self):
        class Boom(self.FakeSb):
            def execute(self):
                raise Exception('{"code":"57014","message":"canceling statement due to statement timeout"}')
        with self.assertRaises(Exception):
            rules.upsert_orders(Boom(fail_times=0), self.rows(), "Ozon FBS")

    def test_happy_path_is_one_upsert_per_500(self):
        fake = self.FakeSb(fail_times=0)
        rules.upsert_orders(fake, self.rows() * 300, "Ozon FBS")
        self.assertEqual([len(c) for c in fake.calls], [500, 100])

if __name__ == "__main__":
    unittest.main()
