"""Шестьдесят первая §3а: ночные шаги заказов не переписывают день ДО окна частью его отправлений.

Ночь 31.10 у РБХ-1 (окно с 01.10) получила в списке FBO часть отправлений 30.09 и заместила ими полную строку
(30.09, SKU 365053838): 2 штуки → 1. Теперь строки / отправления с order_date раньше первого дня окна в заказы не идут.
Сеть и база не зовутся.
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from loaders import ozon_orders_rows as rules  # noqa: E402

NOW = datetime(2026, 10, 31, 0, 49, tzinfo=timezone.utc)   # 03:49 МСК 31.10 → окно с 01.10


def posting(number, created, sku=365053838, price="660"):
    return {"posting_number": number, "status": "delivered", "created_at": created, "in_process_at": created,
            "products": [{"sku": sku, "offer_id": "A", "quantity": 1, "price": {"amount": price, "currency": "RUB"}}],
            "financial_data": {"products": []}}


POSTINGS = [posting("83938539-0135-4", "2026-09-30T16:02:43Z"),            # 30.09 МСК — день до окна
            posting("55468606-0583-2", "2026-10-01T10:58:59Z", price="659"),
            posting("0137723007-0117-4", "2026-09-30T21:29:19Z", price="628")]   # 00:29 МСК 01.10 — первый день окна


class Boundary(unittest.TestCase):
    def test_rows_before_first_day_dropped(self):
        rows, _ = rules.build_order_rows(POSTINGS, "fbo", observed_at="t")
        kept, dropped = rules.drop_before_window(rows, 30, now_utc=NOW)
        self.assertEqual([r["order_date"] for r in dropped], ["2026-09-30"])
        self.assertEqual([(r["order_date"], r["orders_qty"]) for r in kept], [("2026-10-01", 2.0)])

    def test_postings_before_first_day_dropped(self):
        kept, dropped = rules.drop_postings_before_window(POSTINGS, "fbo", 30, now_utc=NOW)
        self.assertEqual([p["posting_number"] for p in dropped], ["83938539-0135-4"])
        self.assertEqual(len(kept), 2)

    def test_fbs_uses_in_process_at(self):
        p = {"posting_number": "1", "status": "delivered", "in_process_at": "2026-09-30T20:59:00Z", "products": []}
        kept, dropped = rules.drop_postings_before_window([p], "fbs", 30, now_utc=NOW)
        self.assertEqual((len(kept), len(dropped)), (0, 1))
        p["in_process_at"] = "2026-09-30T21:00:00Z"
        kept, dropped = rules.drop_postings_before_window([p], "fbs", 30, now_utc=NOW)
        self.assertEqual((len(kept), len(dropped)), (1, 0))


class Steps(unittest.TestCase):
    def test_fbo_step_writes_only_window_rows_items_get_all(self):
        import ozon_fbo_orders_step as step
        got = {}

        class Fbo:
            supabase = object()

            def get_fbo_postings(self, days_back):
                return POSTINGS

            def build_order_rows(self, postings, buyer_prices=None):
                return rules.build_order_rows(postings, "fbo", observed_at="t")[0]

            def save_orders(self, rows):
                got["saved"] = sorted(r["order_date"] for r in rows)

        class Report:
            def fetch_buyer_prices(self, scheme, since, to):
                return {}, {"rows": 0, "skipped": 0, "create": 1, "info": 1, "download": 1, "seconds": 1.0}

        class Items:
            def write(self, sb, postings, schema, order_rows, **kw):
                got["items_postings"] = len(postings)
                got["identity_rows"] = sorted(r["order_date"] for r in order_rows)
                return {"ok": True}

        class Log:
            def dump_raw(self, postings, schema):
                return "x"
        with mock.patch.object(rules, "utc_now", return_value=NOW):
            step.run(fbo_module=Fbo(), log_module=Log(), report_module=Report(), items_module=Items(),
                     state_fn=lambda sb, info: None, now=NOW)
        self.assertEqual(got["saved"], ["2026-10-01"])
        self.assertEqual(got["items_postings"], 3)                         # отправления × товар — все, с верным ключом
        self.assertEqual(got["identity_rows"], ["2026-09-30", "2026-10-01"])   # тождество — на тех же отправлениях

    def test_fbs_step_saves_only_window_postings(self):
        import ozon_fbs_orders_step as step
        saved = []
        fbs_postings = [dict(p, created_at=None) for p in POSTINGS]

        class Fbs:
            supabase = object()

            def get_ozon_fbs_postings(self, days_back):
                return fbs_postings

            def save_ozon_orders(self, postings):
                saved.extend(p["posting_number"] for p in postings)

        class Items:
            def write(self, sb, postings, schema, order_rows, **kw):
                return {"ok": True}

        class Log:
            def dump_raw(self, postings, schema):
                return "x"
        with mock.patch.object(rules, "utc_now", return_value=NOW):
            step.run(fbs_module=Fbs(), log_module=Log(), items_module=Items())
        self.assertEqual(saved, ["55468606-0583-2", "0137723007-0117-4"])


if __name__ == "__main__":
    unittest.main()
