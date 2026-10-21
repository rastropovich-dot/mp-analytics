"""Сорок девятая (ручной путь): scripts/ll_rates_update.py --export — since из последней картинки csv, новые картинки → строки, повтор файла не
дописывается, провал формул → пометка и таблица не трогается, план без --apply ничего не пишет (db_writes 0), --apply дописывает csv, пересобирает
историю и upsert-ит только изменившиеся строки. Vision не зовётся — вход готовые строки csv. Плюс общие функции ll_rates_ocr и changed_rows."""
import argparse
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import ll_rates_update as U  # noqa: E402
import ll_rates_ocr as O  # noqa: E402
import load_metal_rates_1c as L  # noqa: E402

CSV_HEAD = ",".join(O.CSV_COLS)
# две строки в формате csv (06.10 — настоящая из сорок седьмой, у 05.10 блоки доллара / тенге / лома условные): золото 1С 6600,00 от 29.09, серебро 160,00, доллар 86,00 от 10.09
ROW_1005 = ("2026-10-05,08:50,2026-10-05,photo_836@05-10-2026_09-00-24.jpg,83.80,4137.49,05.10.2026,29.09.2026,6521.85,6600.00,-78.15,-1.18,"
            "05.10.2026,29.09.2026,151.96,160.00,-8.04,-5.02,04.10.2026,10.09.2026,83.80,86.00,-2.20,-2.56,04.10.2026,10.09.2026,1550.00,1600.00,-50.00,-3.13,"
            "05.10.2026,29.09.2026,11170.80,11280.92,-110.12,-0.98,,,")
ROW_1006 = ("2026-10-06,08:50,2026-10-06,photo_837@06-10-2026_09-00-17.jpg,85.59,4122,06.10.2026,29.09.2026,6636.22,6600.00,36.22,0.55,"
            "06.10.2026,29.09.2026,153.86,160.00,-6.14,-3.84,06.10.2026,10.09.2026,84.93,86.00,-1.07,-1.24,06.10.2026,10.09.2026,1560.00,1600.00,-40.00,-2.50,"
            "06.10.2026,29.09.2026,11320.00,11280.92,39.08,0.35,,,")
NEW_GOLD = ROW_1006.replace("06.10.2026,29.09.2026,6636.22,6600.00,36.22,0.55", "08.10.2026,07.10.2026,6636.22,6700.00,-63.78,-0.95")


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def row_from_csv_line(line, file=None, img_date=None, check=""):
    tmp = tempfile.mktemp(suffix=".csv")
    with open(tmp, "w") as fh:
        fh.write(CSV_HEAD + "\n" + line + "\n")
    o = O.read_rows(tmp)[0]
    os.unlink(tmp)
    if file:
        o["file"] = file
    if img_date:
        o["img_date"] = o["msg_date"] = img_date
    o["check"] = check
    o["fallback"], o["derived"] = o.get("fallback") or "", o.get("derived") or ""
    return o


class FakeDB:
    def __init__(self, table=None):
        self.table = table or {}
        self.upserts, self.writes = [], 0

    def read_table_rows(self):
        return dict(self.table)

    def upsert_rates(self, rows):
        self.upserts.append(rows)
        for r in rows:
            self.table[(r["metal"], r["set_date"])] = {"rate": r["rate"], "source": r["source"], "seen_from": r["seen_from"], "seen_to": r["seen_to"]}
        self.writes += 1 if rows else 0
        return len(rows)


def table_from_csv(hist, pics):
    rows, _ = L.plan_rows(hist, pics)
    return {(r["metal"], r["set_date"]): {"rate": r["rate"], "source": r["source"], "seen_from": r["seen_from"], "seen_to": r["seen_to"]} for r in rows}


class Stand(unittest.TestCase):
    """Стенд: каталог данных с csv 05.10 + 06.10 и историей; экспорт — result.json с фото 05.10, 06.10, 07.10, 08.10 и файлами-заглушками."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = U.default_paths(data_dir=os.path.join(self.dir, "data"))
        os.makedirs(self.paths["dir"])
        with open(self.paths["csv"], "w") as fh:
            fh.write(CSV_HEAD + "\n" + ROW_1005 + "\n" + ROW_1006 + "\n")
        O.rebuild_history(self.paths["csv"], self.paths["history"])
        self.export = os.path.join(self.dir, "export")
        os.makedirs(os.path.join(self.export, "photos"))
        msgs = []
        for i, (mid, d, name) in enumerate([(930, "2026-10-05T09:00:24", "photo_836@05-10-2026_09-00-24.jpg"), (931, "2026-10-06T09:00:17", "photo_837@06-10-2026_09-00-17.jpg"),
                                            (932, "2026-10-07T09:00:17", "photo_838@07-10-2026_09-00-17.jpg"), (933, "2026-10-08T09:00:17", "photo_839@08-10-2026_09-00-17.jpg")]):
            msgs.append({"id": mid, "type": "message", "date": d, "from": "ботЦУП", "photo": f"photos/{name}", "text": ""})
            with open(os.path.join(self.export, "photos", name), "wb") as fh:
                fh.write(b"\xff\xd8 stub")
        msgs.insert(0, {"id": 900, "type": "message", "date": "2026-10-05T08:00:00", "from": "Дмитрий Симонов", "text": "привет"})
        with open(os.path.join(self.export, "result.json"), "w") as fh:
            json.dump({"name": "LL Курсы", "id": 1, "messages": msgs}, fh, ensure_ascii=False)
        self.lines = []
        self.log = self.lines.append

    def args(self, apply=False, since=None):
        return argparse.Namespace(export=self.export, since=since, apply=apply, reocr=False)

    def recognizer(self, mapping):
        """имя файла → строка; чего нет — «не картинка бота»."""
        def rec(files):
            results, skipped = [], []
            for f in files:
                name = os.path.basename(f)
                if name in mapping:
                    results.append(mapping[name])
                else:
                    skipped.append((name, "500×300 — не картинка бота"))
            return results, skipped
        return rec

    def test_since_comes_from_last_picture_and_duplicates_are_skipped(self):
        o7 = row_from_csv_line(ROW_1006, file="photo_838@07-10-2026_09-00-17.jpg", img_date="2026-10-07")
        o8 = row_from_csv_line(ROW_1006, file="photo_839@08-10-2026_09-00-17.jpg", img_date="2026-10-08")
        db = FakeDB(table_from_csv(self.paths["history"], self.paths["csv"]))
        csv_before = read_text(self.paths["csv"])
        rc, info = U.run(self.args(), self.paths, db, log=self.log, today="2026-10-09", recognize=self.recognizer({o7["file"]: o7, o8["file"]: o8}))
        self.assertEqual(rc, 0)
        self.assertEqual((info["since"], info["photos"], info["new"], info["duplicate"], info["added"]), ("2026-10-06", 3, 2, 1, 2))   # 06.10 повтор, 07 и 08 новые
        self.assertEqual(info["last_picture"], "2026-10-08"); self.assertEqual(info["lost_days"], [])
        self.assertEqual((db.writes, db.upserts), (0, []), "план ничего не пишет")
        self.assertEqual(read_text(self.paths["csv"]), csv_before)
        self.assertTrue(any("к upsert 3" in l for l in self.lines), self.lines)     # seen_to трёх действующих установок → 08.10
        self.assertTrue(any("картинок к разбору: 2 новых, 1 уже в csv (повтор)" in l for l in self.lines), self.lines)

    def test_apply_appends_rebuilds_history_and_upserts_changed_rows_only(self):
        o7 = row_from_csv_line(ROW_1006, file="photo_838@07-10-2026_09-00-17.jpg", img_date="2026-10-07")
        o8 = row_from_csv_line(NEW_GOLD, file="photo_839@08-10-2026_09-00-17.jpg", img_date="2026-10-08")
        db = FakeDB(table_from_csv(self.paths["history"], self.paths["csv"]))
        rc, info = U.run(self.args(apply=True), self.paths, db, log=self.log, today="2026-10-09", recognize=self.recognizer({o7["file"]: o7, o8["file"]: o8}))
        self.assertEqual(rc, 0)
        self.assertEqual(len(O.read_rows(self.paths["csv"])), 4)
        self.assertEqual(info["new_settings"], [("gold", "2026-10-07", "6700.00")])
        self.assertEqual(O.read_history(self.paths["history"])[1], ["gold", "2026-10-07", "6700.00", "2026-10-08"])
        keys = sorted((r["metal"], r["set_date"]) for r in db.upserts[0])
        self.assertEqual(keys, [("gold585", "2026-09-29"), ("gold585", "2026-10-07"), ("silver925", "2026-09-29"), ("usd", "2026-09-10")])
        self.assertEqual(db.table[("gold585", "2026-09-29")]["seen_to"], "2026-10-07", "прежнее золото видно до 07.10")
        self.assertEqual(db.table[("gold585", "2026-10-07")]["rate"], D("6700.00"))
        self.assertEqual((info["rates_changed"], info["compare"], db.writes), (4, (0, 0, 0), 1))
        # второй прогон того же экспорта: since 08.10, новых 0, к upsert 0
        self.lines.clear()
        rc2, info2 = U.run(self.args(apply=True), self.paths, db, log=self.log, today="2026-10-09", recognize=self.recognizer({}))
        self.assertEqual((rc2, info2["since"], info2["new"], info2["duplicate"], info2["rates_changed"]), (0, "2026-10-08", 0, 1, 0))
        self.assertEqual(db.upserts[1], [])

    def test_check_failure_marks_row_and_leaves_table_alone(self):
        bad = row_from_csv_line(NEW_GOLD, file="photo_838@07-10-2026_09-00-17.jpg", img_date="2026-10-07", check="gold: отклонение -63.78 ≠ 6636.22 − 6500.00 = 136.22")
        db = FakeDB(table_from_csv(self.paths["history"], self.paths["csv"]))
        rc, info = U.run(self.args(apply=True, since="2026-10-07"), self.paths, db, log=self.log, today="2026-10-08", recognize=self.recognizer({bad["file"]: bad}))
        self.assertEqual(rc, 1)
        rows = O.read_rows(self.paths["csv"])
        self.assertEqual(len(rows), 3); self.assertIn("отклонение", rows[-1]["check"])
        self.assertEqual(db.upserts, []); self.assertEqual(info["new_settings"], [], "провальная установка 6700 в историю не идёт")
        self.assertTrue(any("ПРОВЕРИТЬ ГЛАЗАМИ photo_838" in l for l in self.lines))
        plan, _ = L.plan_rows(self.paths["history"], self.paths["csv"])
        self.assertEqual(next(r for r in plan if r["metal"] == "gold585")["seen_to"], "2026-10-06", "seen_to из провальной картинки не берётся")

    def test_export_older_than_csv_has_nothing_new(self):
        db = FakeDB(table_from_csv(self.paths["history"], self.paths["csv"]))
        rc, info = U.run(self.args(since="2026-10-09"), self.paths, db, log=self.log, today="2026-10-10", recognize=self.recognizer({}))
        self.assertEqual((rc, info["photos"], info["new"], info["added"]), (0, 0, 0, 0))
        self.assertEqual(info["lost_days"], ["2026-10-07", "2026-10-08", "2026-10-09"])
        self.assertTrue(any("к upsert 0" in l for l in self.lines))

    def test_non_bot_picture_is_listed_and_skipped(self):
        db = FakeDB(table_from_csv(self.paths["history"], self.paths["csv"]))
        rc, info = U.run(self.args(), self.paths, db, log=self.log, today="2026-10-09", recognize=self.recognizer({}))
        self.assertEqual((rc, info["new"], info["added"]), (0, 2, 0))
        self.assertEqual(sum(1 for l in self.lines if "пропуск: photo_83" in l and "не картинка бота" in l), 2)

    def test_lost_days_are_workdays_without_picture_up_to_yesterday(self):
        self.assertEqual(U.lost_days({"2026-10-06"}, "2026-10-06", "2026-10-09"), ["2026-10-07", "2026-10-08"])
        self.assertEqual(U.lost_days({"2026-10-06", "2026-10-08"}, "2026-10-06", "2026-10-09"), ["2026-10-07"])
        self.assertEqual(U.lost_days({"2026-10-09"}, "2026-10-09", "2026-10-12"), [])   # пт 09.10 → пн 12.10: выходные не считаются
        self.assertEqual(U.lost_days(set(), None, "2026-10-09"), [])


class OcrCommonTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.csv = os.path.join(self.dir, "ll_rates.csv"); self.hist = os.path.join(self.dir, "h.csv")
        with open(self.csv, "w") as fh:
            fh.write(CSV_HEAD + "\n" + ROW_1005 + "\n" + ROW_1006 + "\n")

    def test_read_rows_round_trip_and_decimals(self):
        rows = O.read_rows(self.csv)
        self.assertEqual([r["file"] for r in rows], ["photo_836@05-10-2026_09-00-24.jpg", "photo_837@06-10-2026_09-00-17.jpg"])
        self.assertEqual(rows[1]["gold_c1"], D("6600.00")); self.assertEqual(rows[1]["gold_date_1c"], "29.09.2026"); self.assertEqual(rows[1]["check"], "")
        self.assertEqual(",".join(O.row_values(rows[1])), ROW_1006)

    def test_append_rows_skips_files_already_present(self):
        o_dup = row_from_csv_line(ROW_1006)
        o_new = row_from_csv_line(ROW_1006, file="photo_932@07-10-2026_09-00-17.jpg", img_date="2026-10-07")
        self.assertEqual(O.append_rows(self.csv, [o_dup, o_new]), (1, 1))
        self.assertEqual(O.append_rows(self.csv, [o_dup, o_new]), (0, 2))
        self.assertEqual(len(O.read_rows(self.csv)), 3)

    def test_rebuild_history_reports_new_settings_and_skips_failed_rows(self):
        new, gone, n = O.rebuild_history(self.csv, self.hist)
        self.assertEqual((n, len(new), gone), (5, 5, []))
        self.assertEqual(O.read_history(self.hist)[0], ["gold", "2026-09-29", "6600.00", "2026-10-05"])
        bad = row_from_csv_line(NEW_GOLD, file="photo_940@08-10-2026_09-00-17.jpg", img_date="2026-10-08", check="gold: отклонение …")
        O.append_rows(self.csv, [bad])
        self.assertEqual(O.rebuild_history(self.csv, self.hist), ([], [], 5), "строка с провалом формул в историю не идёт")
        good = row_from_csv_line(NEW_GOLD, file="photo_941@08-10-2026_09-10-17.jpg", img_date="2026-10-08")
        O.append_rows(self.csv, [good])
        self.assertEqual(O.rebuild_history(self.csv, self.hist), ([("gold", "2026-10-07", "6700.00")], [], 6))

    def test_history_1c_no_repeats_in_a_row(self):
        self.assertEqual(O.history_1c(O.read_rows(self.csv), "gold"), [("29.09.2026", D("6600.00"), "2026-10-05")])


class LoaderChangedRowsTests(unittest.TestCase):
    def test_changed_rows_picks_missing_and_differing_only(self):
        rows = [{"metal": "gold585", "set_date": "2026-09-29", "rate": D("6600.00"), "source": "ll_bot", "seen_from": "2026-09-30", "seen_to": "2026-10-07"},
                {"metal": "usd", "set_date": "2026-09-10", "rate": D("86.00"), "source": "ll_bot", "seen_from": "2026-09-11", "seen_to": "2026-10-07"},
                {"metal": "silver925", "set_date": "2026-09-29", "rate": D("160.00"), "source": "ll_bot", "seen_from": "2026-09-30", "seen_to": "2026-10-07"}]
        table = {("gold585", "2026-09-29"): {"rate": D("6600.00"), "source": "ll_bot", "seen_from": "2026-09-30", "seen_to": "2026-10-06"},
                 ("usd", "2026-09-10"): {"rate": D("86.00"), "source": "ll_bot", "seen_from": "2026-09-11", "seen_to": "2026-10-07"}}
        self.assertEqual(sorted(r["metal"] for r in L.changed_rows(rows, table)), ["gold585", "silver925"])

    def test_seen_to_ignores_pictures_with_failed_formulas(self):
        d = tempfile.mkdtemp()
        pics = os.path.join(d, "p.csv")
        with open(pics, "w") as fh:
            fh.write("img_date,gold_date_1c,gold_c1,silver_date_1c,silver_c1,usd_date_1c,usd_c1,check\n"
                     "2026-10-06,29.09.2026,6600.00,29.09.2026,160.00,10.09.2026,86.00,\n"
                     "2026-10-07,29.09.2026,6600.00,29.09.2026,160.00,10.09.2026,86.00,gold: отклонение …\n")
        from loaders import unit_cost_history as uch
        self.assertEqual(uch._last_seen_by_rate(pics)[("gold585", "2026-09-29", D("6600.00"))], "2026-10-06")


if __name__ == "__main__":
    unittest.main()
