"""Платное хранение WB: защита от двойного счёта при смене ключей складов (RBH п. 61, план п. 29, 2026-11-08) и видимость складов в остатках.

WB с 09.11 по-новому отображает склады в отчётах. Ночь перечитывает окно 8 дней upsert-ом по ключу (day, nm_id, chrt_id, office_id, …).
Если прошлые дни окна придут с другими office_id / warehouse, старые строки остались бы рядом с новыми и Σ хранения дня удвоилась бы.
Поэтому день, у которого множества (office_id, warehouse) в ответе и в таблице не пересекаются, по умолчанию не пишется и шаг
возвращает отказ. Пути замены (удаления) нет (RBH п. 65, 08.11): ни функции delete, ни флага --replace-rekeyed-days, строки таблицы
не трогаются. Подделка клиента хранит строки в памяти и считает каждую попытку delete / update — сети и базы нет.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from unittest import mock

import loaders.wb_paid_storage_loader as ld
import loaders.wb_stocks_loader as wb_stocks


def item(day, office=0, wh="Склад WB РФ", nm=926793547, chrt=1399395141, price=0.5):
    return {"date": day, "logWarehouseCoef": 0, "officeId": office, "warehouse": wh, "warehouseCoef": 0, "giId": 0, "chrtId": chrt, "size": "0",
            "barcode": "2050087406101", "subject": "Серьги", "brand": "B", "vendorCode": "V1", "nmId": nm, "volume": 1.0, "calcType": "короба",
            "warehousePrice": price, "barcodesCount": 1, "palletPlaceCode": 0, "palletCount": 0, "originalDate": day, "loyaltyDiscount": 0,
            "tariffFixDate": "", "tariffLowerDate": ""}


class MemSB:
    """Таблица в памяти: upsert по KEY, HEAD count с eq / is_, select … limit. delete / update не выполняются, а считаются."""

    def __init__(self, rows=()):
        self.rows = {tuple(r[k] for k in ld.KEY): dict(r) for r in rows}
        self.upserts, self.deletes, self.updates, self.reads = 0, [], [], 0

    def table(self, name):
        assert name == ld.TABLE, name
        return MemQ(self)


class MemQ:
    def __init__(self, sb):
        self.sb, self.filters, self.op, self.payload, self.lim = sb, [], "select", None, None

    def select(self, *a, **k): self.op = "select"; return self
    def eq(self, f, v): self.filters.append((f, "eq", v)); return self
    def is_(self, f, v): self.filters.append((f, "is", None)); return self
    def in_(self, f, vs): self.filters.append((f, "in", set(vs))); return self
    def limit(self, n): self.lim = n; return self
    def delete(self): self.sb.deletes.append(list(self.filters)); self.op = "delete"; return self
    def update(self, payload): self.sb.updates.append(payload); self.op = "update"; return self
    def upsert(self, rows, on_conflict=None):
        assert on_conflict == ",".join(ld.KEY)
        self.op, self.payload = "upsert", list(rows); return self

    def match(self, r):
        for f, how, v in self.filters:
            if how == "eq" and r.get(f) != v: return False
            if how == "is" and r.get(f) is not None: return False
            if how == "in" and r.get(f) not in v: return False
        return True

    def execute(self):
        sb = self.sb
        if self.op == "upsert":
            for r in self.payload: sb.rows[tuple(r[k] for k in ld.KEY)] = dict(r)
            sb.upserts += 1
            return type("R", (), {"data": self.payload, "count": None})()
        if self.op in ("delete", "update"):
            raise AssertionError(f"{self.op} запрещён: строки таблицы не трогаются")
        hit = [r for r in sb.rows.values() if self.match(r)]
        sb.reads += 1
        data = hit[: self.lim] if self.lim else hit
        return type("R", (), {"data": data, "count": len(hit)})()


def table_row(day, office=0, wh="Склад WB РФ", nm=926793547):
    r = ld.build_rows([item(day, office, wh, nm)], "2026-11-01T00:00:00+00:00")[0]
    return r


def run_file(sb, items, d1, d2, **kw):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, f"{d1}_{d2}.json")
        with open(path, "w") as fh:
            json.dump({"taskId": "t", "dateFrom": d1, "dateTo": d2, "rows": items}, fh)
        with redirect_stdout(io.StringIO()) as out:
            p = ld.run(sb, from_file=path, today=date(2026, 11, 10), raw_dir=d, **kw)
    return p, out.getvalue()


def pairs(sb, day):
    return {(r["office_id"], r["warehouse"]) for r in sb.rows.values() if r["day"] == day}


class RekeyedDays(unittest.TestCase):
    def test_rekeyed_day_is_not_written_without_flag_and_step_fails(self):
        sb = MemSB([table_row("2026-11-03"), table_row("2026-11-04")])
        p, out = run_file(sb, [item("2026-11-03", 507, "Коледино"), item("2026-11-04")], "2026-11-03", "2026-11-04")
        self.assertEqual(p["rekeyed"], ["2026-11-03"])
        self.assertTrue(p["failure"])
        self.assertEqual(pairs(sb, "2026-11-03"), {(0, "Склад WB РФ")})       # старые строки дня не тронуты, новые не дописаны
        self.assertEqual(pairs(sb, "2026-11-04"), {(0, "Склад WB РФ")})       # обычный день записан
        self.assertEqual((p["written"], sb.deletes, sb.updates), (1, [], []))
        self.assertNotIn("deleted", p)
        self.assertIn("склады перекодированы WB", out)
        self.assertIn("не записаны", out)

    def test_main_returns_nonzero_on_rekeyed_days(self):
        with mock.patch.object(ld, "run", return_value={"items": 2, "failure": "1 дн. …"}), mock.patch.object(ld, "_client", return_value=object()), \
                mock.patch.object(ld, "WB_API_KEY", "k"), redirect_stdout(io.StringIO()):
            self.assertEqual(ld.main([]), 1)
        with mock.patch.object(ld, "run", return_value={"items": 2, "failure": None}), mock.patch.object(ld, "_client", return_value=object()), \
                mock.patch.object(ld, "WB_API_KEY", "k"), redirect_stdout(io.StringIO()):
            self.assertEqual(ld.main([]), 0)

    def test_rekeyed_day_keeps_old_rows_and_client_gets_no_delete_or_update(self):
        old = [table_row("2026-11-03"), table_row("2026-11-03", nm=17641481)]
        sb = MemSB(old)
        before = {k: dict(v) for k, v in sb.rows.items()}
        p, out = run_file(sb, [item("2026-11-03", 507, "Коледино")], "2026-11-03", "2026-11-03")
        self.assertEqual((p["rekeyed"], p["written"], p["rows"]), (["2026-11-03"], 0, 0))
        self.assertTrue(p["failure"])
        self.assertEqual((sb.deletes, sb.updates, sb.upserts), ([], [], 0))
        self.assertEqual(sb.rows, before)                                      # обе старые строки целы, побайтно
        self.assertIn("db_writes = 0", out)

    def test_replace_flag_does_not_exist(self):
        with mock.patch.object(ld, "WB_API_KEY", "k"), redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit) as cm:
            ld.main(["--replace-rekeyed-days"])
        self.assertEqual(cm.exception.code, 2)                                 # argparse: unrecognized arguments
        self.assertIn("unrecognized arguments: --replace-rekeyed-days", err.getvalue())
        import inspect
        self.assertNotIn("replace_rekeyed", inspect.signature(ld.run).parameters)

    def test_loader_source_has_no_delete_path(self):
        with open(ld.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn(".delete(", src)
        self.assertNotIn(".update(", src)
        self.assertNotIn("replace-rekeyed-days", src)
        self.assertFalse(hasattr(ld, "delete_days"))

    def test_same_warehouses_go_by_plain_upsert(self):
        sb = MemSB([table_row("2026-11-03")])
        p, _ = run_file(sb, [item("2026-11-03"), item("2026-11-03", nm=17641481)], "2026-11-03", "2026-11-03")
        self.assertEqual((p["rekeyed"], p["failure"], p["written"]), ([], None, 2))
        self.assertEqual(len(sb.rows), 2)

    def test_new_day_without_table_rows_is_written(self):
        sb = MemSB([])
        p, _ = run_file(sb, [item("2026-11-05", 507, "Коледино")], "2026-11-05", "2026-11-05")
        self.assertEqual((p["rekeyed"], p["failure"], p["written"]), ([], None, 1))

    def test_partial_overlap_is_not_rekeyed(self):
        sb = MemSB([table_row("2026-11-03")])
        p, _ = run_file(sb, [item("2026-11-03"), item("2026-11-03", 507, "Коледино", nm=17641481)], "2026-11-03", "2026-11-03")
        self.assertEqual((p["rekeyed"], p["failure"], p["written"]), ([], None, 2))

    def test_null_warehouse_is_matched_with_is_null(self):
        sb = MemSB([table_row("2026-11-03", wh=None)])
        p, _ = run_file(sb, [item("2026-11-03", wh="")], "2026-11-03", "2026-11-03")
        self.assertEqual((p["rekeyed"], p["written"]), ([], 1))

    def test_dry_run_without_client_skips_the_check_and_writes_nothing(self):
        p, out = run_file(None, [item("2026-11-03", 507, "Коледино")], "2026-11-03", "2026-11-03", dry_run=True)
        self.assertEqual((p["written"], p["rekeyed"]), (0, []))
        self.assertIn("проверка перекодировки складов пропущена", out)

    def test_step_is_nonfatal_and_nightly_command_has_no_replace_flag(self):
        import run_daily_pipeline as pipeline
        cmd = dict(pipeline.STEPS)["WB: платное хранение"]
        self.assertNotIn("--replace-rekeyed-days", cmd)
        self.assertFalse(pipeline.is_fatal_step("WB: платное хранение"))


def stock(nm, q, wh="Склад WB", wid=-999999):
    return {"nmId": nm, "chrtId": 1, "warehouseId": wid, "warehouseName": wh, "regionName": wh, "quantity": q, "inWayToClient": 0, "inWayFromClient": 0}


class StocksVisibility(unittest.TestCase):
    def describe(self, items):
        return wb_stocks.describe_rows(list(wb_stocks.aggregate_stocks(items, catalog={}, today=date(2026, 11, 10)).values()))

    def test_aggregate_only(self):
        text = self.describe([stock(100, 3), stock(200, 1)])
        self.assertIn("Разбивки по складам нет", text)
        self.assertNotIn("формат WB изменился", text)
        self.assertNotIn("задвоится", text)

    def test_real_warehouses_only(self):
        text = self.describe([stock(100, 3, "Коледино", 507), stock(200, 1, "Казань", 117986)])
        self.assertIn("Складов в ответе: 2 — формат WB изменился", text)
        self.assertNotIn("задвоится", text)
        self.assertNotIn("Разбивки по складам нет", text)

    def test_aggregate_and_warehouses_mixed(self):
        text = self.describe([stock(100, 3), stock(100, 3, "Коледино", 507), stock(200, 1, "Казань", 117986)])
        self.assertIn("⚠️ Агрегат «Склад WB» и склады вместе у 1 SKU — Σ по SKU задвоится", text)


if __name__ == "__main__":
    unittest.main()
