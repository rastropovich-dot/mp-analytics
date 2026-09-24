"""Каждый тип из справочника владельца «Тип начисления → Вид», известный API Ozon, имеет у нас статью.

Ключ — описание типа из /v1/finance/accrual/types (knowledge/ozon/accrual_types_2026-09-23.json) против названий ЛК в
docs/owner_manual_report_instruction.md (тридцать пятая задача, §5). Тип без статьи упал бы ночью в unknown_* и в «прочее»
молча — этот тест не даёт справочнику и свёртке разойтись.
"""
import importlib.util
import json
import os
import unittest

from loaders import ozon_finance_accrual as accrual

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("accrual_types_owner_check", os.path.join(ROOT, "scripts", "accrual_types_owner_check.py"))
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)


class OwnerDirectoryCoverage(unittest.TestCase):
    def test_every_owner_type_known_to_ozon_has_an_article(self):
        types = json.load(open(os.path.join(ROOT, "knowledge", "ozon", "accrual_types_2026-09-23.json")))
        directory = chk.owner_directory()
        known = set(accrual.TYPE_TO_EXPENSE) | accrual.AD_TYPE_IDS | accrual.UNCLASSIFIED_TYPE_IDS | {69}
        # одна строка справочника может подойти нескольким типам по префиксу (63 «Агентское вознаграждение Ozon Агрегатор realFBS»
        # и 66 «Агентское вознаграждение Ozon»): считаем сопоставленным самое длинное описание; одинаковые описания (62 и 124
        # «Перечисление за доставку от покупателя») сопоставлены оба
        matched_rows = {}
        for t in types:
            for name, _kind in chk.match_owner(t.get("description") or "", directory, t["id"]):
                matched_rows.setdefault(name, []).append(t)
        missing = set()
        for name, ts in matched_rows.items():
            longest = max(len(t.get("description") or "") for t in ts)
            for t in ts:
                if len(t.get("description") or "") == longest and t["id"] not in known:
                    missing.add((t["id"], t.get("description")))
        self.assertEqual(sorted(missing), [], f"типы из справочника владельца без статьи у нас: {sorted(missing)}")

    def test_november_2025_types_are_folded(self):
        for type_id in (3, 14, 77):
            self.assertEqual(accrual.TYPE_TO_EXPENSE.get(type_id), "other", type_id)
