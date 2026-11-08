"""П. 44а / 44б (08.11): цена покупателя ozon_posting_items не затирается NULL — однородные пачки upsert; неполнота проверки stale.

Подставной PostgREST (tests/fake_postgrest.py) хранит строки и применяет upsert по columns, которые строит настоящая
postgrest.base_request_builder.pre_upsert: колонка, которой нет у объекта, но есть у пачки, становится null. Сеть и база не зовутся.
"""
import os
import sys
import tempfile
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from loaders import ozon_orders_rows as rules  # noqa: E402
from loaders import ozon_posting_items as items  # noqa: E402
from fake_postgrest import FakePostgrest  # noqa: E402

OBSERVED = "2026-11-06T00:40:00+00:00"
DAY = "2026-10-20T10:00:00Z"
NOW = datetime(2026, 11, 6, 0, 40, tzinfo=timezone.utc)


def posting(number, products, status="delivered", day=DAY, schema="fbo", fin=None):
    p = {"posting_number": number, "status": status, "products": products, "financial_data": {"products": fin or []}}
    if schema == "fbo":
        p.update({"created_at": day, "in_process_at": day})
    else:
        p["in_process_at"] = day
    return p


def product(sku, qty=1, price="1000", offer="A1"):
    return {"sku": sku, "offer_id": offer, "name": "товар", "quantity": qty, "price": {"amount": price, "currency": "RUB"}}


def stored(pg, number, sku):
    return pg.get(items.TABLE, posting_number=number, sku=sku)


def seed_item(pg, number, sku, buyer, status="awaiting_packaging", qty=1, seller=1000.0, schema="fbo", day="2026-10-20",
              observed="2026-11-05T00:40:00+00:00"):
    pg.seed(items.TABLE, [{"posting_number": number, "sku": sku, "offer_id": "A1", "schema": schema, "order_date": day,
                           "ordered_at": DAY, "quantity": qty, "seller_price": seller, "buyer_price": buyer, "status": status,
                           "observed_at": observed, "source": "nightly"}])


def assert_homogeneous(tc, pg):
    ups = pg.upserts(items.TABLE)
    tc.assertTrue(ups)
    for _t, _m, columns, _res, key_sets in ups:
        tc.assertEqual(len(set(key_sets)), 1, "в пачке разные наборы колонок")
        tc.assertEqual(set(key_sets[0]), set(columns))


def night(pg, postings, schema="fbo", buyer_prices=None, keep_buyer=False):
    order_rows, _ = rules.build_order_rows(postings, schema, observed_at=OBSERVED, buyer_prices=buyer_prices)
    return items.write(pg, postings, schema, order_rows, buyer_prices=buyer_prices, keep_buyer=keep_buyer,
                       observed_at=OBSERVED, now_utc=NOW)


class FakeIsFaithful(unittest.TestCase):
    def test_mixed_batch_in_fake_nulls_like_postgrest(self):
        """Контроль самого подставного клиента: смешанная пачка (как отправил бы старый код) затирает цену null."""
        pg = FakePostgrest()
        seed_item(pg, "B", 2, 700.0)
        pg.table(items.TABLE).upsert([{"posting_number": "A", "sku": 1, "buyer_price": 5.0}, {"posting_number": "B", "sku": 2}],
                                     on_conflict=items.KEY).execute()
        self.assertIsNone(stored(pg, "B", 2)["buyer_price"])

    def test_old_code_path_would_lose_the_price(self):
        """Регрессия: прежний upsert (одна пачка со строкой buyer_price None) на том же клиенте затирает измеренную цену."""
        pg = FakePostgrest()
        seed_item(pg, "B", 2, 700.0)
        pg.table(items.TABLE).upsert([{"posting_number": "A", "sku": 1, "buyer_price": 5.0},
                                      {"posting_number": "B", "sku": 2, "buyer_price": None}], on_conflict=items.KEY).execute()
        self.assertIsNone(stored(pg, "B", 2)["buyer_price"])


class BuyerPriceKept(unittest.TestCase):
    """(а), (б), (г), (д), (е), (з), (к)."""

    def test_a_partial_report_updates_a_keeps_b_and_e_other_columns(self):
        pg = FakePostgrest()
        seed_item(pg, "A-1", 101, 640.50)
        seed_item(pg, "B-1", 102, 700.00)
        postings = [posting("A-1", [product(101, 2, "1200")], status="delivering"),
                    posting("B-1", [product(102, 3, "1100")], status="delivered")]
        info = night(pg, postings, buyer_prices={("A-1", "101"): D("650.00")})
        self.assertTrue(info["ok"], info.get("error"))
        a, b = stored(pg, "A-1", 101), stored(pg, "B-1", 102)
        self.assertEqual((a["buyer_price"], b["buyer_price"]), (650.0, 700.0))
        self.assertEqual((a["quantity"], a["seller_price"], a["status"], a["observed_at"]), (2, 1200.0, "delivering", OBSERVED))
        self.assertEqual((b["quantity"], b["seller_price"], b["status"], b["observed_at"]), (3, 1100.0, "delivered", OBSERVED))
        ups = pg.upserts(items.TABLE)
        self.assertEqual(sorted("buyer_price" in u[2] for u in ups), [False, True])
        self.assertEqual((info["buyer_written"], info["buyer_untouched"], info["written"]), (1, 1, 2))
        assert_homogeneous(self, pg)

    def test_b_full_report_failure_keeps_all(self):
        pg = FakePostgrest()
        seed_item(pg, "A-2", 201, 640.50)
        seed_item(pg, "B-2", 202, 700.00)
        info = night(pg, [posting("A-2", [product(201)]), posting("B-2", [product(202)])], buyer_prices={}, keep_buyer=True)
        self.assertEqual((stored(pg, "A-2", 201)["buyer_price"], stored(pg, "B-2", 202)["buyer_price"]), (640.5, 700.0))
        ups = pg.upserts(items.TABLE)
        self.assertEqual(len(ups), 1)
        self.assertNotIn("buyer_price", ups[0][2])
        self.assertTrue(info["buyer_price_kept"])
        self.assertEqual((info["buyer_written"], info["buyer_untouched"]), (0, 2))

    def test_g_new_key_without_price_is_null(self):
        pg = FakePostgrest()
        night(pg, [posting("N-1", [product(301)])], buyer_prices={})
        row = stored(pg, "N-1", 301)
        self.assertIsNone(row.get("buyer_price"))
        self.assertEqual(row["quantity"], 1)

    def test_d_existing_null_gets_price(self):
        pg = FakePostgrest()
        seed_item(pg, "E-1", 401, None)
        night(pg, [posting("E-1", [product(401)])], buyer_prices={("E-1", "401"): D("512.30")})
        self.assertEqual(stored(pg, "E-1", 401)["buyer_price"], 512.3)

    def test_measured_price_is_replaced_by_a_new_measurement(self):
        pg = FakePostgrest()
        seed_item(pg, "M-1", 501, 640.50)
        night(pg, [posting("M-1", [product(501)])], buyer_prices={("M-1", "501"): D("600.00")})
        self.assertEqual(stored(pg, "M-1", 501)["buyer_price"], 600.0)

    def test_k_fbs_foreign_currency_and_missing_customer_price_keep_old(self):
        pg = FakePostgrest()
        for n, s, b in (("F-1", 601, 555.0), ("F-2", 602, 444.0), ("F-3", 603, 333.0)):
            seed_item(pg, n, s, b, schema="fbs")
        postings = [posting("F-1", [product(601)], schema="fbs", fin=[{"product_id": 601, "customer_price": {"amount": "9000", "currency": "KZT"}}]),
                    posting("F-2", [product(602)], schema="fbs", fin=[]),
                    posting("F-3", [product(603)], schema="fbs", fin=[{"product_id": 603, "customer_price": {"amount": "350", "currency": "RUB"}}])]
        info = night(pg, postings, schema="fbs")
        self.assertTrue(info["ok"], info.get("error"))
        self.assertEqual([stored(pg, n, s)["buyer_price"] for n, s in (("F-1", 601), ("F-2", 602), ("F-3", 603))], [555.0, 444.0, 350.0])
        self.assertEqual((info["buyer_written"], info["buyer_untouched"]), (1, 2))
        assert_homogeneous(self, pg)

    def test_z_mixed_column_sets_are_never_sent_and_contract_is_int(self):
        pg = FakePostgrest()
        rows = [{"posting_number": "X", "sku": 1, "quantity": 1, "buyer_price": 5.0},
                {"posting_number": "Y", "sku": 2, "quantity": 1, "buyer_price": None},
                {"posting_number": "Z", "sku": 3, "quantity": 1}]
        self.assertEqual(items.upsert(pg, rows), 3)
        assert_homogeneous(self, pg)
        bad = [{"posting_number": "X", "sku": 1, "quantity": 1, "buyer_price": 5.0}, {"posting_number": "W", "sku": 4, "buyer_price": 6.0}]
        pg2 = FakePostgrest()
        with self.assertRaises(ValueError):
            items.upsert(pg2, bad)
        self.assertEqual(pg2.requests, [])

    def test_z_batches_of_500_stay_homogeneous(self):
        pg = FakePostgrest()
        rows = [{"posting_number": f"P{i}", "sku": i, "quantity": 1, "buyer_price": (float(i) if i % 3 else None)} for i in range(1, 1201)]
        self.assertEqual(items.upsert(pg, rows), 1200)
        sizes = sorted((("buyer_price" in u[2]), len(u[4])) for u in pg.upserts(items.TABLE))
        self.assertEqual(sizes, [(False, 400), (True, 300), (True, 500)])
        assert_homogeneous(self, pg)

    def test_fbo_step_with_partial_report_keeps_old_prices(self):
        import ozon_fbo_orders_step as step
        pg = FakePostgrest()
        seed_item(pg, "S-1", 701, 640.50, day="2026-10-25")
        seed_item(pg, "S-2", 702, 700.00, day="2026-10-25")
        postings = [posting("S-1", [product(701)], day="2026-10-25T10:00:00Z"), posting("S-2", [product(702)], day="2026-10-25T10:00:00Z")]

        class Fbo:
            supabase = pg

            def get_fbo_postings(self, days_back):
                return postings

            def build_order_rows(self, postings_, buyer_prices=None):
                return rules.build_order_rows(postings_, "fbo", observed_at=OBSERVED, buyer_prices=buyer_prices)[0]

            def save_orders(self, rows):
                pass

        class Report:
            def fetch_buyer_prices(self, scheme, since, to):
                return {("S-1", "701"): D("650.00")}, {"rows": 1, "skipped": 0, "create": 1, "info": 1, "download": 1, "seconds": 1}

        class Log:
            def dump_raw(self, postings_, schema):
                return "x"
        step.run(fbo_module=Fbo(), log_module=Log(), report_module=Report(), sb=pg, now=NOW,
                 existing_fn=lambda sb, since: {}, state_fn=lambda sb, info: None)
        self.assertEqual((stored(pg, "S-1", 701)["buyer_price"], stored(pg, "S-2", 702)["buyer_price"]), (650.0, 700.0))
        assert_homogeneous(self, pg)


class OrdersBuyerKnownRisk(unittest.TestCase):
    """(ж) marketplace_orders: фиксирует ТЕКУЩЕЕ поведение main (45-я) — частичный отчёт затирает измеренную сумму ключа null.
    Код заказов в 44а не меняется (решение владельца, outbox 44а шаг 1 §3.2). Упадёт, когда поведение изменят, — это сигнал."""

    def test_partial_report_nulls_measured_order_buyer_amount(self):
        pg = FakePostgrest()
        pg.seed("marketplace_orders", [{"order_date": "2026-10-20", "marketplace_code": "ozon", "marketplace_sku": "801", "order_schema": "fbo",
                                        "orders_qty": 1.0, "orders_amount_seller": 1000.0, "orders_amount_buyer": 640.5,
                                        "cancelled_orders_qty": 0.0, "cancelled_orders_amount_seller": 0.0, "cancelled_orders_amount_buyer": 0.0}])
        rows, _ = rules.build_order_rows([posting("O-1", [product(801)])], "fbo", observed_at=OBSERVED, buyer_prices={})
        rules.upsert_orders(pg, rows, "Ozon FBO")
        row = pg.get("marketplace_orders", order_date="2026-10-20", marketplace_code="ozon", marketplace_sku="801", order_schema="fbo")
        self.assertIsNone(row["orders_amount_buyer"])


class StaleCheckIncomplete(unittest.TestCase):
    """44б: окно и пропущенные дни проверки застрявших — в итоге (pipeline_runtime_state) и в алерте."""

    def test_window_and_skipped_days_in_state_and_alert(self):
        pg = FakePostgrest()
        seed_item(pg, "OLD", 901, None)                                   # 2026-10-20 — день без построенных строк этой ночи
        p = [posting("NEW", [product(902)], day="2026-10-21T10:00:00Z")]
        order_rows, _ = rules.build_order_rows(p, "fbo", observed_at=OBSERVED)
        states = []
        info = items.write(pg, p, "fbo", order_rows, now_utc=NOW, observed_at=OBSERVED, state_fn=lambda sb, s, i: states.append(i))
        self.assertEqual(info["stale_candidates"], 0)
        self.assertEqual(info["stale_skipped_days"], ["2026-10-20"])
        self.assertEqual(info["stale_window"], ["2026-10-08", "2026-11-05"])
        self.assertEqual(states[0]["stale_skipped_days"], ["2026-10-20"])
        fresh = (NOW - timedelta(hours=1)).isoformat()
        line = items.alert_line({"fbo": dict(info, finished_at=fresh)}, now=NOW)
        self.assertEqual(line, "⚠️ отправления × товар Ozon: FBO проверка застрявших ключей неполная: пропущено дней 1")
        self.assertEqual(items.alert_line({"fbo": dict(info, finished_at=fresh, stale_skipped_days=[])}, now=NOW), "")

    def test_skipped_days_shown_next_to_candidates(self):
        fresh = (NOW - timedelta(hours=1)).isoformat()
        line = items.alert_line({"fbs": {"ok": True, "identity_diffs": 0, "stale_candidates": 2, "stale_skipped_days": ["2026-10-20", "2026-10-21"],
                                         "finished_at": fresh}}, now=NOW)
        self.assertIn("FBS застрявшие ключи: кандидатов 2", line)
        self.assertIn("FBS проверка застрявших ключей неполная: пропущено дней 2", line)


# ---------- история: (в), (и), точные исключения сверки ----------

import ozon_posting_items_history as hist  # noqa: E402

RBH1 = SimpleNamespace(CODE="rbh1", DISPLAY_NAME="РБХ-1")


def hposting(number, day, sku, qty=1, price="602.00", status="delivered"):
    return {"posting_number": number, "status": status, "created_at": day, "in_process_at": day,
            "products": [{"sku": sku, "offer_id": "A", "quantity": qty, "price": {"amount": price, "currency": "RUB"}}]}


def order_row(day, sku, qty, amount, schema="fbo"):
    return {"order_date": day, "marketplace_code": "ozon", "marketplace_sku": str(sku), "order_schema": schema,
            "orders_qty": float(qty), "orders_amount_seller": float(amount), "cancelled_orders_qty": 0.0, "cancelled_orders_amount_seller": 0.0,
            "orders_amount_buyer": None, "cancelled_orders_amount_buyer": None}


class HistoryKeepsPrice(unittest.TestCase):
    """(в): история тем же файлом и более свежим без цен — ни одна измеренная цена не стала NULL; «та же ночь» (+5 с)."""

    def put(self, d, name, fetched_at, postings):
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            json.dump({"schema": "fbo", "fetched_at": fetched_at, "postings": postings}, fh)

    def test_c_history_files_without_prices_never_null_a_measured_price(self):
        pg = FakePostgrest()
        seed_item(pg, "H-1", 111, 640.50, day="2026-10-19", observed="2026-10-20T00:40:00+00:00")
        with tempfile.TemporaryDirectory() as d:
            self.put(d, "fbo_20261020T004005Z.json", "2026-10-20T00:40:05+00:00", [hposting("H-1", "2026-10-19T10:00:00Z", 111)])  # «та же ночь» +5 с
            for i in range(2):                                               # повтор тем же файлом
                rows, *_ = hist.build_rows(d, "fbo", "2026-10-19", "2026-10-19")
                keep, _skip = hist.newer_in_table(rows, {("H-1", "111"): stored(pg, "H-1", 111)["observed_at"]})
                self.assertEqual(len(keep), 1 if i == 0 else 0)              # 1-й раз файл новее строки; повтор — строка = файлу
                items.upsert(pg, keep)
                self.assertEqual(stored(pg, "H-1", 111)["buyer_price"], 640.5)
            self.put(d, "fbo_20261101T004000Z.json", "2026-11-01T00:40:00+00:00",
                     [hposting("H-1", "2026-10-19T10:00:00Z", 111, status="cancelled")])   # более свежий файл без цены
            rows, *_ = hist.build_rows(d, "fbo", "2026-10-19", "2026-10-19")
            keep, _skip = hist.newer_in_table(rows, {("H-1", "111"): stored(pg, "H-1", 111)["observed_at"]})
            items.upsert(pg, keep)
        row = stored(pg, "H-1", 111)
        self.assertEqual((row["buyer_price"], row["status"]), (640.5, "cancelled"))
        assert_homogeneous(self, pg)


class ExactDiffExceptions(unittest.TestCase):
    """46а п. 4: исключения сверки — только две точные строки rbh1 / fbo / SKU 365053838."""

    def test_only_the_two_exact_rows_pass(self):
        ok = [("2026-09-30", "365053838", "fbo", D(1), D("602.00")), ("2026-10-01", "365053838", "fbo", D(1), D("659.00"))]
        self.assertEqual(hist.blocking_diffs("rbh1", ok), [])
        for bad in [("2026-09-30", "365053838", "fbo", D(1), D("601.00")), ("2026-09-30", "365053838", "fbo", D(2), D("602.00")),
                    ("2026-09-30", "365053839", "fbo", D(1), D("602.00")), ("2026-10-02", "365053838", "fbo", D(1), D("602.00")),
                    ("2026-09-30", "365053838", "fbs", D(1), D("602.00")), ("2026-10-01", "365053838", "fbo", D(1), D("602.00")),
                    ("2026-09-30", "365053838", "fbo", D(-1), D("-602.00"))]:
            self.assertEqual(hist.blocking_diffs("rbh1", [bad]), [bad], bad)
        self.assertEqual(hist.blocking_diffs("rbh2", ok[:1]), ok[:1])
        self.assertEqual(hist.blocking_diffs("karatov", ok[1:]), ok[1:])


class HistoryApplyBlocked(unittest.TestCase):
    """(и): --apply с расхождением вне точных исключений — 0 записей; точное исключение — запись идёт, отправления не выкинуты."""

    def run_plan(self, orders, apply=True):
        pg = FakePostgrest()
        pg.seed("marketplace_orders", orders)
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "history_fbo_2026-09-30_2026-09-30.json"), "w", encoding="utf-8") as fh:
                json.dump({"schema": "fbo", "fetched_at": "2026-10-02T00:00:00+00:00",
                           "postings": [hposting("R-1", "2026-09-30T10:00:00Z", 365053838), hposting("R-2", "2026-09-30T11:00:00Z", 365053838),
                                        hposting("R-3", "2026-09-30T12:00:00Z", 1001, price="100.00")]}, fh)
            out = []
            with mock.patch.object(hist, "CABINET", RBH1):
                res = hist.plan(pg, "fbo", "2026-09-30", "2026-09-30", d, apply=apply, out=out.append)
        return res, pg, out

    def test_exact_exception_passes_and_rows_are_kept(self):
        res, pg, out = self.run_plan([order_row("2026-09-30", 365053838, 1, "602.00"), order_row("2026-09-30", 1001, 1, "100.00")])
        self.assertFalse(res["blocked"])
        self.assertEqual((res["diff_keys"], res["blocking_diffs"]), (1, 0))
        self.assertEqual(sorted(r["posting_number"] for r in pg.rows(items.TABLE)), ["R-1", "R-2", "R-3"])   # отправления дня не выкинуты

    def test_other_delta_or_other_key_blocks_before_any_write(self):
        for orders in ([order_row("2026-09-30", 365053838, 1, "603.00"), order_row("2026-09-30", 1001, 1, "100.00")],     # +1 / +601,00
                       [order_row("2026-09-30", 365053838, 2, "1204.00"), order_row("2026-09-30", 1001, 0, "0.00")]):       # соседний ключ
            res, pg, out = self.run_plan(orders)
            self.assertTrue(res["blocked"])
            self.assertEqual((pg.upserts(), pg.rows(items.TABLE)), ([], []))
            self.assertTrue(any("стоп до записи" in s for s in out))

    def test_plan_without_apply_never_writes(self):
        res, pg, out = self.run_plan([order_row("2026-09-30", 365053838, 1, "603.00")], apply=False)
        self.assertTrue(res["blocked"])
        self.assertEqual(pg.upserts(), [])


if __name__ == "__main__":
    unittest.main()
