"""Выкупы WB из отчёта реализации: то же зерно (день продажи МСК, nmId), знак возврата, план
add / rewrite / same / delete, день без единой продажи в отчёте не трогается, запись только с
одобрением и не в ночном окне.
"""
import unittest
from decimal import Decimal
from unittest import mock

import scripts.wb_buyouts_rebuild_from_report as rb

D = Decimal


def sale(day_ts, sku, price, amount, oper="Продажа", qty=1, vendor="f000283615", rrd=1):
    return {"rrd_id": rrd, "rr_date": day_ts[:10], "sale_dt": day_ts, "nm_id": sku, "vendor_code": vendor, "seller_oper_name": oper,
            "quantity": qty, "retail_price_with_disc": str(price), "retail_amount": str(amount), "day": rb.sale_day(day_ts)}


def db_row(day, sku, qty, seller, buyer, rid=1, article="F000283615", name="Кольца"):
    return {"id": rid, "buyout_date": day, "marketplace_sku": sku, "article": article, "product_name": name,
            "buyouts_qty": qty, "buyouts_amount_seller": seller, "buyouts_amount_buyer": buyer, "buyouts_units": None}


class DayTests(unittest.TestCase):
    def test_sale_day_is_moscow_time(self):
        self.assertEqual(rb.sale_day("2026-09-09T21:30:00Z"), "2026-09-10")
        self.assertEqual(rb.sale_day("2026-09-09T16:50:53+00:00"), "2026-09-09")


class BuildTests(unittest.TestCase):
    def test_rows_are_summed_by_day_and_sku_with_the_return_sign(self):
        sales = [sale("2026-09-14T07:00:00Z", "1", 86208, 53448, rrd=1), sale("2026-09-14T09:00:00Z", "1", 10000, 6000, rrd=2),
                 sale("2026-09-14T12:00:00Z", "1", 10000, 6000, oper="Возврат", rrd=3), sale("2026-09-14T12:00:00Z", "2", 500, 400, rrd=4)]
        rows = rb.build_rows(sales, {})
        r = rows[("2026-09-14", "1")]
        self.assertEqual((r["buyouts_qty"], r["buyouts_amount_seller"], r["buyouts_amount_buyer"]), (1.0, 86208.0, 53448.0))
        self.assertEqual(r["article"], "F000283615")            # новый ключ: vendorCode прописными
        self.assertIsNone(r["product_name"])
        self.assertEqual((r["revenue_after_commission_vat"], r["commission_amount"], r["vat_amount"]), (0, 0, 0))
        self.assertNotIn("buyouts_units", r)
        self.assertEqual(rows[("2026-09-14", "2")]["buyouts_qty"], 1.0)

    def test_existing_key_keeps_its_article_and_product_name(self):
        db = {("2026-09-14", "1"): db_row("2026-09-14", "1", 1, 86208, 53448)}
        rows = rb.build_rows([sale("2026-09-14T07:00:00Z", "1", 86208, 53448)], db)
        self.assertEqual((rows[("2026-09-14", "1")]["article"], rows[("2026-09-14", "1")]["product_name"]), ("F000283615", "Кольца"))

    def test_a_report_row_without_sale_dt_refuses(self):
        bad = {"rrd_id": 5, "rr_date": "2026-09-14", "sale_dt": None, "nm_id": "1", "vendor_code": "f", "seller_oper_name": "Продажа",
               "quantity": 1, "retail_price_with_disc": "1", "retail_amount": "1"}
        with mock.patch.object(rb.stale_keys, "read_window_rows", return_value=[bad]):
            with self.assertRaises(RuntimeError):
                rb.read_report_sales(object(), "2026-09-14", "2026-09-14")


class ClassifyTests(unittest.TestCase):
    def test_add_rewrite_same_delete_and_untouched(self):
        sales = [sale("2026-09-14T07:00:00Z", "1", 86208, 53448, rrd=1), sale("2026-09-14T08:00:00Z", "2", 500, 400, rrd=2),
                 sale("2026-09-15T08:00:00Z", "3", 700, 600, rrd=3)]
        db = {("2026-09-14", "1"): db_row("2026-09-14", "1", 1, 86208, 53448, rid=1),      # same
              ("2026-09-14", "2"): db_row("2026-09-14", "2", 1, 999, 400, rid=2),          # rewrite
              ("2026-09-14", "9"): db_row("2026-09-14", "9", 1, 100, 90, rid=3),           # delete: день с продажами, ключа в отчёте нет
              ("2026-09-16", "5"): db_row("2026-09-16", "5", 1, 100, 90, rid=4)}           # untouched: в отчёте за 09-16 продаж нет
        rows = rb.build_rows(sales, db)
        cls = rb.classify(rows, db, {"2026-09-14", "2026-09-15"})
        self.assertEqual([k for k, _r, _o in cls["add"]], [("2026-09-15", "3")])
        self.assertEqual([k for k, _r, _o in cls["rewrite"]], [("2026-09-14", "2")])
        self.assertEqual([k for k, _r, _o in cls["same"]], [("2026-09-14", "1")])
        self.assertEqual([k for k, _r, _o in cls["delete"]], [("2026-09-14", "9")])
        self.assertEqual([k for k, _r, _o in cls["untouched"]], [("2026-09-16", "5")])
        by = rb.month_table(cls)["2026-09"]
        self.assertEqual((by["add_n"], by["rewrite_n"], by["same_n"], by["delete_n"], by["untouched_n"]), (1, 1, 1, 1, 1))
        self.assertEqual(by["before_amt"], D("86208") + D("999") + D("100"))   # untouched в «до» не входит
        self.assertEqual(by["after_amt"], D("86208") + D("500") + D("700"))


class FakeSb:
    def __init__(self):
        self.upserts, self.deletes = [], []

    def table(self, name):
        sb = self

        class T:
            def upsert(self, rows, on_conflict=None):
                sb.upserts.append((name, list(rows), on_conflict)); return self

            def delete(self):
                return self

            def in_(self, col, ids):
                sb.deletes.append((name, col, list(ids))); return self

            def eq(self, *_a):
                return self

            def execute(self):
                return None
        return T()


class ApplyTests(unittest.TestCase):
    def test_apply_refuses_inside_the_nightly_window(self):
        with mock.patch.object(rb, "in_nightly_run_window", return_value=True):
            with self.assertRaises(SystemExit):
                rb.apply(FakeSb(), {"add": [], "rewrite": [], "same": [], "delete": [], "untouched": []}, [], "t")

    def test_apply_upserts_then_deletes_listed_ids_after_a_snapshot(self):
        sb = FakeSb()
        row = {"buyout_date": "2026-09-14", "marketplace_code": "wb", "marketplace_sku": "2", "buyouts_qty": 1.0, "buyouts_amount_seller": 500.0, "buyouts_amount_buyer": 400.0}
        old = db_row("2026-09-14", "9", 1, 100, 90, rid=77)
        cls = {"add": [(("2026-09-14", "2"), row, None)], "rewrite": [], "same": [], "delete": [(("2026-09-14", "9"), None, old)], "untouched": []}
        with mock.patch.object(rb, "in_nightly_run_window", return_value=False), mock.patch.object(rb, "snapshot", return_value="snap") as snap:
            written, deleted = rb.apply(sb, cls, [old], "t")
        snap.assert_called_once()
        self.assertEqual((written, deleted), (1, 1))
        self.assertEqual(sb.upserts[0][2], "buyout_date,marketplace_code,marketplace_sku")
        self.assertEqual(sb.deletes, [("marketplace_buyouts", "id", [77])])

    def test_main_without_approval_writes_nothing(self):
        sb = FakeSb()
        with mock.patch.object(rb.loader, "_client", return_value=sb), \
             mock.patch.object(rb, "read_report_sales", return_value=[sale("2026-09-14T07:00:00Z", "1", 86208, 53448)]), \
             mock.patch.object(rb, "read_db", return_value=[]):
            code = rb.main(["--apply", "--date-from", "2026-09-14", "--date-to", "2026-09-14"])
        self.assertEqual(code, 2)
        self.assertEqual((sb.upserts, sb.deletes), ([], []))


if __name__ == "__main__":
    unittest.main()
