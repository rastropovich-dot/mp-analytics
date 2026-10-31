"""Сводная по площадкам читает витрину SKU узким списком колонок (59-я, память). Каждая колонка, которую она
суммирует, обязана быть в этом списке: иначе row.get() молча даёт 0. Так 28.10 «Отзывы» (reviews_spend) легли в
daily_marketplace_kpi нулями на всех датах — узкий список и суммирование пришли из двух веток."""
import inspect
import re
import unittest

import reports_daily_marketplace_kpi as mp_kpi


class SkuKpiColumnsTests(unittest.TestCase):
    def test_every_summed_field_is_read(self):
        source = inspect.getsource(mp_kpi)
        used = set(re.findall(r'row\.get\("([a-z_]+)"\)', source))
        read = set(mp_kpi.SKU_KPI_COLUMNS.split(","))
        self.assertIn("reviews_spend", used)
        self.assertEqual(sorted(used - read), [])


if __name__ == "__main__":
    unittest.main()
