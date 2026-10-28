import unittest
from unittest import mock

import alerts_telegram as alerts

_REVIEWS_REAL = alerts.get_ozon_reviews_lines            # до заглушки модуля — для прямых тестов строки

_BONUS_STUB = mock.patch.object(alerts, "get_ozon_ad_bonus_lines", return_value=[])   # 53-я: строки бонусов читают базу — в тестах заглушка
_REVIEWS_STUB = mock.patch.object(alerts, "get_ozon_reviews_lines", return_value=[])  # «Отзывы» (25.10): строка читает леджер — в тестах заглушка


def setUpModule():
    _BONUS_STUB.start(); _REVIEWS_STUB.start()


def tearDownModule():
    _BONUS_STUB.stop(); _REVIEWS_STUB.stop()


class ReviewsLine(unittest.TestCase):
    class _Sb:
        def __init__(self, rows=None, exc=None):
            self.rows, self.exc, self.calls = rows, exc, []

        def table(self, name):
            self.calls.append(name)
            return self

        def __getattr__(self, name):
            return lambda *a, **k: (self.calls.append((name, a)) or self)

        def execute(self):
            if self.exc:
                raise self.exc
            return type("R", (), {"data": self.rows})()

    def test_reviews_line_from_ledger(self):
        sb = self._Sb([{"type_id": 95, "amount": "-12000.40"}, {"type_id": 96, "amount": "-305"}])
        [line] = self._real()("2026-10-24", sb)
        self.assertEqual(line, "Отзывы (баллы): 12 305 руб. — за баллы (95) 12 000, ускоренный сбор (96) 305; в «Рекламе» и «прочих» не учтены — своя статья (26.10).")
        self.assertIn(("in_", ("type_id", [95, 96])), sb.calls)

    def test_no_rows_no_line_and_failure_is_named(self):
        self.assertEqual(self._real()("2026-10-24", self._Sb([])), [])
        self.assertEqual(self._real()("2026-10-24", self._Sb(exc=RuntimeError("x"))), ["Отзывы (баллы): не прочитано — RuntimeError"])

    @staticmethod
    def _real():
        return _REVIEWS_REAL



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


def _presence(mkpi=True, skpi=True, organic=True, ads_expenses=True, ads_attribution=False,
              fbo=True, fbs=True):
    """table_has_rows по имени таблицы, а не по порядку вызовов: порядок — деталь реализации."""
    by_table = {
        "daily_marketplace_kpi": mkpi,
        "daily_sku_kpi": skpi,
        "ozon_daily_sku_organic": organic,
        "marketplace_expenses": ads_expenses,
        "ozon_daily_sku_ad_attribution": ads_attribution,
    }

    def side_effect(table_name, filters):
        if table_name == "marketplace_orders":
            schema = next(v for a, b, v in filters if "order_schema" in (a, b))
            return {"fbo": fbo, "fbs": fbs}[schema]
        return by_table[table_name]

    return side_effect


class OzonCompletenessGateTests(unittest.TestCase):
    def test_table_has_rows_supports_in_filter_regardless_of_tuple_order(self):
        class _FakeResult:
            def __init__(self, data):
                self.data = data

        class _FakeQuery:
            def __init__(self):
                self.calls = []

            def select(self, _fields):
                return self

            def limit(self, _value):
                return self

            def eq(self, field, value):
                self.calls.append(("eq", field, value))
                return self

            def in_(self, field, value):
                self.calls.append(("in", field, tuple(value)))
                return self

            def execute(self):
                return _FakeResult([{"ok": True}])

        fake_query = _FakeQuery()
        fake_supabase = mock.Mock()
        fake_supabase.table.return_value = fake_query

        with mock.patch.object(alerts, "supabase", fake_supabase):
            self.assertTrue(
                alerts.table_has_rows(
                    "marketplace_expenses",
                    [
                        ("marketplace_code", "eq", "ozon"),
                        ("expense_type", "in", ["advertising_clicks"]),
                    ],
                )
            )
            self.assertTrue(
                alerts.table_has_rows(
                    "marketplace_expenses",
                    [
                        ("eq", "marketplace_code", "ozon"),
                        ("in", "expense_type", ["advertising_clicks"]),
                    ],
                )
            )

        self.assertIn(("in", "expense_type", ("advertising_clicks",)), fake_query.calls)

    def test_missing_organic_is_a_warning_not_a_blocker(self):
        # слово владельца 2026-10-24: органика не гасит Ozon-сводку — оговорка, сводка строится
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows, \
            mock.patch.object(alerts, "get_latest_ozon_performance_status", return_value=None):
            table_has_rows.side_effect = _presence(True, True, False, True, False)
            result = alerts.get_ozon_report_completeness("2026-05-20")
        self.assertTrue(result["complete"])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["warnings"], ["ozon_daily_sku_organic_missing"])
        self.assertTrue(result["organic_missing"])

    def test_missing_organic_with_missing_fbo_orders_stays_incomplete(self):
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows, \
            mock.patch.object(alerts, "get_latest_ozon_performance_status", return_value=None):
            table_has_rows.side_effect = _presence(True, True, False, True, False, fbo=False)
            result = alerts.get_ozon_report_completeness("2026-05-20")
        self.assertFalse(result["complete"])
        self.assertEqual(result["blockers"], ["ozon_fbo_orders_missing"])
        self.assertIn("ozon_daily_sku_organic_missing", result["warnings"])

    def test_summary_without_organic_is_built_with_the_caps_line(self):
        kpi_rows = [_kpi_row("wb"), _kpi_row("ozon"), _kpi_row("ozon", "2026-05-19")]
        ads = {"advertising_clicks": 100, "advertising_order_10": 0, "advertising_order_5": 50, "advertising_order_other": 0,
               "advertising_order_unknown": 0, "advertising_other": 0}
        with mock.patch.object(alerts, "today_local", return_value=alerts.date(2026, 5, 21)), \
            mock.patch.object(alerts, "get_wb_buyout_cohort_summary", return_value="Выкуп по когорте: нет данных."), \
            mock.patch.object(alerts, "get_ozon_ads_breakdown", return_value=ads), \
            mock.patch.object(alerts, "get_ozon_organic_reconciliation", return_value={"available": False}), \
            mock.patch.object(alerts, "get_ozon_report_completeness", return_value={
                "complete": True, "blockers": [], "warnings": ["ozon_daily_sku_organic_missing"], "organic_missing": True}):
            lines = alerts.build_executive_summary(kpi_rows)
        text = "\n".join(lines)
        block = text[text.index("🔵 <b>Ozon вчера</b>"):]
        self.assertTrue(block.startswith("🔵 <b>Ozon вчера</b>\n⚠️ БЕЗ ОРГАНИКИ — ТРЕБУЕТ РАЗБОРА\n"))   # первая строка блока, дословно
        self.assertIn("Заказы: 10 /", block)
        self.assertIn("Реклама: клики", block)
        self.assertIn("Атрибуция (рекламные заказы / органика / доля рекламы): не посчитано: нет органики.", block)
        self.assertNotIn("органика 0", block)
        self.assertNotIn("Атрибуция: реклама", block)
        self.assertNotIn("данные неполные", block)

    def test_completed_day_without_organic_shows_ozon_with_the_caps_line(self):
        kpi_rows = [
            _kpi_row("ozon", "2026-05-20", orders_qty=34, buyouts_qty=20),
            _kpi_row("ozon", "2026-05-19", orders_qty=100, buyouts_qty=80),
            _kpi_row("wb", "2026-05-20"), _kpi_row("wb", "2026-05-19"),
        ]
        with mock.patch.object(alerts, "today_local", return_value=alerts.date(2026, 5, 21)), \
            mock.patch.object(alerts, "get_ozon_report_completeness", return_value={
                "complete": True, "blockers": [], "warnings": ["ozon_daily_sku_organic_missing"], "organic_missing": True}):
            rows = alerts.build_completed_day_alerts(kpi_rows)
        joined = "\n".join(rows)
        self.assertIn("⚠️ БЕЗ ОРГАНИКИ — ТРЕБУЕТ РАЗБОРА", joined)
        self.assertIn("органика не посчитано: нет органики", joined)
        self.assertIn("Ozon: вчера падение заказов", joined)          # вывод по Ozon строится
        self.assertNotIn("полный дневной вывод пропущен", joined)

    def test_missing_ads_marks_ozon_incomplete_not_zero(self):
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows, \
            mock.patch.object(alerts, "get_latest_ozon_performance_status", return_value=None):
            table_has_rows.side_effect = _presence(True, True, True, False, False)
            result = alerts.get_ozon_report_completeness("2026-05-20")
        self.assertFalse(result["complete"])
        self.assertIn("ozon_ads_layer_missing", result["blockers"])
        self.assertFalse(result["ads_present"])

    def test_all_required_layers_present_marks_complete(self):
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows, \
            mock.patch.object(alerts, "get_latest_ozon_performance_status", return_value=None):
            table_has_rows.side_effect = _presence(True, True, True, True, False)
            result = alerts.get_ozon_report_completeness("2026-05-20")
        self.assertTrue(result["complete"])
        self.assertEqual(result["blockers"], [])

    def test_partial_ads_status_marks_ozon_incomplete_even_with_ad_rows(self):
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows, \
            mock.patch.object(alerts, "get_latest_ozon_performance_status", return_value={
                "run_status": "partial_ads",
                "cpc_status": "pending_429",
                "cpo_status": "success",
                "cpc_pending_campaigns": 13,
                "cpc_campaign_units_pending_total": 13,
            }):
            table_has_rows.side_effect = _presence(True, True, True, True, True)
            result = alerts.get_ozon_report_completeness("2026-05-21")
        self.assertFalse(result["complete"])
        self.assertIn("ozon_performance_partial_ads", result["blockers"])
        self.assertIn("ozon_cpc_pending_429", result["blockers"])

    def test_pending_429_alone_marks_ozon_incomplete(self):
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows, \
            mock.patch.object(alerts, "get_latest_ozon_performance_status", return_value={
                "run_status": "running",
                "cpc_status": "pending_429",
                "cpo_status": "success",
                "cpc_pending_campaigns": 0,
                "cpc_campaign_units_pending_total": 0,
            }):
            table_has_rows.side_effect = _presence(True, True, True, True, True)
            result = alerts.get_ozon_report_completeness("2026-05-21")
        self.assertFalse(result["complete"])
        self.assertIn("ozon_cpc_pending_429", result["blockers"])

    def test_pending_campaigns_mark_ozon_incomplete(self):
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows, \
            mock.patch.object(alerts, "get_latest_ozon_performance_status", return_value={
                "run_status": "success",
                "cpc_status": "success",
                "cpo_status": "success",
                "cpc_pending_campaigns": 3,
                "cpc_campaign_units_pending_total": 0,
            }):
            table_has_rows.side_effect = _presence(True, True, True, True, True)
            result = alerts.get_ozon_report_completeness("2026-05-21")
        self.assertFalse(result["complete"])
        self.assertIn("ozon_performance_cpc_incomplete", result["blockers"])

    def test_pending_quota_marks_ozon_incomplete_with_quota_blocker(self):
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows, \
            mock.patch.object(alerts, "get_latest_ozon_performance_status", return_value={
                "run_status": "partial_quota",
                "cpc_status": "pending_quota",
                "cpo_status": "success",
                "cpc_pending_campaigns": 285,
                "cpc_campaign_units_pending_total": 285,
            }):
            table_has_rows.side_effect = _presence(True, True, True, True, True)
            result = alerts.get_ozon_report_completeness("2026-05-25")
        self.assertFalse(result["complete"])
        self.assertIn("ozon_statistics_json_daily_quota_exhausted", result["blockers"])
        self.assertIn("ozon_performance_cpc_incomplete", result["blockers"])

    def test_all_layers_present_with_performance_success_marks_complete(self):
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows, \
            mock.patch.object(alerts, "get_latest_ozon_performance_status", return_value={
                "run_status": "success",
                "cpc_status": "success",
                "cpo_status": "success",
                "cpc_pending_campaigns": 0,
                "cpc_campaign_units_pending_total": 0,
            }):
            table_has_rows.side_effect = _presence(True, True, True, True, True)
            result = alerts.get_ozon_report_completeness("2026-05-21")
        self.assertTrue(result["complete"])
        self.assertTrue(result["performance_status_present"])
        self.assertEqual(result["blockers"], [])

    def test_missing_performance_row_does_not_fail_complete_layers(self):
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows, \
            mock.patch.object(alerts, "get_latest_ozon_performance_status", return_value=None):
            table_has_rows.side_effect = _presence(True, True, True, True, True)
            result = alerts.get_ozon_report_completeness("2026-05-21")
        self.assertTrue(result["complete"])
        self.assertFalse(result["performance_status_present"])

    def test_executive_summary_skips_normal_ozon_block_when_incomplete(self):
        kpi_rows = [
            _kpi_row("wb"),
            _kpi_row("ozon"),
        ]
        with mock.patch.object(alerts, "today_local", return_value=alerts.date(2026, 5, 21)), \
            mock.patch.object(alerts, "get_wb_buyout_cohort_summary", return_value="Выкуп по когорте: нет данных."), \
            mock.patch.object(alerts, "get_ozon_report_completeness", return_value={
                "complete": False,
                "blockers": ["ozon_daily_sku_organic_missing", "ozon_ads_layer_missing"],
            }), \
            mock.patch.object(alerts, "get_ozon_ad_bonus_lines", return_value=["Реклама за счёт бонусов Ozon (не расход): 7 712.23 руб. — кампаний 2"]):
            lines = alerts.build_executive_summary(kpi_rows)
        text = "\n".join(lines)
        self.assertIn("Ozon вчера: данные неполные, управленческий вывод не строим", text)
        self.assertIn("Реклама за счёт бонусов Ozon (не расход): 7 712.23 руб.", text)   # 53-я: строка бонусов и при неполных данных
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
            mock.patch.object(alerts, "get_wb_buyout_cohort_summary", return_value="Выкуп по когорте: нет данных."), \
            mock.patch.object(alerts, "get_ozon_report_completeness", return_value={
                "complete": False,
                "blockers": ["ozon_ads_layer_missing"],
            }):
            rows = alerts.build_completed_day_alerts(kpi_rows)
        joined = "\n".join(rows)
        self.assertIn("<b>Ozon</b>", joined)
        self.assertIn("полный дневной вывод пропущен", joined)
        self.assertNotIn("Ozon: вчера падение заказов", joined)

    def test_wb_summary_unaffected_when_ozon_incomplete(self):
        kpi_rows = [
            _kpi_row("wb"),
            _kpi_row("ozon"),
        ]
        with mock.patch.object(alerts, "today_local", return_value=alerts.date(2026, 5, 21)), \
            mock.patch.object(alerts, "get_wb_buyout_cohort_summary", return_value="Выкуп по когорте: нет данных."), \
            mock.patch.object(alerts, "get_ozon_report_completeness", return_value={
                "complete": False,
                "blockers": ["ozon_daily_sku_organic_missing"],
            }):
            lines = alerts.build_executive_summary(kpi_rows)
        text = "\n".join(lines)
        self.assertIn("🟣 <b>WB вчера</b>", text)

    def test_incomplete_ozon_preserves_section_zero_and_warning_block(self):
        kpi_rows = [
            _kpi_row("wb"),
            _kpi_row("ozon"),
        ]
        with mock.patch.object(alerts, "today_local", return_value=alerts.date(2026, 5, 21)), \
            mock.patch.object(alerts, "get_wb_buyout_cohort_summary", return_value="Выкуп по когорте: нет данных."), \
            mock.patch.object(alerts, "get_ozon_report_completeness", return_value={
                "complete": False,
                "blockers": ["ozon_daily_sku_organic_missing", "ozon_ads_layer_missing"],
            }):
            lines = alerts.build_executive_summary(kpi_rows)
        text = "\n".join(lines)
        self.assertIn("<b>0. Короткая управленческая сводка</b>", text)
        self.assertIn("🟣 <b>WB вчера</b>", text)
        self.assertIn("🔵 <b>Ozon вчера</b>", text)
        self.assertIn("Ozon вчера: данные неполные, управленческий вывод не строим", text)

    def test_build_message_keeps_section_one_when_ozon_incomplete(self):
        kpi_rows = [
            _kpi_row("wb", "2026-05-20"),
            _kpi_row("wb", "2026-05-19", orders_qty=12, buyouts_qty=10),
            _kpi_row("ozon", "2026-05-20"),
            _kpi_row("ozon", "2026-05-19", orders_qty=20, buyouts_qty=15),
        ]
        with mock.patch.object(alerts, "today_local", return_value=alerts.date(2026, 5, 21)), \
            mock.patch.object(alerts, "get_wb_buyout_cohort_summary", return_value="Выкуп по когорте: нет данных."), \
            mock.patch.object(alerts, "get_kpi_rows", return_value=kpi_rows), \
            mock.patch.object(alerts, "overlay_wb_orders_from_sales_funnel", side_effect=lambda rows: rows), \
            mock.patch.object(alerts, "save_today_snapshot", return_value=[]), \
            mock.patch.object(alerts, "build_short_snapshot", return_value=["<b>2. Сегодня на текущий час против вчера</b>"]), \
            mock.patch.object(alerts, "get_ozon_report_completeness", return_value={
                "complete": False,
                "blockers": ["ozon_daily_sku_organic_missing", "ozon_ads_layer_missing"],
            }), \
            mock.patch.object(alerts.supabase, "table") as mock_table:
            mock_table.return_value.select.return_value.order.return_value.order.return_value.limit.return_value.execute.return_value.data = []
            message = alerts.build_message()
        self.assertIn("<b>0. Короткая управленческая сводка</b>", message)
        self.assertIn("<b>1. Полный вчерашний день</b>", message)
        self.assertIn("🟣 <b>WB вчера</b>", message)
        self.assertIn("🔵 <b>Ozon вчера</b>", message)
        self.assertIn("полный дневной вывод пропущен", message)

    def test_complete_ozon_still_renders_normal_details(self):
        kpi_rows = [
            _kpi_row("wb"),
            _kpi_row("ozon"),
        ]
        with mock.patch.object(alerts, "today_local", return_value=alerts.date(2026, 5, 21)), \
            mock.patch.object(alerts, "get_wb_buyout_cohort_summary", return_value="Выкуп по когорте: нет данных."), \
            mock.patch.object(alerts, "get_ozon_report_completeness", return_value={
                "complete": True,
                "blockers": [],
            }), \
            mock.patch.object(alerts, "get_ozon_ads_breakdown", return_value={
                "advertising_clicks": 1200,
                "advertising_order_10": 0,
                "advertising_order_5": 5000,
                "advertising_order_other": 0,
                "advertising_order_unknown": 0,
                "advertising_other": 0,
            }), \
            mock.patch.object(alerts, "get_ozon_organic_reconciliation", return_value={
                "available": True,
                "sum_total_orders_revenue": 25000,
                "sum_ad_orders_revenue": 5000,
                "sum_organic_orders_revenue": 20000,
                "status_counts": {"ok": 1},
                "warning_count": 0,
            }), \
            mock.patch.object(alerts, "get_ozon_ad_bonus_lines", return_value=["Реклама за счёт бонусов Ozon (не расход): 17 440.54 руб. — кампаний 2"]):
            lines = alerts.build_executive_summary(kpi_rows)
        text = "\n".join(lines)
        self.assertIn("🔵 <b>Ozon вчера</b>", text)
        self.assertIn("Реклама: клики", text)
        self.assertIn("Реклама за счёт бонусов Ozon (не расход): 17 440.54 руб.", text)   # 53-я: строкой после рекламы
        self.assertIn("Атрибуция:", text)
        self.assertNotIn("данные неполные", text)

    def test_cpo_other_rate_is_named_not_unrecognised(self):
        """56-я: CPO со ставкой вне 5 / 10 % — «заказ другая ставка», а не «не распознано/прочее»."""
        kpi_rows = [_kpi_row("wb"), _kpi_row("ozon")]
        with mock.patch.object(alerts, "today_local", return_value=alerts.date(2026, 5, 21)), \
            mock.patch.object(alerts, "get_wb_buyout_cohort_summary", return_value="Выкуп по когорте: нет данных."), \
            mock.patch.object(alerts, "get_ozon_report_completeness", return_value={"complete": True, "blockers": []}), \
            mock.patch.object(alerts, "get_ozon_ads_breakdown", return_value={
                "advertising_clicks": 1200, "advertising_order_10": 0, "advertising_order_5": 5000,
                "advertising_order_other": 6000, "advertising_order_unknown": 0, "advertising_other": 0,
            }), \
            mock.patch.object(alerts, "get_ozon_organic_reconciliation", return_value={
                "available": True, "sum_total_orders_revenue": 25000, "sum_ad_orders_revenue": 5000,
                "sum_organic_orders_revenue": 20000, "status_counts": {"ok": 1}, "warning_count": 0,
            }), \
            mock.patch.object(alerts, "get_ozon_ad_bonus_lines", return_value=[]):
            text = "\n".join(alerts.build_executive_summary(kpi_rows))
        self.assertIn("заказ другая ставка", text)
        self.assertNotIn("не распознано", text)

    def test_dry_run_does_not_send_or_save_snapshot(self):
        with mock.patch.object(alerts, "build_message", return_value="preview message") as build_message, \
            mock.patch.object(alerts, "send_telegram") as send_telegram, \
            mock.patch("builtins.print") as print_mock:
            result = alerts.main(["--dry-run"])
        self.assertEqual(result, "preview message")
        build_message.assert_called_once_with(target_date=None, skip_snapshot=True)
        send_telegram.assert_not_called()
        print_mock.assert_any_call("preview message")

    def test_no_send_skip_snapshot_preview_target_date(self):
        with mock.patch.object(alerts, "build_message", return_value="preview message") as build_message, \
            mock.patch.object(alerts, "send_telegram") as send_telegram:
            result = alerts.main(["--no-send", "--skip-snapshot", "--target-date", "2026-05-20"])
        self.assertEqual(result, "preview message")
        build_message.assert_called_once_with(target_date="2026-05-20", skip_snapshot=True)
        send_telegram.assert_not_called()

    def test_get_ozon_report_completeness_checks_expense_type_in_without_crash(self):
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows, \
            mock.patch.object(alerts, "get_latest_ozon_performance_status", return_value=None):
            table_has_rows.side_effect = _presence(True, True, True, True, False)
            result = alerts.get_ozon_report_completeness("2026-05-21")
        self.assertTrue(result["ads_present"])

    def test_build_message_skip_snapshot_does_not_write_intraday(self):
        kpi_rows = [
            _kpi_row("wb", "2026-05-20"),
            _kpi_row("ozon", "2026-05-20"),
        ]
        with mock.patch.object(alerts, "get_kpi_rows", return_value=kpi_rows), \
            mock.patch.object(alerts, "overlay_wb_orders_from_sales_funnel", side_effect=lambda rows: rows), \
            mock.patch.object(alerts, "save_today_snapshot") as save_today_snapshot, \
            mock.patch.object(alerts, "build_completed_day_alerts", return_value=["ozon complete"]), \
            mock.patch.object(alerts, "build_executive_summary", return_value=["summary"]), \
            mock.patch.object(alerts, "build_short_snapshot", return_value=["snapshot"]), \
            mock.patch.object(alerts.supabase, "table") as mock_table:
            mock_table.return_value.select.return_value.order.return_value.order.return_value.limit.return_value.execute.return_value.data = []
            alerts.build_message(skip_snapshot=True)
        save_today_snapshot.assert_not_called()


class OzonOrdersPresenceGateTests(unittest.TestCase):
    """Шаг FBO нефатальный с 2026-09-14; тихий пропуск заказов должен стать блокером."""

    def test_missing_fbo_orders_marks_ozon_incomplete(self):
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows, \
            mock.patch.object(alerts, "get_latest_ozon_performance_status", return_value=None):
            table_has_rows.side_effect = _presence(fbo=False)
            result = alerts.get_ozon_report_completeness("2026-09-13")
        self.assertFalse(result["complete"])
        self.assertIn("ozon_fbo_orders_missing", result["blockers"])
        self.assertNotIn("ozon_fbs_orders_missing", result["blockers"])
        self.assertFalse(result["fbo_orders_present"])

    def test_missing_fbs_orders_marks_ozon_incomplete(self):
        with mock.patch.object(alerts, "table_has_rows") as table_has_rows, \
            mock.patch.object(alerts, "get_latest_ozon_performance_status", return_value=None):
            table_has_rows.side_effect = _presence(fbs=False)
            result = alerts.get_ozon_report_completeness("2026-09-13")
        self.assertIn("ozon_fbs_orders_missing", result["blockers"])

    def test_orders_check_asks_for_the_target_date_and_schema(self):
        seen = []

        def record(table_name, filters):
            if table_name == "marketplace_orders":
                seen.append(tuple(sorted(filters)))
            return True

        with mock.patch.object(alerts, "table_has_rows", side_effect=record), \
            mock.patch.object(alerts, "get_latest_ozon_performance_status", return_value=None):
            alerts.get_ozon_report_completeness("2026-09-13")
        self.assertIn(tuple(sorted([("marketplace_code", "eq", "ozon"), ("order_schema", "eq", "fbo"),
                                    ("order_date", "eq", "2026-09-13")])), seen)


if __name__ == "__main__":
    unittest.main()
