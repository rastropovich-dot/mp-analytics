import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader


def _campaign(campaign_id="24375352", title="F000283615", placement=None):
    if placement is None:
        placement = ["PLACEMENT_TOP_PROMOTION"]
    return {
        "id": str(campaign_id),
        "title": title,
        "state": "CAMPAIGN_STATE_RUNNING",
        "advObjectType": "SKU",
        "PaymentType": "CPC",
        "placement": placement,
        "budget": "0",
        "dailyBudget": "0",
        "weeklyBudget": "37050000000",
        "budgetType": "PRODUCT_CAMPAIGN_BUDGET_TYPE_WEEKLY",
        "expenseStrategy": "DAILY_BUDGET",
        "productCampaignMode": "PRODUCT_CAMPAIGN_MODE_AUTO",
        "productAutopilotStrategy": "TARGET_BIDS",
        "createdAt": "2026-04-04T15:34:51.325475Z",
        "updatedAt": "2026-05-08T21:32:33.208026Z",
    }


class OzonCampaignMetadataSnapshotTests(unittest.TestCase):
    def test_build_rows_maps_campaign_fields(self):
        rows = loader.build_ozon_campaign_metadata_snapshot_rows(
            [_campaign()],
            snapshot_date="2026-05-16",
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["snapshot_date"], "2026-05-16")
        self.assertEqual(row["marketplace_code"], "ozon")
        self.assertEqual(row["campaign_id"], "24375352")
        self.assertEqual(row["title"], "F000283615")
        self.assertEqual(row["state"], "CAMPAIGN_STATE_RUNNING")
        self.assertEqual(row["adv_object_type"], "SKU")
        self.assertEqual(row["payment_type"], "CPC")
        self.assertEqual(row["placement"], ["PLACEMENT_TOP_PROMOTION"])
        self.assertEqual(row["daily_budget"], "0")
        self.assertEqual(row["weekly_budget"], "37050000000")
        self.assertEqual(row["product_campaign_mode"], "PRODUCT_CAMPAIGN_MODE_AUTO")
        self.assertEqual(row["product_autopilot_strategy"], "TARGET_BIDS")
        self.assertIn("captured_at", row)
        self.assertEqual(row["raw_campaign_json"]["id"], "24375352")

    def test_dry_run_filters_requested_campaigns_and_does_not_write(self):
        client = mock.Mock()
        client.list_campaigns.return_value = [
            _campaign("24375352", placement=["PLACEMENT_TOP_PROMOTION"]),
            _campaign("24375331", placement=["PLACEMENT_SEARCH_AND_CATEGORY"]),
            _campaign("99999999", title="OTHER"),
        ]

        summary = loader.campaign_metadata_snapshot_dry_run(
            client,
            snapshot_date="2026-05-16",
            campaign_ids=["24375331", "24375352"],
        )

        client.list_campaigns.assert_called_once_with()
        self.assertEqual(summary["mode"], "campaign_metadata_snapshot_dry_run")
        self.assertEqual(summary["db_writes"], 0)
        self.assertEqual(summary["campaign_mutations"], 0)
        self.assertEqual(summary["total_campaigns"], 3)
        self.assertEqual(summary["matched_campaign_count"], 2)
        self.assertEqual(
            [row["campaign_id"] for row in summary["campaign_rows"]],
            ["24375352", "24375331"],
        )
        self.assertIn("title", summary["present_fields"])
        self.assertIn("payment_type", summary["present_fields"])
        self.assertNotIn("title", summary["missing_fields"])

    def test_dry_run_can_use_preloaded_campaigns_without_http_calls(self):
        client = mock.Mock()

        summary = loader.campaign_metadata_snapshot_dry_run(
            client,
            snapshot_date="2026-05-16",
            campaign_ids=["24375352"],
            campaigns=[_campaign("24375352")],
        )

        client.list_campaigns.assert_not_called()
        self.assertEqual(summary["matched_campaign_count"], 1)
        self.assertEqual(summary["campaign_rows"][0]["campaign_id"], "24375352")

    def test_field_summary_marks_missing_values(self):
        rows = loader.build_ozon_campaign_metadata_snapshot_rows(
            [
                {
                    "id": "24375352",
                    "title": "F000283615",
                    "state": "CAMPAIGN_STATE_RUNNING",
                    "advObjectType": "SKU",
                    "PaymentType": "CPC",
                }
            ],
            snapshot_date="2026-05-16",
        )

        summary = loader.build_campaign_metadata_field_summary(rows)
        self.assertIn("title", summary["present_fields"])
        self.assertIn("state", summary["present_fields"])
        self.assertIn("placement", summary["missing_fields"])
        self.assertIn("daily_budget", summary["missing_fields"])


if __name__ == "__main__":
    unittest.main()
