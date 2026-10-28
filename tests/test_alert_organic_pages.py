"""59-я: органика дня в алерте — страницами (> 1 000 ключей в день) и строка «реклама больше всех заказов товара»."""
import unittest

import alerts_telegram as alerts
try:
    from test_alert_ads_breakdown_pages import _Sb   # discover: каталог tests в sys.path
except ImportError:
    from tests.test_alert_ads_breakdown_pages import _Sb


def _row(sku, total, ad, flag):
    return {"marketplace_code": "ozon", "sale_date": "2026-10-26", "marketplace_sku": f"{sku:05d}", "total_orders_qty": total,
            "total_orders_revenue": total * 100, "ad_orders_qty": ad, "ad_orders_revenue": ad * 100,
            "organic_orders_revenue": (total - ad) * 100, "calculation_status": "ok",
            "warning": "ad_orders_exceed_total" if flag else None, "ad_exceeds_total": flag}


class OrganicPages(unittest.TestCase):
    def test_reads_all_pages_and_names_excess(self):
        rows = [_row(i, 2, 1, False) for i in range(1, 2501)] + [_row(9000 + i, 1, 3, True) for i in range(3)]
        sb = _Sb(rows)
        rec = alerts.get_ozon_organic_reconciliation("2026-10-26", sb=sb)
        self.assertEqual(rec["organic_rows_count"], 2503)
        self.assertEqual((rec["ad_exceeds_total_count"], rec["ad_exceeds_total_excess_qty"]), (3, 6.0))
        line = alerts.format_ozon_attribution_summary(rec["raw_ad_revenue"], rec["raw_organic_revenue"], None, rec)
        self.assertIn("Реклама больше всех заказов товара: 3 SKU, лишних 6 шт", line)

    def test_no_line_without_excess(self):
        rec = alerts.get_ozon_organic_reconciliation("2026-10-26", sb=_Sb([_row(1, 2, 1, False)]))
        line = alerts.format_ozon_attribution_summary(100, 100, None, rec)
        self.assertNotIn("Реклама больше всех заказов", line)


if __name__ == "__main__":
    unittest.main()
