"""Сорок пятая §3 и §5.

§3: отчёт ЛК FBO не пришёл (ночь 09-29: «не готов за 180 с») — строки пишутся без колонок цены покупателя, upsert их не трогает,
у ключа остаётся прежнее измерение, у нового ключа — null; удачный отчёт переписывает как раньше. Итог — для строки алерта.
§5: цена покупателя не в рублях (KZT …) — «не измерено», а не рубли: рублёвого эквивалента в ответе Ozon нет.
Сеть и база не зовутся: модули шага подменены.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import alerts_telegram as alerts  # noqa: E402
import ozon_fbo_orders_step as step  # noqa: E402
from loaders import ozon_orders_rows as rules  # noqa: E402
from loaders import ozon_postings_report as report  # noqa: E402

OBSERVED = "2026-09-30T00:35:00+00:00"


def fbo_posting(number, sku, day="2026-09-28T10:00:00Z", price="1000"):
    return {"posting_number": number, "status": "delivered", "created_at": day, "in_process_at": day,
            "products": [{"sku": sku, "offer_id": "F1", "name": "кольцо", "quantity": 1, "price": {"amount": price, "currency": "RUB"}}],
            "financial_data": {"products": []}}


class FakeFbo:
    def __init__(self, postings):
        self.postings, self.saved, self.supabase = postings, None, object()

    def get_fbo_postings(self, days_back):
        return self.postings

    def build_order_rows(self, postings, buyer_prices=None):
        rows, _ = rules.build_order_rows(postings, "fbo", observed_at=OBSERVED, buyer_prices=buyer_prices)
        return rows

    def save_orders(self, rows):
        self.saved = rows


class FakeLog:
    def dump_raw(self, postings, schema):
        return f"data/postings_raw/{schema}_test.json"


class FakeReport:
    def __init__(self, prices=None, exc=None):
        self.prices, self.exc = prices, exc

    def fetch_buyer_prices(self, scheme, since, to):
        if self.exc:
            raise self.exc
        return self.prices, {"rows": len(self.prices), "skipped": 0, "create": 1, "info": 3, "download": 1, "seconds": 12.3, "foreign_currency": 0}


def run_step(report_obj, existing=None, existing_exc=None):
    fbo = FakeFbo([fbo_posting("p1", "11"), fbo_posting("p2", "22")])
    state, calls = {}, []

    def existing_fn(sb, since):
        calls.append(since)
        if existing_exc:
            raise existing_exc
        return existing or {}

    step.run(fbo_module=fbo, log_module=FakeLog(), report_module=report_obj, existing_fn=existing_fn,
             state_fn=lambda sb, payload: state.update(payload), now=datetime(2026, 9, 30, 0, 33, tzinfo=timezone.utc))
    return fbo.saved, state, calls


class KeepMeasuredBuyerTests(unittest.TestCase):
    def test_failed_report_keeps_measured_buyer_and_counts(self):
        saved, state, calls = run_step(FakeReport(exc=RuntimeError("report/info fbo: отчёт не готов за 420 с (status=waiting)")),
                                       existing={("2026-09-28", "11"): True})
        self.assertEqual(len(saved), 2)
        for row in saved:
            for col in rules.BUYER_COLUMNS:
                self.assertNotIn(col, row)                         # upsert не трогает — прежнее измерение остаётся
            self.assertIn("orders_amount_seller", row)             # остальные поля пишутся как обычно
        self.assertEqual(calls, ["2026-09-28"])                    # прежние ключи — с начала окна строк
        self.assertFalse(state["ok"])
        self.assertIn("не готов", state["error"])
        self.assertEqual((state["rows"], state["rows_kept_measured"], state["rows_without_price"], state["rows_new_keys"]), (2, 1, 1, 1))

    def test_new_keys_on_failure_stay_without_price(self):
        saved, state, _ = run_step(FakeReport(exc=RuntimeError("HTTP 500")), existing={})
        self.assertEqual((state["rows_kept_measured"], state["rows_without_price"], state["rows_new_keys"]), (0, 2, 2))
        self.assertTrue(all(col not in row for row in saved for col in rules.BUYER_COLUMNS))   # колонка без default → null в базе

    def test_successful_report_overwrites_as_before(self):
        saved, state, calls = run_step(FakeReport(prices={("p1", "11"): D("600")}))
        by = {r["marketplace_sku"]: r for r in saved}
        self.assertEqual(by["11"]["orders_amount_buyer"], 600.0)
        self.assertIsNone(by["22"]["orders_amount_buyer"])         # отчёт пришёл, но цены нет — «не измерено», как было
        self.assertEqual(calls, [])                                # прежние ключи не читаются
        self.assertTrue(state["ok"])
        self.assertEqual((state["seconds"], state["rows"]), (12.3, 2))

    def test_unread_existing_keys_still_do_not_overwrite(self):
        saved, state, _ = run_step(FakeReport(exc=RuntimeError("HTTP 500")), existing_exc=RuntimeError("timeout"))
        self.assertTrue(all(col not in row for row in saved for col in rules.BUYER_COLUMNS))
        self.assertIn("timeout", state["count_error"])
        self.assertNotIn("rows_kept_measured", state)

    def test_wait_is_longer_but_capped(self):
        self.assertEqual(report.MAX_WAIT_SECONDS, 420)
        self.assertLessEqual(report.MAX_WAIT_SECONDS, 600)


class AlertLineTests(unittest.TestCase):
    NOW = datetime(2026, 9, 30, 7, 30, tzinfo=timezone.utc)

    def payload(self, **kw):
        p = {"ok": False, "error": "RuntimeError: report/info fbo: отчёт не готов за 420 с (status=waiting)", "rows": 3904,
             "rows_kept_measured": 3790, "rows_without_price": 114, "rows_new_keys": 114, "finished_at": "2026-09-30T00:38:00+00:00"}
        p.update(kw)
        return p

    def test_line_only_on_fresh_failure(self):
        line = alerts.fbo_buyer_line(self.payload(), now=self.NOW)
        self.assertIn("отчёт ЛК не получен", line)
        self.assertIn("у 3790 осталось прежнее измерение, 114 без цены", line)
        self.assertEqual(alerts.fbo_buyer_line(None, now=self.NOW), "")
        self.assertEqual(alerts.fbo_buyer_line(self.payload(ok=True, error=None), now=self.NOW), "")
        stale = self.payload(finished_at=(self.NOW - timedelta(hours=30)).isoformat())
        self.assertEqual(alerts.fbo_buyer_line(stale, now=self.NOW), "")

    def test_line_without_counts_when_keys_unread(self):
        p = self.payload()
        for k in ("rows_kept_measured", "rows_without_price", "rows_new_keys"):
            p.pop(k)
        self.assertEqual(alerts.fbo_buyer_line(p, now=self.NOW).count("строк окна"), 0)


def fbs_posting(customer_price, currency, sku="33"):
    day = "2026-09-28T10:00:00Z"
    return {"posting_number": "f1", "status": "delivered", "in_process_at": day,
            "products": [{"sku": sku, "offer_id": "F1", "name": "кольцо", "quantity": 2, "price": {"amount": "1000", "currency": "RUB"}}],
            "financial_data": {"products": [{"product_id": sku, "customer_price": {"amount": customer_price, "currency": currency}}]}}


class ForeignCurrencyTests(unittest.TestCase):
    def test_fbs_customer_price_in_kzt_is_unmeasured(self):
        rows, counters = rules.build_order_rows([fbs_posting("50000", "KZT")], "fbs", observed_at=OBSERVED)
        self.assertIsNone(rows[0]["orders_amount_buyer"])          # не 100 000 «рублей»
        self.assertEqual(rows[0]["orders_amount_seller"], 2000.0)  # цена продавца в рублях — как была
        self.assertEqual(counters["buyer_price_foreign_currency"], 1)

    def test_fbs_customer_price_in_rub_is_summed(self):
        rows, counters = rules.build_order_rows([fbs_posting("600", "RUB")], "fbs", observed_at=OBSERVED)
        self.assertEqual(rows[0]["orders_amount_buyer"], 1200.0)
        self.assertNotIn("buyer_price_foreign_currency", counters)
        self.assertFalse(rules.foreign_currency(""))
        self.assertTrue(rules.foreign_currency("BYN"))

    def test_cabinet_csv_row_in_kzt_is_skipped(self):
        text = "﻿Номер отправления;SKU;Оплачено покупателем;Код валюты покупателя\np1;11;600;RUB\np2;22;50000;KZT\n"
        stats = {}
        prices, skipped = report.parse_report(text, stats)
        self.assertEqual(prices, {("p1", "11"): D("600")})
        self.assertEqual((skipped, stats["foreign_currency"]), (1, 1))
        self.assertEqual(report.parse_report(text)[0], prices)   # без stats — то же самое (backfill зовёт так)


if __name__ == "__main__":
    unittest.main()
