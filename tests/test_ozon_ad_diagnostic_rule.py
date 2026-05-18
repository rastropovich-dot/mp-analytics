import unittest

import reports_ozon_ad_diagnostic_rule as rule


def _kpi_row(
    kpi_date="2026-05-16",
    sku="1300079194",
    buyouts_qty=2,
    buyouts_revenue=223513,
    orders_qty=3,
    orders_revenue=332469,
    ad_spend=25417.14,
    ad_orders_qty=1,
    ad_orders_revenue=110823,
    organic_orders_revenue=221646,
    stock_qty=227,
    stock_status="stock_ok",
):
    return {
        "kpi_date": kpi_date,
        "marketplace_code": "ozon",
        "marketplace_sku": sku,
        "article": "F000283615",
        "product_name": "Item",
        "orders_qty": orders_qty,
        "orders_amount_seller": orders_revenue,
        "buyouts_qty": buyouts_qty,
        "buyouts_amount_seller": buyouts_revenue,
        "ad_spend": ad_spend,
        "ad_orders_qty": ad_orders_qty,
        "ad_orders_revenue": ad_orders_revenue,
        "organic_orders_revenue": organic_orders_revenue,
        "stock_qty": stock_qty,
        "stock_status": stock_status,
    }


def _expense_row(expense_date="2026-05-16", expense_type="advertising_clicks", amount=1000.0):
    return {
        "expense_date": expense_date,
        "marketplace_code": "ozon",
        "marketplace_sku": "1300079194",
        "expense_type": expense_type,
        "expense_amount": amount,
    }


def _attr_row(sale_date="2026-05-16", campaign_id="24375352", spend=1500.0, orders=1, revenue=110823.0):
    return {
        "sale_date": sale_date,
        "marketplace_code": "ozon",
        "marketplace_sku": "1300079194",
        "ad_source": "cpc",
        "campaign_id": str(campaign_id),
        "ad_spend": spend,
        "ad_orders_qty": orders,
        "ad_orders_revenue": revenue,
    }


def _decision_row(
    decision_status="ready",
    data_quality_status="ok",
    organic_reconciliation_status="clean",
    stock_qty=227,
    stock_status="stock_ok",
):
    return {
        "kpi_date": "2026-05-16",
        "marketplace_code": "ozon",
        "marketplace_sku": "1300079194",
        "decision_status": decision_status,
        "data_quality_status": data_quality_status,
        "organic_reconciliation_status": organic_reconciliation_status,
        "stock_qty": stock_qty,
        "stock_status": stock_status,
    }


class OzonAdDiagnosticRuleTests(unittest.TestCase):
    def test_eligible_sku_and_healthy_campaign_green_keep(self):
        kpi_rows = [
            _kpi_row(kpi_date="2026-05-16", ad_spend=3500.0),
            _kpi_row(kpi_date="2026-05-15", ad_spend=3200.0, ad_orders_qty=1, ad_orders_revenue=111000, organic_orders_revenue=100000),
            _kpi_row(kpi_date="2026-05-14", ad_spend=2800.0, ad_orders_qty=1, ad_orders_revenue=109000, organic_orders_revenue=90000),
            _kpi_row(kpi_date="2026-05-13", buyouts_qty=2, buyouts_revenue=210000),
            _kpi_row(kpi_date="2026-05-12", buyouts_qty=2, buyouts_revenue=205000),
        ]
        expenses = [
            _expense_row("2026-05-16", "advertising_clicks", 1500),
            _expense_row("2026-05-16", "commission", 40000),
            _expense_row("2026-05-16", "logistics", 500),
            _expense_row("2026-05-16", "other", 1000),
        ]
        attrs = [
            _attr_row("2026-05-16", "24375352", 1500, 1, 110823),
            _attr_row("2026-05-15", "24375352", 1400, 1, 111000),
            _attr_row("2026-05-14", "24375352", 1300, 1, 109000),
        ]
        report = rule.build_report(
            "ozon",
            "1300079194",
            "2026-05-16",
            ["24375352"],
            32963,
            kpi_rows=kpi_rows,
            expense_rows=expenses,
            attribution_rows=attrs,
            decision_row=_decision_row(),
        )
        self.assertEqual(report["eligibility"]["cpc_control_eligibility_status"], "eligible_for_diagnostic")
        self.assertEqual(report["eligibility"]["sku_total_economics_status"], "GREEN")
        self.assertEqual(report["campaigns"][0]["status"], "GREEN")
        self.assertEqual(report["campaigns"][0]["recommendation"], "keep_or_cautious_increase")
        self.assertFalse(report["final_recommendation"]["live_action_allowed"])

    def test_consecutive_high_spend_zero_order_days_red_reduce_candidate(self):
        kpi_rows = [
            _kpi_row(kpi_date="2026-05-16", ad_spend=3200.0),
            _kpi_row(kpi_date="2026-05-15", ad_spend=3100.0),
            _kpi_row(kpi_date="2026-05-14", ad_spend=3000.0),
            _kpi_row(kpi_date="2026-05-13", buyouts_qty=2, buyouts_revenue=210000),
            _kpi_row(kpi_date="2026-05-12", buyouts_qty=2, buyouts_revenue=205000),
        ]
        expenses = [
            _expense_row("2026-05-16", "advertising_clicks", 1400),
            _expense_row("2026-05-16", "commission", 40000),
        ]
        attrs = [
            _attr_row("2026-05-16", "24375331", 1600, 0, 0),
            _attr_row("2026-05-15", "24375331", 1600, 0, 0),
            _attr_row("2026-05-14", "24375331", 1500, 0, 0),
        ]
        report = rule.build_report(
            "ozon",
            "1300079194",
            "2026-05-16",
            ["24375331"],
            32963,
            kpi_rows=kpi_rows,
            expense_rows=expenses,
            attribution_rows=attrs,
            decision_row=_decision_row(),
        )
        self.assertEqual(report["campaigns"][0]["status"], "RED")
        self.assertEqual(report["campaigns"][0]["recommendation"], "reduce_candidate")

    def test_weak_but_positive_campaign_yellow_watch(self):
        kpi_rows = [
            _kpi_row(kpi_date="2026-05-16", ad_spend=3500.0),
            _kpi_row(kpi_date="2026-05-15", ad_spend=3200.0, ad_orders_qty=1, ad_orders_revenue=111000, organic_orders_revenue=100000),
            _kpi_row(kpi_date="2026-05-14", ad_spend=2800.0, ad_orders_qty=1, ad_orders_revenue=109000, organic_orders_revenue=90000),
            _kpi_row(kpi_date="2026-05-13", buyouts_qty=2, buyouts_revenue=210000),
            _kpi_row(kpi_date="2026-05-12", buyouts_qty=2, buyouts_revenue=205000),
        ]
        expenses = [
            _expense_row("2026-05-16", "advertising_clicks", 1500),
            _expense_row("2026-05-16", "commission", 40000),
        ]
        attrs = [
            _attr_row("2026-05-16", "24375352", 1200, 1, 110823),
            _attr_row("2026-05-15", "24375352", 1100, 1, 111000),
            _attr_row("2026-05-14", "24375352", 1000, 1, 109000),
            _attr_row("2026-05-16", "24375331", 1300, 0, 0),
            _attr_row("2026-05-15", "24375331", 1200, 1, 50000),
            _attr_row("2026-05-14", "24375331", 1200, 0, 0),
        ]
        report = rule.build_report(
            "ozon",
            "1300079194",
            "2026-05-16",
            ["24375331", "24375352"],
            32963,
            kpi_rows=kpi_rows,
            expense_rows=expenses,
            attribution_rows=attrs,
            decision_row=_decision_row(),
        )
        by_campaign = {item["campaign_id"]: item for item in report["campaigns"]}
        self.assertEqual(by_campaign["24375352"]["status"], "GREEN")
        self.assertEqual(by_campaign["24375331"]["status"], "YELLOW")
        self.assertIn("stronger_by_revenue_volume", by_campaign["24375352"]["reasons"])
        self.assertIn("weaker_by_revenue_volume:24375352", by_campaign["24375331"]["reasons"])

    def test_selected_cpo_pressure_does_not_make_cpc_campaign_red_by_itself(self):
        kpi_rows = [
            _kpi_row(kpi_date="2026-05-16", ad_spend=25417.14),
            _kpi_row(kpi_date="2026-05-15", buyouts_qty=2, buyouts_revenue=210000),
            _kpi_row(kpi_date="2026-05-14", buyouts_qty=2, buyouts_revenue=205000),
        ]
        expenses = [
            _expense_row("2026-05-16", "advertising_clicks", 3369.84),
            _expense_row("2026-05-16", "advertising_order_selected_cpo", 22047.30),
            _expense_row("2026-05-16", "commission", 48772.92),
            _expense_row("2026-05-16", "other", 1071.5),
        ]
        attrs = [_attr_row("2026-05-16", "24375352", 1697.90, 1, 110823)]
        report = rule.build_report(
            "ozon",
            "1300079194",
            "2026-05-16",
            ["24375352"],
            32963,
            kpi_rows=kpi_rows,
            expense_rows=expenses,
            attribution_rows=attrs,
            decision_row=_decision_row(),
        )
        self.assertEqual(report["eligibility"]["sku_total_economics_status"], "YELLOW")
        self.assertEqual(report["eligibility"]["cpc_control_eligibility_status"], "eligible_for_diagnostic")
        self.assertIn("selected_cpo_pressure", report["eligibility"]["sku_total_economics_reasons"])
        self.assertEqual(report["campaigns"][0]["status"], "GREEN")
        self.assertEqual(report["final_recommendation"]["status"], "YELLOW")
        self.assertEqual(report["final_recommendation"]["action"], "diagnostic_only_hold")

    def test_selected_cpo_included_in_total_but_excluded_from_controllable_cpc_spend(self):
        kpi_rows = [
            _kpi_row(kpi_date="2026-05-16", ad_spend=25417.14),
            _kpi_row(kpi_date="2026-05-15", buyouts_qty=2, buyouts_revenue=210000),
            _kpi_row(kpi_date="2026-05-14", buyouts_qty=2, buyouts_revenue=205000),
        ]
        expenses = [
            _expense_row("2026-05-16", "advertising_clicks", 3369.84),
            _expense_row("2026-05-16", "advertising_order_selected_cpo", 22047.30),
            _expense_row("2026-05-16", "commission", 48772.92),
            _expense_row("2026-05-16", "other", 1071.5),
        ]
        attrs = [_attr_row("2026-05-16", "24375352", 1697.90, 1, 110823)]
        report = rule.build_report(
            "ozon",
            "1300079194",
            "2026-05-16",
            ["24375352"],
            32963,
            kpi_rows=kpi_rows,
            expense_rows=expenses,
            attribution_rows=attrs,
            decision_row=_decision_row(),
        )
        self.assertAlmostEqual(report["sku_economics"]["selected_cpo_spend"], 22047.30, places=2)
        self.assertAlmostEqual(report["sku_economics"]["cpc_spend"], 3369.84, places=2)
        self.assertAlmostEqual(report["sku_economics"]["controllable_ad_spend"], 3369.84, places=2)
        self.assertAlmostEqual(report["sku_economics"]["non_controllable_ad_spend"], 22047.30, places=2)
        self.assertAlmostEqual(report["sku_economics"]["total_ad_spend"], 25417.14, places=2)
        self.assertAlmostEqual(report["sku_economics"]["cpc_tacos"], 0.0151, places=4)
        self.assertAlmostEqual(report["sku_economics"]["selected_cpo_tacos"], 0.0986, places=4)
        self.assertAlmostEqual(report["sku_economics"]["selected_cpo_spend"], 22047.30, places=2)
        self.assertAlmostEqual(report["sku_economics"]["actual_ad_spend"], 25417.14, places=2)
        self.assertAlmostEqual(report["campaigns"][0]["windows"]["5d"]["revenue"], 110823.0, places=2)
        self.assertFalse(report["final_recommendation"]["live_action_allowed"])

    def test_data_quality_not_ok_blocks_eligibility(self):
        kpi_rows = [
            _kpi_row(kpi_date="2026-05-16", ad_spend=3500.0),
            _kpi_row(kpi_date="2026-05-15", buyouts_qty=2, buyouts_revenue=210000),
            _kpi_row(kpi_date="2026-05-14", buyouts_qty=2, buyouts_revenue=205000),
        ]
        expenses = [_expense_row("2026-05-16", "commission", 40000)]
        attrs = [_attr_row("2026-05-16", "24375352", 1500, 1, 110823)]
        report = rule.build_report(
            "ozon",
            "1300079194",
            "2026-05-16",
            ["24375352"],
            32963,
            kpi_rows=kpi_rows,
            expense_rows=expenses,
            attribution_rows=attrs,
            decision_row=_decision_row(data_quality_status="missing_stock"),
        )
        self.assertEqual(report["eligibility"]["cpc_control_eligibility_status"], "blocked")
        self.assertEqual(report["final_recommendation"]["status"], "RED")

    def test_peer_comparison_marks_stronger_campaign(self):
        kpi_rows = [
            _kpi_row(kpi_date="2026-05-16", ad_spend=3500.0),
            _kpi_row(kpi_date="2026-05-15", buyouts_qty=2, buyouts_revenue=210000),
            _kpi_row(kpi_date="2026-05-14", buyouts_qty=2, buyouts_revenue=205000),
            _kpi_row(kpi_date="2026-05-13", buyouts_qty=2, buyouts_revenue=210000),
            _kpi_row(kpi_date="2026-05-12", buyouts_qty=2, buyouts_revenue=205000),
        ]
        expenses = [_expense_row("2026-05-16", "commission", 40000)]
        attrs = [
            _attr_row("2026-05-16", "24375352", 1200, 1, 110823),
            _attr_row("2026-05-15", "24375352", 1200, 1, 111000),
            _attr_row("2026-05-14", "24375352", 1200, 1, 109000),
            _attr_row("2026-05-16", "24375331", 1300, 0, 0),
            _attr_row("2026-05-15", "24375331", 1200, 1, 50000),
        ]
        report = rule.build_report(
            "ozon",
            "1300079194",
            "2026-05-16",
            ["24375331", "24375352"],
            32963,
            kpi_rows=kpi_rows,
            expense_rows=expenses,
            attribution_rows=attrs,
            decision_row=_decision_row(),
        )
        by_campaign = {item["campaign_id"]: item for item in report["campaigns"]}
        self.assertIn("stronger_by_revenue_volume", by_campaign["24375352"]["reasons"])
        self.assertIn("stronger_by_roas", by_campaign["24375352"]["reasons"])
        self.assertIn("stronger_by_stability", by_campaign["24375352"]["reasons"])
        self.assertIn("weaker_by_revenue_volume:24375352", by_campaign["24375331"]["reasons"])
        self.assertIn("weaker_by_roas:24375352", by_campaign["24375331"]["reasons"])
        self.assertIn("weaker_by_stability:24375352", by_campaign["24375331"]["reasons"])


if __name__ == "__main__":
    unittest.main()
