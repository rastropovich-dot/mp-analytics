"""Окно по 62 дня не должно портить здоровые даты.

Бэкфилл 28 дырявых дат тремя окнами затрагивает 145 дат. Записывать все нельзя:
marketplace_expenses ключуется БЕЗ campaign_id, upsert замещает строку целиком,
а набор кампаний в окне собран под целевые даты. Здоровая дата, переписанная
неполным набором, потеряет часть расхода — тот же механизм, что осыпает историю
заказов WB.

Поэтому тест проверяет не «фильтр работает», а «фильтр не даёт испортить»:
любая посторонняя дата, дошедшая до записи, обязана уронить прогон.
"""

import unittest

import loaders.ozon_performance_ads_loader as L

TARGETS = ["2026-08-12", "2026-08-13", "2026-08-17"]


def exp(d, sku="1", amt=100.0):
    return {"expense_date": d, "marketplace_code": "ozon", "marketplace_sku": sku,
            "expense_type": "advertising_clicks", "expense_amount": amt}


def att(d, sku="1", camp="c1"):
    return {"sale_date": d, "marketplace_code": "ozon", "marketplace_sku": sku,
            "ad_source": "cpc", "attribution_type": "direct", "campaign_id": camp, "ad_spend": 1.0}


class ParseTargetDatesTests(unittest.TestCase):
    def test_parses_comma_separated(self):
        self.assertEqual(L.parse_target_dates("2026-08-12,2026-08-13"), ["2026-08-12", "2026-08-13"])

    def test_tolerates_spaces_and_trailing_commas(self):
        self.assertEqual(L.parse_target_dates(" 2026-08-12 , 2026-08-13 ,"), ["2026-08-12", "2026-08-13"])

    def test_empty_means_no_filter(self):
        for raw in (None, "", "  ", ","):
            self.assertEqual(L.parse_target_dates(raw), [], repr(raw))


class EnforceTargetDatesTests(unittest.TestCase):
    def test_keeps_only_target_dates(self):
        rows = [exp("2026-08-12"), exp("2026-08-15"), exp("2026-08-13"), exp("2026-08-31")]
        kept, dropped = L.enforce_target_dates(rows, TARGETS, "expense_date", "marketplace_expenses")
        self.assertEqual(sorted(r["expense_date"] for r in kept), ["2026-08-12", "2026-08-13"])
        self.assertEqual(dropped, 2)

    def test_healthy_dates_from_a_62_day_window_are_dropped(self):
        """Главный случай: окно вернуло 62 даты, целевых три."""
        from datetime import date, timedelta
        rows = []
        d = date(2026, 8, 4)
        while d <= date(2026, 8, 24):
            rows.append(exp(d.isoformat())); d += timedelta(days=1)
        kept, dropped = L.enforce_target_dates(rows, TARGETS, "expense_date", "marketplace_expenses")
        self.assertEqual(len(kept), 3)
        self.assertEqual(dropped, 18)
        self.assertEqual(sorted({r["expense_date"] for r in kept}), sorted(TARGETS))

    def test_attribution_uses_its_own_date_field(self):
        rows = [att("2026-08-12"), att("2026-08-20"), att("2026-08-17")]
        kept, dropped = L.enforce_target_dates(rows, TARGETS, "sale_date", "ozon_daily_sku_ad_attribution")
        self.assertEqual(dropped, 1)
        self.assertEqual(sorted(r["sale_date"] for r in kept), ["2026-08-12", "2026-08-17"])

    def test_no_targets_means_pass_through(self):
        rows = [exp("2026-08-12"), exp("2026-08-15")]
        kept, dropped = L.enforce_target_dates(rows, [], "expense_date", "x")
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, 0)

    def test_empty_input_is_safe(self):
        kept, dropped = L.enforce_target_dates([], TARGETS, "expense_date", "x")
        self.assertEqual((kept, dropped), ([], 0))

    def test_missing_date_field_is_dropped_not_written(self):
        rows = [exp("2026-08-12"), {"marketplace_sku": "9", "expense_amount": 5.0}]
        kept, dropped = L.enforce_target_dates(rows, TARGETS, "expense_date", "x")
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 1)


class GuardTests(unittest.TestCase):
    """Проверяем не «работает», а «не портит»: чужая дата обязана уронить прогон."""

    def test_leaked_foreign_date_raises(self):
        class Sneaky(dict):
            """Строка, меняющая дату после фильтра — имитация любой ошибки отбора."""
            def __init__(self):
                super().__init__(exp("2026-08-12"))
                self._reads = 0
            def get(self, k, default=None):
                if k == "expense_date":
                    self._reads += 1
                    return "2026-08-12" if self._reads == 1 else "2026-08-15"
                return super().get(k, default)

        with self.assertRaises(RuntimeError) as ctx:
            L.enforce_target_dates([Sneaky()], TARGETS, "expense_date", "marketplace_expenses")
        self.assertIn("вне целевого списка", str(ctx.exception))
        self.assertIn("2026-08-15", str(ctx.exception))

    def test_error_names_the_table(self):
        class Sneaky(dict):
            def __init__(self):
                super().__init__(att("2026-08-12")); self._reads = 0
            def get(self, k, default=None):
                if k == "sale_date":
                    self._reads += 1
                    return "2026-08-12" if self._reads == 1 else "2026-07-01"
                return super().get(k, default)
        with self.assertRaises(RuntimeError) as ctx:
            L.enforce_target_dates([Sneaky()], TARGETS, "sale_date", "ozon_daily_sku_ad_attribution")
        self.assertIn("ozon_daily_sku_ad_attribution", str(ctx.exception))


class MultiDayGuardTests(unittest.TestCase):
    def test_flag_exists_and_defaults_to_none(self):
        import sys
        from unittest import mock
        with mock.patch.object(sys, "argv", ["loader", "--mode", "cpc-backfill"]):
            args = L.parse_args()
        self.assertIsNone(args.target_dates)
        self.assertEqual(L.parse_target_dates(args.target_dates), [])

    def test_flag_parses_a_list(self):
        import sys
        from unittest import mock
        with mock.patch.object(sys, "argv", ["loader", "--mode", "cpc-backfill",
                                             "--target-dates", "2026-08-12,2026-08-13"]):
            args = L.parse_args()
        self.assertEqual(L.parse_target_dates(args.target_dates), ["2026-08-12", "2026-08-13"])


if __name__ == "__main__":
    unittest.main()
