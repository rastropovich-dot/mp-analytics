"""Леджер начислений по типам: дата × type_id → сумма со знаком Ozon.

Нужен, чтобы лист по выкупам собирался из одной базы: типы 41 + 54 (реклама) и
25 / 10 (компенсации) в marketplace_expenses не пишутся вовсе, а 1, 51, 96
растворены в статьях. Классификации в леджере нет — тип хранится числом.
"""
import unittest
from collections import defaultdict
from decimal import Decimal
from unittest import mock

import loaders.ozon_expenses_loader as loader
from loaders import ozon_finance_accrual as accrual

DAY = "2026-09-10"


def service(type_id, amount):
    return {"type_id": type_id, "accrued": {"amount": amount, "currency": "RUB"}}


def item_fee(day, sku, *services):
    return {"accrual_id": f"{day}-{sku}-{len(services)}", "date": f"{day}T00:00:00Z", "accrued_category": "ITEM",
            "item_fees": {"fees": [{"sku": sku, "fees": list(services)}]}}


def posting(day, sku, *delivery_services, commission="100.00"):
    return {"accrual_id": f"{day}-p-{sku}", "date": f"{day}T00:00:00Z", "accrued_category": "POSTING", "unit_number": "1-1",
            "posting": {"products": [{"sku": sku, "commission": {"sale_commission": {"amount": commission}, "sale_amount": {"amount": "1000"}, "services": []},
                                      "delivery": {"services": list(delivery_services)}}]}}


ACCRUALS = [
    posting(DAY, 11, service(32, "-50.10"), service(1, "-12.20")),
    posting(DAY, 12, service(32, "-49.90")),
    item_fee(DAY, 11, service(41, "-300.00"), service(54, "-200.00")),
    item_fee(DAY, 13, service(25, "1500.00"), service(10, "-100.00")),
    item_fee(DAY, 14, service(51, "-24.40"), service(96, "-12.20")),
    item_fee(DAY, 15, service(777, "-5.55")),                          # незнакомый тип
    item_fee(DAY, 16, service(38, "-7.00")), item_fee(DAY, 17, service(38, "7.00")),   # свернулся в ноль
    item_fee("2026-09-11", 11, service(32, "-1.00")),
]


def ledger(accruals=ACCRUALS, names=None):
    rows = accrual.build_type_ledger_rows(accruals, names or {32: "Logistic", 41: "ClickPromotion"}, loaded_at="t")
    return {(r["accrual_date"], r["type_id"]): r for r in rows}


class LedgerRowsTests(unittest.TestCase):
    def test_sums_keep_the_sign_ozon_sends(self):
        rows = ledger()
        self.assertEqual(rows[(DAY, 32)]["amount"], -100.00)      # списание с продавца — отрицательно
        self.assertEqual(rows[(DAY, 32)]["lines"], 2)
        self.assertEqual(rows[(DAY, 25)]["amount"], 1500.00)      # начисление продавцу — положительно
        self.assertEqual(rows[(DAY, 10)]["amount"], -100.00)
        self.assertEqual(rows[("2026-09-11", 32)]["amount"], -1.00)

    def test_every_type_is_kept_without_classification(self):
        rows = ledger()
        self.assertEqual({t for (d, t) in rows if d == DAY}, {32, 1, 41, 54, 25, 10, 51, 96, 777, 38})
        self.assertEqual(rows[(DAY, 777)]["amount"], -5.55)       # незнакомый тип хранится числом, без else

    def test_zero_sum_is_written_because_absent_is_not_zero(self):
        rows = ledger()
        self.assertEqual((rows[(DAY, 38)]["amount"], rows[(DAY, 38)]["lines"]), (0.0, 2))

    def test_names_come_from_the_type_dictionary_and_may_be_missing(self):
        rows = ledger()
        self.assertEqual((rows[(DAY, 32)]["type_name"], rows[(DAY, 777)]["type_name"]), ("Logistic", None))
        self.assertEqual(rows[(DAY, 32)]["loaded_at"], "t")

    def test_ads_and_compensations_are_in_the_ledger_but_still_not_in_expenses(self):
        expense_rows, _counters, _unknown = accrual.build_expense_rows(ACCRUALS)
        by_type = defaultdict(Decimal)
        for r in expense_rows:
            by_type[r["expense_type"]] += Decimal(str(r["expense_amount"]))
        self.assertEqual(set(by_type), {"commission", "logistics", "other", "subscription", "unknown_777"})
        # логистика 50,10 + 49,90 + 1,00; прочее — типы 1 и 96 (тип 38 свернулся в ноль); 41, 54, 25, 10 сюда не попали
        self.assertEqual((by_type["logistics"], by_type["other"], by_type["subscription"], by_type["unknown_777"]),
                         (Decimal("101.00"), Decimal("24.40"), Decimal("24.40"), Decimal("5.55")))
        rows = ledger()
        self.assertEqual((rows[(DAY, 41)]["amount"], rows[(DAY, 54)]["amount"]), (-300.00, -200.00))

    def test_ledger_folded_into_articles_equals_expenses_with_the_sign_flipped(self):
        """Тот самый арбитр посева: Σ леджера по типам статьи × (−1) = статья расходов."""
        expense_rows, _c, _u = accrual.build_expense_rows(ACCRUALS)
        expenses = defaultdict(Decimal)
        for r in expense_rows:
            if r["expense_type"] != "commission":
                expenses[(r["expense_date"], r["expense_type"])] += Decimal(str(r["expense_amount"]))
        folded = defaultdict(Decimal)
        for (day, type_id), r in ledger().items():
            if type_id in accrual.AD_TYPE_IDS or type_id in accrual.UNCLASSIFIED_TYPE_IDS:
                continue
            folded[(day, accrual.TYPE_TO_EXPENSE.get(type_id, f"unknown_{type_id}"))] -= Decimal(str(r["amount"]))
        self.assertEqual({k: v for k, v in folded.items() if v}, dict(expenses))

    def test_lines_without_a_date_or_a_type_are_named_not_swallowed(self):
        no_date = item_fee(DAY, 1, service(32, "-1.00"))
        no_date["date"] = None
        broken = [no_date, item_fee(DAY, 2, service(None, "-2.00")), item_fee(DAY, 3, service(32, "-3.00"))]
        with mock.patch("builtins.print") as printed:
            rows = accrual.build_type_ledger_rows(broken)
        self.assertEqual([(r["type_id"], r["amount"]) for r in rows], [(32, -3.00)])
        self.assertIn("без даты 1", printed.call_args[0][0])
        self.assertIn("без type_id 1", printed.call_args[0][0])


class FakeTable:
    def __init__(self, client, name):
        self.client, self.name = client, name

    def upsert(self, rows, **kw):
        self.rows, self.kw = rows, kw
        return self

    def execute(self):
        if self.name in self.client.failing:
            raise RuntimeError(f"relation {self.name} does not exist")
        self.client.written[self.name].extend(self.rows)
        self.client.conflict[self.name] = self.kw.get("on_conflict")
        return type("R", (), {"data": self.rows})()


class FakeClient:
    def __init__(self):
        self.written, self.conflict, self.failing = defaultdict(list), {}, set()

    def table(self, name):
        return FakeTable(self, name)


class NightlyStepTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.fetches = []
        self.window = {"day_from": "2026-08-23", "day_to": "2026-09-22", "days": 31, "pages": 62, "requests": 62, "retries": 0, "failures": 0, "complete": True}
        self.cleanups = {"expenses": mock.Mock(return_value=0), "ledger": mock.Mock(return_value=0)}
        for target, attr, value in ((loader, "supabase", self.client),
                                    (accrual, "fetch_window_checked", lambda days_back=30: self.fetches.append(days_back) or (ACCRUALS, self.window)),
                                    (accrual, "load_accrual_types", lambda: {32: "Logistic"}),
                                    (loader, "cleanup_stale_expenses", self.cleanups["expenses"]),
                                    (loader, "cleanup_stale_ledger", self.cleanups["ledger"])):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def test_one_fetch_feeds_both_expenses_and_the_ledger(self):
        loader.run()
        self.assertEqual(self.fetches, [30])
        self.assertTrue(self.client.written["marketplace_expenses"])
        ledger_rows = self.client.written["ozon_accrual_daily_types"]
        self.assertEqual(len(ledger_rows), 11)
        self.assertEqual(self.client.conflict["ozon_accrual_daily_types"], "accrual_date,type_id")
        # чистка застрявших ключей — после записи, с паспортом того же сбора и построенными строками
        self.cleanups["expenses"].assert_called_once()
        window, rows, apply = self.cleanups["expenses"].call_args[0]
        self.assertEqual((window, len(rows), apply), (self.window, len(self.client.written["marketplace_expenses"]), True))
        self.cleanups["ledger"].assert_called_once()
        self.assertEqual((self.cleanups["ledger"].call_args[0][0], len(self.cleanups["ledger"].call_args[0][1]), self.cleanups["ledger"].call_args[0][2]), (self.window, 11, True))

    def test_dry_run_writes_nothing_and_plans_cleanup_without_apply(self):
        with mock.patch("builtins.print"):
            loader.run(apply=False)
        self.assertFalse(self.client.written["marketplace_expenses"])
        self.assertFalse(self.client.written["ozon_accrual_daily_types"])
        self.assertEqual(self.cleanups["expenses"].call_args[0][2], False)
        self.assertEqual(self.cleanups["ledger"].call_args[0][2], False)

    def test_failed_cleanup_does_not_fail_the_step(self):
        self.cleanups["expenses"].side_effect = RuntimeError("boom")
        self.cleanups["ledger"].side_effect = RuntimeError("boom")
        with mock.patch("builtins.print") as printed:
            loader.run()
        self.assertTrue(self.client.written["marketplace_expenses"])
        self.assertTrue(self.client.written["ozon_accrual_daily_types"])
        self.assertTrue(any("чистка застрявших ключей marketplace_expenses не выполнена" in str(c.args[0]) for c in printed.call_args_list))
        self.assertTrue(any("чистка застрявших ключей ozon_accrual_daily_types не выполнена" in str(c.args[0]) for c in printed.call_args_list))

    def test_a_failing_ledger_write_does_not_stop_the_expenses_step(self):
        self.client.failing.add("ozon_accrual_daily_types")
        with mock.patch("builtins.print") as printed:
            loader.run()                                        # не падает
        self.assertTrue(self.client.written["marketplace_expenses"])
        self.assertFalse(self.client.written["ozon_accrual_daily_types"])
        self.assertTrue(any("НЕ записан" in str(c.args[0]) for c in printed.call_args_list))

    def test_a_failing_expenses_write_still_fails_the_step(self):
        self.client.failing.add("marketplace_expenses")
        with self.assertRaises(RuntimeError):
            loader.run()
        self.assertFalse(self.client.written["ozon_accrual_daily_types"])


if __name__ == "__main__":
    unittest.main()
