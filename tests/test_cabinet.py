"""cabinet.py и cabinets/: профиль по умолчанию, выбор по MP_CABINET, overlay .env.<кабинет>, guard по хосту Supabase и его
присутствие во всех точках входа (первая задача RBH, §2). Сети нет: guard выходит до создания клиента."""
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
        self.assertIn("cabinet.assert_env()", sh)
        self.assertLess(sh.find("cabinet.assert_env()"), sh.find("scripts/report_finrez.py"))
        pipeline = open(os.path.join(ROOT, "run_daily_pipeline.py"), encoding="utf-8").read()
        self.assertLess(pipeline.find("print(cabinet.banner(CABINET))"), pipeline.find("🚀 Запуск ежедневного пайплайна"))
        alert = open(os.path.join(ROOT, "alerts_telegram.py"), encoding="utf-8").read()
        self.assertRegex(alert, r"lines = \[\n\s*cabinet\.banner\(CABINET\),")
        self.assertLess(alert.find("cabinet.assert_env("), alert.find("create_client("))


if __name__ == "__main__":
    unittest.main()
