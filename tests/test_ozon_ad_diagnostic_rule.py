import unittest
from unittest import mock

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


def _attr_row(
    sale_date="2026-05-16",
    campaign_id="24375352",
    spend=1500.0,
    orders=1,
    revenue=110823.0,
    clicks=100,
    views=4000,
):
    return {
        "sale_date": sale_date,
        "marketplace_code": "ozon",
        "marketplace_sku": "1300079194",
        "ad_source": "cpc",
        "campaign_id": str(campaign_id),
        "ad_spend": spend,
        "ad_orders_qty": orders,
        "ad_orders_revenue": revenue,
        "ad_clicks": clicks,
        "ad_views": views,
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
    def test_article_unit_costs_preferred_over_known_fallback(self):
        cost, source, warning = rule.resolve_cogs_for_sku(
            "ozon",
            "1300079194",
            "F000283615",
            "2026-05-16",
            None,
            "manual_or_default",
            article_costs={"F000283615": 40000.0},
        )
        self.assertEqual(cost, 40000.0)
        self.assertEqual(source, "article_unit_costs")
        self.assertIsNone(warning)

    def test_cli_cogs_still_works(self):
        cost, source, warning = rule.resolve_cogs_for_sku(
            "ozon",
            "999",
            "X",
            "2026-05-16",
            12345.0,
            "manual_or_default",
            article_costs={},
        )
        self.assertEqual(cost, 12345.0)
        self.assertEqual(source, "cli")
        self.assertIsNone(warning)

    def test_missing_article_unit_costs_table_does_not_crash(self):
        api_error = rule.APIError({"message": "Could not find the table 'public.article_unit_costs' in the schema cache", "code": "PGRST205"})
        with mock.patch.object(rule, "execute_read_with_retry", side_effect=api_error):
            mapping, warning = rule.load_article_unit_costs("ozon", ["F000283615"], "2026-05-16")
        self.assertEqual(mapping, {})
        self.assertEqual(warning, "article_unit_costs_table_missing")

    def test_known_sku_fallback_still_works_for_1300079194(self):
        cost, source, warning = rule.resolve_cogs_for_sku(
            "ozon",
            "1300079194",
            "F000283615",
            "2026-05-16",
            None,
            "manual_or_default",
            article_costs={},
        )
        self.assertEqual(cost, 32963.0)
        self.assertEqual(source, "known_sku_cogs")
        self.assertIsNone(warning)

    def test_cogs_source_shown_in_output(self):
        report = rule.build_report(
            "ozon",
            "1300079194",
            "2026-05-16",
            ["24375352"],
            32963,
            kpi_rows=[
                _kpi_row(kpi_date="2026-05-16"),
                _kpi_row(kpi_date="2026-05-15", buyouts_qty=2, buyouts_revenue=210000),
                _kpi_row(kpi_date="2026-05-14", buyouts_qty=2, buyouts_revenue=205000),
            ],
            expense_rows=[_expense_row("2026-05-16", "advertising_clicks", 1000)],
            attribution_rows=[_attr_row("2026-05-16", "24375352", 500, 1, 50000)],
            decision_row=_decision_row(),
            cogs_source="known_sku_cogs",
            cogs_lookup_warning="article_unit_costs_table_missing",
        )
        self.assertEqual(report["cogs_source"], "known_sku_cogs")
        self.assertEqual(report["cogs_lookup_warning"], "article_unit_costs_table_missing")

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
            selected_cpo_source_rows=[
                {
                    "sale_date": "2026-05-16",
                    "ordered_sku": "other-sku",
                    "promoted_sku": "other-sku",
                    "spend": 1000.0,
                }
            ],
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
            _kpi_row(kpi_date="2026-05-16", ad_spend=25417.14, orders_revenue=221646),
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
        self.assertIn("orders_revenue_denominator_mismatch", report["eligibility"]["sku_total_economics_reasons"])
        self.assertIn("buyout_economics_caution=0.1137", report["eligibility"]["sku_total_economics_reasons"])
        self.assertEqual(report["campaigns"][0]["status"], "GREEN")
        self.assertEqual(report["final_recommendation"]["status"], "YELLOW")
        self.assertEqual(report["final_recommendation"]["action"], "diagnostic_only_hold")

    def test_selected_cpo_included_in_total_but_excluded_from_controllable_cpc_spend(self):
        kpi_rows = [
            _kpi_row(kpi_date="2026-05-16", ad_spend=25417.14, orders_revenue=221646),
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
        self.assertEqual(report["sku_economics"]["orders_revenue_source"], "ad_attributed_plus_organic")
        self.assertAlmostEqual(report["sku_economics"]["orders_revenue_from_kpi"], 221646.0, places=2)
        self.assertAlmostEqual(report["sku_economics"]["orders_revenue_from_ad_plus_organic"], 332469.0, places=2)
        self.assertAlmostEqual(report["sku_economics"]["orders_revenue_used_for_tacos"], 332469.0, places=2)
        self.assertAlmostEqual(report["sku_economics"]["orders_revenue_mismatch_abs"], 110823.0, places=2)
        self.assertAlmostEqual(report["sku_economics"]["orders_revenue_mismatch_pct"], 0.3333, places=4)
        self.assertAlmostEqual(report["sku_economics"]["total_orders_revenue"], 332469.0, places=2)
        self.assertAlmostEqual(report["sku_economics"]["ad_attributed_revenue"], 110823.0, places=2)
        self.assertAlmostEqual(report["sku_economics"]["organic_revenue"], 221646.0, places=2)
        self.assertAlmostEqual(report["sku_economics"]["total_order_tacos"], 0.0764, places=4)
        self.assertAlmostEqual(report["sku_economics"]["cpc_order_tacos"], 0.0101, places=4)
        self.assertAlmostEqual(report["sku_economics"]["selected_cpo_order_tacos"], 0.0663, places=4)
        self.assertAlmostEqual(report["sku_economics"]["buyout_tacos"], 0.1137, places=4)
        self.assertAlmostEqual(report["campaigns"][0]["windows"]["5d"]["revenue"], 110823.0, places=2)
        self.assertFalse(report["final_recommendation"]["live_action_allowed"])

    def test_selected_cpo_unknown_is_surfaced_without_downgrading_cpc_campaign(self):
        kpi_rows = [
            _kpi_row(kpi_date="2026-05-16", ad_spend=3369.84, orders_revenue=221646, ad_orders_revenue=110823, organic_orders_revenue=221646),
            _kpi_row(kpi_date="2026-05-15", buyouts_qty=2, buyouts_revenue=210000),
            _kpi_row(kpi_date="2026-05-14", buyouts_qty=2, buyouts_revenue=205000),
        ]
        expenses = [
            _expense_row("2026-05-16", "advertising_clicks", 3369.84),
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
            selected_cpo_source_rows=[],
        )
        self.assertEqual(report["sku_economics"]["selected_cpo_status"], "not_loaded_unknown")
        self.assertEqual(report["sku_economics"]["selected_cpo_warning"], "selected_cpo_unknown_may_understate_ad_spend")
        self.assertIn("selected_cpo_unknown_may_understate_ad_spend", report["eligibility"]["sku_total_economics_reasons"])
        self.assertEqual(report["campaigns"][0]["status"], "GREEN")

    def test_total_order_tacos_uses_total_orders_revenue_including_organic(self):
        kpi_rows = [
            _kpi_row(kpi_date="2026-05-16", orders_revenue=221646, ad_orders_revenue=110823, organic_orders_revenue=221646),
            _kpi_row(kpi_date="2026-05-15", buyouts_qty=2, buyouts_revenue=210000),
            _kpi_row(kpi_date="2026-05-14", buyouts_qty=2, buyouts_revenue=205000),
        ]
        expenses = [
            _expense_row("2026-05-16", "advertising_clicks", 3369.84),
            _expense_row("2026-05-16", "advertising_order_selected_cpo", 22047.30),
        ]
        report = rule.build_report(
            "ozon",
            "1300079194",
            "2026-05-16",
            ["24375352"],
            32963,
            kpi_rows=kpi_rows,
            expense_rows=expenses,
            attribution_rows=[_attr_row("2026-05-16", "24375352", 1697.90, 1, 110823)],
            decision_row=_decision_row(),
        )
        self.assertAlmostEqual(report["sku_economics"]["total_order_tacos"], 0.0764, places=4)
        self.assertAlmostEqual(report["sku_economics"]["cpc_order_tacos"], 0.0101, places=4)
        self.assertAlmostEqual(report["sku_economics"]["selected_cpo_order_tacos"], 0.0663, places=4)

    def test_kpi_orders_revenue_mismatch_is_reported_in_diagnostics(self):
        kpi_rows = [
            _kpi_row(kpi_date="2026-05-16", orders_revenue=221646, ad_orders_revenue=110823, organic_orders_revenue=221646),
            _kpi_row(kpi_date="2026-05-15", buyouts_qty=2, buyouts_revenue=210000),
            _kpi_row(kpi_date="2026-05-14", buyouts_qty=2, buyouts_revenue=205000),
        ]
        report = rule.build_report(
            "ozon",
            "1300079194",
            "2026-05-16",
            ["24375352"],
            32963,
            kpi_rows=kpi_rows,
            expense_rows=[_expense_row("2026-05-16", "advertising_clicks", 3369.84)],
            attribution_rows=[_attr_row("2026-05-16", "24375352", 1697.90, 1, 110823)],
            decision_row=_decision_row(),
        )
        self.assertIn("orders_revenue_denominator_mismatch", report["eligibility"]["sku_total_economics_reasons"])

    def test_high_buyout_tacos_alone_does_not_make_healthy_campaign_red(self):
        kpi_rows = [
            _kpi_row(kpi_date="2026-05-16", orders_revenue=332469, buyouts_revenue=223513),
            _kpi_row(kpi_date="2026-05-15", buyouts_qty=2, buyouts_revenue=210000),
            _kpi_row(kpi_date="2026-05-14", buyouts_qty=2, buyouts_revenue=205000),
        ]
        expenses = [
            _expense_row("2026-05-16", "advertising_clicks", 3369.84),
            _expense_row("2026-05-16", "advertising_order_selected_cpo", 22047.30),
            _expense_row("2026-05-16", "commission", 48772.92),
        ]
        report = rule.build_report(
            "ozon",
            "1300079194",
            "2026-05-16",
            ["24375352"],
            32963,
            kpi_rows=kpi_rows,
            expense_rows=expenses,
            attribution_rows=[
                _attr_row("2026-05-16", "24375352", 1697.90, 1, 110823),
                _attr_row("2026-05-15", "24375352", 1716.53, 2, 221300),
            ],
            decision_row=_decision_row(),
        )
        self.assertEqual(report["eligibility"]["cpc_control_eligibility_status"], "eligible_for_diagnostic")
        self.assertEqual(report["campaigns"][0]["status"], "GREEN")
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

    def test_campaign_aggregation_includes_clicks_views_and_derived_metrics(self):
        report = rule.build_report(
            "ozon",
            "1300079194",
            "2026-05-16",
            ["24375352"],
            32963,
            kpi_rows=[
                _kpi_row(kpi_date="2026-05-16"),
                _kpi_row(kpi_date="2026-05-15", buyouts_qty=2, buyouts_revenue=210000),
                _kpi_row(kpi_date="2026-05-14", buyouts_qty=2, buyouts_revenue=205000),
            ],
            expense_rows=[_expense_row("2026-05-16", "advertising_clicks", 3369.84)],
            attribution_rows=[
                _attr_row("2026-05-16", "24375352", 1697.90, 1, 110823, clicks=142, views=5112),
                _attr_row("2026-05-15", "24375352", 1409.74, 0, 0, clicks=127, views=4830),
                _attr_row("2026-05-14", "24375352", 1941.28, 0, 0, clicks=156, views=5587),
                _attr_row("2026-05-13", "24375352", 1716.53, 2, 221300, clicks=152, views=5184),
                _attr_row("2026-05-12", "24375352", 1412.30, 1, 112863, clicks=122, views=4674),
            ],
            decision_row=_decision_row(),
        )
        campaign = report["campaigns"][0]
        self.assertEqual(campaign["views_5d"], 25387.0)
        self.assertEqual(campaign["clicks_5d"], 699.0)
        self.assertAlmostEqual(campaign["ctr_5d"], 0.0275, places=4)
        self.assertAlmostEqual(campaign["cvr_5d"], 0.0057, places=4)
        self.assertAlmostEqual(campaign["avg_cpc_5d"], 11.70, places=2)
        self.assertAlmostEqual(campaign["cost_per_ad_order_5d"], 2044.44, places=2)

    def test_low_ctr_and_low_cvr_add_watch_reasons_without_live_action(self):
        report = rule.build_report(
            "ozon",
            "1300079194",
            "2026-05-16",
            ["24375331"],
            32963,
            kpi_rows=[
                _kpi_row(kpi_date="2026-05-16"),
                _kpi_row(kpi_date="2026-05-15", buyouts_qty=2, buyouts_revenue=210000),
                _kpi_row(kpi_date="2026-05-14", buyouts_qty=2, buyouts_revenue=205000),
            ],
            expense_rows=[_expense_row("2026-05-16", "advertising_clicks", 3000)],
            attribution_rows=[
                _attr_row("2026-05-16", "24375331", 1000, 1, 50000, clicks=250, views=20000),
                _attr_row("2026-05-15", "24375331", 1000, 0, 0, clicks=250, views=20000),
                _attr_row("2026-05-14", "24375331", 1000, 0, 0, clicks=250, views=20000),
            ],
            decision_row=_decision_row(),
        )
        campaign = report["campaigns"][0]
        self.assertIn("low_ctr_watch", campaign["reasons"])
        self.assertIn("low_cvr_watch", campaign["reasons"])
        self.assertFalse(report["final_recommendation"]["live_action_allowed"])

    def test_batch_selects_only_ready_ok_clean_sku(self):
        ready_row = {
            "kpi_date": "2026-05-16",
            "marketplace_code": "ozon",
            "marketplace_sku": "1300079194",
            "decision_status": "ready",
            "data_quality_status": "ok",
            "organic_reconciliation_status": "clean",
        }
        kpi_row = _kpi_row()
        with mock.patch.object(rule, "load_ready_decision_rows", return_value=[ready_row]), \
            mock.patch.object(rule, "fetch_all", return_value=[kpi_row]), \
            mock.patch.object(rule, "discover_campaign_ids", return_value=["24375352"]), \
            mock.patch.object(rule, "run_dry_report", return_value={
                "sku": "1300079194",
                "sku_economics": {
                    "orders_revenue": 221646.0,
                    "buyouts_revenue": 223513.0,
                    "total_ad_spend": 25417.14,
                    "cpc_spend": 3369.84,
                    "selected_cpo_spend": 22047.3,
                    "total_order_tacos": 0.0764,
                    "cpc_order_tacos": 0.0101,
                    "selected_cpo_order_tacos": 0.0663,
                    "buyout_tacos": 0.1137,
                    "cogs_missing": False,
                },
                "campaigns": [{"campaign_id": "24375352", "status": "GREEN"}],
                "eligibility": {"reasons": []},
                "final_recommendation": {"status": "YELLOW", "action": "diagnostic_only_hold", "live_action_allowed": False},
            }):
            report = rule.run_batch_dry_report("ozon", "2026-05-16", 20, None, "manual_or_default")
        self.assertEqual(len(report["rows"]), 1)
        self.assertEqual(report["rows"][0]["marketplace_sku"], "1300079194")
        self.assertFalse(report["rows"][0]["live_action_allowed"])

    def test_cogs_missing_does_not_crash_batch_or_single(self):
        kpi_rows = [
            _kpi_row(kpi_date="2026-05-16", sku="999", orders_revenue=100000, ad_orders_revenue=50000, organic_orders_revenue=50000),
            _kpi_row(kpi_date="2026-05-15", sku="999", buyouts_qty=2, buyouts_revenue=210000),
            _kpi_row(kpi_date="2026-05-14", sku="999", buyouts_qty=2, buyouts_revenue=205000),
        ]
        report = rule.build_report(
            "ozon",
            "999",
            "2026-05-16",
            ["24375352"],
            None,
            kpi_rows=kpi_rows,
            expense_rows=[_expense_row("2026-05-16", "advertising_clicks", 1000)],
            attribution_rows=[_attr_row("2026-05-16", "24375352", 500, 1, 50000)],
            decision_row=_decision_row(),
        )
        self.assertTrue(report["sku_economics"]["cogs_missing"])
        self.assertIn("cogs_missing", report["eligibility"]["sku_total_economics_reasons"])
        self.assertFalse(report["final_recommendation"]["live_action_allowed"])

    def test_batch_output_contains_order_and_buyout_tacos(self):
        row = rule.summarize_batch_row(
            {
                "sku": "1300079194",
                "article": "F000283615",
                "product_name": "Item",
                "sku_economics": {
                    "orders_revenue": 221646.0,
                    "buyouts_revenue": 223513.0,
                    "total_ad_spend": 25417.14,
                    "cpc_spend": 3369.84,
                    "selected_cpo_spend": 22047.3,
                    "total_order_tacos": 0.0764,
                    "cpc_order_tacos": 0.0101,
                    "selected_cpo_order_tacos": 0.0663,
                    "buyout_tacos": 0.1137,
                    "cogs_missing": False,
                },
                "campaigns": [
                    {"campaign_id": "24375352", "status": "GREEN"},
                    {"campaign_id": "24375331", "status": "YELLOW"},
                ],
                "eligibility": {"reasons": ["selected_cpo_pressure"]},
                "final_recommendation": {"status": "YELLOW", "action": "diagnostic_only_hold", "live_action_allowed": False},
            }
        )
        self.assertIn("total_order_tacos", row)
        self.assertIn("buyout_tacos", row)
        self.assertFalse(row["live_action_allowed"])


if __name__ == "__main__":
    unittest.main()
