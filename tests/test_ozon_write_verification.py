import unittest
from unittest import mock

import loaders.ozon_performance_ads_loader as loader


def expense_row(sku, spend, expense_type="advertising_clicks"):
    return {"expense_date": "2026-07-13", "marketplace_code": "ozon",
            "marketplace_sku": sku, "expense_type": expense_type, "expense_amount": spend}


def summary(completed_total, status="success", target_date="2026-07-13"):
    return {"target_date": target_date, "overall_status": status,
            "cpc_campaign_units_completed_total": completed_total}


class HardFailTest(unittest.TestCase):
    def test_completed_units_with_zero_rows_is_not_success(self):
        s = summary(1123)
        with mock.patch.object(loader, "read_neighbour_cpc_spend_median", return_value=None):
            v = loader.verify_write_result(s, [])
        self.assertEqual(s["overall_status"], "completed_without_data")
        self.assertTrue(v["hard_fail"])
        self.assertEqual(v["hard_fail_reason"], "completed_units_without_written_rows")

    def test_none_written_rows_is_treated_as_zero(self):
        s = summary(970)
        with mock.patch.object(loader, "read_neighbour_cpc_spend_median", return_value=None):
            loader.verify_write_result(s, None)
        self.assertEqual(s["overall_status"], "completed_without_data")

    def test_success_survives_when_rows_were_written(self):
        s = summary(1123)
        with mock.patch.object(loader, "read_neighbour_cpc_spend_median", return_value=None):
            v = loader.verify_write_result(s, [expense_row("1", 100.0)])
        self.assertEqual(s["overall_status"], "success")
        self.assertFalse(v["hard_fail"])

    def test_zero_completed_units_is_not_downgraded(self):
        # Нечего было качать — это не провал записи.
        s = summary(0)
        with mock.patch.object(loader, "read_neighbour_cpc_spend_median", return_value=None):
            loader.verify_write_result(s, [])
        self.assertEqual(s["overall_status"], "success")

    def test_already_partial_status_is_not_overwritten(self):
        # Такая дата и так видна в бэклоге, статус менять незачем.
        s = summary(500, status="partial_quota")
        with mock.patch.object(loader, "read_neighbour_cpc_spend_median", return_value=None):
            v = loader.verify_write_result(s, [])
        self.assertEqual(s["overall_status"], "partial_quota")
        self.assertTrue(v["hard_fail"])


class NeighbourMedianWarningTest(unittest.TestCase):
    def test_low_spend_warns_but_never_blocks(self):
        s = summary(1123)
        with mock.patch.object(loader, "read_neighbour_cpc_spend_median", return_value=123727.14):
            v = loader.verify_write_result(s, [expense_row("1", 141.28)])
        self.assertEqual(s["overall_status"], "success", "медиана — предупреждение, не блокировка")
        self.assertTrue(v["warnings"])
        self.assertFalse(v["hard_fail"])

    def test_normal_spend_produces_no_warning(self):
        s = summary(1123)
        with mock.patch.object(loader, "read_neighbour_cpc_spend_median", return_value=100000.0):
            v = loader.verify_write_result(s, [expense_row("1", 90000.0)])
        self.assertEqual(v["warnings"], [])

    def test_missing_neighbour_data_is_tolerated(self):
        s = summary(1123)
        with mock.patch.object(loader, "read_neighbour_cpc_spend_median", return_value=None):
            v = loader.verify_write_result(s, [expense_row("1", 100.0)])
        self.assertIsNone(v["neighbour_cpc_spend_median"])
        self.assertEqual(v["warnings"], [])


class RealDatesTest(unittest.TestCase):
    """Фактические цифры обеих дат. Ни одна не даёт rows_written = 0:
    2026-05-12 записала 1 строку / 1 412,30 ₽, 2026-07-13 — 70 строк / 14 216,52 ₽.
    Жёсткий отказ их НЕ ловит, ловит предупреждение по медиане."""

    def test_2026_05_12_is_caught_by_warning_not_by_hard_fail(self):
        s = summary(970, target_date="2026-05-12")
        with mock.patch.object(loader, "read_neighbour_cpc_spend_median", return_value=96756.65):
            v = loader.verify_write_result(s, [expense_row("1", 1412.30)])
        self.assertFalse(v["hard_fail"])
        self.assertTrue(v["warnings"])
        self.assertEqual(s["overall_status"], "success")

    def test_2026_07_13_is_caught_by_warning_not_by_hard_fail(self):
        rows = [expense_row(str(i), 14216.52 / 70, "advertising_other") for i in range(70)]
        s = summary(1123, target_date="2026-07-13")
        with mock.patch.object(loader, "read_neighbour_cpc_spend_median", return_value=123727.14):
            v = loader.verify_write_result(s, rows)
        self.assertFalse(v["hard_fail"])
        self.assertTrue(v["warnings"])
        self.assertAlmostEqual(v["ad_spend_written"], 14216.52, places=1)


class WrittenSummaryTest(unittest.TestCase):
    def test_breakdown_is_by_expense_type(self):
        rows = [expense_row("1", 100.0), expense_row("2", 50.0),
                expense_row("3", 25.0, "advertising_other")]
        v = loader.summarize_written_expense_rows(rows)
        self.assertEqual(v["rows_written"], 3)
        self.assertEqual(v["spend_written"], 175.0)
        self.assertEqual(v["by_expense_type"]["advertising_clicks"], {"rows": 2, "spend": 150.0})
        self.assertEqual(v["by_expense_type"]["advertising_other"], {"rows": 1, "spend": 25.0})

    def test_save_rows_returns_empty_list_when_nothing_to_write(self):
        self.assertEqual(loader.save_rows([]), [])


class ConsumersUnderstandNewStatusTest(unittest.TestCase):
    """Именно молчаливый провал в ветку «неизвестный статус → всё хорошо»
    и потерял 24 даты."""

    def test_recovery_worker_keeps_the_date_in_the_backlog(self):
        import scripts.ozon_performance_recovery_worker as worker
        row = {"run_status": "completed_without_data", "cpc_status": "success",
               "cpc_pending_campaigns": 0, "cpc_campaign_units_pending_total": 0}
        self.assertTrue(worker.is_partial_ads_candidate(row))
        self.assertFalse(worker.is_complete_status_row(row))

    def test_alerts_do_not_stay_silent(self):
        import alerts_telegram
        import inspect
        source = inspect.getsource(alerts_telegram.get_ozon_report_completeness)
        self.assertIn("completed_without_data", source)

    def test_decision_layer_flags_it_as_risky(self):
        import reports_sku_decision_candidates as candidates
        import inspect
        self.assertIn("completed_without_data", inspect.getsource(candidates))

    def test_pipeline_precheck_does_not_treat_it_as_loaded(self):
        # .eq("run_status","success") не совпадёт — pre-phase пропускается.
        self.assertNotEqual(loader.COMPLETED_WITHOUT_DATA_STATUS, "success")


if __name__ == "__main__":
    unittest.main()
