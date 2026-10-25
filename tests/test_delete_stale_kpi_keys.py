"""delete_stale_kpi_keys: ключ удаляется, только если у него нет НИ ОДНОГО источника витрины — хранение Ozon тоже источник (56-я §3)."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from scripts import delete_stale_kpi_keys as dk


class _Q:
    def __init__(self, db, table):
        self.db, self.table, self.filters, self.deleting = db, table, [], False

    def select(self, *_a): return self
    def eq(self, col, val): self.filters.append((col, {str(val)})); return self
    def in_(self, col, vals): self.filters.append((col, {str(v) for v in vals})); return self
    def order(self, *_a, **_k): return self
    def limit(self, *_a): return self
    def delete(self): self.deleting = True; return self

    def execute(self):
        rows = [r for r in self.db.get(self.table, []) if all(str(r.get(c)) in v for c, v in self.filters)]
        if self.deleting:
            self.db[self.table] = [r for r in self.db[self.table] if r not in rows]
            self.db.setdefault("_deleted", []).extend(rows)
        class R: pass
        res = R(); res.data = rows
        return res


class _SB:
    def __init__(self, db): self.db = db
    def table(self, name): return _Q(self.db, name)


def _kpi(i, day, sku, ad):
    return {"id": i, "kpi_date": day, "marketplace_code": "ozon", "marketplace_sku": sku, "orders_qty": 0, "orders_amount_seller": 0,
            "buyouts_qty": 0, "buyouts_amount_seller": 0, "ad_spend": ad, "commission_amount": 0, "logistics_amount": 0,
            "other_expenses_amount": 0, "ad_orders_revenue": 0, "organic_orders_revenue": 0}


class DeleteStaleKpiKeysTests(unittest.TestCase):
    def run_main(self, db, apply):
        keys = [{"kpi_date": "2026-10-12", "marketplace_sku": s, "orders_qty": 0, "orders_amount_seller": 0} for s in ("111", "222", "333")]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "keys.json")
            json.dump({"keys": keys}, open(path, "w"))
            out = io.StringIO()
            with mock.patch.object(dk, "sb", return_value=_SB(db)), mock.patch.object(dk, "SNAP_DIR", tmp), \
                 mock.patch("sys.argv", ["x", "--keys", path] + (["--apply"] if apply else [])), redirect_stdout(out):
                dk.main()
        return out.getvalue()

    def db(self):
        return {
            "daily_sku_kpi": [_kpi(1, "2026-10-12", "111", 500), _kpi(2, "2026-10-12", "222", 300), _kpi(3, "2026-10-12", "333", 100)],
            "marketplace_expenses": [{"expense_date": "2026-10-12", "marketplace_code": "ozon", "marketplace_sku": "333"}],
            "ozon_placement_by_product_daily": [{"day": "2026-10-12", "sku": 222}],
        }

    def test_plan_protects_storage_and_expense_keys_and_writes_nothing(self):
        db = self.db()
        text = self.run_main(db, apply=False)
        self.assertIn("с живым источником (НЕ удаляются, ночной KPI перестроит сам): 2", text)
        self.assertIn("ozon_placement_by_product_daily", text)
        self.assertIn("К УДАЛЕНИЮ: 1 ключей", text)
        self.assertIn("db_writes = 0", text)
        self.assertEqual(len(db["daily_sku_kpi"]), 3)

    def test_apply_deletes_only_the_key_without_any_source(self):
        db = self.db()
        text = self.run_main(db, apply=True)
        self.assertEqual([r["marketplace_sku"] for r in db["_deleted"]], ["111"])
        self.assertEqual(sorted(r["marketplace_sku"] for r in db["daily_sku_kpi"]), ["222", "333"])
        self.assertIn("db_writes = 1", text)


if __name__ == "__main__":
    unittest.main()
