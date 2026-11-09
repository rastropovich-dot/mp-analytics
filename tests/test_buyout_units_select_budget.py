"""Штуки выкупов, варианты C и D разбора 08.11 (замер rbh1: 24 549 отправлений → 123 обращения, 48 × 429, обрыв).

C — спрашиваем accrual/postings только по отправлениям ключей без штук и ключей свежих дней; измеренные старые ключи не
перезаписываются. D — бюджет ожиданий по 429 и времени шага: кончился → стоп с причиной без RuntimeError, ничего не пишется.
Строка ratelimit-remaining печатается до выхода из fetch_postings при любом исходе. Сеть не нужна: http_retry.post подставной.
"""
import importlib.util
import os
import unittest
from unittest import mock

from loaders import ozon_buyout_units as bu

OLD, NEW = "2026-10-10", "2026-11-08"


def byday(number, products, day):
    """products: [(sku, sale_amount, sale_commission)]"""
    return {"accrued_category": "POSTING", "date": f"{day}T00:00:00Z", "unit_number": number,
            "posting": {"products": [{"sku": s, "commission": {"sale_amount": {"amount": a}, "sale_commission": {"amount": c}}}
                                     for s, a, c in products]}}


def line69(sku, price, qty, accrued, day):
    return {"type_id": 69, "sku": sku, "accrual_date": day, "quantity": qty, "seller_price": {"amount": price}, "accrued": {"amount": accrued}}


ACCRUALS = [
    byday("A-1", [(11, "2000.00", "-800.00")], OLD),                          # старый ключ (OLD, 11) — измерен
    byday("B-1", [(11, "1000.00", "-400.00"), (12, "500.00", "-200.00")], OLD),  # смешанное: измеренный 11 и неизмеренный 12
    byday("C-1", [(13, "300.00", "-120.00")], NEW),                           # свежий день — измерен, но спрашиваем
]
POSTINGS = {
    "A-1": {"posting_number": "A-1", "accruals": [line69(11, "1000.00", 2, "-800.00", OLD)]},
    "B-1": {"posting_number": "B-1", "accruals": [line69(11, "1000.00", 1, "-400.00", OLD), line69(12, "250.00", 2, "-200.00", OLD)]},
    "C-1": {"posting_number": "C-1", "accruals": [line69(13, "100.00", 3, "-120.00", NEW)]},
}
EXISTING = {(OLD, "11"): 2, (OLD, "12"): None, (NEW, "13"): 1}


def load_step():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "ozon_buyout_units_step.py")
    spec = importlib.util.spec_from_file_location("units_step_cd", path)
    step = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(step)
    return step


class Clock:
    def __init__(self, start=100.0):
        self.now = start

    def __call__(self):
        return self.now


class SelectPostings(unittest.TestCase):
    def test_only_unmeasured_and_recent_keys_are_asked(self):
        numbers, need, sel = bu.select_postings(ACCRUALS, EXISTING, NEW, recent_days=3)
        self.assertEqual(numbers, ["B-1", "C-1"])                       # A-1 — только измеренный старый ключ, не спрашиваем
        self.assertEqual(need, {(OLD, "12"), (NEW, "13")})
        self.assertEqual((sel["postings_total"], sel["postings_need"], sel["keys_total"], sel["keys_need"], sel["keys_measured_skipped"]),
                         (3, 2, 3, 2, 1))
        self.assertEqual(sel["recent_from"], "2026-11-06")

    def test_key_without_a_buyout_row_is_asked(self):
        numbers, need, _ = bu.select_postings(ACCRUALS, {(OLD, "11"): 2}, NEW, recent_days=1)
        self.assertIn((OLD, "12"), need)                                # строки выкупа нет — «штуки без строки» остаются сигналом
        self.assertEqual(numbers, ["B-1", "C-1"])


class StepWithSelection(unittest.TestCase):
    def run_step(self, dry_run=False, fetch=None, existing=None):
        step = load_step()
        asked, written = [], []

        def fake_fetch(numbers, counters, budget=None):
            asked.extend(numbers)
            return [POSTINGS[n] for n in numbers]
        fake_accrual = type("A", (), {"fetch_window": staticmethod(lambda days_back=30: ACCRUALS)})
        fake_units = type("U", (), {"CHUNK": 200, "sold_postings": staticmethod(bu.sold_postings), "units_by_key": staticmethod(bu.units_by_key),
                                    "select_postings": staticmethod(bu.select_postings), "Budget": bu.Budget, "BudgetExhausted": bu.BudgetExhausted,
                                    "plan_update": staticmethod(bu.plan_update), "fetch_postings": staticmethod(fetch or fake_fetch),
                                    "read_existing_keys": staticmethod(lambda sb, d1, d2: dict(existing if existing is not None else EXISTING)),
                                    "write_units": staticmethod(lambda sb, rows: written.append(dict(rows)) or len(rows))})
        with mock.patch("builtins.print") as printed:
            result = step.run(None, dry_run=dry_run, accrual_module=fake_accrual, units_module=fake_units)
        text = "\n".join(str(c.args[0]) for c in printed.call_args_list)
        return result, asked, written, text

    def test_measured_old_key_is_not_overwritten_even_inside_an_asked_posting(self):
        # B-1 несёт и измеренный (OLD, 11) = 2, и неизмеренный (OLD, 12): по ответу (OLD, 11) получилось бы 1 — не пишем
        result, asked, written, text = self.run_step()
        self.assertEqual(asked, ["B-1", "C-1"])
        self.assertEqual(written, [{(OLD, "12"): 2, (NEW, "13"): 3}])
        self.assertNotIn((OLD, "11"), written[0])
        self.assertEqual(result, 2)
        self.assertIn("измеренных ранее и не запрашиваемых 1", text)

    def test_ordinary_dry_run_asks_and_writes_nothing(self):
        result, asked, written, text = self.run_step(dry_run=True)
        self.assertEqual((result, written), (0, []))
        self.assertEqual(asked, ["B-1", "C-1"])
        self.assertIn("db_writes = 0", text)

    def test_budget_stop_writes_nothing_and_says_why(self):
        def exhausted(numbers, counters, budget=None):
            counters["requests"] = 7
            raise bu.BudgetExhausted("ожидания по 429 уже 600 с + 60 с > бюджета 600 с")
        result, asked, written, text = self.run_step(fetch=exhausted)
        step = load_step()
        # стоп — не «успешно»: run() отдаёт BUDGET_STOP, процесс выходит с кодом 3 (п. 37 советника 08.11), записи нет
        self.assertEqual((result, written), (step.BUDGET_STOP, []))
        self.assertEqual(step.exit_code(result), step.BUDGET_STOP_EXIT_CODE)
        self.assertNotEqual(step.BUDGET_STOP_EXIT_CODE, 0)
        self.assertIn("стоп по бюджету шага", text)
        self.assertIn("ничего не записано, db_writes = 0", text)

    def test_ordinary_results_exit_zero(self):
        step = load_step()
        self.assertEqual([step.exit_code(r) for r in (0, 2, 117)], [0, 0, 0])   # число записанных строк — не код процесса


class PipelineSeesTheStop(unittest.TestCase):
    def test_exit_code_3_is_a_failed_non_fatal_step(self):
        """run_daily_pipeline видит невыполненный шаг только по коду процесса: код 3 → failed, Telegram «Шаг не выполнен», прогон дальше."""
        import sys
        import run_daily_pipeline as pipeline
        self.assertFalse(pipeline.is_fatal_step("Ozon: штуки выкупов"))
        command = f'{sys.executable} -c "import sys; print(\'⚠️ Штуки выкупов: стоп по бюджету шага\'); sys.exit(3)"'
        with mock.patch.object(pipeline, "send_failure_alert") as alert, mock.patch.object(pipeline, "prepare_command", side_effect=lambda c: c), \
                mock.patch("builtins.print"):
            result = pipeline.run_step("Ozon: штуки выкупов", command, fatal=False)
        self.assertEqual((result["failed"], result["returncode"]), (True, 3))
        title, returncode, tail = alert.call_args.args[:3]
        self.assertEqual((title, returncode, alert.call_args.kwargs.get("fatal")), ("Ozon: штуки выкупов", 3, False))
        self.assertIn("стоп по бюджету шага", "\n".join(tail))


def _resp(code, body=None):
    r = mock.Mock(status_code=code, text="")
    r.json.return_value = body or {}
    r.headers = {}
    return r


class FetchBudget(unittest.TestCase):
    def test_wait_budget_stops_before_the_pause_that_does_not_fit(self):
        clock = Clock()
        budget = bu.Budget(wait_seconds=100, time_seconds=10_000, clock=clock)
        printed = []
        with mock.patch.object(bu.http_retry, "post", return_value=_resp(429)) as post, mock.patch.object(bu.time, "sleep") as sleep, \
                mock.patch.object(bu.accrual, "headers", return_value={}), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            counters = {}
            with self.assertRaises(bu.BudgetExhausted) as ctx:
                bu.fetch_postings(["A-1"], counters, budget=budget)
        self.assertNotIsInstance(ctx.exception, RuntimeError)
        self.assertEqual((post.call_count, counters["429"], budget.waited), (2, 2, 60))
        self.assertEqual(sum(1 for c in sleep.call_args_list if c.args[0] == 60), 1)
        self.assertTrue(any(line.lstrip().startswith("ratelimit-remaining:") and "бюджет шага" in line for line in printed))

    def test_time_budget_stops_before_the_request(self):
        clock = Clock()
        budget = bu.Budget(wait_seconds=600, time_seconds=60, clock=clock, started=0)   # уже прошло 100 с
        with mock.patch.object(bu.http_retry, "post") as post, mock.patch.object(bu.time, "sleep"), mock.patch("builtins.print"):
            with self.assertRaises(bu.BudgetExhausted) as ctx:
                bu.fetch_postings(["A-1"], {}, budget=budget)
        self.assertEqual(post.call_count, 0)
        self.assertIn("время шага", str(ctx.exception))

    def test_ordinary_path_prints_ratelimit_and_returns_answers(self):
        budget = bu.Budget(clock=Clock())
        printed = []
        ok = _resp(200, {"posting_accruals": [{"posting_number": "A-1"}]})
        with mock.patch.object(bu.http_retry, "post", return_value=ok), mock.patch.object(bu.time, "sleep"), \
                mock.patch.object(bu.accrual, "headers", return_value={}), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            out = bu.fetch_postings(["A-1"], {}, budget=budget)
        self.assertEqual(out, [{"posting_number": "A-1"}])
        self.assertEqual(budget.waited, 0)
        self.assertTrue(any("ratelimit-remaining:" in line for line in printed))

    def test_defaults_come_from_env_names(self):
        self.assertEqual((bu.WAIT_BUDGET_SECONDS, bu.TIME_BUDGET_SECONDS, bu.RECENT_DAYS, bu.PAUSE_SECONDS), (600, 1800, 3, 1.5))


if __name__ == "__main__":
    unittest.main()
