"""Отчёт реализации WB → wb_sales_report_rows: пагинация, пейсинг, строки, защиты.

Держим: страницы по rrdId с паузой под лимит 1/мин; 429 и таймаут повторяются
ограниченно и кончаются именованной ошибкой; деньги идут строкой без float;
обязательное поле без значения — отказ; дубли rrdId и чужие даты — отказ;
dry-run ничего не пишет; чистка застрявших только при полном сборе.
"""
import json
import unittest
from datetime import date
from decimal import Decimal
from unittest import mock

import requests

import loaders.wb_sales_report_loader as loader

OBSERVED = "2026-09-23T10:00:00+00:00"


def item(rrd, day="2026-09-21", oper="Продажа", **extra):
    base = {"rrdId": rrd, "reportId": 6338020260921, "dateFrom": day, "dateTo": day, "createDate": "2026-09-22",
            "rrDate": day, "saleDt": f"{day}T10:00:00Z", "orderDt": f"{day}T09:00:00Z", "nmId": 218330479,
            "vendorCode": "f000283615", "techSize": "17,5", "srid": f"s{rrd}", "shkId": 1, "docTypeName": "Продажа" if oper == "Продажа" else "",
            "sellerOperName": oper, "bonusTypeName": "", "officeName": "Склад WB", "quantity": 1,
            "retailPriceWithDisc": "1255921", "retailAmount": "716456", "forPay": "696945.97", "ppvzReward": "18115.294",
            "acquiringFee": "31488.21", "vw": Decimal("-24666.7819672131147540"), "deliveryService": "", "paidStorage": None}
    base.update(extra)
    return base


def response(status, items=None):
    body = json.dumps(items or [], default=str).encode("utf-8")
    return mock.Mock(status_code=status, content=body, text=body.decode("utf-8"))


class BuildRowTests(unittest.TestCase):
    def test_money_is_kept_as_decimal_text_not_float(self):
        row = loader.build_row(item(1), OBSERVED)
        self.assertEqual(row["retail_price_with_disc"], "1255921")
        self.assertEqual(row["for_pay"], "696945.97")
        self.assertEqual(row["ppvz_reward"], "18115.294")
        self.assertEqual(row["vw"], "-24666.7819672131147540")
        self.assertIsNone(row["delivery_service"])
        self.assertIsNone(row["paid_storage"])
        self.assertEqual((row["rrd_id"], row["rr_date"], row["report_period"], row["observed_at"]), (1, "2026-09-21", "daily", OBSERVED))
        self.assertEqual(row["tech_size"], "17,5")

    def test_missing_required_field_is_a_failure(self):
        with self.assertRaises(RuntimeError):
            loader.build_row(item(1, rrDate=""), OBSERVED)

    def test_non_numeric_money_is_a_failure(self):
        with self.assertRaises(RuntimeError):
            loader.build_row(item(1, forPay="abc"), OBSERVED)


class FetchTests(unittest.TestCase):
    def test_pages_follow_rrd_id_with_a_pause_and_stop_on_a_short_page(self):
        pages = [response(200, [item(1), item(2)]), response(200, [item(3)])]
        counters = {"requests": 0, "429": 0, "transient": 0}
        sleeps = []
        with mock.patch.object(loader.requests, "post", side_effect=pages) as post:
            items = loader.fetch_period("2026-09-21", "2026-09-21", counters, sleep_fn=sleeps.append, page_limit=2)
        self.assertEqual([x["rrdId"] for x in items], [1, 2, 3])
        self.assertEqual(post.call_args_list[1].kwargs["json"]["rrdId"], 2)
        self.assertEqual(post.call_args_list[0].kwargs["json"]["period"], "daily")
        self.assertEqual(sleeps, [loader.SLEEP_SECONDS])
        self.assertEqual(counters["requests"], 2)

    def test_204_ends_the_period_with_what_was_collected(self):
        pages = [response(200, [item(1), item(2)]), response(204)]
        counters = {"requests": 0, "429": 0, "transient": 0}
        with mock.patch.object(loader.requests, "post", side_effect=pages):
            items = loader.fetch_period("2026-09-21", "2026-09-21", counters, sleep_fn=lambda s: None, page_limit=2)
        self.assertEqual(len(items), 2)

    def test_429_is_retried_then_named(self):
        counters = {"requests": 0, "429": 0, "transient": 0}
        with mock.patch.object(loader.requests, "post", return_value=response(429)):
            with self.assertRaises(RuntimeError) as caught:
                loader.request_page({"dateFrom": "x"}, counters, sleep_fn=lambda s: None)
        self.assertIn("429", str(caught.exception))
        self.assertEqual(counters["429"], loader.MAX_ATTEMPTS)

    def test_timeout_is_retried_then_named(self):
        counters = {"requests": 0, "429": 0, "transient": 0}
        with mock.patch.object(loader.requests, "post", side_effect=requests.exceptions.ReadTimeout("x")):
            with self.assertRaises(RuntimeError) as caught:
                loader.request_page({"dateFrom": "x"}, counters, sleep_fn=lambda s: None)
        self.assertIn("сетевой отказ", str(caught.exception))
        self.assertEqual(counters["transient"], loader.MAX_ATTEMPTS)

    def test_duplicate_rrd_ids_and_foreign_dates_are_refused(self):
        with self.assertRaises(RuntimeError):
            loader.check_items([item(1), item(1)], "2026-09-21", "2026-09-21")
        with self.assertRaises(RuntimeError):
            loader.check_items([item(1, day="2026-09-30")], "2026-09-21", "2026-09-21")


class RunTests(unittest.TestCase):
    def fake_sb(self):
        sb = mock.MagicMock()
        query = mock.MagicMock()
        for name in ("select", "gte", "lte", "order", "eq"):
            getattr(query, name).return_value = query
        query.range.return_value = query
        query.execute.return_value = mock.Mock(data=[])
        sb.table.return_value = query
        return sb, query

    def test_dry_run_makes_no_writes(self):
        sb, query = self.fake_sb()
        with mock.patch.object(loader, "fetch_period", return_value=[item(1), item(2, oper="Возврат")]):
            result = loader.run(sb, days_back=2, today=date(2026, 9, 23), dry_run=True, sleep_fn=lambda s: None)
        query.upsert.assert_not_called()
        query.delete.assert_not_called()
        self.assertEqual((result["rows"], result["written"]), (2, 0))

    def test_window_ends_yesterday_and_rows_are_upserted_by_rrd_id(self):
        sb, query = self.fake_sb()
        captured = {}

        def fake_fetch(date_from, date_to, counters, **kw):
            captured.update(date_from=date_from, date_to=date_to)
            return [item(1, day=date_to)]

        with mock.patch.object(loader, "fetch_period", side_effect=fake_fetch):
            result = loader.run(sb, days_back=21, today=date(2026, 9, 23), sleep_fn=lambda s: None)
        self.assertEqual((captured["date_from"], captured["date_to"]), ("2026-09-02", "2026-09-22"))
        self.assertEqual(query.upsert.call_args.kwargs["on_conflict"], "rrd_id")
        self.assertEqual(result["written"], 1)

    def test_stale_rows_are_deleted_only_when_the_fetch_is_complete(self):
        sb, query = self.fake_sb()
        # в таблице лежит строка окна, которой в свежем ответе нет
        query.execute.return_value = mock.Mock(data=[{"rrd_id": 99, "rr_date": "2026-09-21", "seller_oper_name": "Продажа", "retail_price_with_disc": "1"}])
        with mock.patch.object(loader, "fetch_period", return_value=[item(1)]), \
             mock.patch.object(loader, "_delete_by_rrd_id", return_value=1) as delete:
            result = loader.run(sb, days_back=2, today=date(2026, 9, 23), sleep_fn=lambda s: None)
        delete.assert_called_once()
        self.assertEqual(delete.call_args.args[1][0]["rrd_id"], 99)
        self.assertEqual(result["deleted"], 1)

    def test_a_failed_fetch_neither_writes_nor_cleans(self):
        sb, query = self.fake_sb()
        with mock.patch.object(loader, "fetch_period", side_effect=RuntimeError("WB sales report: 429 не изжит")):
            with self.assertRaises(RuntimeError):
                loader.run(sb, days_back=2, today=date(2026, 9, 23), sleep_fn=lambda s: None)
        query.upsert.assert_not_called()
        query.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class SameNightSkipTests(unittest.TestCase):
    """Один сбор за ночь: шаг продаж собрал окно, шаг отчёта видит свежие строки за вчера и в API не ходит; --force — ходит."""

    class Sb:
        def __init__(self, observed_at):
            self.observed_at = observed_at

        def table(self, _name):
            sb = self

            class T:
                def select(self, *_a):
                    return self

                def eq(self, *_a):
                    return self

                def order(self, *_a, **_k):
                    return self

                def limit(self, *_a):
                    return self

                def execute(self):
                    return mock.Mock(data=[{"observed_at": sb.observed_at}] if sb.observed_at else [])
            return T()

    def test_fresh_collection_for_yesterday_skips_the_api(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 9, 25, 0, 20, 0, tzinfo=timezone.utc)
        sb = self.Sb("2026-09-25T00:19:20+00:00")
        self.assertIsNotNone(loader.collected_recently(sb, "2026-09-24", now))
        self.assertIsNone(loader.collected_recently(self.Sb("2026-09-24T00:19:20+00:00"), "2026-09-24", now))   # сутки назад — не эта ночь
        self.assertIsNone(loader.collected_recently(self.Sb(None), "2026-09-24", now))
        # run() берёт настоящее «сейчас» — сбор кладём на минуту назад, иначе тест стареет вместе с календарём (упал 09-25)
        fresh = self.Sb((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
        with mock.patch.object(loader.requests, "post") as post:
            result = loader.run(fresh, days_back=21, today=date(2026, 9, 25), sleep_fn=lambda _s: None)
        post.assert_not_called()
        self.assertTrue(result["skipped"])
        self.assertEqual((result["requests"], result["written"], result["window"]), (0, 0, ("2026-09-04", "2026-09-24")))

    def test_force_goes_to_the_api_anyway(self):
        sb = self.Sb("2026-09-25T00:19:20+00:00")
        with mock.patch.object(loader.requests, "post", return_value=response(204)) as post, \
             mock.patch.object(loader, "cleanup_stale", return_value=0):
            result = loader.run(sb, days_back=21, today=date(2026, 9, 25), dry_run=True, sleep_fn=lambda _s: None, force=True)
        post.assert_called_once()
        self.assertFalse(result["skipped"])
