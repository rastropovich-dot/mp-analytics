"""Сорок первая §2: чистые части живой проверки книги в Excel — разбор структурного вывода osascript, шапки статичных листов, сравнение
области сводной со статичным листом, блоки «Артикула». Excel и osascript не зовутся (subprocess подменён)."""
import os
import subprocess
import sys
import unittest
from decimal import Decimal
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import finrez_excel_check as x  # noqa: E402

D = Decimal


class Parse(unittest.TestCase):
    def test_structured_output(self):
        got = x.parse_structured('{"апр", 1.5, missing value, {2, "a\\"b"}, true, false, date "Friday, 1", «constant ****xlab», -63231586.17, 1.0E+3}')
        self.assertEqual(got, ["апр", D("1.5"), None, [2, 'a"b'], True, False, "date Friday, 1", "«constant ****xlab»", D("-63231586.17"), D("1.0E+3")])
        self.assertEqual(x.parse_structured('"16.110.3"'), "16.110.3")
        self.assertEqual(x.parse_structured("16.110.3"), "16.110.3")           # голый текст без кавычек — как есть
        self.assertEqual(x.parse_structured("42"), 42)
        self.assertEqual(x.parse_structured("{}"), [])

    def test_osascript_errors_are_verbatim(self):
        def run(args, capture_output, text, timeout):
            return subprocess.CompletedProcess(args, 1, "", "42:46: execution error: Получена ошибка от «Microsoft Excel»: Время AppleEvent истекло. (-1712)\n")
        with self.assertRaises(x.ExcelError) as cm:
            x.osascript('tell application "Microsoft Excel" to get version', run=run)
        self.assertIn("Время AppleEvent истекло. (-1712)", str(cm.exception))

        def slow(args, capture_output, text, timeout):
            raise subprocess.TimeoutExpired(args, timeout)
        with self.assertRaises(x.ExcelError) as cm:
            x.osascript("x", timeout=7, run=slow)
        self.assertIn("7 с", str(cm.exception))
        self.assertEqual(x.q('a"b\\c'), '"a\\"b\\\\c"')


OZON_STATIC = [["Выкупы Ozon — форма"],
               ["Названия столбцов", "Комиссия", None, "Логистика", None, "Прочее", None, "Реклама", None, "Товарооборот", None, "Эквайринг", None, "Оборот (с НДС)"],
               ["Названия строк", " Начисления", " Ст-ть продаж в себ-ти", " Начисления", " Ст-ть", " Начисления", " Ст-ть", " Начисления", " Ст-ть", " Начисления", " Ст-ть", " Начисления", " Ст-ть", "x"],
               ["апр", -100.5, 0, -10, 0, -5, 0, -20, 0, 1000, 300, -7, 0, 1000],
               ["01.апр", -50, 0, -5, 0, -2, 0, -10, 0, 500, 150, -3, 0, 500],
               ["Общий итог", -100.5, 0, -10, 0, -5, 0, -20, 0, 1000, 300, -7, 0, 1000]]
# область сводной владельца после пересчёта: фильтры сверху, «Названия столбцов», статьи, подшапка, месяцы
OZON_PIVOT = [["Статус", "(Все)"], [None], [None, "Названия столбцов"], [None, "Комиссия", None, "Логистика", None, "Прочее", None, "Реклама", None, "Товарооборот", None, "Эквайринг", None],
              ["Названия строк", "Начисления", "Ст-ть продаж в себ-ти", "Начисления", "Ст-ть продаж в себ-ти", "Начисления", "Ст-ть продаж в себ-ти", "Начисления", "Ст-ть продаж в себ-ти",
               "Начисления", "Ст-ть продаж в себ-ти", "Начисления", "Ст-ть продаж в себ-ти"],
              ["апр", D("-100.5"), 0, -10, 0, -5, 0, -20, 0, 1000, 300, -7, 0],
              ["Общий итог", D("-100.5"), 0, -10, 0, -5, 0, -20, 0, 1000, 300, -7, 0]]


class Static(unittest.TestCase):
    def test_ozon_buyouts_header_mapping(self):
        st = x.static_ozon_buyouts(OZON_STATIC)
        self.assertEqual(st["апр"], {"Комиссия": D("-100.5"), "Логистика": D(-10), "Прочее": D(-5), "Реклама": D(-20), "Товарооборот": D(1000), "Эквайринг": D(-7)})
        self.assertEqual(set(st), {"апр", "Общий итог"})                              # дни не берутся

    def test_orders_two_blocks(self):
        rows = [["Заказы —"], ["Названия столбцов", "Ozon", None, None, "WB"], ["Названия строк", "Оборот (с НДС)", "Выручка", "Гр.фин.рез, %", "Оборот (с НДС)", "Выручка", "Гр.фин.рез, %"],
                ["апр", 100, 50, 0.1, 200, 90, 0.2], ["01.апр", 100, 50, 0.1, 200, 90, 0.2]]
        st = x.static_orders(rows)
        self.assertEqual(st[("Ozon", "апр")], {"Оборот (с НДС)": D(100), "Выручка": D(50), "Гр.фин.рез, %": D("0.1")})
        self.assertEqual(st[("WB", "апр")]["Оборот (с НДС)"], D(200))

    def test_by_header(self):
        rows = [["t"], ["Названия строк", "Продажи, ₽", "Комиссия, ₽", "Оборот (с НДС)"], ["апр", 10, 4, 10], ["Общий итог", 10, 4, 10]]
        st = x.static_by_header(rows, 2, x.WB_MONEY)
        self.assertEqual(st["апр"], {"Продажи, ₽": D(10), "Комиссия, ₽": D(4)})


class Compare(unittest.TestCase):
    def test_pivot_against_static_ozon(self):
        st = x.static_ozon_buyouts(OZON_STATIC)
        table = x.compare(st, OZON_PIVOT, x.ARTICLES, article_header=True)
        self.assertEqual(len(table), 12)                                                # 2 метки × 6 статей
        self.assertEqual([t for t in table if t[4]], [])
        broken = [r[:] for r in OZON_PIVOT]
        broken[5] = ["апр", D("-100.5"), 0, -10, 0, -5, 0, -21, 0, 1000, 300, -7, 0]
        bad = [t for t in x.compare(st, broken, x.ARTICLES, article_header=True) if t[4]]
        self.assertEqual(bad, [("апр", "Реклама", D(-20), D(-21), D("1.00"))])

    def test_pivot_without_months_is_reported(self):
        table = x.compare({}, [["Статус", "(Все)"], ["a", "b"]], x.ARTICLES, article_header=True)
        self.assertEqual(table[0][4], "в области сводной не найдено строк-месяцев")

    def test_article_sheet_blocks(self):
        grid = [["Артикул", "F1"], ["Площадка заказов (МП)", "Ozon"], ["№ месяца", 4, 5], ["Показатель", "апр", "май", "Итого"],
                ["Выкупы Ozon (с НДС, знак сводной владельца)", None, None], ["Товарооборот", 1000, 0, 1000], ["Комиссия", -400, 0, -400], ["Ст-ть продаж", 300, 0, 300],
                ["Выкупы WB (с НДС, модуль)", None, None], ["Продажи, ₽", 7, 0, 7],
                ["Заказы (площадка — B2; месяц — по номеру в строке 3)", None, None], ["Заказы, ₽", 1500, 0, 1500], ["Фин.рез", 10, 0, 10]]
        buy = {("F1", "апр"): {"Товарооборот": D(1000), "Комиссия": D(-400), "Ст-ть продаж в себ-ти": D(300)}}
        orders = {("F1", "Ozon", 4): {"Заказы, ₽": D(1500), "Фин.рез": D(11)}}
        wbb = {("F1", "апр"): {"Продажи, ₽": D(7)}}
        table = x.compare_article_sheet(grid, "F1", "Ozon", buy, orders, wbb)
        bad = [t for t in table if t[5]]
        self.assertEqual(bad, [("orders", "Фин.рез", "апр", D(10), D(11), D("-1.00"))])
        self.assertEqual(len(table), 6)                                                 # май везде 0 и пусто — не сравнивается; блок WB — 1

    def test_month_labels_of_excel(self):
        self.assertEqual([x.month_key(v) for v in ("сен", "сент", "сент.", "мая", "апр.", "Sep", "Общий итог", "01.сент", 4, None)],
                         ["сен", "сен", "сен", "май", "апр", "сен", None, None, None, None])
        st = {"апр": {"Товарооборот": D(1)}, "сен": {"Товарооборот": D(2)}}
        grid = [["Названия строк", "Начисления"], [None, "Товарооборот"], ["Названия строк", "Начисления"], ["апр", 1], ["сент", 2], ["Общий итог", 3]]
        table = x.compare(st, grid, ("Товарооборот",), article_header=True)
        self.assertEqual([t for t in table if t[4]], [])                                # «сент» области = «сен» листа
        table = x.compare({"апр": {"Товарооборот": D(1)}, "май": {"Товарооборот": D(9)}}, grid, ("Товарооборот",), article_header=True)
        self.assertIn("меток статичного листа нет в области сводной: ['май']", table[-1][4])


class Run(unittest.TestCase):
    def test_excel_unreachable_gives_code_2(self):
        def slow(args, capture_output, text, timeout):
            raise subprocess.TimeoutExpired(args, timeout)
        lines = []
        self.assertEqual(x.run("/tmp/nope.xlsx", run_=slow, out=lines.append), 2)
        self.assertTrue(lines[0].startswith("Excel недоступен"))


if __name__ == "__main__":
    unittest.main()
