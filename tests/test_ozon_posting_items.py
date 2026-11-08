"""Шестьдесят первая: отправление × товар Ozon (loaders/ozon_posting_items.py) и запись в ночных шагах FBO / FBS.

Тождество на фикстуре настоящего сырья (РБХ-1, tests/fixtures/ozon_postings_items_sample.json): Σ штук и Σ штуки × цена по
(день, SKU, схема) = строкам marketplace_orders, построенным тем же правилом той же ночи. Запись нефатальна, отказ ЛК FBO не затирает
цену покупателя, застрявшие ключи — только внутри окна. Сеть и база не зовутся.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from loaders import ozon_orders_rows as rules  # noqa: E402
from loaders import ozon_posting_items as items  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "ozon_postings_items_sample.json")
OBSERVED = "2026-11-06T00:40:00+00:00"


def fixture(schema):
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["postings"][schema]


def posting(number, products, status="delivered", day="2026-11-03T10:00:00Z", schema="fbo", fin=None):
    p = {"posting_number": number, "status": status, "products": products, "financial_data": {"products": fin or []}}
    if schema == "fbo":
        p.update({"created_at": day, "in_process_at": day})
    else:
        p["in_process_at"] = day
    return p


def product(sku, qty=1, price="1000", offer="A1"):
    return {"sku": sku, "offer_id": offer, "name": "товар", "quantity": qty, "price": {"amount": price, "currency": "RUB"}}


class IdentityOnRawFixture(unittest.TestCase):
    def test_fbo_and_fbs_sums_equal_order_rows(self):
        for schema in ("fbo", "fbs"):
            postings = fixture(schema)
            rows, counters = items.item_rows(postings, schema, OBSERVED)
            order_rows, _ = rules.build_order_rows(postings, schema, observed_at=OBSERVED)
            self.assertEqual(items.check_identity(rows, order_rows), [], schema)
            self.assertEqual(sum(r["quantity"] for r in rows),
                             int(sum(D(str(r["orders_qty"])) + D(str(r["cancelled_orders_qty"])) for r in order_rows)), schema)
            self.assertEqual(len({(r["posting_number"], r["sku"]) for r in rows}), len(rows), schema)
            self.assertGreater(len(rows), len(postings) - 1 if schema == "fbo" else len(postings), schema)  # у FBS есть многотоварные

    def test_fbs_buyer_price_is_customer_price_per_unit(self):
        postings = fixture("fbs")
        rows, counters = items.item_rows(postings, "fbs", OBSERVED)
        self.assertEqual(counters.get("buyer_unknown", 0), 0)
        p = next(x for x in postings if any(pr["quantity"] > 1 for pr in x["products"]))
        pr = next(pr for pr in p["products"] if pr["quantity"] > 1)
        fin = next(f for f in p["financial_data"]["products"] if str(f["product_id"]) == str(pr["sku"]))
        row = next(r for r in rows if r["posting_number"] == p["posting_number"] and r["sku"] == pr["sku"])
        self.assertEqual(D(str(row["buyer_price"])), D(str(fin["customer_price"]["amount"])).quantize(D("0.01")))
        self.assertEqual(row["quantity"], pr["quantity"])

    def test_cancelled_kept_with_status(self):
        rows, _ = items.item_rows(fixture("fbo"), "fbo", OBSERVED)
        self.assertTrue(any(r["status"] == "cancelled" for r in rows))

    def test_fbo_without_report_buyer_unknown(self):
        rows, counters = items.item_rows(fixture("fbo"), "fbo", OBSERVED)
        self.assertTrue(all(r["buyer_price"] is None for r in rows))
        self.assertEqual(counters["buyer_unknown"], len(rows))


class Rules(unittest.TestCase):
    def test_same_skips_as_order_rows(self):
        postings = [
            posting("1-1", [product(11, 2, "500")]),
            posting("1-2", [product(11)], status="cancelled_from_split_pending"),
            posting("1-3", [product(12, 0)]),
            posting("1-4", [{"sku": None, "quantity": 1, "price": {"amount": "1", "currency": "RUB"}}]),
            {"posting_number": "1-5", "status": "delivered", "products": [product(13)]},   # без даты
            posting("1-6", [product(14)], status="cancelled"),
        ]
        rows, c = items.item_rows(postings, "fbo", OBSERVED)
        order_rows, _ = rules.build_order_rows(postings, "fbo", observed_at=OBSERVED)
        self.assertEqual(items.check_identity(rows, order_rows), [])
        self.assertEqual(sorted((r["posting_number"], r["sku"]) for r in rows), [("1-1", 11), ("1-6", 14)])
        self.assertEqual((c["split_parent_skipped"], c["zero_qty"], c["no_sku"], c["no_date"]), (1, 1, 1, 1))
        r = next(r for r in rows if r["sku"] == 11)
        self.assertEqual((r["quantity"], r["seller_price"], r["order_date"], r["ordered_at"]), (2, 500.0, "2026-11-03", "2026-11-03T10:00:00Z"))

    def test_duplicate_sku_in_posting_summed(self):
        rows, c = items.item_rows([posting("2-1", [product(21, 1, "100"), product(21, 3, "200")])], "fbo", OBSERVED)
        self.assertEqual(c["duplicate_sku_in_posting"], 1)
        self.assertEqual((len(rows), rows[0]["quantity"], rows[0]["seller_price"]), (1, 4, 175.0))
        order_rows, _ = rules.build_order_rows([posting("2-1", [product(21, 1, "100"), product(21, 3, "200")])], "fbo", observed_at=OBSERVED)
        self.assertEqual(items.check_identity(rows, order_rows), [])

    def test_fbs_ordered_at_is_in_process_at(self):
        rows, _ = items.item_rows([posting("3-1", [product(31)], schema="fbs", day="2026-11-03T22:30:00Z")], "fbs", OBSERVED)
        self.assertEqual((rows[0]["order_date"], rows[0]["ordered_at"]), ("2026-11-04", "2026-11-03T22:30:00Z"))   # МСК

    def test_identity_names_mismatch(self):
        rows, _ = items.item_rows([posting("4-1", [product(41)])], "fbo", OBSERVED)
        order_rows, _ = rules.build_order_rows([posting("4-1", [product(41, 2)])], "fbo", observed_at=OBSERVED)
        diffs = items.check_identity(rows, order_rows)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0][0], ("2026-11-03", "41", "fbo"))

    def test_fbo_buyer_from_report(self):
        rows, c = items.item_rows([posting("5-1", [product(51, 2, "1000")])], "fbo", OBSERVED, buyer_prices={("5-1", "51"): D("640.50")})
        self.assertEqual((rows[0]["buyer_price"], c["buyer_known"]), (640.5, 1))


class Write(unittest.TestCase):
    NOW = datetime(2026, 11, 6, 0, 40, tzinfo=timezone.utc)

    def run_write(self, postings, order_rows, **kw):
        calls = {"upsert": [], "cleanup": [], "state": []}
        kw.setdefault("upsert_fn", lambda sb, rows: calls["upsert"].append(rows) or len(rows))
        kw.setdefault("report_fn", lambda sb, rows, schema, days_back, now_utc=None:
                      calls["cleanup"].append("report") or {"candidates": 0, "examples": []})
        kw.setdefault("state_fn", lambda sb, schema, info: calls["state"].append((schema, info)))
        info = items.write(object(), postings, "fbo", order_rows, now_utc=self.NOW, **kw)
        return info, calls

    def test_written_and_state(self):
        p = [posting("6-1", [product(61)])]
        order_rows, _ = rules.build_order_rows(p, "fbo", observed_at=OBSERVED)
        info, calls = self.run_write(p, order_rows)
        self.assertTrue(info["ok"])
        self.assertEqual((info["rows"], info["identity_diffs"], info["written"]), (1, 0, 1))
        self.assertEqual(calls["cleanup"], ["report"])   # 44-я: только отчёт о застрявших, без удаления
        self.assertEqual((info["stale_candidates"], info["stale_examples"]), (0, []))
        self.assertNotIn("stale_deleted", info)
        self.assertEqual(calls["state"][0][0], "fbo")
        self.assertIn("buyer_price", calls["upsert"][0][0])

    def test_dry_run_no_writes(self):
        p = [posting("6-2", [product(62)])]
        info, calls = self.run_write(p, [], dry_run=True)
        self.assertEqual(info["identity_diffs"], 1)   # пустые заказы — расхождение названо, а не проглочено
        self.assertEqual((calls["upsert"], calls["state"], calls["cleanup"]), ([], [], ["report"]))   # отчёт читает окно и в dry-run, как прежде

    def test_keep_buyer_strips_column(self):
        p = [posting("6-3", [product(63)])]
        info, calls = self.run_write(p, [], keep_buyer=True)
        self.assertNotIn("buyer_price", calls["upsert"][0][0])
        self.assertTrue(info["buyer_price_kept"])

    def test_upsert_failure_is_not_fatal(self):
        def boom(sb, rows):
            raise RuntimeError("relation \"public.ozon_posting_items\" does not exist")
        info, calls = self.run_write([posting("6-4", [product(64)])], [], upsert_fn=boom)
        self.assertFalse(info["ok"])
        self.assertIn("does not exist", info["error"])
        self.assertEqual(calls["cleanup"], [])           # без записи отчёта о застрявших нет
        self.assertFalse(calls["state"][0][1]["ok"])     # отказ уходит в итог для алерта

    def test_cleanup_failure_named(self):
        def bad_cleanup(sb, rows, schema, days_back, now_utc=None):
            raise TimeoutError("57014")
        info, _ = self.run_write([posting("6-5", [product(65)])], [], report_fn=bad_cleanup)
        self.assertTrue(info["ok"])
        self.assertIn("57014", info["stale_error"])

    def test_alert_line(self):
        fresh = (self.NOW - timedelta(hours=1)).isoformat()
        old = (self.NOW - timedelta(hours=30)).isoformat()
        self.assertEqual(items.alert_line({"fbo": {"ok": True, "identity_diffs": 0, "finished_at": fresh}}, now=self.NOW), "")
        self.assertEqual(items.alert_line({}, now=self.NOW), "")
        line = items.alert_line({"fbo": {"ok": False, "error": "X", "finished_at": fresh},
                                 "fbs": {"ok": True, "identity_diffs": 2, "identity_keys": 90, "finished_at": fresh}}, now=self.NOW)
        self.assertIn("FBO не записаны (X)", line)
        self.assertIn("FBS расходятся с заказами на 2 ключах из 90", line)
        self.assertEqual(items.alert_line({"fbo": {"ok": False, "error": "X", "finished_at": old}}, now=self.NOW), "")


class AlertWiring(unittest.TestCase):
    def test_alert_module_line(self):
        import alerts_telegram as alerts
        now = datetime(2026, 11, 6, 7, 30, tzinfo=timezone.utc)
        fresh = (now - timedelta(hours=7)).isoformat()
        self.assertEqual(alerts.POSTING_ITEMS_STATE_KEYS, {"fbo": "ozon_posting_items:fbo", "fbs": "ozon_posting_items:fbs"})
        self.assertIn("FBS не записаны", alerts.posting_items_line({"fbs": {"ok": False, "error": "e", "finished_at": fresh}}, now=now))
        self.assertEqual(alerts.posting_items_line({"fbo": None, "fbs": None}, now=now), "")


class Stale(unittest.TestCase):
    NOW = datetime(2026, 11, 6, 0, 40, tzinfo=timezone.utc)   # 03:40 МСК 06.11

    def test_window_excludes_first_day_and_today(self):
        self.assertEqual(items.stale_window(30, self.NOW), (datetime(2026, 10, 8).date(), datetime(2026, 11, 5).date()))
        start, _ = rules.nightly_window(30, self.NOW)
        self.assertEqual(start.astimezone(timezone(timedelta(hours=3))).date().isoformat(), "2026-10-07")   # окно сбора — с 07.10

    def test_report_names_only_inside_window(self):
        built = [{"posting_number": "A", "sku": 1, "order_date": "2026-10-20"},
                 {"posting_number": "B", "sku": 2, "order_date": "2026-10-21"}]
        table = [{"posting_number": "A", "sku": 1, "order_date": "2026-10-20", "status": "delivered"},
                 {"posting_number": "Z", "sku": 9, "order_date": "2026-10-20", "status": "awaiting_packaging"},   # застрял
                 {"posting_number": "Y", "sku": 8, "order_date": "2026-10-25", "status": "delivering"}]           # день без строк — не считаем
        rep = items.report_stale(object(), built, "fbo", 30, now_utc=self.NOW,
                                 read_fn=lambda sb, schema, a, b: [r for r in table if a <= r["order_date"] <= b])
        self.assertEqual(rep["candidates"], 1)
        self.assertEqual(rep["examples"], [{"posting_number": "Z", "sku": "9", "order_date": "2026-10-20"}])
        self.assertEqual(rep["skipped_days"], ["2026-10-25"])

    def test_report_reads_only_inside_window(self):
        seen = []
        items.report_stale(object(), [], "fbs", 30, now_utc=self.NOW,
                           read_fn=lambda sb, schema, a, b: seen.append((schema, a, b)) or [])
        self.assertEqual(seen, [("fbs", "2026-10-08", "2026-11-05")])

    def test_report_examples_capped_at_5_keys_only(self):
        built = [{"posting_number": "A", "sku": 1, "order_date": "2026-10-20"}]
        table = [{"posting_number": f"S{i}", "sku": 100 + i, "order_date": "2026-10-20", "status": "delivering", "quantity": 3}
                 for i in range(8)]
        rep = items.report_stale(object(), built, "fbo", 30, now_utc=self.NOW, read_fn=lambda sb, schema, a, b: table)
        self.assertEqual(rep["candidates"], 8)
        self.assertEqual(len(rep["examples"]), 5)
        self.assertTrue(all(set(e) == {"posting_number", "sku", "order_date"} for e in rep["examples"]))


class _Query:
    """Подставной построитель PostgREST: каждое имя метода записывается; данные чтения — из таблицы по фильтрам eq."""

    def __init__(self, sb, table):
        self.sb, self.table, self.ops, self.filters, self.payload = sb, table, [], {}, None

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.ops.append(name)
            self.sb.calls.append((self.table, name))
            if name == "eq":
                self.filters[args[0]] = args[1]
            if name in ("upsert", "insert", "update", "delete"):
                self.payload = args[0] if args else None
            if name == "range":
                self.range = args
            return self
        return call

    def execute(self):
        class R:
            pass
        r = R()
        r.data = []
        if "select" in self.ops and self.table == items.TABLE:
            rows = [x for x in self.sb.rows if all(str(x.get(k)) == str(v) for k, v in self.filters.items())]
            a, b = getattr(self, "range", (0, 10 ** 9))
            r.data = rows[a:b + 1]
        if "upsert" in self.ops and self.table == items.TABLE:
            self.sb.upserted.extend(self.payload)
        return r


class FakeSb:
    def __init__(self, rows):
        self.rows, self.calls, self.upserted = rows, [], []

    def table(self, name):
        return _Query(self, name)


class NoDelete(unittest.TestCase):
    """44-я: в модуле нет пути удаления; запись с кандидатами не шлёт ни delete, ни update."""
    NOW = datetime(2026, 11, 6, 0, 40, tzinfo=timezone.utc)

    def test_static_no_delete_path_in_module(self):
        with open(items.__file__, encoding="utf-8") as fh:
            src = fh.read()
        for bad in (".delete(", "delete_keys", "delete_fn", "delete_by_id", "delete_by_date_and_column", "stale_keys.cleanup",
                    "cleanup_stale", ".truncate(", "truncate "):
            self.assertNotIn(bad, src, bad)
        self.assertFalse(hasattr(items, "delete_keys") or hasattr(items, "cleanup_stale"))

    def test_write_not_dry_run_with_candidates_sends_no_delete_or_update(self):
        p = [posting("7-1", [product(71, 2, "1000")], day="2026-10-20T10:00:00Z")]
        order_rows, _ = rules.build_order_rows(p, "fbo", observed_at=OBSERVED)
        old = [{"posting_number": f"OLD-{i}", "sku": 700 + i, "order_date": "2026-10-20", "schema": "fbo", "status": "awaiting_packaging"}
               for i in range(7)]
        sb = FakeSb(old)
        info = items.write(sb, p, "fbo", order_rows, now_utc=self.NOW, observed_at=OBSERVED)
        self.assertTrue(info["ok"], info.get("error"))
        self.assertEqual((info["written"], info["stale_candidates"]), (1, 7))
        self.assertEqual(len(info["stale_examples"]), 5)
        self.assertEqual(info["stale_examples"][0], {"posting_number": "OLD-0", "sku": "700", "order_date": "2026-10-20"})
        methods = {m for _t, m in sb.calls}
        self.assertNotIn("delete", methods)
        self.assertNotIn("update", methods)
        self.assertEqual({t for t, m in sb.calls if m == "upsert"}, {items.TABLE, "pipeline_runtime_state"})
        self.assertEqual([r["posting_number"] for r in sb.upserted], ["7-1"])   # записана строка ночи, старые ключи не тронуты
        self.assertEqual(len(sb.rows), 7)
        self.assertNotIn("stale_deleted", info)

    def test_alert_line_candidates(self):
        fresh = (self.NOW - timedelta(hours=1)).isoformat()
        line = items.alert_line({"fbo": {"ok": True, "identity_diffs": 0, "stale_candidates": 3, "finished_at": fresh}}, now=self.NOW)
        self.assertEqual(line, "⚠️ отправления × товар Ozon: FBO застрявшие ключи: кандидатов 3, не удалены (удаление запрещено)")
        both = items.alert_line({"fbs": {"ok": True, "identity_diffs": 2, "identity_keys": 9, "stale_candidates": 1, "finished_at": fresh}},
                                now=self.NOW)
        self.assertIn("FBS расходятся с заказами на 2 ключах из 9", both)
        self.assertIn("FBS застрявшие ключи: кандидатов 1, не удалены (удаление запрещено)", both)
        self.assertEqual(items.alert_line({"fbo": {"ok": True, "identity_diffs": 0, "stale_candidates": 0, "finished_at": fresh}},
                                          now=self.NOW), "")


class Steps(unittest.TestCase):
    def test_fbs_step_writes_items_after_orders(self):
        import ozon_fbs_orders_step as step
        order = []

        class Fbs:
            supabase = object()
            DEFAULT_DAYS_BACK = 30

            def get_ozon_fbs_postings(self, days_back):
                return fixture("fbs")

            def save_ozon_orders(self, postings):
                order.append("orders")

        class Items:
            def write(self, sb, postings, schema, order_rows, days_back=30, dry_run=False, **kw):
                order.append(("items", schema, dry_run, len(order_rows) > 0))
                return {"ok": True}

        class Log:
            def dump_raw(self, postings, schema):
                order.append("raw")
                return "x"
        step.run(fbs_module=Fbs(), log_module=Log(), items_module=Items())
        self.assertEqual(order, ["orders", ("items", "fbs", False, True), "raw"])
        order.clear()
        step.run(fbs_module=Fbs(), log_module=Log(), items_module=Items(), dry_run=True)
        self.assertEqual(order, [("items", "fbs", True, True), "raw"])

    def test_fbo_step_keep_buyer_when_report_failed(self):
        import ozon_fbo_orders_step as step
        got = []

        class Fbo:
            supabase = object()

            def get_fbo_postings(self, days_back):
                return fixture("fbo")

            def build_order_rows(self, postings, buyer_prices=None):
                return rules.build_order_rows(postings, "fbo", observed_at=OBSERVED, buyer_prices=buyer_prices)[0]

            def save_orders(self, rows):
                got.append("orders")

        class Report:
            def fetch_buyer_prices(self, scheme, since, to):
                raise TimeoutError("не готов за 420 с")

        class Items:
            def write(self, sb, postings, schema, order_rows, days_back=30, buyer_prices=None, keep_buyer=False, dry_run=False, **kw):
                got.append(("items", schema, keep_buyer, dry_run, items.check_identity(items.item_rows(postings, schema, OBSERVED)[0], order_rows)))
                return {"ok": True}

        class Log:
            def dump_raw(self, postings, schema):
                return "x"
        step.run(fbo_module=Fbo(), log_module=Log(), report_module=Report(), items_module=Items(),
                 existing_fn=lambda sb, since: {}, state_fn=lambda sb, info: None)
        self.assertEqual(got, ["orders", ("items", "fbo", True, False, [])])


if __name__ == "__main__":
    unittest.main()
