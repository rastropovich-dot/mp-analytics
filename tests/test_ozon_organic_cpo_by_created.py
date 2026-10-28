"""59-я, органика шаги 1–2: строки ОЗЗ с номером и датой создания заказа, ОЗЗ в органике по дню создания, сырая органика с
флагом ad_exceeds_total, пустые строки не пишутся, чистка ключей окна, расширение ночного окна под позднюю ОЗЗ."""
import unittest
from unittest import mock

import reports_ozon_sku_organic as organic
from loaders import ozon_cpo_order_lines as cpo_lines

CSV = (
    "﻿;Оплата за заказ (все товары). Отчёт по заказам, Период 23.10.2026-23.10.2026\n"
    "Дата;ID заказа;Номер заказа;SKU;SKU продвигаемого товара;Артикул;Название товара;Количество;Стоимость продажи, ₽;Стоимость, ₽;"
    "Ставка, %;Ставка, ₽;Расход, ₽\n"
    "23.10.2026;1;0156082973-0217;100;100;A100;Подвеска;1;11605,00;11605,00;6,00;696,30;696,30\n"
    "23.10.2026;2;0156000000-0001;100;200;A100;Подвеска;2;20000,00;20000,00;5,00;1000,00;1000,00\n"
    "23.10.2026;3;;300;300;A300;Без номера;1;500,00;500,00;5,00;25,00;25,00\n"
    "23.10.2026;4;0156111111-0001;300;300;A300;Кольцо;1;700,00;700,00;6,00;42,00;42,00\n"
)


class _Query:
    """Минимальный построитель PostgREST: запоминает фильтры, отдаёт строки из таблицы-подделки."""

    def __init__(self, db, table):
        self.db, self.table, self.filters, self.op, self.payload = db, table, [], "select", None
        self._range = None

    def select(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def filter(self, col, op, value):
        self.filters.append((col, op, value))
        return self

    def eq(self, col, value):
        self.filters.append((col, "eq", value))
        return self

    def gte(self, col, value):
        self.filters.append((col, "gte", value))
        return self

    def lte(self, col, value):
        self.filters.append((col, "lte", value))
        return self

    def or_(self, expr):
        self.filters.append((None, "or", expr))
        return self

    def upsert(self, rows, on_conflict=None):
        self.op, self.payload = "upsert", rows
        return self

    def delete(self):
        self.op = "delete"
        return self

    def _match(self, row):
        for col, op, value in self.filters:
            if op == "eq" and str(row.get(col)) != str(value):
                return False
            if op == "gte" and str(row.get(col)) < str(value):
                return False
            if op == "lte" and str(row.get(col)) > str(value):
                return False
            if op == "like(any)":
                prefixes = [p[:-1] for p in value.strip("{}").split(",")]
                if not any(str(row.get(col)).startswith(p) for p in prefixes):
                    return False
        return True

    def execute(self):
        rows = self.db.setdefault(self.table, [])
        if self.op == "upsert":
            self.db.setdefault("_upserts", []).append((self.table, list(self.payload)))
            return type("R", (), {"data": self.payload})()
        if self.op == "delete":
            gone = [r for r in rows if self._match(r)]
            self.db[self.table] = [r for r in rows if not self._match(r)]
            self.db.setdefault("_deletes", []).append((self.table, gone))
            return type("R", (), {"data": gone})()
        data = [r for r in rows if self._match(r)]
        if self._range:
            data = data[self._range[0]:self._range[1] + 1]
        return type("R", (), {"data": data})()


class _Sb:
    def __init__(self, db):
        self.db = db

    def table(self, name):
        return _Query(self.db, name)


STATUS_LOG = [
    {"posting_number": "0156082973-0217-1", "order_date": "2026-10-23", "observed_at": "2026-10-24T01:00:00Z"},
    {"posting_number": "0156082973-0217-1", "order_date": "2026-10-23", "observed_at": "2026-10-25T01:00:00Z"},
    {"posting_number": "0156000000-0001-1", "order_date": "2026-10-10", "observed_at": "2026-10-11T01:00:00Z"},
]


class ParseAndDates(unittest.TestCase):
    def test_parse_lines_keys_and_counters(self):
        rows, counters = cpo_lines.parse_lines(CSV)
        self.assertEqual(len(rows), 3)
        self.assertEqual(counters["without_number"], 1)
        self.assertEqual(counters["spend_file"], 1763.30)          # Σ всех строк файла, и без номера тоже
        first = rows[0]
        self.assertEqual((first["charge_date"], first["order_number"], first["marketplace_sku"], first["promoted_sku"]),
                         ("2026-10-23", "0156082973-0217", "100", "100"))
        self.assertEqual((first["qty"], first["sale_amount"], first["rate_percent"], first["spend"]), (1.0, 11605.0, 6.0, 696.3))

    def test_lookup_and_attach(self):
        sb = _Sb({"ozon_posting_status_log": [dict(r) for r in STATUS_LOG]})
        rows, _ = cpo_lines.parse_lines(CSV)
        created = cpo_lines.lookup_created_dates(sb, [r["order_number"] for r in rows])
        self.assertEqual(created, {"0156082973-0217": "2026-10-23", "0156000000-0001": "2026-10-10"})
        missing = cpo_lines.attach_created_dates(rows, created)
        self.assertEqual(missing, 1)
        line = cpo_lines.summary_line(rows, missing)
        self.assertIn("созданы в день списания 1, раньше 1, без даты создания 1", line)

    def test_store_dry_run_writes_nothing(self):
        db = {"ozon_posting_status_log": [dict(r) for r in STATUS_LOG]}
        with mock.patch("builtins.print"):
            out = cpo_lines.store(_Sb(db), CSV, write=False)
        self.assertEqual(out["writes"], 0)
        self.assertNotIn("_upserts", db)

    def test_save_lines_deletes_vanished_keys_of_the_same_charge_day(self):
        old = {"charge_date": "2026-10-23", "order_number": "OLD-1", "marketplace_sku": "9", "promoted_sku": "9"}
        other_day = {"charge_date": "2026-10-22", "order_number": "OLD-2", "marketplace_sku": "9", "promoted_sku": "9"}
        db = {"ozon_cpo_order_lines": [old, other_day]}
        rows, _ = cpo_lines.parse_lines(CSV)
        written, deleted = cpo_lines.save_lines(_Sb(db), rows)
        self.assertEqual((written, deleted), (3, 1))
        self.assertEqual(db["_deletes"][0][1], [old])               # чужой день списания не тронут
        self.assertIn(other_day, db["ozon_cpo_order_lines"])

    def test_store_survives_missing_table(self):
        class Broken(_Sb):
            def table(self, name):
                if name == cpo_lines.TABLE:
                    raise RuntimeError("relation does not exist")
                return super().table(name)
        with mock.patch("builtins.print"):
            out = cpo_lines.store(Broken({"ozon_posting_status_log": []}), CSV)
        self.assertEqual(out["writes"], 0)


def _line(charge, created, sku, qty, amount):
    return {"charge_date": charge, "order_number": f"N{charge}{sku}{created}", "marketplace_sku": sku, "promoted_sku": sku,
            "order_created_date": created, "qty": qty, "sale_amount": amount, "article": "A" + sku}


class CpoByCreated(unittest.TestCase):
    def test_lines_move_to_creation_day_and_attribution_cpo_is_dropped_for_covered_days(self):
        lines = [_line("2026-10-23", "2026-10-23", "100", 1, 1000),      # 6 %: день заказа
                 _line("2026-10-23", "2026-10-10", "100", 2, 2000),      # 5 %: списано при доставке
                 _line("2026-10-23", None, "300", 1, 700)]               # без даты создания — день списания
        cpo, charge_dates, counters = organic.load_cpo_by_created("2026-10-10", "2026-10-23", lines)
        self.assertEqual(charge_dates, {"2026-10-23"})
        self.assertEqual(cpo[("2026-10-23", "100")]["ad_orders_qty"], 1)
        self.assertEqual(cpo[("2026-10-10", "100")]["ad_orders_qty"], 2)
        self.assertEqual(cpo[("2026-10-23", "300")]["ad_orders_qty"], 1)
        self.assertEqual((counters["same_day"], counters["earlier"], counters["without_created_date"]), (1, 1, 1))

        attribution = [
            {"sale_date": "2026-10-23", "marketplace_code": "ozon", "marketplace_sku": "100", "ad_source": "cpo", "attribution_type": "direct",
             "ad_orders_qty": 3, "ad_orders_revenue": 3000},
            {"sale_date": "2026-10-23", "marketplace_code": "ozon", "marketplace_sku": "100", "ad_source": "cpc", "attribution_type": "direct",
             "ad_orders_qty": 1, "ad_orders_revenue": 500},
            {"sale_date": "2026-10-10", "marketplace_code": "ozon", "marketplace_sku": "100", "ad_source": "cpo", "attribution_type": "direct",
             "ad_orders_qty": 4, "ad_orders_revenue": 4000},                # день без строк ОЗЗ — по дню списания, как раньше
        ]
        with mock.patch.object(organic, "fetch_all", return_value=attribution):
            grouped, _dates, by_charge = organic.load_ad_attribution("2026-10-10", "2026-10-23", charge_dates)
        self.assertEqual(grouped[("2026-10-23", "100")]["ad_orders_qty"], 1)        # ОЗЗ 23.10 из атрибуции снята
        self.assertEqual(grouped[("2026-10-10", "100")]["ad_orders_qty"], 4)
        self.assertEqual(by_charge, {"2026-10-10"})
        merged = organic.merge_ad(grouped, cpo)
        self.assertEqual(merged[("2026-10-23", "100")]["ad_orders_qty"], 2)          # клик 1 + ОЗЗ созданная 23.10
        self.assertEqual(merged[("2026-10-10", "100")]["ad_orders_qty"], 6)          # ОЗЗ по списанию 4 + доставленная 2

    def test_read_failure_falls_back(self):
        with mock.patch.object(cpo_lines, "read_for_created_window", side_effect=RuntimeError("no table")), \
                mock.patch("builtins.print"):
            cpo, charge_dates, counters = organic.load_cpo_by_created("2026-10-23", "2026-10-23")
        self.assertEqual((cpo, charge_dates, counters["read_failed"]), ({}, set(), True))


class BuildRows(unittest.TestCase):
    def build(self, totals, attribution, lines):
        with mock.patch.object(organic, "load_total_orders", return_value=totals), \
                mock.patch.object(organic, "fetch_all", return_value=attribution), \
                mock.patch.object(organic, "load_ad_expense_dates", return_value={"2026-10-23"}):
            return organic.build_organic_rows("2026-10-23", "2026-10-23", lines)

    def test_raw_organic_flag_and_no_empty_rows(self):
        totals = {("2026-10-23", "100"): {"total_orders_qty": 1, "total_orders_revenue": 1000, "total_revenue_source": "seller_analytics"},
                  ("2026-10-23", "200"): {"total_orders_qty": 5, "total_orders_revenue": 5000, "total_revenue_source": "seller_analytics"},
                  ("2026-10-23", "999"): {"total_orders_qty": 0, "total_orders_revenue": 0, "total_revenue_source": "marketplace_orders"}}
        attribution = [{"sale_date": "2026-10-23", "marketplace_code": "ozon", "marketplace_sku": "100", "ad_source": "cpc",
                        "attribution_type": "direct", "ad_orders_qty": 2, "ad_orders_revenue": 1500}]
        rows, summary = self.build(totals, attribution, [_line("2026-10-23", "2026-10-23", "200", 1, 1000)])
        by_sku = {r["marketplace_sku"]: r for r in rows}
        self.assertEqual(set(by_sku), {"100", "200"})                                  # 999: ни заказов, ни рекламы — не пишется
        self.assertEqual((by_sku["100"]["organic_orders_qty"], by_sku["100"]["organic_orders_revenue"]), (-1, -500))
        self.assertTrue(by_sku["100"]["ad_exceeds_total"])
        self.assertEqual((by_sku["200"]["organic_orders_qty"], by_sku["200"]["ad_exceeds_total"]), (4, False))
        self.assertEqual(summary["ad_exceeds_total"], {"rows": 1, "excess_qty": 1.0})
        self.assertEqual(summary["status_counts"]["empty_not_written"], 1)
        line = organic.format_summary_line(summary)
        self.assertIn("реклама больше всех заказов у 1 SKU, лишних 1 шт", line)
        self.assertNotIn("по дню списания", line)


class TotalsCreated(unittest.TestCase):
    def test_marketplace_orders_fallback_counts_cancelled(self):
        rows = [{"id": 1, "order_date": "2026-10-23", "marketplace_sku": "100", "orders_qty": 2, "orders_amount_seller": 2000,
                 "cancelled_orders_qty": 1, "cancelled_orders_amount_seller": 900}]
        with mock.patch.object(organic, "fetch_all", return_value=rows):
            grouped = organic.load_total_orders_from_marketplace_orders("2026-10-23", "2026-10-23")
        self.assertEqual((grouped[("2026-10-23", "100")]["total_orders_qty"], grouped[("2026-10-23", "100")]["total_orders_revenue"]),
                         (3.0, 2900.0))

    def test_fetch_all_orders_by_full_key_and_maps_filters(self):
        with mock.patch.object(organic.stale_keys, "read_window_rows", return_value=[]) as rw:
            organic.fetch_all("t", [("sale_date", "gte", "2026-10-01"), ("or_", "a.gt.0")], ("sale_date", "marketplace_sku"))
        _sb, table, select, filters, order = rw.call_args[0]
        self.assertEqual(filters, [("gte", "sale_date", "2026-10-01"), ("or_", "a.gt.0")])
        self.assertEqual(order, ("sale_date", "marketplace_sku"))


class SaveAndWindow(unittest.TestCase):
    def test_save_rows_deletes_keys_of_the_window_without_rows(self):
        db = {"ozon_daily_sku_organic": [
            {"sale_date": "2026-10-23", "marketplace_code": "ozon", "marketplace_sku": "100"},
            {"sale_date": "2026-10-23", "marketplace_code": "ozon", "marketplace_sku": "555"},   # пустая строка прежних прогонов
            {"sale_date": "2026-10-22", "marketplace_code": "ozon", "marketplace_sku": "777"}]}  # день без построенных — не трогать
        built = [{"sale_date": "2026-10-23", "marketplace_code": "ozon", "marketplace_sku": "100", "total_revenue_source": "x"}]
        with mock.patch.object(organic, "supabase", _Sb(db)), mock.patch("builtins.print"):
            written, deleted = organic.save_rows(built, "2026-10-22", "2026-10-23")
        self.assertEqual((written, deleted), (1, 1))
        self.assertEqual([r["marketplace_sku"] for r in db["_deletes"][0][1]], ["555"])
        self.assertNotIn("total_revenue_source", db["_upserts"][0][1][0])

    def test_extend_for_late_cpo(self):
        lines = [{"charge_date": "2026-10-23", "order_created_date": "2026-10-05"},
                 {"charge_date": "2026-10-23", "order_created_date": None}]
        with mock.patch.object(organic, "fetch_all", return_value=lines), mock.patch("builtins.print"):
            self.assertEqual(organic.extend_for_late_cpo("2026-10-23", "2026-10-23"), "2026-10-05")
        with mock.patch.object(organic, "fetch_all", return_value=[{"charge_date": "2026-10-23", "order_created_date": "2026-01-01"}]), \
                mock.patch("builtins.print"):
            self.assertEqual(organic.extend_for_late_cpo("2026-10-23", "2026-10-23"), "2026-09-08")   # не глубже 45 дней
        with mock.patch.object(organic, "fetch_all", side_effect=RuntimeError("no table")), mock.patch("builtins.print"):
            self.assertEqual(organic.extend_for_late_cpo("2026-10-23", "2026-10-23"), "2026-10-23")


if __name__ == "__main__":
    unittest.main()
