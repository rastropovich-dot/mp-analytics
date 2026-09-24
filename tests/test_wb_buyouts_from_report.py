"""Ночной путь выкупов WB из отчёта реализации (loaders/wb_buyouts_from_report.run).

Держим: окно days_back дней до вчера по дате продажи МСК; add + rewrite пишутся, same — нет; ключ окна,
которого в отчёте нет, снимается только при полном сборе и только в дне, где отчёт дал хоть одну
продажу; dry-run ничего не пишет; пустое окно — не пишет и не чистит.
"""
import unittest
from datetime import date
from unittest import mock

import loaders.wb_buyouts_from_report as bfr


def sale(day_ts, sku, price, amount, oper="Продажа", qty=1, rrd=1):
    return {"rrd_id": rrd, "rr_date": day_ts[:10], "sale_dt": day_ts, "nm_id": sku, "vendor_code": "f000283615", "seller_oper_name": oper,
            "quantity": qty, "retail_price_with_disc": str(price), "retail_amount": str(amount), "day": bfr.sale_day(day_ts)}


def db_row(day, sku, qty, seller, buyer, rid):
    return {"id": rid, "buyout_date": day, "marketplace_sku": sku, "article": "F000283615", "product_name": "Кольца",
            "buyouts_qty": qty, "buyouts_amount_seller": seller, "buyouts_amount_buyer": buyer, "buyouts_units": None}


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
                return mock.Mock(data=[{"id": i} for i in (sb.deletes[-1][2] if sb.deletes else [])])
        return T()


TODAY = date(2026, 9, 25)   # окно 09-04 … 09-24


class RunTests(unittest.TestCase):
    def run_with(self, sales, db_rows, dry_run=False, complete=True):
        sb = FakeSb()
        with mock.patch.object(bfr, "read_report_sales", return_value=sales) as rs, \
             mock.patch.object(bfr, "read_db", return_value=db_rows) as rd:
            result = bfr.run(sb, days_back=21, today=TODAY, dry_run=dry_run, complete=complete)
        self.assertEqual(rs.call_args[0][1:], ("2026-09-04", "2026-09-24"))
        self.assertEqual(rd.call_args[0][1:], ("2026-09-04", "2026-09-24"))
        return sb, result

    def test_add_and_rewrite_are_written_same_is_not_and_stale_key_is_deleted(self):
        sales = [sale("2026-09-24T07:00:00Z", "1", 86208, 53448, rrd=1),           # add
                 sale("2026-09-20T07:00:00Z", "2", 500, 400, rrd=2),               # rewrite (в базе 999)
                 sale("2026-09-20T08:00:00Z", "3", 700, 600, rrd=3)]               # same
        db = [db_row("2026-09-20", "2", 1, 999, 400, rid=2), db_row("2026-09-20", "3", 1, 700, 600, rid=3),
              db_row("2026-09-20", "9", 1, 100, 90, rid=9),                        # застрял: день с продажами, ключа в отчёте нет
              db_row("2026-09-10", "5", 1, 100, 90, rid=5)]                        # день без продаж в отчёте — не трогать
        sb, result = self.run_with(sales, db)
        written = [r for _t, rows, _c in sb.upserts for r in rows]
        self.assertEqual(sorted((r["buyout_date"], r["marketplace_sku"]) for r in written), [("2026-09-20", "2"), ("2026-09-24", "1")])
        self.assertEqual(sb.upserts[0][2], "buyout_date,marketplace_code,marketplace_sku")
        self.assertEqual(sb.deletes, [("marketplace_buyouts", "id", [9])])
        self.assertEqual((result["written"], result["deleted"], result["rows"], result["source"]), (2, 1, 3, "wb_sales_report_rows"))

    def test_incomplete_report_writes_but_does_not_delete(self):
        sales = [sale("2026-09-20T07:00:00Z", "2", 500, 400)]
        db = [db_row("2026-09-20", "9", 1, 100, 90, rid=9)]
        sb, result = self.run_with(sales, db, complete=False)
        self.assertEqual(len(sb.upserts), 1)
        self.assertEqual(sb.deletes, [])
        self.assertEqual(result["deleted"], 0)

    def test_dry_run_writes_and_deletes_nothing(self):
        sales = [sale("2026-09-20T07:00:00Z", "2", 500, 400)]
        db = [db_row("2026-09-20", "9", 1, 100, 90, rid=9)]
        sb, result = self.run_with(sales, db, dry_run=True)
        self.assertEqual((sb.upserts, sb.deletes), ([], []))
        self.assertEqual((result["written"], result["deleted"]), (0, 0))

    def test_empty_window_neither_writes_nor_deletes(self):
        sb, result = self.run_with([], [db_row("2026-09-20", "9", 1, 100, 90, rid=9)])
        self.assertEqual((sb.upserts, sb.deletes), ([], []))
        self.assertEqual(result["rows"], 0)

    def test_return_is_signed_and_moscow_day_applies(self):
        sales = [sale("2026-09-19T21:30:00Z", "1", 1000, 800, rrd=1),                     # 00:30 МСК 09-20
                 sale("2026-09-20T05:00:00Z", "1", 300, 200, oper="Возврат", rrd=2)]
        sb, _ = self.run_with(sales, [])
        [row] = [r for _t, rows, _c in sb.upserts for r in rows]
        self.assertEqual((row["buyout_date"], row["buyouts_qty"], row["buyouts_amount_seller"], row["buyouts_amount_buyer"]), ("2026-09-20", 0.0, 700.0, 600.0))
        self.assertEqual(row["article"], "F000283615")


if __name__ == "__main__":
    unittest.main()
