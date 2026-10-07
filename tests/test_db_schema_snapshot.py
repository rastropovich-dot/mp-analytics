"""scripts/db_schema_snapshot.py (§5.2а первой задачи RBH): разбор sql/, DDL базового файла, сравнение снимков — без сети и без базы."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import db_schema_snapshot as dss  # noqa: E402


def snap(**over):
    base = {
        "meta": {"taken_at": "2026-10-07T21:00:00+00:00", "cabinet": "karatov", "ref": "abc", "schema": "public", "queries": 15},
        "tables": [{"table_name": "marketplaces", "table_type": "BASE TABLE"}, {"table_name": "ozon_products", "table_type": "BASE TABLE"},
                   {"table_name": "v_orders", "table_type": "VIEW"}],
        "columns": [
            {"table_name": "marketplaces", "column_name": "id", "ordinal_position": 1, "data_type": "integer", "udt_name": "int4",
             "character_maximum_length": None, "numeric_precision": 32, "numeric_scale": 0, "is_nullable": "NO",
             "column_default": "nextval('marketplaces_id_seq'::regclass)", "is_identity": "NO", "identity_generation": None},
            {"table_name": "marketplaces", "column_name": "code", "ordinal_position": 2, "data_type": "character varying", "udt_name": "varchar",
             "character_maximum_length": 16, "numeric_precision": None, "numeric_scale": None, "is_nullable": "NO", "column_default": None,
             "is_identity": "NO", "identity_generation": None},
            {"table_name": "marketplaces", "column_name": "amount", "ordinal_position": 3, "data_type": "numeric", "udt_name": "numeric",
             "character_maximum_length": None, "numeric_precision": 14, "numeric_scale": 2, "is_nullable": "YES", "column_default": "0",
             "is_identity": "NO", "identity_generation": None},
            {"table_name": "marketplaces", "column_name": "tags", "ordinal_position": 4, "data_type": "ARRAY", "udt_name": "_text",
             "character_maximum_length": None, "numeric_precision": None, "numeric_scale": None, "is_nullable": "YES", "column_default": None,
             "is_identity": "NO", "identity_generation": None},
            {"table_name": "ozon_products", "column_name": "sku", "ordinal_position": 1, "data_type": "bigint", "udt_name": "int8",
             "character_maximum_length": None, "numeric_precision": 64, "numeric_scale": 0, "is_nullable": "NO", "column_default": None,
             "is_identity": "YES", "identity_generation": "BY DEFAULT"},
        ],
        "constraints": [{"table_name": "marketplaces", "conname": "marketplaces_pkey", "contype": "p", "definition": "PRIMARY KEY (id)"},
                        {"table_name": "marketplaces", "conname": "marketplaces_code_key", "contype": "u", "definition": "UNIQUE (code)"},
                        {"table_name": "marketplaces", "conname": "marketplaces_amount_check", "contype": "c", "definition": "CHECK ((amount >= (0)::numeric))"},
                        {"table_name": "ozon_products", "conname": "ozon_products_pkey", "contype": "p", "definition": "PRIMARY KEY (sku)"}],
        "indexes": [{"table_name": "marketplaces", "indexname": "marketplaces_pkey", "indexdef": "CREATE UNIQUE INDEX marketplaces_pkey ON public.marketplaces USING btree (id)"},
                    {"table_name": "marketplaces", "indexname": "marketplaces_code_idx", "indexdef": "CREATE INDEX marketplaces_code_idx ON public.marketplaces USING btree (code)"},
                    {"table_name": "ozon_products", "indexname": "ozon_products_offer_id_idx", "indexdef": "CREATE INDEX ozon_products_offer_id_idx ON public.ozon_products USING btree (offer_id)"}],
        "views": [{"viewname": "v_orders", "definition": " SELECT 1 AS x;"}],
        "matviews": [],
        "functions": [{"proname": "f_sum", "args": "a integer", "definition": "CREATE OR REPLACE FUNCTION public.f_sum(a integer)\n RETURNS integer\n LANGUAGE sql\nAS $function$ select a $function$"}],
        "sequences": [{"sequence_name": "marketplaces_id_seq", "data_type": "integer", "start_value": "1", "increment": "1"}],
        "triggers": [],
        "rls": [{"table_name": "marketplaces", "rls_enabled": True, "rls_forced": False}, {"table_name": "ozon_products", "rls_enabled": False, "rls_forced": False}],
        "policies": [{"table_name": "marketplaces", "policyname": "read_all", "cmd": "SELECT", "permissive": "PERMISSIVE", "roles": "{anon}", "qual": "true", "with_check": None}],
        "grants": [{"table_name": "marketplaces", "grantee": "service_role", "privileges": "DELETE,INSERT,SELECT,UPDATE"}],
        "extensions": [{"extname": "plpgsql", "extversion": "1.0"}],
        "row_estimates": [{"table_name": "marketplaces", "rows_estimate": 2}, {"table_name": "ozon_products", "rows_estimate": 5440}],
        "marketplaces_rows": [{"id": 1, "code": "ozon"}, {"id": 2, "code": "wb"}],
    }
    base.update(over)
    return base


CREATED = {"table": {"ozon_products": "20260924_create_ozon_products.sql"}, "view": {}, "function": {}, "index": {"ozon_products_offer_id_idx": "20260924_create_ozon_products.sql"}}


class SqlDirTests(unittest.TestCase):
    def test_dated_files_only_and_kinds(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "20260924_a.sql"), "w").write(
                'create table if not exists public.ozon_products (sku bigint primary key);\n'
                'CREATE INDEX IF NOT EXISTS ozon_products_offer_id_idx ON public.ozon_products (offer_id);\n'
                'create or replace view public.wb_funnel_products_latest as select 1;\n'
                'create or replace function ozon_statistics_json_usage_quota_windows() returns int language sql as $$ select 1 $$;\n'
                'create unique index if not exists ux_x on t (a);\n')
            open(os.path.join(d, "ozon_statistics_json_usage_last_24h.sql"), "w").write("create view usage_last_24h as select 1;")
            got = dss.sql_dir_objects(d)
        self.assertEqual(got["table"], {"ozon_products": "20260924_a.sql"})
        self.assertEqual(set(got["index"]), {"ozon_products_offer_id_idx", "ux_x"})
        self.assertEqual(got["view"], {"wb_funnel_products_latest": "20260924_a.sql"})       # файл без даты не в счёт
        self.assertEqual(got["function"], {"ozon_statistics_json_usage_quota_windows": "20260924_a.sql"})

    def test_real_sql_dir_matches_the_known_counts(self):
        got = dss.sql_dir_objects()
        self.assertEqual((len(got["table"]), len(got["view"]), len(got["function"])), (27, 1, 1))   # outbox RBH 09-30: 27 таблиц, 1 вьюха, 1 функция
        self.assertIn("article_unit_costs", got["table"])
        self.assertNotIn("marketplace_orders", got["table"])                                       # один из 11 объектов панели


class CoverageAndBaselineTests(unittest.TestCase):
    def test_coverage_marks_objects_missing_from_sql(self):
        rows = dss.coverage(snap(), CREATED)
        by = {(k, n): src for k, n, _c, src in rows}
        self.assertEqual(by[("table", "marketplaces")], "—")
        self.assertEqual(by[("table", "ozon_products")], "20260924_create_ozon_products.sql")
        self.assertEqual(by[("view", "v_orders")], "—")
        self.assertEqual(by[("function", "f_sum(a integer)")], "—")

    def test_column_types(self):
        s = snap()
        types = {c["column_name"]: dss.column_type(c) for c in s["columns"]}
        self.assertEqual(types, {"id": "integer", "code": "varchar(16)", "amount": "numeric(14,2)", "tags": "text[]", "sku": "bigint"})

    def test_baseline_sql_only_for_objects_sql_does_not_create(self):
        text = dss.baseline_sql(snap(), CREATED)
        self.assertIn('create sequence if not exists public."marketplaces_id_seq";', text)
        self.assertIn('create table if not exists public."marketplaces" (', text)
        self.assertIn('"id" integer default nextval(\'marketplaces_id_seq\'::regclass) not null', text)
        self.assertIn('"amount" numeric(14,2) default 0', text)
        self.assertIn('constraint "marketplaces_pkey" PRIMARY KEY (id)', text)
        self.assertIn('constraint "marketplaces_amount_check" CHECK', text)
        self.assertIn('create index if not exists marketplaces_code_idx ON public.marketplaces', text)
        self.assertNotIn("marketplaces_pkey ON", text)                                   # индекс PK не дублируется
        self.assertNotIn('create table if not exists public."ozon_products"', text)     # таблица из sql/ — не в базовом файле
        self.assertNotIn("ozon_products_offer_id_idx", text)
        self.assertIn('create or replace view public."v_orders" as', text)
        self.assertIn("CREATE OR REPLACE FUNCTION public.f_sum", text)
        self.assertIn('alter table public."marketplaces" enable row level security;', text)
        self.assertIn('create policy "read_all" on public."marketplaces" as permissive for select to anon using (true);', text)
        self.assertIn('grant DELETE, INSERT, SELECT, UPDATE on public."marketplaces" to service_role;', text)
        self.assertTrue(text.startswith("-- ") and text.rstrip().endswith("commit;"))

    def test_identity_column(self):
        c = [c for c in snap()["columns"] if c["column_name"] == "sku"][0]
        self.assertEqual(dss.column_ddl(c), '    "sku" bigint generated by default as identity not null')


class CompareTests(unittest.TestCase):
    def test_equal_snapshots_have_no_diff(self):
        self.assertEqual(dss.compare(snap(), snap()), {})

    def test_diff_lists_only_a_only_b_and_changed(self):
        b = snap()
        b["columns"] = [dict(c, is_nullable="YES") if c["column_name"] == "code" else c for c in b["columns"] if c["column_name"] != "tags"]
        b["indexes"] = b["indexes"] + [{"table_name": "ozon_products", "indexname": "extra_idx", "indexdef": "CREATE INDEX extra_idx ON public.ozon_products USING btree (sku)"}]
        diff = dss.compare(snap(), b)
        self.assertEqual(set(diff), {"columns", "indexes"})
        self.assertEqual(diff["columns"]["only_a"], [("marketplaces", "tags")])
        self.assertEqual([k for k, _a, _b in diff["columns"]["differ"]], [("marketplaces", "code")])
        self.assertEqual(diff["indexes"]["only_b"], [("ozon_products", "extra_idx")])
        self.assertEqual(dss.compare_total(diff), 3)


class SnapshotRunnerTests(unittest.TestCase):
    def test_take_snapshot_runs_every_query_and_marketplaces_only_if_present(self):
        seen = []

        def run(query):
            seen.append(query)
            if query == dss.QUERIES["tables"]:
                return [{"table_name": "marketplaces", "table_type": "BASE TABLE"}]
            if query == dss.MARKETPLACES_QUERY:
                return [{"id": 1}]
            return []
        s = dss.take_snapshot(run, "karatov", "abc")
        self.assertEqual(s["meta"]["queries"], len(dss.QUERIES) + 1)
        self.assertEqual(s["marketplaces_rows"], [{"id": 1}])
        self.assertEqual(seen[-1], dss.MARKETPLACES_QUERY)
        s2 = dss.take_snapshot(lambda q: [], "rbh1", "xyz")
        self.assertEqual((s2["meta"]["queries"], s2["marketplaces_rows"]), (len(dss.QUERIES), []))

    def test_every_query_is_read_only_sql(self):
        for q in list(dss.QUERIES.values()) + [dss.MARKETPLACES_QUERY]:
            self.assertTrue(q.lower().lstrip().startswith("select"), q[:40])
            for bad in ("insert ", "update ", "delete ", "drop ", "alter ", "create "):
                self.assertNotIn(bad, q.lower())

    def test_project_ref_from_profile_host(self):
        import cabinet
        self.assertEqual(dss.project_ref(cabinet.profile("karatov")), "pkrsrwjrlurlfpdyixei")
        with self.assertRaises(SystemExit):
            dss.project_ref(cabinet.profile("rbh1"))

    def test_main_compare_and_from_file_need_no_network(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.json"), os.path.join(d, "b.json")
            json.dump(snap(), open(a, "w"))
            json.dump(snap(), open(b, "w"))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = dss.main(["--compare", a, b])
            self.assertEqual(rc, 0)
            self.assertIn("различий 0", buf.getvalue())
            out_sql = os.path.join(d, "baseline.sql")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = dss.main(["--cabinet", "karatov", "--from-file", a, "--baseline-sql", out_sql])
            self.assertEqual(rc, 0)
            self.assertIn("db_writes = 0", buf.getvalue())
            self.assertIn("не создаются датированными файлами sql/", buf.getvalue())
            self.assertTrue(os.path.exists(out_sql))


if __name__ == "__main__":
    unittest.main()
