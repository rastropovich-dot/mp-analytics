"""Листы «Заказы WB» в утренней книге Ozon (п. 95 РБХ, вариант 2 из п. 93.4): scripts/book_wb_sheet.py добавляет их в конец
книги теми же функциями report_wb_month, что пишут их в отдельную книгу WB.

Держим: названия листов — report_wb_month.PLATFORMS (у кабинета с буквой «Дискаунтера» три листа, без неё — два: «Заказы WB» и
«Заказы WB Standard»); строки, итог и ячейки листа равны тому, что write_xlsx кладёт в книгу WB; воронка не прочиталась или пуста —
книга без этих листов, причина напечатана, лист «WB - <месяц>» на месте; любая другая ошибка листов заказов лист выкупов не роняет;
недописанные листы заказов из книги убираются; подпись доставки от новых ключей сводки не меняется.

Без базы и без диска: источники — подделки, книга — в памяти (load_workbook и Workbook.save подменены).
"""
import importlib.util
import os
import sys
import unittest
from datetime import date
from decimal import Decimal
from unittest import mock

import openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = Decimal


def load(name, as_name=None):
    spec = importlib.util.spec_from_file_location(as_name or name, os.path.join(ROOT, "scripts", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


book = load("book_wb_sheet")
send = load("send_ozon_month_report")
rep = book.wb_report

DAYS = ["2026-09-01", "2026-09-02"]
TODAY = date(2026, 9, 10)
SB = object()                                   # клиент базы не нужен: все читатели подменены


def row(day, oper="Продажа", code="f000283615", size="17,5", **extra):
    base = {"rrd_id": extra.pop("rrd_id", 1), "rr_date": day, "sale_dt": f"{day}T10:00:00Z", "seller_oper_name": oper,
            "doc_type": "Продажа" if oper == "Продажа" else "", "vendor_code": code, "tech_size": size, "nm_id": 1, "quantity": 1,
            "retail_price_with_disc": None, "retail_amount": None, "for_pay": None, "commission_percent": None, "ppvz_reward": None,
            "rebill_logistic_cost": None, "delivery_service": None, "acquiring_fee": None, "paid_storage": None, "penalty": None,
            "deduction": None, "cashback_discount": None}
    base.update(extra)
    return base


REPORT = [
    row("2026-09-01", retail_price_with_disc="100000", retail_amount="57000", for_pay="55600", commission_percent="40", rrd_id=1),
    row("2026-09-01", retail_price_with_disc="10000", retail_amount="6000", for_pay="5560", commission_percent="40", code="t000000001", rrd_id=2),
    row("2026-09-01", oper="Возврат", retail_price_with_disc="5000", retail_amount="3000", for_pay="2780", commission_percent="40", rrd_id=3),
    row("2026-09-02", oper="Доставка", delivery_service="244", rrd_id=4),
]


def funnel_row(day, nm, code, orders, order_sum, buyouts=0, buyout_sum=0, cancels=0, cancel_sum=0, subject="Серьги"):
    return {"day": day, "nm_id": nm, "vendor_code": code, "subject_name": subject, "order_count": orders, "order_sum": order_sum,
            "buyout_count": buyouts, "buyout_sum": buyout_sum, "cancel_count": cancels, "cancel_sum": cancel_sum}


FUNNEL = [
    funnel_row("2026-09-01", 1, "F000283615", 2, 200000, buyouts=1, buyout_sum=90000),
    funnel_row("2026-09-01", 2, "t000000001", 1, 50000, cancels=1, cancel_sum=50000),
    funnel_row("2026-09-01", 3, "F000009483", 0, 0),
    funnel_row("2026-09-02", 4, "X1", 1, 1000),
]
ADS = {"2026-09-01": D("1220")}


def cost_fn(code, size):
    """(себестоимость, ступень) — договор cost_fn читателей report_wb_month: СС есть только у артикулов f…"""
    return (D("110"), "uniform") if str(code or "").lower().startswith("f") else (None, "none")


class FakeHist:
    cost_fn = staticmethod(cost_fn)

    @staticmethod
    def note():
        return "СС: подделка теста."


def sources(module, funnel=FUNNEL, funnel_error=None):
    """Подмена всех читателей базы у report_wb_month модуля module (book.wb_report). Возвращает менеджер контекста и подделку воронки."""
    r = module.wb_report
    funnel_mock = mock.Mock(side_effect=funnel_error) if funnel_error else mock.Mock(return_value=list(funnel))
    stack = mock.patch.multiple(
        r,
        load_rows_db=mock.Mock(return_value=list(REPORT)),
        load_cost_history_for=mock.Mock(return_value=FakeHist()),
        load_ads_db=mock.Mock(return_value=(dict(ADS), D(0))),
        advance_net_by_day=mock.Mock(return_value=({}, D(0))),
        advance_info_text=mock.Mock(return_value=""),
        load_funnel_db=funnel_mock,
    )
    return stack, funnel_mock


def ozon_book():
    wb = openpyxl.Workbook()
    wb.active.title = "Ozon - сентябрь"
    wb.create_sheet("Заказы")
    return wb


def cells(ws):
    return {(c.row, c.column): (c.value, c.number_format) for line in ws.iter_rows() for c in line if c.value is not None}


def printed(print_mock):
    return "\n".join(" ".join(str(a) for a in call.args) for call in print_mock.call_args_list)


def add_in_memory(module, wb):
    """add_wb_sheet без диска: load_workbook отдаёт книгу из памяти, save только записывает вызов."""
    with mock.patch("openpyxl.load_workbook", return_value=wb), mock.patch.object(openpyxl.Workbook, "save") as save, \
            mock.patch("builtins.print") as out:
        total, info = module.add_wb_sheet("/nonexistent/book.xlsx", "2026-09", "2026-09-02", sb=SB, today=TODAY)
    return total, info, save, printed(out)


TITLES = [title for _platform, title in rep.PLATFORMS]


class BuildOrders(unittest.TestCase):
    """Профиль набора (test_000: кабинет с буквой «Дискаунтера» и константой владельца) — три листа."""

    def test_platforms_of_the_profile_give_three_sheets(self):
        self.assertEqual(TITLES, ["Заказы WB", "Заказы WB Standard", "Заказы WB Дискаунтер"])

    def test_rows_and_totals_are_report_wb_month_functions(self):
        coinvest = rep.coinvest_by_day(REPORT)
        with mock.patch.object(rep, "load_funnel_db", return_value=list(FUNNEL)) as funnel:
            orders, note = book.build_wb_orders(SB, REPORT, DAYS, cost_fn, ADS, True, TODAY, {"commission_pct": D("0.4")}, "СС: подделка теста.")
        funnel.assert_called_once_with(SB, "2026-09-01", "2026-09-02")
        self.assertEqual([o[0] for o in orders], TITLES)
        self.assertEqual(note, f"воронка по товарам: {rep.FUNNEL_TABLE}, строк 4")
        for (platform, title), (o_title, o_rows, o_total, o_notes) in zip(rep.PLATFORMS, orders):
            want = rep.build_orders_daily(FUNNEL, DAYS, cost_fn, ADS, coinvest, TODAY, True, platform, after_commission=rep.OWNER_ORDERS_AFTER_COMMISSION)
            self.assertEqual(o_rows, want, title)
            self.assertEqual(o_total, rep.orders_total_row(want), title)
            self.assertIn(f"лист «{title}»", o_notes[0])
        o_all = orders[0][1]
        self.assertEqual((o_all[0]["orders_qty"], o_all[0]["orders_sum"]), (3, D("250000")))
        self.assertEqual(rep.q(o_all[0]["revenue"]), rep.q(D("250000") * D("0.58") / rep.vat_for("2026-09-01")))   # константа владельца профиля
        self.assertEqual(o_all[0]["ads"], D("1220") / rep.vat_for("2026-09-01"))
        self.assertIsNone(orders[1][1][0]["ads"])                                # реклама на площадки не делится
        self.assertIn("как K владельца", orders[0][3][1])
        self.assertIn("СС: подделка теста.", orders[0][3][2])

    def test_builder_calls_report_wb_month_once_per_platform(self):
        with mock.patch.object(rep, "load_funnel_db", return_value=list(FUNNEL)), \
                mock.patch.object(rep, "build_orders_daily", wraps=rep.build_orders_daily) as build, \
                mock.patch.object(rep, "orders_total_row", wraps=rep.orders_total_row) as total:
            book.build_wb_orders(SB, REPORT, DAYS, cost_fn, ADS, True, TODAY, {"commission_pct": D("0.4")})
        self.assertEqual([c.args[7] for c in build.call_args_list], [p for p, _t in rep.PLATFORMS])
        self.assertEqual(total.call_count, len(rep.PLATFORMS))

    def test_funnel_not_read_gives_no_sheets_and_the_reason(self):
        error = RuntimeError(f"не прочитать {rep.FUNNEL_TABLE}: нет таблицы")
        with mock.patch.object(rep, "load_funnel_db", side_effect=error):
            orders, note = book.build_wb_orders(SB, REPORT, DAYS, cost_fn, ADS, True, TODAY, {"commission_pct": D("0.4")})
        self.assertEqual(orders, [])
        self.assertEqual(note, f"воронка по товарам не прочитана: не прочитать {rep.FUNNEL_TABLE}: нет таблицы; листы «Заказы WB» не собраны")

    def test_empty_funnel_gives_no_sheets_and_says_so(self):
        with mock.patch.object(rep, "load_funnel_db", return_value=[]):
            orders, note = book.build_wb_orders(SB, REPORT, DAYS, cost_fn, ADS, True, TODAY, {"commission_pct": D("0.4")})
        self.assertEqual(orders, [])
        self.assertEqual(note, f"воронка по товарам: {rep.FUNNEL_TABLE}, строк 0; листы «Заказы WB» не собраны")


class WriteSheets(unittest.TestCase):
    def orders(self):
        with mock.patch.object(rep, "load_funnel_db", return_value=list(FUNNEL)):
            return book.build_wb_orders(SB, REPORT, DAYS, cost_fn, ADS, True, TODAY, {"commission_pct": D("0.4")})[0]

    def test_sheets_go_to_the_end_with_report_wb_month_header(self):
        wb = ozon_book()
        wb.create_sheet("WB - сентябрь", 1)
        titles = book.write_wb_orders_sheets(wb, self.orders())
        self.assertEqual(titles, TITLES)
        self.assertEqual(wb.sheetnames, ["Ozon - сентябрь", "WB - сентябрь", "Заказы"] + TITLES)
        head = [h for h, _k, _f in rep.ORDERS_COLS if h]
        for title in TITLES:
            ws = wb[title]
            self.assertEqual(ws["A1"].value, f"Статистика созданных заказов Wildberries — {title}")
            self.assertEqual([c.value for c in ws[3] if c.value], head, title)
            self.assertEqual(ws.cell(row=4, column=1).value, date(2026, 9, 1))
            self.assertEqual(ws.cell(row=6, column=1).value, "Итого")
        ws = wb["Заказы WB"]
        self.assertEqual((ws.cell(row=4, column=2).value, ws.cell(row=4, column=3).value), (3, 250000.0))
        self.assertEqual((ws.cell(row=6, column=2).value, ws.cell(row=6, column=3).value), (4, 251000.0))
        self.assertTrue(str(ws.cell(row=8, column=1).value).startswith("Источник: воронка продаж WB по товарам"))

    def test_sheets_equal_the_sheets_of_the_separate_wb_book(self):
        orders = self.orders()
        month_rows = rep.build_daily(REPORT, DAYS, cost_fn, ADS, TODAY)
        with mock.patch.object(openpyxl.Workbook, "save", autospec=True) as save:      # write_xlsx — как у книги …_wb_…, без диска
            rep.write_xlsx(os.path.join(ROOT, "tests", "never_written_95.xlsx"), "2026-09", month_rows, rep.total_row(month_rows), ["n"], orders=orders)
        reference = save.call_args[0][0]
        self.assertEqual(reference.sheetnames, ["WB - сентябрь"] + TITLES)
        wb = ozon_book()
        book.write_wb_orders_sheets(wb, orders)
        compared = 0
        for title in TITLES:
            ours, theirs = cells(wb[title]), cells(reference[title])
            self.assertEqual(ours, theirs, title)
            self.assertEqual(wb[title].freeze_panes, reference[title].freeze_panes)
            compared += len(ours)
        self.assertGreater(compared, 100)

    def test_writer_is_report_wb_month_write_orders_sheet(self):
        orders = self.orders()
        with mock.patch.object(rep, "_write_orders_sheet") as write:
            wb = ozon_book()
            titles = book.write_wb_orders_sheets(wb, orders)
        self.assertEqual(titles, TITLES)
        self.assertEqual([c.args for c in write.call_args_list], [(wb, t, r, tot, n) for t, r, tot, n in orders])

    def test_existing_orders_sheet_is_replaced_not_duplicated(self):
        wb = ozon_book()
        wb.create_sheet("Заказы WB")["A1"] = "старый"
        book.write_wb_orders_sheets(wb, self.orders())
        self.assertEqual([wb.sheetnames.count(t) for t in TITLES], [1, 1, 1])
        self.assertNotIn("Заказы WB1", wb.sheetnames)
        self.assertNotEqual(wb["Заказы WB"]["A1"].value, "старый")
        self.assertEqual(wb.sheetnames[:2], ["Ozon - сентябрь", "Заказы"])      # листы Ozon не тронуты


class AddToBook(unittest.TestCase):
    def test_book_gets_wb_sheet_and_orders_sheets(self):
        wb = ozon_book()
        stack, funnel = sources(book)
        with stack:
            total, info, save, text = add_in_memory(book, wb)
        self.assertEqual(wb.sheetnames, ["Ozon - сентябрь", "WB - сентябрь", "Заказы"] + TITLES)
        save.assert_called_once_with("/nonexistent/book.xlsx")
        funnel.assert_called_once_with(SB, "2026-09-01", "2026-09-02")
        self.assertEqual(info["orders_sheets"], TITLES)
        self.assertNotIn("orders", info)
        self.assertEqual((info["rows"], info["days"]), (4, 2))
        self.assertEqual(total["turnover"], D("105000"))                         # лист выкупов — как раньше: 100000 + 10000 − 5000
        self.assertEqual(wb["WB - сентябрь"].cell(row=4, column=2).value, 105000.0)
        self.assertEqual(wb["Заказы WB"].cell(row=4, column=3).value, 250000.0)
        self.assertIn("листы заказов WB: Заказы WB, Заказы WB Standard, Заказы WB Дискаунтер", text)
        self.assertIn(f"воронка по товарам: {rep.FUNNEL_TABLE}, строк 4", text)

    def test_build_wb_daily_keeps_its_four_values_and_old_keys(self):
        stack, _funnel = sources(book)
        with stack:
            daily, total, notes, info = book.build_wb_daily("2026-09", "2026-09-02", sb=SB, today=TODAY)
        self.assertEqual([r["date"] for r in daily], DAYS)
        self.assertEqual({"rows": info["rows"], "days": info["days"], "young": info["young"]}, {"rows": 4, "days": 2, "young": []})
        self.assertEqual([o[0] for o in info["orders"]], TITLES)
        self.assertTrue(any(str(n).startswith("Источник: отчёт реализации WB") for n in notes))

    def test_funnel_failure_leaves_the_book_without_orders_sheets_and_prints_why(self):
        wb = ozon_book()
        stack, _funnel = sources(book, funnel_error=RuntimeError(f"не прочитать {rep.FUNNEL_TABLE}: statement timeout"))
        with stack:
            total, info, save, text = add_in_memory(book, wb)
        self.assertEqual(wb.sheetnames, ["Ozon - сентябрь", "WB - сентябрь", "Заказы"])
        self.assertEqual(info["orders_sheets"], [])
        self.assertEqual(total["turnover"], D("105000"))
        save.assert_called_once_with("/nonexistent/book.xlsx")
        self.assertIn("листы заказов WB: нет; воронка по товарам не прочитана", text)
        self.assertIn("statement timeout", text)
        self.assertIn("листы «Заказы WB» не собраны", text)

    def test_empty_funnel_leaves_the_book_without_orders_sheets(self):
        wb = ozon_book()
        stack, _funnel = sources(book, funnel=[])
        with stack:
            _total, info, _save, text = add_in_memory(book, wb)
        self.assertEqual(wb.sheetnames, ["Ozon - сентябрь", "WB - сентябрь", "Заказы"])
        self.assertIn("строк 0; листы «Заказы WB» не собраны", text)
        self.assertEqual(info["orders_sheets"], [])

    def test_unexpected_error_in_orders_does_not_take_the_wb_sheet_down(self):
        wb = ozon_book()
        stack, _funnel = sources(book)
        with stack, mock.patch.object(rep, "build_orders_daily", side_effect=ValueError("плохая строка воронки")):
            total, info, save, text = add_in_memory(book, wb)
        self.assertEqual(wb.sheetnames, ["Ozon - сентябрь", "WB - сентябрь", "Заказы"])
        self.assertEqual(total["turnover"], D("105000"))
        self.assertEqual(info["orders_sheets"], [])
        self.assertIn("листы «Заказы WB» не собраны: ValueError: плохая строка воронки", text)
        save.assert_called_once_with("/nonexistent/book.xlsx")

    def test_failed_write_removes_half_written_orders_sheets(self):
        wb = ozon_book()
        real, calls = rep._write_orders_sheet, []

        def second_fails(book_wb, title, rows, total, notes):
            calls.append(title)
            if len(calls) == 2:
                book_wb.create_sheet(title)                                     # лист начат и брошен
                raise ValueError("ячейка не записалась")
            return real(book_wb, title, rows, total, notes)

        stack, _funnel = sources(book)
        with stack, mock.patch.object(rep, "_write_orders_sheet", side_effect=second_fails):
            total, info, save, text = add_in_memory(book, wb)
        self.assertEqual(calls, TITLES[:2])
        self.assertEqual(wb.sheetnames, ["Ozon - сентябрь", "WB - сентябрь", "Заказы"])
        self.assertEqual(info["orders_sheets"], [])
        self.assertEqual(wb["WB - сентябрь"].cell(row=4, column=2).value, 105000.0)
        self.assertIn("листы «Заказы WB» не записаны: ValueError: ячейка не записалась", text)
        save.assert_called_once_with("/nonexistent/book.xlsx")


class DeliveryCaptionUnchanged(unittest.TestCase):
    def test_new_summary_keys_do_not_change_the_caption_line(self):
        fake = mock.Mock()
        fake.add_wb_sheet.return_value = ({"turnover": 16877663.0, "revenue": 7.9e6, "fin_result": 1.9e6, "ebitda": 1e5, "commission": 1, "logistics": 1},
                                          {"rows": 15053, "days": 24, "young": ["2026-09-23", "2026-09-24"],
                                           "orders_sheets": ["Заказы WB", "Заказы WB Standard"], "funnel_note": "воронка по товарам: x, строк 1"})
        with mock.patch.dict("sys.modules", {"book_wb_sheet": fake}), mock.patch("builtins.print"):
            line = send.add_wb_sheet_nonfatal("/nonexistent/x.xlsx", "2026-09", "2026-09-24")
        self.assertEqual(line, "WB: оборот 16,9 млн, выручка 7,9 млн, фин. рез. 1,9 млн, Ebitda 0,1 млн")


class RbhProfileTwoSheets(unittest.TestCase):
    """Профиль без буквы «Дискаунтера» и без константы владельца (rbh1): два листа, множитель «Выручки» расчётный."""

    @classmethod
    def setUpClass(cls):
        cls.saved_env = dict(os.environ)
        os.environ["MP_CABINET"] = "rbh1"
        os.environ["SUPABASE_URL"] = "http://supabase.tests.invalid"
        os.environ.setdefault("SUPABASE_SERVICE_KEY", "test")
        cls.before = sys.modules.pop("report_wb_month", None)
        cls.book = load("book_wb_sheet", "book_wb_sheet_rbh1_95")
        cls.rep = cls.book.wb_report
        cls.titles = [title for _platform, title in cls.rep.PLATFORMS]

    @classmethod
    def tearDownClass(cls):
        if cls.before is not None:
            sys.modules["report_wb_month"] = cls.before
        else:
            sys.modules.pop("report_wb_month", None)
        os.environ.clear()
        os.environ.update(cls.saved_env)

    def build(self, total):
        with mock.patch.object(self.rep, "load_funnel_db", return_value=list(FUNNEL)):
            return self.book.build_wb_orders(SB, REPORT, DAYS, cost_fn, ADS, True, TODAY, total, "СС: подделка теста.")

    def test_two_sheets_named_as_asked(self):
        self.assertIsNot(self.rep, rep)                                          # свой модуль report_wb_month под профилем rbh1
        self.assertIsNone(self.rep.DISCOUNTER_LETTER)
        self.assertIsNone(self.rep.OWNER_ORDERS_AFTER_COMMISSION)
        self.assertEqual(self.titles, ["Заказы WB", "Заказы WB Standard"])
        orders, _note = self.build({"commission_pct": D("0.4")})
        self.assertEqual([o[0] for o in orders], ["Заказы WB", "Заказы WB Standard"])

    def test_revenue_uses_one_minus_commission_of_the_buyouts_sheet(self):
        orders, _note = self.build({"commission_pct": D("0.4")})
        coinvest = self.rep.coinvest_by_day(REPORT)
        for (platform, _title), (_t, o_rows, _total, o_notes) in zip(self.rep.PLATFORMS, orders):
            self.assertEqual(o_rows, self.rep.build_orders_daily(FUNNEL, DAYS, cost_fn, ADS, coinvest, TODAY, True, platform, after_commission=D("0.6000")))
            self.assertIn("расчётный множитель (1 − комиссия листа 40.00%)", o_notes[1])
            self.assertIn("площадки не делятся", o_notes[0])
        day = orders[0][1][0]
        self.assertEqual(day["revenue"], D("250000") * D("0.6000") / self.rep.vat_for("2026-09-01"))
        self.assertEqual(orders[0][1][0]["orders_sum"], orders[1][1][0]["orders_sum"])      # без буквы всё — Standard
        self.assertIsNone(orders[1][1][0]["ads"])

    def test_no_commission_no_revenue_and_the_note_says_so(self):
        orders, _note = self.build({"commission_pct": None})
        self.assertIsNone(orders[0][1][0]["revenue"])
        self.assertIsNone(orders[0][2]["revenue"])
        self.assertIn("Выручка и Маржа пусты", orders[0][3][1])

    def test_book_of_the_rbh_profile_gets_exactly_two_orders_sheets(self):
        wb = ozon_book()
        stack, _funnel = sources(self.book)
        with stack:
            _total, info, save, text = add_in_memory(self.book, wb)
        self.assertEqual(wb.sheetnames, ["Ozon - сентябрь", "WB - сентябрь", "Заказы", "Заказы WB", "Заказы WB Standard"])
        self.assertEqual(info["orders_sheets"], ["Заказы WB", "Заказы WB Standard"])
        self.assertEqual([c.value for c in wb["Заказы WB Standard"][3] if c.value], [h for h, _k, _f in self.rep.ORDERS_COLS if h])
        self.assertIn("листы заказов WB: Заказы WB, Заказы WB Standard;", text)
        save.assert_called_once_with("/nonexistent/book.xlsx")


if __name__ == "__main__":
    unittest.main()
