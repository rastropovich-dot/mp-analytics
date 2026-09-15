"""Лог статусов: пишутся только изменения; дата когорты — как у загрузчиков заказов."""
import unittest
from decimal import Decimal

from loaders import ozon_posting_status_log as log


def _fbo(number, status, created="2026-09-10T08:00:00Z", price="1000", qty=1, cancel=None):
    p = {"posting_number": number, "status": status, "substatus": f"posting_{status}", "created_at": created,
         "in_process_at": created, "products": [{"sku": 1, "quantity": qty, "price": {"amount": price, "currency": "RUB"}}]}
    if cancel:
        p["cancel_reason_id"] = 505
        p["cancellation"] = {"cancel_reason": "…", "cancellation_type": cancel, "cancellation_initiator": "Клиент"}
    return p


class ObservationRowsTests(unittest.TestCase):
    def test_first_observation_is_written_with_null_previous(self):
        rows, c = log.observation_rows([_fbo("A-1", "delivering")], "fbo", "2026-09-15T18:59:11+00:00", "nightly", {})
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["posting_number"], r["status"], r["previous_status"], r["schema"]), ("A-1", "delivering", None, "fbo"))
        self.assertEqual(r["order_date"], "2026-09-10")
        self.assertEqual(r["amount"], "1000.00")
        self.assertEqual(c["new"], 1)

    def test_unchanged_status_writes_nothing(self):
        rows, c = log.observation_rows([_fbo("A-1", "delivering")], "fbo", "t", "nightly", {"A-1": "delivering"})
        self.assertEqual(rows, [])
        self.assertEqual(c["unchanged"], 1)

    def test_change_to_cancelled_keeps_reason_and_previous(self):
        rows, c = log.observation_rows([_fbo("A-1", "cancelled", cancel="Client")], "fbo", "t", "nightly", {"A-1": "delivering"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["previous_status"], "delivering")
        self.assertEqual(rows[0]["cancel_reason_id"], 505)
        self.assertEqual(rows[0]["cancellation_type"], "Client")
        self.assertIsNone(rows[0]["cancelled_after_ship"])
        self.assertEqual(c["changed"], 1)

    def test_fbs_uses_in_process_at_and_keeps_cancelled_after_ship(self):
        p = {"posting_number": "B-1", "status": "cancelled", "in_process_at": "2026-09-01T21:30:00Z", "shipment_date": "2026-09-03T00:00:00Z",
             "cancellation": {"cancel_reason_id": 506, "cancellation_type": "client", "cancelled_after_ship": True},
             "products": [{"sku": 2, "quantity": 2, "price": "500.00"}]}
        rows, _ = log.observation_rows([p], "fbs", "t", "nightly", {})
        self.assertEqual(rows[0]["order_date"], "2026-09-02")   # 21:30 UTC = 00:30 МСК следующего дня
        self.assertTrue(rows[0]["cancelled_after_ship"])
        self.assertEqual(rows[0]["cancel_reason_id"], 506)
        self.assertEqual(rows[0]["amount"], "1000.00")

    def test_duplicate_posting_in_one_response_is_counted_once(self):
        rows, c = log.observation_rows([_fbo("A-1", "delivering"), _fbo("A-1", "delivering")], "fbo", "t", "nightly", {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(c["duplicates"], 1)

    def test_posting_without_date_is_skipped_and_counted(self):
        p = {"posting_number": "C-1", "status": "delivering", "products": []}
        rows, c = log.observation_rows([p], "fbo", "t", "nightly", {})
        self.assertEqual(rows, [])
        self.assertEqual(c["no_date"], 1)


class AmountTests(unittest.TestCase):
    def test_amount_sums_price_times_quantity_over_products(self):
        p = {"products": [{"sku": 1, "quantity": 2, "price": {"amount": "10.50"}}, {"sku": 2, "quantity": 1, "price": "5"}]}
        self.assertEqual(log.posting_amount(p), Decimal("26.00"))


if __name__ == "__main__":
    unittest.main()


class PipelineWiringTests(unittest.TestCase):
    def test_log_step_follows_the_order_steps_and_is_non_fatal(self):
        import run_daily_pipeline as pipeline
        titles = [t for t, _ in pipeline.build_steps()]
        cmds = dict(pipeline.build_steps())
        self.assertLess(titles.index("Ozon: загрузка FBO заказов"), titles.index("Ozon: лог статусов отправлений"))
        self.assertLess(titles.index("Ozon: загрузка FBS заказов"), titles.index("Ozon: лог статусов отправлений"))
        self.assertIn("Ozon: лог статусов отправлений", pipeline.NON_FATAL_STEPS)
        self.assertIn("ozon_fbo_orders_step.py", cmds["Ozon: загрузка FBO заказов"])
        self.assertIn("ozon_fbs_orders_step.py", cmds["Ozon: загрузка FBS заказов"])
        self.assertIn("--apply", cmds["Ozon: лог статусов отправлений"])
