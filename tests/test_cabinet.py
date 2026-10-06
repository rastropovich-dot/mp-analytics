"""cabinet.py и cabinets/: профиль по умолчанию, выбор по MP_CABINET, overlay .env.<кабинет>, guard по хосту Supabase и его
присутствие во всех точках входа (первая задача RBH, §2). Сети нет: guard выходит до создания клиента."""
import ast
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import cabinet  # noqa: E402

KARATOV_URL = "https://pkrsrwjrlurlfpdyixei.supabase.co"


def env(**kw):
    """Окружение для guard: без MP_CABINET, если не передан; root — пустой каталог, чтобы .env репозитория не мешал."""
    return dict(kw)


class ProfileSelectionTests(unittest.TestCase):
    def test_default_is_karatov(self):
        self.assertEqual(cabinet.cabinet_code({}), "karatov")
        self.assertEqual(cabinet.cabinet_code({"MP_CABINET": ""}), "karatov")
        self.assertEqual(cabinet.profile("karatov").DISPLAY_NAME, "KARATOV")

    def test_env_selects_rbh_profiles_case_insensitive(self):
        self.assertEqual(cabinet.cabinet_code({"MP_CABINET": "rbh1"}), "rbh1")
        self.assertEqual(cabinet.cabinet_code({"MP_CABINET": " RBH2 "}), "rbh2")
        self.assertEqual(cabinet.profile("rbh1").DISPLAY_NAME, "РБХ-1")
        self.assertEqual(cabinet.profile("rbh2").DISPLAY_NAME, "РБХ-2")
        self.assertEqual((cabinet.profile("rbh1").DATA_DIR, cabinet.profile("rbh2").LOGS_DIR), ("data/rbh1", "logs/rbh2"))
        self.assertEqual((cabinet.profile("rbh1").OZON_LEGAL_NAME, cabinet.profile("rbh1").WB_LEGAL_NAME), ("Попова", "ИП Рафикова"))
        self.assertEqual((cabinet.profile("rbh2").OZON_LEGAL_NAME, cabinet.profile("rbh2").WB_LEGAL_NAME), ("Малимон", "ИП Плахов"))
        self.assertEqual((cabinet.profile("rbh1").BRAND_DEFAULT, cabinet.profile("rbh2").SHOP), ("Beautyhome.me", "РБХ-2"))
        for code in ("rbh1", "rbh2"):   # косметика: ювелирных словарей и констант листов KARATOV в профилях РБХ нет (ответ владельца 09-30)
            p = cabinet.profile(code)
            self.assertEqual((p.BRAND_BY_LETTER, p.OZON_PLATFORMS, p.DISCOUNTER_LETTER, p.OWNER_AFTER_COMMISSION, p.COST_INDEX, p.OVERHEAD_PER_DAY), ({}, (), None, (), (), {}))
            self.assertIsNone(p.COST_SNAPSHOT_FILE)

    def test_unknown_cabinet_exits_with_known_list(self):
        with self.assertRaises(SystemExit) as cm:
            cabinet.cabinet_code({"MP_CABINET": "rbh"})
        self.assertIn("karatov, rbh1, rbh2", str(cm.exception))

    def test_profiles_share_one_schema(self):
        names = [sorted(n for n in dir(cabinet.profile(c)) if n.isupper()) for c in cabinet.KNOWN_CODES]
        self.assertEqual(names[0], names[1])
        self.assertEqual(names[1], names[2])
        self.assertIn("SUPABASE_HOST", names[0])
        self.assertIn("DATA_DIR", names[0])

    def test_karatov_keeps_root_data_and_logs(self):
        p = cabinet.profile("karatov")
        self.assertEqual((p.DATA_DIR, p.LOGS_DIR, p.REPORT_PREFIX), ("data", "logs", ""))
        self.assertEqual(cabinet.data_path("reports", "x.xlsx", prof=p, root="/r"), "/r/data/reports/x.xlsx")
        self.assertEqual(cabinet.logs_dir(cabinet.profile("rbh1"), root="/r"), "/r/logs/rbh1")

    def test_banner(self):
        self.assertEqual(cabinet.banner(cabinet.profile("karatov")), "кабинет: KARATOV")
        self.assertEqual(cabinet.banner(cabinet.profile("rbh2")), "кабинет: РБХ-2")


class SupabaseHostTests(unittest.TestCase):
    def test_host_forms(self):
        self.assertEqual(cabinet.supabase_host("https://Abc.supabase.co/"), "abc.supabase.co")
        self.assertEqual(cabinet.supabase_host("abc.supabase.co"), "abc.supabase.co")
        self.assertEqual(cabinet.supabase_host("http://supabase.tests.invalid"), "supabase.tests.invalid")
        self.assertEqual(cabinet.supabase_host(""), "")
        self.assertEqual(cabinet.supabase_host(None), "")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_matching_host_returns_profile(self):
        prof = cabinet.assert_env(env(SUPABASE_URL=KARATOV_URL + "/"), root=self.root)
        self.assertEqual(prof.CODE, "karatov")

    def test_foreign_host_exits_naming_both_hosts(self):
        with self.assertRaises(SystemExit) as cm:
            cabinet.assert_env(env(SUPABASE_URL="https://zzz.supabase.co"), root=self.root)
        msg = str(cm.exception)
        self.assertIn("zzz.supabase.co", msg)
        self.assertIn("pkrsrwjrlurlfpdyixei.supabase.co", msg)
        self.assertIn("KARATOV", msg)

    def test_missing_url_exits(self):
        with self.assertRaises(SystemExit) as cm:
            cabinet.assert_env(env(), root=self.root)
        self.assertIn("SUPABASE_URL не задан", str(cm.exception))

    def test_rbh_profile_without_host_refuses_any_real_base(self):
        with self.assertRaises(SystemExit) as cm:
            cabinet.assert_env(env(MP_CABINET="rbh1", SUPABASE_URL=KARATOV_URL), root=self.root)
        msg = str(cm.exception)
        self.assertIn("cabinets/rbh1.py", msg)
        self.assertIn("pkrsrwjrlurlfpdyixei.supabase.co", msg)   # на что указывало окружение — видно

    def test_rbh_profile_with_host_accepts_only_its_base(self):
        with mock.patch.object(cabinet.profile("rbh2"), "SUPABASE_HOST", "rbh2-ref.supabase.co"):
            prof = cabinet.assert_env(env(MP_CABINET="rbh2", SUPABASE_URL="https://rbh2-ref.supabase.co"), root=self.root)
            self.assertEqual(prof.CODE, "rbh2")
            with self.assertRaises(SystemExit):
                cabinet.assert_env(env(MP_CABINET="rbh2", SUPABASE_URL=KARATOV_URL), root=self.root)

    def test_test_host_is_allowed_for_every_profile(self):
        for code in cabinet.KNOWN_CODES:
            prof = cabinet.assert_env(env(MP_CABINET=code, SUPABASE_URL="http://supabase.tests.invalid"), root=self.root)
            self.assertEqual(prof.CODE, code)

    def test_overlay_env_cabinet_wins_but_empty_lines_do_not_erase(self):
        with open(os.path.join(self.root, ".env"), "w", encoding="utf-8") as fh:
            fh.write("SUPABASE_URL=https://common.supabase.co\nTELEGRAM_CHAT_ID=-100\nONLY_BASE=b\n")
        with open(os.path.join(self.root, ".env.rbh1"), "w", encoding="utf-8") as fh:
            fh.write("SUPABASE_URL=https://rbh1-ref.supabase.co\nTELEGRAM_CHAT_ID=\nONLY_OVERLAY=o\n")
        e = env(MP_CABINET="rbh1", ONLY_BASE="from-shell")
        with mock.patch.object(cabinet.profile("rbh1"), "SUPABASE_HOST", "rbh1-ref.supabase.co"):
            prof = cabinet.assert_env(e, root=self.root)
        self.assertEqual(prof.CODE, "rbh1")
        self.assertEqual(e["SUPABASE_URL"], "https://rbh1-ref.supabase.co")   # файл кабинета главнее общего .env
        self.assertEqual(e["TELEGRAM_CHAT_ID"], "-100")                          # пустая строка кабинета не затирает общее
        self.assertEqual(e["ONLY_BASE"], "from-shell")                            # общий .env не перезаписывает окружение
        self.assertEqual(e["ONLY_OVERLAY"], "o")

    def test_karatov_ignores_rbh_overlays(self):
        with open(os.path.join(self.root, ".env.rbh1"), "w", encoding="utf-8") as fh:
            fh.write("SUPABASE_URL=https://rbh1-ref.supabase.co\n")
        e = env(SUPABASE_URL=KARATOV_URL)
        cabinet.assert_env(e, root=self.root)
        self.assertEqual(e["SUPABASE_URL"], KARATOV_URL)
        self.assertIsNone(cabinet.load_overlay("karatov", e, self.root))


def entry_points():
    """Файлы, где обязан стоять guard: создают клиент Supabase, берут его импортом из загрузчика, имеют --apply и __main__,
    зовутся шагом пайплайна, либо названы задачей (пайплайн, алерт, книги)."""
    named = {"run_daily_pipeline.py", "alerts_telegram.py", "scripts/report_finrez.py", "scripts/report_finrez_wb.py",
             "scripts/report_ozon_month.py", "scripts/report_wb_month.py", "scripts/send_ozon_month_report.py", "scripts/book_wb_sheet.py"}
    skip_dirs = {"venv", ".git", "tests", "cabinets", "data", "logs", "knowledge", "spec", "snapshots", "docs", "sql", "ops"}
    found = {}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".py") or fn == "cabinet.py":
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), ROOT)
            src = open(os.path.join(dirpath, fn), encoding="utf-8").read()
            reasons = []
            if re.search(r"create_client\(", src):
                reasons.append("create_client")
            if re.search(r"^\s*(from|import) loaders\.\w+ import .*\bsupabase\b", src, re.M):
                reasons.append("client-import")
            if "--apply" in src and re.search(r'^if __name__ == "__main__":', src, re.M):
                reasons.append("--apply")
            if rel in named:
                reasons.append("named")
            if reasons:
                found[rel] = (src, reasons)
    pipeline = open(os.path.join(ROOT, "run_daily_pipeline.py"), encoding="utf-8").read()
    for m in re.finditer(r"python3 (\S+\.py)", pipeline):
        rel = m.group(1)
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        if rel in found:
            found[rel][1].append("pipeline-step")
        else:
            found[rel] = (open(path, encoding="utf-8").read(), ["pipeline-step"])
    return found


class EntryPointsHaveGuardTests(unittest.TestCase):
    def test_every_entry_point_calls_assert_env_before_creating_a_client(self):
        found = entry_points()
        self.assertGreaterEqual(len(found), 60, sorted(found))
        missing, late = [], []
        for rel, (src, reasons) in sorted(found.items()):
            g = src.find("cabinet.assert_env(")
            if g < 0:
                missing.append(f"{rel} [{', '.join(reasons)}]")
                continue
            m = re.search(r"create_client\(", src)
            if m and m.start() < g:
                late.append(rel)
        self.assertEqual(missing, [], "без guard:\n" + "\n".join(missing))
        self.assertEqual(late, [], "create_client раньше guard:\n" + "\n".join(late))

    def test_pipeline_steps_are_all_guarded(self):
        found = entry_points()
        steps = [rel for rel, (_s, r) in found.items() if "pipeline-step" in r or rel in ("alerts_telegram.py",)]
        self.assertGreaterEqual(len(steps), 20, steps)

    def test_nightly_script_and_first_lines(self):
        sh = open(os.path.join(ROOT, "scripts", "finrez_nightly.sh"), encoding="utf-8").read()
        self.assertIn("cabinet.py --shell", sh)                      # guard + профиль одним вызовом (§3): каталоги, префикс, первый месяц
        self.assertLess(sh.find("cabinet.py --shell"), sh.find("scripts/report_finrez.py"))
        for var in ("MP_DATA_DIR", "MP_LOGS_DIR", "MP_REPORT_PREFIX", "MP_BOOK_MONTH_FROM"):
            self.assertIn(var, sh)
        self.assertNotIn("data/reports/", sh)                        # пути — только из профиля
        pipeline = open(os.path.join(ROOT, "run_daily_pipeline.py"), encoding="utf-8").read()
        self.assertLess(pipeline.find("print(cabinet.banner(CABINET))"), pipeline.find("🚀 Запуск ежедневного пайплайна"))
        alert = open(os.path.join(ROOT, "alerts_telegram.py"), encoding="utf-8").read()
        self.assertRegex(alert, r"lines = \[\n\s*cabinet\.banner\(CABINET\),")
        self.assertLess(alert.find("cabinet.assert_env("), alert.find("create_client("))


CABINET_LITERALS = re.compile(r"KARATOV|Топаз|ТОПАЗ|ГОЛДСТАРТ")


def executable_strings(path):
    """Строковые константы файла, кроме docstring-ов (первая строка модуля / класса / функции); комментарии ast не видит."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docs.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.lineno, node.value
        elif isinstance(node, ast.Name):
            yield node.lineno, node.id


class NoCabinetLiteralsOutsideProfilesTests(unittest.TestCase):
    """§3 первой задачи RBH: кабинет KARATOV живёт только в cabinets/karatov.py — в коде вне cabinets/ и tests/ его имён нет
    в исполняемых строках и идентификаторах (docstring-и и комментарии — история, им можно)."""

    SKIP = {"venv", ".git", "tests", "cabinets", "data", "logs", "knowledge", "spec", "docs", "sql", "ops", "snapshots"}

    def test_no_karatov_or_topaz_literals_in_code(self):
        hits = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP and not d.startswith(".")]
            for fn in filenames:
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, ROOT)
                if fn.endswith(".sh"):
                    for i, line in enumerate(open(path, encoding="utf-8"), 1):
                        if CABINET_LITERALS.search(line) and not line.lstrip().startswith("#"):
                            hits.append(f"{rel}:{i}")
                elif fn.endswith(".py"):
                    hits.extend(f"{rel}:{ln}" for ln, text in executable_strings(path) if CABINET_LITERALS.search(text))
        self.assertEqual(hits, [], "литералы кабинета вне профиля:\n" + "\n".join(hits))


class NoDataOrLogsPathLiteralsTests(unittest.TestCase):
    """Каталоги данных и логов — только через cabinet.data_path / logs_path (дополнение 09-28: у rbh1 / rbh2 — data/<кабинет>, logs/<кабинет>):
    в коде вне cabinets/ и tests/ нет строк «data» / «logs» в os.path.join и нет строк, начинающихся с «data/» / «logs/» (docstring-и — можно)."""

    SKIP = NoCabinetLiteralsOutsideProfilesTests.SKIP

    def test_no_path_literals(self):
        hits = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP and not d.startswith(".")]
            for fn in filenames:
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, ROOT)
                if fn.endswith(".sh"):
                    for i, line in enumerate(open(path, encoding="utf-8"), 1):
                        if re.search(r'(^|[\s="])(data|logs)/', line) and not line.lstrip().startswith("#") and "MP_DATA_DIR" not in line and "MP_LOGS_DIR" not in line:
                            hits.append(f"{rel}:{i}")
                    continue
                if not fn.endswith(".py") or fn == "cabinet.py":
                    continue
                tree = ast.parse(open(path, encoding="utf-8").read())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "join":
                        for a in node.args:
                            if isinstance(a, ast.Constant) and a.value in ("data", "logs"):
                                hits.append(f"{rel}:{a.lineno} join({a.value!r})")
                for ln, text in executable_strings(path):
                    if isinstance(text, str) and re.match(r"(data|logs)/", text):
                        hits.append(f"{rel}:{ln} {text[:40]!r}")
        self.assertEqual(hits, [], "пути data/ и logs/ мимо профиля:\n" + "\n".join(hits))

    def test_logs_path(self):
        self.assertEqual(cabinet.logs_path("x", "y.json", prof=cabinet.profile("rbh2"), root="/r"), "/r/logs/rbh2/x/y.json")
        self.assertEqual(cabinet.logs_path("x", prof=cabinet.profile("karatov"), root="/r"), "/r/logs/x")


class RbhProfilesGiveNotSetTests(unittest.TestCase):
    """Модули книг на профиле без ювелирных словарей и констант листов: «не задано» → None / пусто, не ноль и не падение."""

    @classmethod
    def setUpClass(cls):
        import importlib
        cls.saved = dict(os.environ)
        os.environ["MP_CABINET"] = "rbh1"
        os.environ["SUPABASE_URL"] = "http://supabase.tests.invalid"
        os.environ.setdefault("SUPABASE_SERVICE_KEY", "test")
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        names = ["ozon_orders_forecast", "ozon_product_catalog", "report_ozon_month", "report_wb_month", "report_finrez_wb", "finrez_pivots"]
        cls.before = {n: sys.modules.pop(n, None) for n in names}
        cls.mods = {n: importlib.import_module(n) for n in names}

    @classmethod
    def tearDownClass(cls):
        for n, m in cls.before.items():
            if m is not None:
                sys.modules[n] = m
            else:
                sys.modules.pop(n, None)
        os.environ.clear()
        os.environ.update(cls.saved)

    def test_forecast_owner_constants_not_set(self):
        fc = self.mods["ozon_orders_forecast"]
        self.assertEqual((fc.OWNER, fc.OWNER_AFTER_COMMISSION), ({}, ()))
        self.assertIsNone(fc.owner_after_commission("2026-09-05", "Основная"))

    def test_catalog_brand_and_categories_default(self):
        cat = self.mods["ozon_product_catalog"]
        self.assertEqual((cat.brand_of("T1"), cat.brand_of("F1"), cat.brand_of("")), ("Beautyhome.me", "Beautyhome.me", None))
        self.assertEqual((cat.OWNER_CATEGORIES, cat.TYPE_TO_CATEGORY, cat.category_by_name("Серьги")), ((), {}, "прочее"))
        self.assertIn("не заданы", cat.brand_rule_text())
        self.assertTrue(cat.OUT_DIR.endswith(os.path.join("data", "rbh1", "ozon_products")))

    def test_ozon_month_platforms_metals_costs_not_set(self):
        rom = self.mods["report_ozon_month"]
        self.assertEqual((rom.SNAP, rom.PLATFORMS, rom.METALS, rom.COST_INDEX, rom.OVERHEAD_PER_DAY), (None, (), (), (), {}))
        self.assertEqual((rom.platform_of("F1"), rom.metal_of("серьги 925")), (rom.NO_PLATFORM, rom.NO_METAL))
        self.assertIsNone(rom.cost_index_for("2026-09-09")); self.assertIsNone(rom.overhead_for("2026-09-09"))
        self.assertTrue(rom.OUT_DIR.endswith(os.path.join("data", "rbh1", "reports")))

    def test_wb_month_no_discounter_and_no_multiplier(self):
        rwm = self.mods["report_wb_month"]
        self.assertIsNone(rwm.DISCOUNTER_LETTER); self.assertIsNone(rwm.OWNER_ORDERS_AFTER_COMMISSION)
        self.assertEqual([p for p, _t in rwm.PLATFORMS], ["all", "standard"])
        self.assertEqual(rwm.platform_of("t1"), "standard")
        total = rwm.orders_total_row([{"date": "2026-09-01", "vat": rwm.vat_for("2026-09-01"), "cards": 1, "orders_qty": 1, "funnel_buyouts_qty": 0,
                                       "funnel_cancel_qty": 0, "no_cost_qty": 0, "orders_sum": rwm.Decimal("100"), "revenue": None, "cogs": rwm.Z,
                                       "margin": None, "funnel_buyouts_sum": rwm.Z, "funnel_cancel_sum": rwm.Z, "no_cost_sum": rwm.Z, "ads": None,
                                       "cost_sources": {}}])
        self.assertEqual((total["orders_sum"], total["revenue"], total["margin"], total["margin_pct"]), (rwm.Decimal("100"), None, None, None))

    def test_wb_finrez_labels_from_profile(self):
        fw = self.mods["report_finrez_wb"]
        self.assertEqual((fw.SHOP, fw.brand_of("t1"), fw.brand_of("", "КОЮЗ Топаз")), ("РБХ-1", "Beautyhome.me", "Beautyhome.me"))
        self.assertEqual(fw.BRAND_LABELS, {"Beautyhome.me", "(без товара)"})
        self.assertEqual((fw.OWNER_CATEGORIES, fw.category_of("Ювелирные кольца")), ((), "прочее"))

    def test_pivot_samples_in_cabinet_data_dir(self):
        fp = self.mods["finrez_pivots"]
        for spec in fp.SPECS:
            self.assertIn(os.path.join("data", "rbh1", "owner_finrez_"), spec["owner"])


class ShellExportsTests(unittest.TestCase):
    def test_exports_for_karatov_and_rbh(self):
        text = cabinet.shell_exports(cabinet.profile("karatov"), root="/r")
        self.assertIn("export MP_CABINET_NAME=KARATOV", text)
        self.assertIn("export MP_DATA_DIR=/r/data\n", text + "\n")
        self.assertIn("export MP_REPORT_PREFIX=''", text)
        self.assertIn("export MP_BOOK_MONTH_FROM=2026-04", text)
        text = cabinet.shell_exports(cabinet.profile("rbh2"), root="/r")
        self.assertIn("export MP_DATA_DIR=/r/data/rbh2", text)
        self.assertIn("export MP_LOGS_DIR=/r/logs/rbh2", text)
        self.assertIn("export MP_REPORT_PREFIX=rbh2_", text)
        self.assertIn("export MP_BOOK_MONTH_FROM=''", text)

    def test_new_profile_names_shared_and_empty_for_rbh(self):
        for code in ("rbh1", "rbh2"):
            p = cabinet.profile(code)
            self.assertEqual((p.METALS, p.OWNER_CATEGORIES, p.TYPE_TO_CATEGORY, p.KIND_TO_CATEGORY, p.NAME_RULES, p.CATEGORY_BY_SUBJECT, p.BOOK_MONTH_FROM),
                             ((), (), {}, {}, (), {}, None))
        k = cabinet.profile("karatov")
        self.assertEqual(set(k.TYPE_TO_CATEGORY.values()) | set(k.CATEGORY_BY_SUBJECT.values()), set(k.OWNER_CATEGORIES))
        self.assertEqual(sorted(k.GOLDEN_SKU["campaigns"]), ["24375331", "24375352"])


if __name__ == "__main__":
    unittest.main()
