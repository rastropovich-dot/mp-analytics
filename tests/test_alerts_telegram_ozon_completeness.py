import unittest
from unittest import mock

import alerts_telegram as alerts


def _kpi_row(marketplace_code, kpi_date="2026-05-20", orders_qty=10, orders_amount=100000, buyouts_qty=8, buyouts_amount=80000):
    return {
        "marketplace_code": marketplace_code,
        "kpi_date": kpi_date,
        "orders_qty": orders_qty,
        "orders_amount_seller": orders_amount,
        "buyouts_qty": buyouts_qty,
        "buyouts_amount_seller": buyouts_amount,
        "commission_amount": 1000,
        "logistics_amount": 200,
        "other_expenses_amount": 300,
        "ad_spend": 400,
        "ad_orders_revenue": 5000,
        "organic_orders_revenue": 20000,
        "ad_share_revenue": 0.2,
    }


class OzonCompletenessGateTests(unittest.TestCase):
    def test_missing_organic_marks_ozon_incomplete(self):
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows:
            table_has_rows.side_effect = [True, True, False, True, False]
            result = alerts.get_ozon_report_completeness("2026-05-20")
        self.assertFalse(result["complete"])
        self.assertIn("ozon_daily_sku_organic_missing", result["blockers"])

    def test_missing_ads_marks_ozon_incomplete_not_zero(self):
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows:
            table_has_rows.side_effect = [True, True, True, False, False]
            result = alerts.get_ozon_report_completeness("2026-05-20")
        self.assertFalse(result["complete"])
        self.assertIn("ozon_ads_layer_missing", result["blockers"])
        self.assertFalse(result["ads_present"])

    def test_all_required_layers_present_marks_complete(self):
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows:
            table_has_rows.side_effect = [True, True, True, True, False]
            result = alerts.get_ozon_report_completeness("2026-05-20")
        self.assertTrue(result["complete"])
        self.assertEqual(result["blockers"], [])

    def test_executive_summary_skips_normal_ozon_block_when_incomplete(self):
        kpi_rows = [
            _kpi_row("wb"),
            _kpi_row("ozon"),
        ]
        with mock.patch.object(alerts, "today_local", return_value=alerts.date(2026, 5, 21)), \
            mock.patch.object(alerts, "get_ozon_report_completeness", return_value={
                "complete": False,
                "blockers": ["ozon_daily_sku_organic_missing", "ozon_ads_layer_missing"],
            }):
            lines = alerts.build_executive_summary(kpi_rows)
        text = "\n".join(lines)
        self.assertIn("Ozon вчера: данные неполные, управленческий вывод не строим", text)
        self.assertNotIn("Реклама: клики", text)
        self.assertNotIn("Атрибуция: реклама", text)

    def test_completed_day_alerts_skip_ozon_risk_when_incomplete(self):
        kpi_rows = [
            _kpi_row("ozon", "2026-05-20", orders_qty=34, buyouts_qty=20),
            _kpi_row("ozon", "2026-05-19", orders_qty=100, buyouts_qty=80),
            _kpi_row("ozon", "2026-05-18", orders_qty=100, buyouts_qty=80),
            _kpi_row("wb", "2026-05-20", orders_qty=10, buyouts_qty=8),
            _kpi_row("wb", "2026-05-19", orders_qty=30, buyouts_qty=24),
            _kpi_row("wb", "2026-05-18", orders_qty=30, buyouts_qty=24),
        ]
        with mock.patch.object(alerts, "today_local", return_value=alerts.date(2026, 5, 21)), \
            mock.patch.object(alerts, "get_ozon_report_completeness", return_value={
                "complete": False,
                "blockers": ["ozon_ads_layer_missing"],
            }):
            rows = alerts.build_completed_day_alerts(kpi_rows)
        joined = "\n".join(rows)
        self.assertIn("Ozon: вчера данные неполные", joined)
        self.assertNotIn("Ozon: вчера падение заказов", joined)

    def test_wb_summary_unaffected_when_ozon_incomplete(self):
        kpi_rows = [
            _kpi_row("wb"),
            _kpi_row("ozon"),
        ]
        with mock.patch.object(alerts, "today_local", return_value=alerts.date(2026, 5, 21)), \
            mock.patch.object(alerts, "get_ozon_report_completeness", return_value={
                "complete": False,
                "blockers": ["ozon_daily_sku_organic_missing"],
            }):
            lines = alerts.build_executive_summary(kpi_rows)
        text = "\n".join(lines)
        self.assertIn("🟣 <b>WB вчера</b>", text)


if __name__ == "__main__":
    unittest.main()
