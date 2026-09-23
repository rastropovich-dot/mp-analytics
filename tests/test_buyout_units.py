"""Штуки выкупов: quantity из accrual/postings, сведённая построчно с by-day.

Позиции считает by-day (у товарной строки количества нет), штуки — строка типа 69. Сводим по
равенствам seller_price × quantity = sale_amount и accrued = sale_commission со знаком; не свелось —
ключ остаётся «не измерен» (null), а не получает позиции или ноль.
"""
import unittest
from collections import defaultdict
from unittest import mock

import run_daily_pipeline as pipeline
from loaders import ozon_buyout_units as bu

DAY = "2026-09-10"


def byday(number, sku, sale_amount, sale_commission, day=DAY):
    return {"accrued_category": "POSTING", "date": f"{day}T00:00:00Z", "unit_number": number,
            "posting": {"products": [{"sku": sku, "commission": {"sale_amount": {"amount": sale_amount}, "sale_commission": {"amount": sale_commission}}}]}}


def line69(sku, price, qty, accrued, day=DAY):
    return {"type_id": 69, "sku": sku, "accrual_date": day, "quantity": qty, "seller_price": {"amount": price}, "accrued": {"amount": accrued}}


ACCRUALS = [byday("A-1", 11, "3000.00", "-1200.00"),        # одна позиция, три штуки
            byday("B-1", 11, "1000.00", "-400.00"),         # тот же sku, одна штука
            byday("C-1", 12, "-500.00", "200.00"),          # возврат: штуки со знаком минус
            byday("D-1", 13, "0", "0")]                     # пустая строка — не выкуп
POSTINGS = [{"posting_number": "A-1", "accruals": [line69(11, "1000.00", 3, "-1200.00"), {"type_id": 32, "sku": 11, "accrual_date": DAY, "accrued": {"amount": "-50"}}]},
            {"posting_number": "B-1", "accruals": [line69(11, "1000.00", 1, "-400.00")]},
            # возврат у Ozon: seller_price отрицательна, quantity положительна (сырьё 2026-09-16 … 09-19)
            {"posting_number": "C-1", "accruals": [line69(12, "-500.00", 1, "200.00")]}]


class UnitsTests(unittest.TestCase):
    def test_units_are_signed_and_summed_per_day_and_sku(self):
        units, c, unmatched, positions = bu.units_by_key(ACCRUALS, POSTINGS)
        self.assertEqual(units, {(DAY, "11"): 4, (DAY, "12"): -1})
        self.assertEqual(positions, {(DAY, "11"): 2, (DAY, "12"): -1})
        self.assertEqual((c["rows"], c["matched_rows"], c["keys_unmeasured"], c["rows_with_2_plus"]), (3, 3, 0, 1))
        self.assertEqual(unmatched, {})

    def test_a_row_that_does_not_match_leaves_its_key_unmeasured(self):
        """Не свелось — не угадываем: ни позиций, ни нуля, ни частичной суммы по ключу."""
        postings = [POSTINGS[0], {"posting_number": "B-1", "accruals": [line69(11, "999.00", 1, "-400.00")]}, POSTINGS[2]]
        units, c, unmatched, _ = bu.units_by_key(ACCRUALS, postings)
        self.assertEqual(units, {(DAY, "12"): -1})                 # ключ (день, 11) выпал целиком, хотя A-1 свёлся
        self.assertEqual(unmatched, {(DAY, "11"): 1})
        self.assertEqual((c["keys_measured"], c["keys_unmeasured"]), (1, 1))

    def test_one_type_69_line_serves_one_byday_row(self):
        twice = [byday("A-1", 11, "1000.00", "-400.00"), byday("A-1", 11, "1000.00", "-400.00")]
        units, c, unmatched, _ = bu.units_by_key(twice, [{"posting_number": "A-1", "accruals": [line69(11, "1000.00", 1, "-400.00")]}])
        self.assertEqual((units, unmatched), ({}, {(DAY, "11"): 1}))

    def test_sold_postings_follow_the_buyout_rows_rule(self):
        self.assertEqual(bu.sold_postings(ACCRUALS), ["A-1", "B-1", "C-1"])

    def test_only_existing_rows_are_written_and_only_changes(self):
        units = {(DAY, "11"): 4, (DAY, "12"): -1, (DAY, "99"): 2}
        existing = {(DAY, "11"): None, (DAY, "12"): -1, (DAY, "77"): None}
        to_write, same, no_row, no_units = bu.plan_update(units, existing)
        self.assertEqual((to_write, same), ({(DAY, "11"): 4}, 1))
        self.assertEqual((no_row, no_units), ([(DAY, "99")], [(DAY, "77")]))

    def test_write_sends_the_key_and_the_units_column_only(self):
        sent = []

        class T:
            def upsert(self, rows, **kw):
                sent.append((rows, kw)); return self
            def execute(self):
                return None
        bu.write_units(type("SB", (), {"table": lambda self, name: T()})(), {(DAY, "11"): 4})
        self.assertEqual(sent[0][0], [{"buyout_date": DAY, "marketplace_code": "ozon", "marketplace_sku": "11", "buyouts_units": 4}])
        self.assertEqual(sent[0][1]["on_conflict"], "buyout_date,marketplace_code,marketplace_sku")

    def test_429_is_waited_out_three_times_then_fails_loudly(self):
        resp = mock.Mock(status_code=429, text="rate limit")
        with mock.patch.object(bu.http_retry, "post", return_value=resp), mock.patch.object(bu.time, "sleep") as sleep:
            counters = defaultdict(int)
            with self.assertRaises(RuntimeError):
                bu.fetch_postings(["A-1"], counters)
        self.assertEqual((counters["requests"], counters["429"]), (3, 3))
        self.assertEqual(sum(1 for c in sleep.call_args_list if c.args[0] == 60), 3)

    def test_429_retried_inside_http_retry_is_counted(self):
        # Ручной прогон 2026-09-23: http_retry повторил десять 429 по 1 с, а шаг напечатал «пауз 429 — 0»
        def rate_limited(code):
            r = mock.Mock(status_code=code, text="")
            r.json.return_value = {"code": 8} if code == 429 else {"posting_accruals": [{"posting_number": "A-1"}]}
            r.headers = {}
            return r
        session = mock.Mock()
        session.request.side_effect = [rate_limited(429), rate_limited(429), rate_limited(200)]
        real_post = bu.http_retry.post

        def post(url, **kw):
            return real_post(url, session=session, sleep_fn=lambda s: None, **kw)
        with mock.patch.object(bu.http_retry, "post", side_effect=post), mock.patch.object(bu.time, "sleep"), \
                mock.patch.object(bu.accrual, "headers", return_value={}), mock.patch("builtins.print"):
            counters = {}
            out = bu.fetch_postings(["A-1"], counters)
        self.assertEqual(len(out), 1)
        self.assertEqual((counters["requests"], counters["http"], counters["retries"]), (1, 3, 2))
        self.assertEqual((counters["429"], counters["antispam_429"]), (2, 0))
        self.assertEqual(counters["reasons"], {"rate_limit_per_second": 2})

    def test_count_429_sees_retries_and_last_response(self):
        stats = {"reasons": {"rate_limit_per_second": 3, "http_502": 1}}
        self.assertEqual(bu.http_retry.count_429(stats), 3)
        self.assertEqual(bu.http_retry.count_429(stats, mock.Mock(status_code=429)), 4)
        self.assertEqual(bu.http_retry.count_429({}, mock.Mock(status_code=200)), 0)


class StepTests(unittest.TestCase):
    def test_dry_run_writes_nothing_and_tells_zero_keys_from_real_ones(self):
        import importlib.util, os
        spec = importlib.util.spec_from_file_location("units_step", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "ozon_buyout_units_step.py"))
        step = importlib.util.module_from_spec(spec); spec.loader.exec_module(step)
        zero_pair = [byday("Z-1", 20, "100.00", "-40.00"), byday("Z-2", 20, "-100.00", "40.00")]        # продажа и возврат в один день
        postings = POSTINGS + [{"posting_number": "Z-1", "accruals": [line69(20, "100.00", 1, "-40.00")]}, {"posting_number": "Z-2", "accruals": [line69(20, "-100.00", 1, "40.00")]}]
        fake_accrual = type("A", (), {"fetch_window": staticmethod(lambda days_back=30: ACCRUALS + zero_pair)})
        fake_units = type("U", (), {"CHUNK": 200, "sold_postings": staticmethod(bu.sold_postings), "units_by_key": staticmethod(bu.units_by_key),
                                    "plan_update": staticmethod(bu.plan_update), "fetch_postings": staticmethod(lambda numbers, counters: postings),
                                    "read_existing_keys": staticmethod(lambda sb, d1, d2: {(DAY, "11"): None}),
                                    "write_units": staticmethod(lambda sb, rows: self.fail("сухой прогон не пишет"))})
        with mock.patch("builtins.print") as printed:
            self.assertEqual(step.run(None, dry_run=True, accrual_module=fake_accrual, units_module=fake_units), 0)
        text = "\n".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertIn("штуки без строки выкупа 2 (из них НЕНУЛЕВЫХ 1)", text)     # (день, 12) — настоящий, (день, 20) — свернулся в ноль
        self.assertIn("db_writes = 0", text)


class PipelineTests(unittest.TestCase):
    def test_step_runs_right_after_buyouts_and_is_non_fatal(self):
        titles = [t for t, _cmd in pipeline.build_steps()] if hasattr(pipeline, "build_steps") else None
        self.assertIn("Ozon: штуки выкупов", pipeline.NON_FATAL_STEPS)
        if titles:
            self.assertEqual(titles.index("Ozon: штуки выкупов"), titles.index("Ozon: дневные финоперации") + 1)


if __name__ == "__main__":
    unittest.main()
