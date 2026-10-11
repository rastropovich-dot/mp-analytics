"""scripts/db_migrate.py (§5.2б): план, применение файлом-транзакцией со строкой версии, стоп на расхождении sha256, засев, «all» без KARATOV.
Без сети: вместо Management API — функция-заглушка, запоминающая запросы."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import cabinet  # noqa: E402
import db_migrate as dm  # noqa: E402


class FakeDb:
    """Таблица версий в памяти; DDL-запросы просто копятся."""

    def __init__(self, table_exists=False, rows=None, fail_on=None):
        self.table_exists, self.rows, self.fail_on, self.queries = table_exists, dict(rows or {}), fail_on, []

    def __call__(self, query):
        self.queries.append(query)
        if query.startswith("select filename, sha256"):
            if not self.table_exists:
                raise RuntimeError('Management API: relation "public.schema_migrations" does not exist')
            return [{"filename": f, "sha256": s} for f, s in sorted(self.rows.items())]
        if query.startswith("create table if not exists public.schema_migrations"):
            self.table_exists = True
            return []
        if self.fail_on and self.fail_on in query:
            raise RuntimeError("Management API HTTP 400: syntax error")
        for line in query.splitlines():
            if line.startswith("insert into public.schema_migrations"):
                parts = line.split("'")
                self.rows[parts[1]] = parts[3]
        return []


def make_sql_dir(d, names):
    for n in names:
        open(os.path.join(d, n), "w", encoding="utf-8").write(f"create table if not exists t_{n[:8]} (id int);\n")


class PlanTests(unittest.TestCase):
    def test_baseline_first_then_dated_and_pending_vs_applied(self):
        with tempfile.TemporaryDirectory() as d:
            make_sql_dir(d, ["20260506_b.sql", "00000000_baseline.sql", "20260505_a.sql", "notes.md", "undated.sql"])
            files = cabinet.sql_files(d)
            self.assertEqual(files, ["00000000_baseline.sql", "20260505_a.sql", "20260506_b.sql"])
            sha_a = cabinet.file_sha256(os.path.join(d, "20260505_a.sql"))
            pending, mismatched, ok = dm.plan(d, {"20260505_a.sql": sha_a})
            self.assertEqual([f for f, _s in pending], ["00000000_baseline.sql", "20260506_b.sql"])
            self.assertEqual((mismatched, ok), ([], ["20260505_a.sql"]))

    def test_changed_applied_file_is_a_stop(self):
        with tempfile.TemporaryDirectory() as d:
            make_sql_dir(d, ["20260505_a.sql"])
            out = []
            res = dm.migrate("rbh1", FakeDb(True, {"20260505_a.sql": "deadbeef"}), d, apply=True, out=out.append)
            self.assertEqual((res["mismatched"], res["done"], res["error"]), (1, 0, "sha256 mismatch"))
            self.assertTrue(any("изменён; стоп" in line for line in out))


class ApplyTests(unittest.TestCase):
    def test_plan_only_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            make_sql_dir(d, ["20260505_a.sql"])
            db = FakeDb(False)
            res = dm.migrate("rbh1", db, d, out=lambda *_: None)
            self.assertEqual((res["pending"], res["writes"], db.table_exists), (1, 0, False))
            self.assertEqual(len(db.queries), 1)   # только чтение таблицы версий

    def test_apply_creates_table_and_wraps_each_file_with_its_version_row(self):
        with tempfile.TemporaryDirectory() as d:
            make_sql_dir(d, ["00000000_baseline.sql", "20260505_a.sql"])
            db = FakeDb(False)
            out = []
            res = dm.migrate("rbh1", db, d, apply=True, applied_by="test", out=out.append)
            self.assertEqual((res["done"], res["writes"], res["error"]), (2, 3, None))
            self.assertTrue(db.table_exists)
            self.assertEqual(sorted(db.rows), ["00000000_baseline.sql", "20260505_a.sql"])
            self.assertEqual(db.rows["20260505_a.sql"], cabinet.file_sha256(os.path.join(d, "20260505_a.sql")))
            q = db.queries[-1]
            self.assertTrue(q.startswith("begin;\n") and q.rstrip().endswith("commit;"))
            self.assertIn("create table if not exists t_20260505", q)
            self.assertIn("insert into public.schema_migrations (filename, sha256, applied_by, note) values ('20260505_a.sql'", q)
            self.assertIn("'test', null", q)
            self.assertTrue(any("применён за" in line for line in out))

    def test_file_with_its_own_transaction_gets_a_separate_version_row(self):
        text = "begin;\ncreate table x (id int);\ncommit;\n"
        q, record = dm.apply_query(text, "20260601_x.sql", "abc", "me")
        self.assertEqual(q, text)
        self.assertIn("'20260601_x.sql', 'abc', 'me', 'файл со своей транзакцией", record)
        q2, record2 = dm.apply_query("DO $$\nBEGIN\n select 1;\nEND $$;\n", "20260531_do.sql", "abc", "me")
        self.assertIsNone(record2)                       # BEGIN внутри DO $$ — не транзакция, файл оборачивается
        self.assertTrue(q2.startswith("begin;"))

    def test_failure_stops_before_the_next_file_and_records_nothing_for_it(self):
        with tempfile.TemporaryDirectory() as d:
            make_sql_dir(d, ["20260505_a.sql", "20260506_b.sql", "20260507_c.sql"])
            db = FakeDb(True, fail_on="t_20260506")
            res = dm.migrate("rbh1", db, d, apply=True, out=lambda *_: None)
            self.assertEqual((res["done"], res["error"] is not None), (1, True))
            self.assertIn("20260506_b.sql: RuntimeError", res["error"])
            self.assertEqual(sorted(db.rows), ["20260505_a.sql"])
            self.assertFalse(any("t_20260507" in q for q in db.queries))

    def test_mark_applied_records_without_executing(self):
        with tempfile.TemporaryDirectory() as d:
            make_sql_dir(d, ["00000000_baseline.sql", "20260505_a.sql"])
            db = FakeDb(False)
            res = dm.migrate("karatov", db, d, mark_applied=True, applied_by="seed", out=lambda *_: None)
            self.assertEqual((res["done"], res["writes"]), (2, 3))
            self.assertFalse(any("create table if not exists t_" in q for q in db.queries))
            self.assertTrue(all("засев" in q for q in db.queries if q.startswith("insert")))

    def test_apply_only_executes_listed_files_and_marks_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            make_sql_dir(d, ["00000000_baseline.sql", "20260505_a.sql", "20260506_b.sql"])
            db = FakeDb(False)
            res = dm.migrate("karatov", db, d, apply=True, applied_by="seed", out=lambda *_: None, apply_only={"20260506_b.sql"})
            self.assertEqual((res["done"], res["writes"]), (3, 4))
            ddl = [q for q in db.queries if "create table if not exists t_" in q]
            self.assertEqual(len(ddl), 1); self.assertIn("t_20260506", ddl[0])
            self.assertEqual(sorted(db.rows), ["00000000_baseline.sql", "20260505_a.sql", "20260506_b.sql"])
            seeds = [q for q in db.queries if q.startswith("insert") and "засев" in q]
            self.assertEqual(len(seeds), 2)

    def test_second_run_is_idle(self):
        with tempfile.TemporaryDirectory() as d:
            make_sql_dir(d, ["20260505_a.sql"])
            db = FakeDb(False)
            dm.migrate("rbh1", db, d, apply=True, out=lambda *_: None)
            res = dm.migrate("rbh1", db, d, apply=True, out=lambda *_: None)
            self.assertEqual((res["applied_before"], res["pending"], res["done"], res["writes"]), (1, 0, 0, 0))


class ClassifyAgainstSnapshotTests(unittest.TestCase):
    """План засева KARATOV: файл, чьи объекты уже в базе, — «пустой» (--mark-applied); файл с новым объектом — «применить»."""

    def test_file_objects_and_classification(self):
        objs = dm.file_objects("create table if not exists public.t1 (id int);\nalter table public.t2 add column if not exists c1 text, add column c2 int;\n"
                               "create index if not exists i1 on t1 (id);\ncreate or replace function public.f1() returns int language sql as $$ select 1 $$;")
        self.assertEqual(objs["table"], {"t1"}); self.assertEqual(objs["column"], {("t2", "c1"), ("t2", "c2")})
        self.assertEqual((objs["index"], objs["function"]), ({"i1"}, {"f1"}))
        snap = {"meta": {}, "tables": [{"table_name": "t2", "table_type": "BASE TABLE"}], "functions": [], "indexes": [],
                "columns": [{"table_name": "t2", "column_name": "c1"}, {"table_name": "t2", "column_name": "c2"}]}
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "20260101_a.sql"), "w").write("alter table t2 add column if not exists c1 text;\n-- x\nalter table t2 add column if not exists c2 int;")
            open(os.path.join(d, "20260102_b.sql"), "w").write("create table if not exists t1 (id int);")
            rows = dm.classify_against(snap, [("20260101_a.sql", "x"), ("20260102_b.sql", "y")], d)
        self.assertEqual([(r[0], r[1], r[2]) for r in rows], [("20260101_a.sql", 2, "пустой"), ("20260102_b.sql", 1, "применить")])
        self.assertEqual(rows[1][3], ["table t1"])

    def test_identifiers_compared_truncated_to_63(self):
        long_name = "idx_" + "a" * 70
        snap = {"meta": {}, "tables": [{"table_name": "t", "table_type": "BASE TABLE"}], "functions": [], "indexes": [{"indexname": long_name[:63]}], "columns": []}
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "20260101_i.sql"), "w").write(f"create index if not exists {long_name} on t (id);")
            rows = dm.classify_against(snap, [("20260101_i.sql", "x")], d)
        self.assertEqual(rows[0][2], "пустой")

    def test_offline_plan_needs_against_and_no_apply(self):
        with self.assertRaises(SystemExit):
            dm.main(["--cabinet", "rbh1", "--offline"])
        with self.assertRaises(SystemExit):
            dm.main(["--cabinet", "rbh1", "--offline", "--against", "x.json", "--apply"])


class MainGuardsTests(unittest.TestCase):
    def test_all_excludes_karatov_and_karatov_needs_the_flag(self):
        self.assertEqual(dm.ALL_CODES, ("rbh1", "rbh2"))
        with self.assertRaises(SystemExit) as cm:
            dm.main(["--cabinet", "karatov", "--mark-applied"])
        self.assertIn("--allow-karatov", str(cm.exception))
        with self.assertRaises(SystemExit):
            dm.main(["--cabinet", "rbh1", "--apply", "--mark-applied"])

    def test_real_sql_dir_order_creates_tables_before_altering_them(self):
        """Пустая база rbh1 10-11: три `alter table if exists` 20260506 шли раньше своих create и молча пропускались, функция 20260831
        читала колонку из файла, стоящего позже по алфавиту. Проверка по всем файлам: цель alter / index существует к этому моменту."""
        import re
        created = set()
        bad = []
        for fn in cabinet.sql_files():
            low = re.sub(r"--[^\n]*", "", open(os.path.join(ROOT, "sql", fn), encoding="utf-8").read()).lower()
            new_tables = set(re.findall(r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?\"?([a-z_]+)", low))
            for m in re.finditer(r"alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?(?:public\.)?\"?([a-z_]+)", low):
                if m.group(1) not in created | new_tables:
                    bad.append(f"{fn}: alter {m.group(1)} раньше create")
            for m in re.finditer(r"create\s+(?:unique\s+)?index\s+(?:if\s+not\s+exists\s+)?\S+\s+on\s+(?:public\.)?\"?([a-z_]+)", low):
                if m.group(1) not in created | new_tables:
                    bad.append(f"{fn}: index on {m.group(1)} раньше create")
            created |= new_tables
        self.assertEqual(bad, [], "\n".join(bad))
        files = cabinet.sql_files()
        self.assertLess(files.index("20260831_add_request_kind_to_ozon_statistics_json_usage.sql"), files.index("20260831_z_add_quota_window_snapshot.sql"))

    def test_real_sql_dir_has_baseline_first_and_no_top_level_transactions(self):
        files = cabinet.sql_files()
        self.assertEqual(files[0], "00000000_baseline_public_schema.sql")
        self.assertEqual(len(files), 45)                                     # базовый + 44 датированных (10-11: гранты API-ролей, снятие unique)
        for fn in files:
            text = open(os.path.join(ROOT, "sql", fn), encoding="utf-8").read()
            self.assertIsNone(dm.TXN_RE.search(text), fn)                   # BEGIN в 20260531 — внутри DO $$, без «;»


if __name__ == "__main__":
    unittest.main()
