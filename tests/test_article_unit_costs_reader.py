"""Читатель article_unit_costs на схеме 2026-09-14: ключ (offer_id_norm, snapshot_date).

Регистр: у Ozon «-Изгт», в 1С «-ИЗгт» — искать по lower(), отдавать по исходному
ключу вызывающего. Снимок: последний не новее даты; если все снимки новее —
самый ранний и предупреждение вслух, а не пустой результат.
"""
import unittest
from unittest import mock

import reports_ozon_ad_diagnostic_rule as rule


def _result(rows):
    return mock.Mock(data=rows)


class ArticleUnitCostsReaderTests(unittest.TestCase):
    def _run(self, rows, articles, as_of):
        captured = {}

        def fake_read(fn, label=None):
            return _result(rows)

        with mock.patch.object(rule, "execute_read_with_retry", side_effect=fake_read), \
             mock.patch.object(rule, "supabase") as sb:
            chain = sb.table.return_value.select.return_value.eq.return_value.in_.return_value.order.return_value
            chain.execute.return_value = _result(rows)
            out = rule.load_article_unit_costs("ozon", articles, as_of)
            captured["in_args"] = sb.table.return_value.select.return_value.eq.return_value.in_.call_args
        return out, captured

    def test_lookup_is_case_insensitive_and_keyed_by_caller_string(self):
        rows = [{"offer_id_norm": "f007773760-16,5-изгт", "snapshot_date": "2026-05-20", "unit_cost": "12345.67"}]
        (mapping, warning), captured = self._run(rows, ["F007773760-16,5-Изгт"], "2026-09-13")
        self.assertEqual(mapping, {"F007773760-16,5-Изгт": 12345.67})
        self.assertIsNone(warning)
        self.assertEqual(captured["in_args"].args, ("offer_id_norm", ["f007773760-16,5-изгт"]))

    def test_latest_snapshot_not_newer_than_date_wins(self):
        rows = [  # порядок как из БД: snapshot_date desc
            {"offer_id_norm": "f000283615", "snapshot_date": "2026-12-01", "unit_cost": "31000.00"},
            {"offer_id_norm": "f000283615", "snapshot_date": "2026-05-20", "unit_cost": "29390.06"},
            {"offer_id_norm": "f000283615", "snapshot_date": "2026-01-10", "unit_cost": "27000.00"},
        ]
        (mapping, warning), _ = self._run(rows, ["F000283615"], "2026-09-13")
        self.assertEqual(mapping, {"F000283615": 29390.06})
        self.assertIsNone(warning)

    def test_all_snapshots_newer_than_date_falls_back_to_earliest_and_warns(self):
        rows = [
            {"offer_id_norm": "f000283615", "snapshot_date": "2026-12-01", "unit_cost": "31000.00"},
            {"offer_id_norm": "f000283615", "snapshot_date": "2026-05-20", "unit_cost": "29390.06"},
        ]
        (mapping, warning), _ = self._run(rows, ["F000283615"], "2026-04-01")
        self.assertEqual(mapping, {"F000283615": 29390.06})
        self.assertEqual(warning, "cost_snapshot_newer_than_date")

    def test_unknown_offer_id_is_absent_not_zero(self):
        (mapping, warning), _ = self._run([], ["F007780899-ТП"], "2026-09-13")
        self.assertEqual(mapping, {})
        self.assertIsNone(warning)

    def test_missing_table_still_reported_not_raised(self):
        api_error = rule.APIError({"message": "Could not find the table 'public.article_unit_costs' in the schema cache", "code": "PGRST205"})
        with mock.patch.object(rule, "execute_read_with_retry", side_effect=api_error):
            mapping, warning = rule.load_article_unit_costs("ozon", ["F000283615"], "2026-05-16")
        self.assertEqual(mapping, {})
        self.assertEqual(warning, "article_unit_costs_table_missing")

    def test_known_sku_fallback_is_the_1c_value(self):
        """Хардкод 32 963 в файле 1С отсутствовал; по решению владельца 2026-09-14 — 29 390,06."""
        self.assertEqual(rule.KNOWN_SKU_COGS["1300079194"], 29390.06)


if __name__ == "__main__":
    unittest.main()
