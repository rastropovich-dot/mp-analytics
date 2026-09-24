"""Воронка WB по товарам: строки таблицы, окно 14 дней, пейсинг, сводка дня из того же прохода, застрявшие карточки.

Держим: строка (день, nmId) со всеми полями карточки и статистики; карточка без nmId — не строка; окно —
days_back дней до вчера, между любыми запросами PAGE_SLEEP_SECONDS; сводка дня пишется, потом строки по
товарам, потом чистка застрявших (только при полном дне); повтор nmId между страницами — день неполный;
dry-run не пишет; нет таблицы по товарам — сводки записаны, шаг падает с перечнем дней.
"""
import io
import unittest
from contextlib import redirect_stdout
from datetime import date
from unittest import mock

import loaders.wb_sales_funnel_orders_loader as funnel


def card(nm, vendor="F000310094", orders=1, order_sum=1000, buyouts=0, buyout_sum=0, **extra):
    c = {"product": {"nmId": nm, "title": "Кольцо", "vendorCode": vendor, "brandName": "KARATOV", "subjectId": 54, "subjectName": "Ювелирные кольца",
                     "productRating": 10, "feedbackRating": 4.7, "stocks": {"wb": 508, "mp": 0, "balanceSum": 7920139}},
         "statistic": {"selected": {"openCount": 291, "cartCount": 38, "orderCount": orders, "orderSum": order_sum, "buyoutCount": buyouts,
                                    "buyoutSum": buyout_sum, "cancelCount": 1, "cancelSum": 24039, "avgPrice": 23875, "addToWishlist": 5}}}
    c.update(extra)
    return c


def page(products, status=200):
    resp = mock.Mock(status_code=status)
    resp.json.return_value = {"data": {"products": products}}
    return resp


class FakeSb:
    def __init__(self, existing=None, fail_products=False):
        self.upserts, self.deletes, self.existing, self.fail_products = [], [], existing or [], fail_products

    def table(self, name):
        sb = self

        class T:
            def __init__(self):
                self._delete = None

            def upsert(self, rows, on_conflict=None):
                if name == funnel.TABLE and sb.fail_products:
                    raise RuntimeError("relation \"wb_funnel_products_daily\" does not exist")
                sb.upserts.append((name, rows if isinstance(rows, dict) else list(rows), on_conflict)); return self

            def select(self, *_a):
                return self

            def eq(self, col, value):
                if self._delete is not None:
                    self._delete["eq"] = (col, value)
                return self

            def in_(self, col, values):
                self._delete["in"] = (col, list(values)); return self

            def order(self, *_a, **_k):
                return self

            def range(self, *_a):
                return self

            def delete(self):
                self._delete = {"table": name}; return self

            def execute(self):
                if self._delete is not None:
                    sb.deletes.append(self._delete); return mock.Mock(data=[{"x": 1}] * len(self._delete["in"][1]))
                return mock.Mock(data=list(sb.existing) if name == funnel.TABLE else [])
        return T()


class RowTests(unittest.TestCase):
    def test_product_row_has_the_key_and_fields(self):
        row = funnel.build_product_row("2026-09-01", card(17641481, orders=10, order_sum=238753, buyouts=3, buyout_sum=70531), "obs")
        self.assertEqual((row["day"], row["nm_id"], row["vendor_code"], row["brand"], row["subject_id"]), ("2026-09-01", 17641481, "F000310094", "KARATOV", 54))
        self.assertEqual((row["order_count"], row["order_sum"], row["buyout_count"], row["buyout_sum"], row["cancel_sum"]), (10, "238753", 3, "70531", "24039"))
        self.assertEqual((row["stocks_wb"], row["balance_sum"], row["feedback_rating"], row["observed_at"]), (508, "7920139", "4.7", "obs"))

    def test_card_without_nm_id_is_not_a_row(self):
        self.assertIsNone(funnel.build_product_row("2026-09-01", {"statistic": {"selected": {"orderCount": 2, "orderSum": 100}}}, "obs"))


class RunTests(unittest.TestCase):
    def test_window_pacing_summary_and_product_rows_in_one_pass(self):
        answers = [page([card(1, orders=2, order_sum=200)]), page([card(1, orders=3, order_sum=300), card(2, vendor="t000000001", orders=1, order_sum=50)])]
        sb = FakeSb()
        sleeps = []
        with mock.patch.object(funnel.requests, "post", side_effect=answers) as post, redirect_stdout(io.StringIO()):
            result = funnel.run(days_back=2, today=date(2026, 9, 25), sb=sb, sleep_fn=sleeps.append)
        self.assertEqual(post.call_count, 2)
        days = [c.kwargs["json"]["selectedPeriod"]["start"] for c in post.call_args_list]
        self.assertEqual(days, ["2026-09-23", "2026-09-24"])
        self.assertEqual(sleeps, [funnel.PAGE_SLEEP_SECONDS])                      # между днями; страниц по одной
        summaries = [r for t, r, _c in sb.upserts if t == funnel.SUMMARY_TABLE]
        self.assertEqual([(s["order_date"], s["orders_qty"], s["orders_amount"], s["source"]) for s in summaries],
                         [("2026-09-23", 2.0, 200.0, "wb_sales_funnel"), ("2026-09-24", 4.0, 350.0, "wb_sales_funnel")])
        products = [(t, rows, c) for t, rows, c in sb.upserts if t == funnel.TABLE]
        self.assertEqual([c for _t, _r, c in products], ["day,nm_id", "day,nm_id"])
        self.assertEqual([(r["day"], r["nm_id"]) for _t, rows, _c in products for r in rows], [("2026-09-23", 1), ("2026-09-24", 1), ("2026-09-24", 2)])
        self.assertEqual((result["days"], result["cards"], result["written"], result["requests"]), (2, 3, 3, 2))

    def test_second_page_is_fetched_after_a_pause_and_duplicate_nm_id_makes_the_day_incomplete(self):
        first = [card(i) for i in range(1, funnel.PAGE_LIMIT + 1)]
        second = [card(funnel.PAGE_LIMIT), card(funnel.PAGE_LIMIT + 5)]        # nmId 1000 повторился на второй странице
        sleeps = []
        with mock.patch.object(funnel.requests, "post", side_effect=[page(first), page(second)]) as post, redirect_stdout(io.StringIO()):
            result = funnel.fetch_wb_sales_funnel_day("2026-09-24", sleep_fn=sleeps.append)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[1].kwargs["json"]["offset"], funnel.PAGE_LIMIT)
        self.assertEqual(sleeps, [funnel.PAGE_SLEEP_SECONDS])
        self.assertEqual((result["pages"], result["products_count"], result["dup_nm_ids"], result["complete"]), (2, funnel.PAGE_LIMIT + 2, 1, False))

    def test_stale_card_is_deleted_only_on_a_complete_day(self):
        existing = [{"day": "2026-09-24", "nm_id": 7, "vendor_code": "F1", "order_count": 1, "order_sum": 10},
                    {"day": "2026-09-24", "nm_id": 1, "vendor_code": "F1", "order_count": 1, "order_sum": 10}]
        rows = [funnel.build_product_row("2026-09-24", card(1), "obs")]
        sb = FakeSb(existing=existing)
        with redirect_stdout(io.StringIO()):
            written, deleted = funnel.save_products(sb, "2026-09-24", rows, complete=True, apply=True)
        self.assertEqual((written, deleted), (1, 1))
        self.assertEqual(sb.deletes[0]["in"], ("nm_id", [7]))
        self.assertEqual(sb.deletes[0]["eq"], ("day", "2026-09-24"))
        sb2 = FakeSb(existing=existing)
        with redirect_stdout(io.StringIO()):
            written, deleted = funnel.save_products(sb2, "2026-09-24", rows, complete=False, apply=True)
        self.assertEqual((written, deleted, sb2.deletes), (1, 0, []))

    def test_dry_run_writes_nothing(self):
        sb = FakeSb()
        with mock.patch.object(funnel.requests, "post", return_value=page([card(1)])), redirect_stdout(io.StringIO()):
            result = funnel.run(days_back=1, today=date(2026, 9, 25), sb=sb, sleep_fn=lambda _s: None, dry_run=True)
        self.assertEqual((sb.upserts, sb.deletes, result["written"]), ([], [], 0))

    def test_missing_products_table_keeps_summaries_and_fails_at_the_end(self):
        sb = FakeSb(fail_products=True)
        with mock.patch.object(funnel.requests, "post", return_value=page([card(1)])), redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as caught:
                funnel.run(days_back=2, today=date(2026, 9, 25), sb=sb, sleep_fn=lambda _s: None)
        self.assertEqual([t for t, _r, _c in sb.upserts], [funnel.SUMMARY_TABLE, funnel.SUMMARY_TABLE])
        self.assertIn("2026-09-23", str(caught.exception)); self.assertIn("2026-09-24", str(caught.exception))

    def test_default_window_is_14_days_and_env_override_still_works(self):
        self.assertEqual(funnel.DEFAULT_DAYS_BACK, 14)
        with mock.patch.dict(funnel.os.environ, {"WB_SALES_FUNNEL_DAYS_BACK": "4"}):
            self.assertEqual(funnel.get_days_back(), 4)


if __name__ == "__main__":
    unittest.main()
