"""Остатки Ozon: модуль создаёт клиент Supabase и пишет через него (RBH 2026-11-08, починка ночи 07.11).

Было: при переделке шага (`rbh-stock-sku`, мерж 1bc547c, main 219b748) строка
`supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)` ушла вместе с чтением sku_catalog, а `supabase.`
осталось в fetch_all, stock_identity_columns_supported и save_stocks. Шаг снимал /v4 и падал на записи
(`NameError: name 'supabase' is not defined`) у трёх кабинетов; stock_identity_columns_supported() молча отдавал
False — NameError глотал `except Exception`. Набор на мерже прошёл: тесты шага звали только чистые функции.

Здесь модуль каждый раз грузится заново из файла с подставными dotenv / cabinet / supabase и фальшивым окружением —
ни .env, ни настоящих ключей, ни сети. Последний класс закрывает весь класс дефекта: функция модуля ссылается на
глобальное имя, которого модуль не создаёт и которое не встроено (symtable, без импорта и без сети).
"""
import builtins
import contextlib
import glob
import importlib.util
import io
import json
import os
import symtable
import sys
import types
import unittest
from unittest import mock

import loaders.http_retry  # noqa: F401 — модуль импортирует его; грузим до подмены sys.modules, чтобы patch.dict его не выгрузил
import requests  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOADER = os.path.join(ROOT, "loaders", "ozon_stocks_loader.py")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "ozon_v4_stocks_20261029_sample.json")
ON_CONFLICT = "stock_date,marketplace_code,marketplace_sku,warehouse_name"
FAKE_ENV = {
    "MP_CABINET": "karatov",
    "SUPABASE_URL": "http://supabase.stocks-test.invalid",
    "SUPABASE_SERVICE_KEY": "fake.stocks.key",
    "OZON_CLIENT_ID": "0",
    "OZON_API_KEY": "fake-ozon-api-key",
}


class FakeQuery:
    """Цепочка PostgREST: table → select / eq / gte / lte / order / range / limit / upsert → execute."""

    def __init__(self, client, table):
        self.client, self.table, self.ops = client, table, []

    def __getattr__(self, name):
        def op(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self
        return op

    def execute(self):
        self.client.executed.append((self.table, list(self.ops)))
        if self.client.fail:
            raise self.client.fail
        return types.SimpleNamespace(data=self.client.answer(self.table, self.ops))


class FakeClient:
    def __init__(self, pages=None, fail=None):
        self.pages, self.fail, self.executed = list(pages or []), fail, []

    def table(self, name):
        return FakeQuery(self, name)

    def answer(self, table, ops):
        if any(name == "range" for name, _, _ in ops):
            return self.pages.pop(0) if self.pages else []
        return []

    def upserts(self):
        return [(t, ops) for t, ops in self.executed if any(name == "upsert" for name, _, _ in ops)]


def load_module(client=None, guard=None, events=None):
    """Загрузить loaders/ozon_stocks_loader.py заново. Вернуть (модуль, порядок событий, аргументы create_client)."""
    events, created = (events if events is not None else []), []
    client = client or FakeClient()

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: events.append("load_dotenv")
    cabinet = types.ModuleType("cabinet")

    def assert_env():
        events.append("assert_env")
        if guard:
            raise guard
        return "karatov"
    cabinet.assert_env = assert_env
    supabase = types.ModuleType("supabase")

    def create_client(url, key):
        events.append("create_client")
        created.append((url, key))
        return client
    supabase.create_client = create_client

    spec = importlib.util.spec_from_file_location("_ozon_stocks_loader_under_test", LOADER)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, FAKE_ENV), \
            mock.patch.dict(sys.modules, {"dotenv": dotenv, "cabinet": cabinet, "supabase": supabase}):
        spec.loader.exec_module(module)
    return module, events, created


def sample(cab):
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["cabinets"][cab]


class ClientIsCreated(unittest.TestCase):
    def test_guard_runs_before_the_single_client(self):
        module, events, created = load_module()
        self.assertEqual(events, ["load_dotenv", "assert_env", "create_client"])
        self.assertEqual(created, [(FAKE_ENV["SUPABASE_URL"], FAKE_ENV["SUPABASE_SERVICE_KEY"])])
        self.assertTrue(hasattr(module, "supabase"))

    def test_failed_guard_creates_no_client(self):
        events = []
        with self.assertRaises(SystemExit):
            load_module(guard=SystemExit("кабинет и база не совпали"), events=events)
        self.assertEqual(events, ["load_dotenv", "assert_env"])


class IdentityColumnsProbe(unittest.TestCase):
    def test_answer_of_the_client_decides_true(self):
        client = FakeClient()
        module, _, _ = load_module(client)
        self.assertIs(module.stock_identity_columns_supported(), True)
        self.assertEqual(len(client.executed), 1)
        table, ops = client.executed[0]
        self.assertEqual(table, "stock_daily")
        self.assertEqual(ops[0], ("select", ("product_id,stock_marketplace_sku,decision_marketplace_sku,stock_identity_status",), {}))
        self.assertEqual(ops[1], ("limit", (1,), {}))

    def test_refusal_of_the_client_decides_false(self):
        client = FakeClient(fail=RuntimeError("column stock_daily.product_id does not exist"))
        module, _, _ = load_module(client)
        self.assertIs(module.stock_identity_columns_supported(), False)
        self.assertEqual(len(client.executed), 1)   # False — от ответа клиента, а не от NameError до запроса


class FetchAll(unittest.TestCase):
    def test_pages_until_short_page_with_filters_and_order(self):
        full = [{"i": n} for n in range(1000)]
        client = FakeClient(pages=[full, [{"i": "a"}, {"i": "b"}, {"i": "c"}]])
        module, _, _ = load_module(client)
        rows = module.fetch_all("marketplace_orders", [("marketplace_code", "eq", "ozon"), ("order_date", "gte", "2026-08-01"),
                                                       ("order_date", "lte", "2026-11-07")], order="order_date")
        self.assertEqual(len(rows), 1003)
        self.assertEqual([t for t, _ in client.executed], ["marketplace_orders", "marketplace_orders"])
        for (_, ops), (lo, hi) in zip(client.executed, [(0, 999), (1000, 1999)]):
            self.assertEqual(ops, [("select", ("*",), {}), ("eq", ("marketplace_code", "ozon"), {}),
                                   ("gte", ("order_date", "2026-08-01"), {}), ("lte", ("order_date", "2026-11-07"), {}),
                                   ("order", ("order_date",), {}), ("range", (lo, hi), {})])


class SaveStocks(unittest.TestCase):
    def save(self, rows):
        client = FakeClient()
        module, _, _ = load_module(client)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            module.save_stocks(rows)
        return client.upserts(), out.getvalue()

    def test_501_rows_go_in_500_plus_1_with_the_same_conflict_key(self):
        rows = [{"stock_date": "2026-11-08", "marketplace_code": "ozon", "marketplace_sku": str(n), "warehouse_name": "fbo"}
                for n in range(501)]
        upserts, out = self.save(rows)
        self.assertEqual([t for t, _ in upserts], ["stock_daily", "stock_daily"])
        sent = [ops[0] for _, ops in upserts]
        self.assertEqual([len(args[0]) for _, args, _ in sent], [500, 1])
        self.assertEqual([kwargs for _, _, kwargs in sent], [{"on_conflict": ON_CONFLICT}] * 2)
        self.assertEqual([r for _, args, _ in sent for r in args[0]], rows)
        self.assertIn("501 строк", out)

    def test_empty_set_writes_nothing(self):
        upserts, out = self.save([])
        self.assertEqual(upserts, [])
        self.assertIn("Нет остатков для записи", out)

    def test_rbh_fixture_rows_are_written_in_one_batch_each(self):
        for cab, want in (("rbh1", 10), ("rbh2", 8)):
            client = FakeClient()
            module, _, _ = load_module(client)
            rows, counters = module.build_stock_rows(sample(cab), True, {}, "2026-10-29")
            self.assertEqual(len(rows), want, cab)
            self.assertEqual(dict(counters), {"sku_from_api": want}, cab)
            with contextlib.redirect_stdout(io.StringIO()):
                module.save_stocks(rows)
            upserts = client.upserts()
            self.assertEqual(len(upserts), 1, cab)
            name, args, kwargs = upserts[0][1][0]
            self.assertEqual((name, args[0], kwargs), ("upsert", rows, {"on_conflict": ON_CONFLICT}), cab)


BUILTIN_NAMES = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__loader__", "__package__", "__path__",
                                      "__builtins__"}


def undefined_globals(path):
    """Глобальные имена, которые читают функции модуля, а модуль их не создаёт (присваивание, import, def, global)."""
    with open(path, encoding="utf-8") as fh:
        top = symtable.symtable(fh.read(), path, "exec")
    defined = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported()}
    used = []

    def walk(table):
        for child in table.get_children():
            for s in child.get_symbols():
                if s.is_declared_global() and s.is_assigned():
                    defined.add(s.get_name())
                if s.is_global() and s.is_referenced():
                    used.append((s.get_name(), child.get_name()))
            walk(child)
    walk(top)
    return sorted({(name, where) for name, where in used if name not in defined and name not in BUILTIN_NAMES})


class NoFunctionReadsANameTheModuleNeverCreates(unittest.TestCase):
    def test_stocks_loader(self):
        self.assertEqual(undefined_globals(LOADER), [])

    def test_whole_project_code(self):
        files = sorted(set(glob.glob(os.path.join(ROOT, "*.py")) + glob.glob(os.path.join(ROOT, "loaders", "*.py"))
                           + glob.glob(os.path.join(ROOT, "scripts", "*.py")) + glob.glob(os.path.join(ROOT, "cabinets", "*.py"))))
        self.assertGreater(len(files), 100)
        found = {os.path.relpath(p, ROOT): u for p in files for u in [undefined_globals(p)] if u}
        self.assertEqual(found, {})

    def test_detector_sees_the_defect_of_219b748(self):
        src = ("from supabase import create_client\nSUPABASE_URL = 1\n\n"
               "def save_stocks(rows):\n    supabase.table('stock_daily').upsert(rows).execute()\n")
        with mock.patch("builtins.open", mock.mock_open(read_data=src)):   # файл не пишется: источник подставлен в open
            self.assertEqual(undefined_globals("<probe>"), [("supabase", "save_stocks")])


if __name__ == "__main__":
    unittest.main()
