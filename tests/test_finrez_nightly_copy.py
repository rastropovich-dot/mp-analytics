"""Третья задача RBH 05.11 §1: копия книги в iCloud — только при коде сборки 0.

Было: при коде ≠ 0 шаг копии брал файл книги с тем же именем, оставшийся от прошлой ночи, и копировал его владельцу
(30.10 — книга rbh2 29.10, 01.11 — октябрьские файлы обоих кабинетов): содержимое iCloud прежнее, время файла новое.
Скрипт гоняется по-настоящему в пустом каталоге, python3 подменён заглушкой (как в tests/test_net_retry.py, NightlyLoopTests).
"""
import datetime
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STUB = """#!/bin/zsh
case "$1" in
  cabinet.py) print -r -- "export MP_CABINET=$CAB MP_CABINET_NAME=TEST MP_DATA_DIR=$PWD/data MP_LOGS_DIR=$PWD/logs MP_REPORT_PREFIX=t_ MP_BOOK_MONTH_FROM=2026-09"; exit 0 ;;
  scripts/report_finrez.py)
    if [ "$BUILD_RC" != 0 ]; then echo "AssertionError: приёмка не сошлась"; exit $BUILD_RC; fi
    for a in "$@"; do if [ "$prev" = "--out" ]; then echo fresh > "$a"; fi; prev="$a"; done; exit 0 ;;
  *) exit 0 ;;
esac
"""


class NightlyCopyOnlyOnSuccessTests(unittest.TestCase):

    def run_script(self, build_rc, cabinet="rbh1"):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "scripts")); os.makedirs(os.path.join(d, "venv", "bin"))
        shutil.copy(os.path.join(ROOT, "scripts", "finrez_nightly.sh"), os.path.join(d, "scripts", "finrez_nightly.sh"))
        stub = os.path.join(d, "venv", "bin", "python3")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(STUB)
        os.chmod(stub, 0o755)
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        name = f"t_finrez_2026-09_{yesterday[:7]}.xlsx"
        os.makedirs(os.path.join(d, "data", "reports"))
        with open(os.path.join(d, "data", "reports", name), "w") as f:
            f.write("stale")  # книга прошлой ночи с тем же именем
        icloud = os.path.join(d, "icloud")
        os.makedirs(icloud)
        env = {k: v for k, v in os.environ.items() if k not in ("FINREZ_COPY_DIR", "FINREZ_RETRY_PAUSES", "FINREZ_NO_STATUS")}
        env.update({"CAB": cabinet, "BUILD_RC": str(build_rc), "FINREZ_RETRY_PAUSES": "0", "FINREZ_NO_STATUS": "1",
                    "FINREZ_COPY_DIR": icloud})
        p = subprocess.run(["zsh", os.path.join(d, "scripts", "finrez_nightly.sh")], cwd=d, env=env,
                           capture_output=True, text=True, timeout=60)
        sub = icloud if cabinet == "karatov" else os.path.join(icloud, cabinet)
        copied = os.path.join(sub, name)
        return p.returncode, (open(copied).read() if os.path.exists(copied) else None), p.stdout

    def test_failed_build_does_not_copy_last_nights_book(self):
        rc, copied, out = self.run_script(build_rc=1)
        self.assertEqual(rc, 1, out)
        self.assertIsNone(copied, out)
        self.assertIn("копия пропущена: сборка с кодом 1", out)
        self.assertIn("копия не делалась (код 1)", out)

    def test_failed_build_karatov_root_is_not_touched_either(self):
        rc, copied, out = self.run_script(build_rc=1, cabinet="karatov")
        self.assertEqual((rc, copied), (1, None), out)

    def test_successful_build_copies_the_fresh_book(self):
        rc, copied, out = self.run_script(build_rc=0)
        self.assertEqual((rc, copied), (0, "fresh\n"), out)
        self.assertIn("копия →", out)

    def test_successful_build_karatov_copies_to_the_root(self):
        rc, copied, out = self.run_script(build_rc=0, cabinet="karatov")
        self.assertEqual((rc, copied), (0, "fresh\n"), out)


if __name__ == "__main__":
    unittest.main()
