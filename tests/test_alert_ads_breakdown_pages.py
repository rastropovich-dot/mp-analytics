"""59-я: строка «Реклама» утреннего алерта читает расходы дня страницами (день Ozon с > 1 000 строк), Selected CPO и итог в строке;
строка пика памяти шагов витрины."""
import unittest
from unittest import mock

import alerts_telegram as alerts
from loaders import peak_memory


class _Sb:
    """PostgREST в миниатюре: фильтры eq / like, сортировка, range — и не больше 1 000 строк на ответ, как у Supabase."""
    LIMIT = 1000

    def __init__(self, rows):
        self.rows, self.requests = rows, 0

    def table(self, _name):
        return _Query(self)


class _Query:
    def __init__(self, sb):
        self.sb, self.filters, self.orders, self.window = sb, [], [], None

    def select(self, _columns):
        return self

    def eq(self, column, value):
        self.filters.append(lambda r: str(r[column]) == str(value)); return self

    def like(self, column, pattern):
        prefix = pattern.rstrip("%")
        self.filters.append(lambda r: str(r[column]).startswith(prefix)); return self

    def order(self, column):
        self.orders.append(column); return self

    def range(self, start, end):
        self.window = (start, end); return self

    def execute(self):
        self.sb.requests += 1
        rows = [r for r in self.sb.rows if all(f(r) for f in self.filters)]
        rows.sort(key=lambda r: tuple(str(r[c]) for c in self.orders))
        start, end = self.window or (0, len(rows))
        return type("R", (), {"data": rows[start:min(end + 1, start + _Sb.LIMIT)]})()


def _day_rows():
    """26.10-подобный день: 2 500 строк расходов Ozon, из них рекламы 1 300 (клики 900 × 100, CPO 5 % 200 × 50, другая ставка 150 × 30,
    Selected 50 × 20) и 1 200 строк комиссии / логистики; плюс чужой день и WB — в сумму не входят."""
    rows, sku = [], 0
    for expense_type, n, amount in (("advertising_clicks", 900, 100), ("advertising_order_5", 200, 50),
                                    ("advertising_order_other", 150, 30), ("advertising_order_selected_cpo", 50, 20),
                                    ("commission", 700, 999), ("logistics", 500, 999)):
        for _ in range(n):
            sku += 1
            rows.append({"marketplace_code": "ozon", "expense_date": "2026-10-26", "marketplace_sku": f"{sku:06d}",
                         "expense_type": expense_type, "expense_amount": amount})
    rows.append({"marketplace_code": "ozon", "expense_date": "2026-10-25", "marketplace_sku": "1", "expense_type": "advertising_clicks", "expense_amount": 7})
    rows.append({"marketplace_code": "wb", "expense_date": "2026-10-26", "marketplace_sku": "1", "expense_type": "advertising_clicks", "expense_amount": 7})
    return rows


class AdsBreakdownPages(unittest.TestCase):
    def test_day_with_more_than_1000_rows_is_read_whole(self):
        sb = _Sb(_day_rows())
        ads = alerts.get_ozon_ads_breakdown("2026-10-26", sb=sb)
        self.assertEqual(ads["advertising_clicks"], 90_000)
        self.assertEqual(ads["advertising_order_5"], 10_000)
        self.assertEqual(ads["advertising_order_other"], 4_500)
        self.assertEqual(ads["advertising_order_selected_cpo"], 1_000)
        self.assertEqual(sum(ads.values()), 105_500)
        self.assertEqual(sb.requests, 2)        # 1 300 строк рекламы — две страницы

    def test_old_single_request_would_have_lost_rows(self):
        """Контроль самой подмены: один запрос без range отдаёт 1 000 строк из 2 500 — так строка и врала весь октябрь."""
        q = _Sb(_day_rows()).table("marketplace_expenses").eq("expense_date", "2026-10-26")
        self.assertEqual(len(q.execute().data), 1000)

    def test_line_shows_selected_cpo_and_total(self):
        kpi_row = {"kpi_date": "2026-05-20", "marketplace_code": "ozon", "orders_qty": 10, "orders_amount_seller": 25000,
                   "buyouts_qty": 5, "buyouts_amount_seller": 12000, "ad_spend": 105500, "commission_amount": 1,
                   "logistics_amount": 1, "other_expenses_amount": 1, "ad_orders_revenue": 0, "organic_orders_revenue": 0,
                   "ad_share_revenue": 0}
        wb_row = dict(kpi_row, marketplace_code="wb")
        ads = {"advertising_clicks": 90000, "advertising_order_10": 0, "advertising_order_5": 10000, "advertising_order_other": 4500,
               "advertising_order_selected_cpo": 1000, "advertising_order_unknown": 0, "advertising_other": 0}
        with mock.patch.object(alerts, "today_local", return_value=alerts.date(2026, 5, 21)), \
            mock.patch.object(alerts, "get_wb_buyout_cohort_summary", return_value=""), \
            mock.patch.object(alerts, "get_ozon_report_completeness", return_value={"complete": True, "blockers": []}), \
            mock.patch.object(alerts, "get_ozon_ads_breakdown", return_value=ads), \
            mock.patch.object(alerts, "get_ozon_organic_reconciliation", return_value={"available": False}), \
            mock.patch.object(alerts, "get_ozon_ad_bonus_lines", return_value=[]), \
            mock.patch.object(alerts, "get_ozon_reviews_lines", return_value=[]):
            text = "\n".join(alerts.build_executive_summary([wb_row, kpi_row]))
        line = next(s for s in text.split("\n") if s.startswith("Реклама: клики"))
        self.assertEqual(line, "Реклама: клики 90 000, заказ 10% 0, заказ 5% 10 000, заказ другая ставка 4 500, "
                               "Selected CPO 1 000; итого 105 500.")

    def test_unknown_advertising_kind_is_named(self):
        """Незнакомый вид рекламы не проходит мимо строки: «не распознано/прочее»."""
        sb = _Sb([{"marketplace_code": "ozon", "expense_date": "2026-10-26", "marketplace_sku": "1",
                   "expense_type": "advertising_new_kind", "expense_amount": 77}])
        ads = alerts.get_ozon_ads_breakdown("2026-10-26", sb=sb)
        self.assertEqual(ads["advertising_new_kind"], 77)


class PeakMemoryLine(unittest.TestCase):
    def test_line(self):
        self.assertGreater(peak_memory.peak_bytes(), 1024 * 1024)
        line = peak_memory.line("KPI: расчет SKU")
        self.assertTrue(line.startswith("пик памяти шага «KPI: расчет SKU»: "))
        self.assertIn(" МБ (max RSS; лимит Render 2 048 МБ)", line)

    def test_linux_kilobytes(self):
        with mock.patch.object(peak_memory.sys, "platform", "linux"), \
            mock.patch.object(peak_memory.resource, "getrusage", return_value=type("U", (), {"ru_maxrss": 1_500_000})()):
            self.assertEqual(peak_memory.peak_bytes(), 1_536_000_000)


if __name__ == "__main__":
    unittest.main()
