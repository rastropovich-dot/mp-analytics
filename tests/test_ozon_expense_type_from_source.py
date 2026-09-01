import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader


CPC_CAMPAIGN = {"id": "111", "paymentType": "CPC", "title": "Оплата за клик"}


def report(campaign_id="111", sku="900", spend=1000.0, date="2026-07-13"):
    return {campaign_id: {"rows": [{
        "date": date, "sku": sku, "moneySpent": spend, "campaignId": campaign_id,
    }]}}


class ArchivedCampaignRegressionTest(unittest.TestCase):
    """2026-07-13: при бэкфилле спустя 49 дней архивных кампаний уже нет в
    /api/client/campaign, метаданные пустые, и расход уезжал в advertising_other."""

    def test_missing_campaign_metadata_no_longer_becomes_advertising_other(self):
        rows, counters = loader.build_rows(
            report(), campaigns_by_id={}, date_from="2026-07-13",
            source_expense_type=loader.CPC_SOURCE_EXPENSE_TYPE,
        )
        self.assertEqual([r["expense_type"] for r in rows], ["advertising_clicks"])
        self.assertEqual(counters["expense_type_from_source_without_campaign_metadata"], 1)

    def test_old_behaviour_is_what_produced_advertising_other(self):
        # Без source_expense_type воспроизводится прежний дефект.
        rows, _ = loader.build_rows(report(), campaigns_by_id={}, date_from="2026-07-13")
        self.assertEqual([r["expense_type"] for r in rows], ["advertising_other"])

    def test_spend_is_preserved_either_way(self):
        fixed, _ = loader.build_rows(report(), {}, "2026-07-13",
                                     source_expense_type=loader.CPC_SOURCE_EXPENSE_TYPE)
        self.assertEqual(sum(r["expense_amount"] for r in fixed), 1000.0)

    def test_present_metadata_still_yields_clicks(self):
        rows, counters = loader.build_rows(
            report(), campaigns_by_id={"111": CPC_CAMPAIGN}, date_from="2026-07-13",
            source_expense_type=loader.CPC_SOURCE_EXPENSE_TYPE,
        )
        self.assertEqual([r["expense_type"] for r in rows], ["advertising_clicks"])
        self.assertEqual(counters["expense_type_from_source_without_campaign_metadata"], 0)


class ResolveExpenseTypePriorityTest(unittest.TestCase):
    def test_source_wins_over_campaign_text(self):
        # Текст говорит "за заказ", но источник знает, что это CPC-выгрузка.
        misleading = {"id": "111", "title": "Оплата за заказ 5%"}
        self.assertEqual(
            loader.resolve_expense_type({}, misleading, source_expense_type="advertising_clicks"),
            "advertising_clicks",
        )

    def test_explicit_operator_map_wins_over_source(self):
        with mock.patch.dict(loader.EXPLICIT_CAMPAIGN_TYPES,
                             {"advertising_order_5": {"777"}}, clear=False):
            self.assertEqual(
                loader.resolve_expense_type(
                    {"campaignId": "777"}, {}, source_expense_type="advertising_clicks"),
                "advertising_order_5",
            )

    def test_text_classification_remains_the_fallback(self):
        self.assertEqual(
            loader.resolve_expense_type({}, {"id": "1", "title": "Оплата за клик"}),
            "advertising_clicks",
        )
        self.assertEqual(loader.resolve_expense_type({}, {"id": "2"}), "advertising_other")

    def test_ambiguous_cases_are_counted(self):
        counters = {}
        from collections import defaultdict
        counters = defaultdict(int)
        loader.resolve_expense_type({}, {"id": "2"}, counters=counters)
        self.assertEqual(counters["expense_type_from_campaign_text"], 1)
        loader.resolve_expense_type({}, {}, source_expense_type="advertising_clicks", counters=counters)
        self.assertEqual(counters["expense_type_from_source"], 1)


if __name__ == "__main__":
    unittest.main()
