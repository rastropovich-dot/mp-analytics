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


class OrderStepWrappersTests(unittest.TestCase):
    """Сырьё для лога — после записи заказов и под защитой: его отказ не роняет фатальный шаг."""

    def _fake_log(self, raise_it):
        calls = []

        class FakeLog:
            @staticmethod
            def dump_raw(postings, schema):
                calls.append(("dump", schema, len(postings)))
                if raise_it:
                    raise OSError("No space left on device")
                return f"data/postings_raw/{schema}_x.json"
        return FakeLog, calls

    def test_fbo_orders_are_saved_before_raw_dump_and_dump_failure_does_not_raise(self):
        from scripts import ozon_fbo_orders_step as step
        order = []

        class FakeFbo:
            @staticmethod
            def get_fbo_postings(days_back):
                order.append("fetch"); return [{"posting_number": "A-1"}]

            @staticmethod
            def build_order_rows(postings, buyer_prices=None):
                order.append(("build", buyer_prices)); return [{"row": 1}]

            @staticmethod
            def save_orders(rows):
                order.append("save")

        class FakeReport:
            @staticmethod
            def fetch_buyer_prices(scheme, since, to):
                order.append(("report", scheme)); return {("A-1", "1"): 10}, {"rows": 1, "skipped": 0, "create": 1, "info": 2, "download": 1, "seconds": 9.5}
        fake_log, calls = self._fake_log(raise_it=True)
        step.run(fbo_module=FakeFbo, log_module=fake_log, report_module=FakeReport)
        # отчёт ЛК — после списка и до сборки строк; его цены уходят в build_order_rows
        self.assertEqual(order, ["fetch", ("report", "fbo"), ("build", {("A-1", "1"): 10}), "save"])
        self.assertEqual(calls, [("dump", "fbo", 1)])

    def test_fbs_orders_are_saved_before_raw_dump_and_dump_failure_does_not_raise(self):
        from scripts import ozon_fbs_orders_step as step
        order = []

        class FakeFbs:
            @staticmethod
            def get_ozon_fbs_postings(days_back):
                order.append("fetch"); return [{"posting_number": "B-1"}]

            @staticmethod
            def save_ozon_orders(postings):
                order.append("save")
        fake_log, calls = self._fake_log(raise_it=True)
        step.run(fbs_module=FakeFbs, log_module=fake_log)
        self.assertEqual(order, ["fetch", "save"])
        self.assertEqual(calls, [("dump", "fbs", 1)])

    def test_dump_happens_after_save_when_it_succeeds(self):
        from scripts import ozon_fbo_orders_step as step
        order = []

        class FakeFbo:
            @staticmethod
            def get_fbo_postings(days_back): return []

            @staticmethod
            def build_order_rows(postings, buyer_prices=None): order.append(("build", buyer_prices)); return []

            @staticmethod
            def save_orders(rows): order.append("save")

        class FakeLog:
            @staticmethod
            def dump_raw(postings, schema): order.append("dump"); return "p"

        class BrokenReport:
            @staticmethod
            def fetch_buyer_prices(scheme, since, to): raise RuntimeError("report/info fbo: отчёт не готов за 180 с (status=waiting)")
        step.run(fbo_module=FakeFbo, log_module=FakeLog, report_module=BrokenReport)
        # отчёт ЛК упал — заказы всё равно записаны (цены покупателя пустые → amount_buyer null), шаг не упал
        self.assertEqual(order, [("build", {}), "save", "dump"])


class _CappedQuery:
    """PostgREST в миниатюре: in_ / order / range и потолок 1 000 строк на ответ — как у живого сервера."""
    CAP = 1000

    def __init__(self, rows, calls):
        self.rows, self.calls, self.numbers, self.orders, self.span = rows, calls, None, [], None

    def select(self, _cols): return self
    def in_(self, _col, values): self.numbers = set(values); return self
    def order(self, col, desc=False): self.orders.append((col, desc)); return self
    def range(self, a, b): self.span = (a, b); return self

    def execute(self):
        self.calls.append(len(self.numbers))
        out = [r for r in self.rows if r["posting_number"] in self.numbers]
        for col, desc in reversed(self.orders):
            out.sort(key=lambda r: r[col], reverse=desc)
        if self.span is not None:
            out = out[self.span[0]:self.span[1] + 1]
        class R: pass
        res = R(); res.data = out[:self.CAP]
        return res


class _CappedClient:
    def __init__(self, rows): self.rows, self.calls = rows, []
    def table(self, _name): return _CappedQuery(self.rows, self.calls)


def _history(number, statuses, day0=1):
    return [{"posting_number": number, "observed_at": f"2026-10-{day0 + i:02d}T01:00:00+00:00", "status": s}
            for i, s in enumerate(statuses)]


class LoadLastStatusTests(unittest.TestCase):
    """55-я §4: 500 номеров одним запросом без страниц — при 2–8 строках истории ответ упирается в 1 000 строк."""

    def test_posting_with_eight_history_rows_returns_its_latest_status(self):
        statuses = ["awaiting_packaging", "awaiting_deliver", "delivering", "delivering", "delivering",
                    "delivering", "delivering", "delivered"]
        sb = _CappedClient(_history("A-8", statuses))
        self.assertEqual(log.load_last_status(sb, ["A-8"]), {"A-8": "delivered"})

    def test_tail_of_a_large_batch_is_not_lost(self):
        """600 отправлений × 3 строки = 1 800 строк: прежний читатель (500 номеров, без range) видел 1 000 строк из 1 500
        первой пачки, и треть номеров выглядела «впервые»."""
        rows, numbers = [], []
        for i in range(600):
            number = f"P-{i:04d}"
            numbers.append(number)
            rows += _history(number, ["awaiting_deliver", "delivering", "delivered" if i % 2 else "cancelled"])
        sb = _CappedClient(rows)
        last = log.load_last_status(sb, numbers)
        self.assertEqual(len(last), 600)
        self.assertEqual(last["P-0599"], "delivered")
        self.assertEqual(last["P-0000"], "cancelled")
        self.assertTrue(all(n <= log.LAST_STATUS_NUMBERS for n in sb.calls))

    def test_pages_are_followed_when_one_batch_exceeds_the_cap(self):
        """Даже пачка в 100 номеров может перевалить за 1 000 строк (по 12 строк истории) — тогда идёт вторая страница."""
        rows = []
        for i in range(100):
            rows += _history(f"Q-{i:03d}", ["delivering"] * 11 + ["delivered"], day0=1)
        sb = _CappedClient(rows)
        last = log.load_last_status(sb, [f"Q-{i:03d}" for i in range(100)])
        self.assertEqual(set(last.values()), {"delivered"})
        self.assertEqual(len(last), 100)
        self.assertEqual(len(sb.calls), 2)          # 1 200 строк — две страницы

    def test_old_reader_shape_would_have_lost_the_tail(self):
        """Контроль самого теста: один запрос на 500 номеров без range возвращает не все номера."""
        rows = []
        for i in range(500):
            rows += _history(f"R-{i:03d}", ["delivering", "delivering", "delivered"])
        q = _CappedClient(rows).table("x").select("*").in_("posting_number", [f"R-{i:03d}" for i in range(500)])
        q = q.order("posting_number").order("observed_at", desc=True)
        self.assertLess(len({r["posting_number"] for r in q.execute().data}), 500)


if __name__ == "__main__":
    unittest.main()
