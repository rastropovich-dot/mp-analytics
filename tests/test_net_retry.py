"""Пятьдесят четвёртая §1а: транзиентные сетевые отказы (прокси 503, обрыв, reset, таймаут) повторяются 3 × 20 с; не транзиентные — сразу
наружу; справочный читатель курса книгу не роняет; постраничное чтение книги и бонусов идёт через повтор. Сети в тестах нет."""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import httpx  # noqa: E402

from loaders import net_retry, ozon_ad_bonus, unit_cost_history as uch  # noqa: E402
import report_ozon_month as rep  # noqa: E402


class Res:
    def __init__(self, data):
        self.data = data


class FlakyQuery:
    """Первые n вызовов execute падают transient-ошибкой, потом отдают данные."""

    def __init__(self, fails, data, exc=None):
        self.fails, self.data, self.calls = fails, data, 0
        self.exc = exc or httpx.ProxyError("503 Service Unavailable")

    def execute(self):
        self.calls += 1
        if self.calls <= self.fails:
            raise self.exc
        return Res(self.data)

    def __getattr__(self, name):
        return lambda *a, **k: self


class RetryTests(unittest.TestCase):
    def test_transient_classification(self):
        self.assertTrue(net_retry.is_transient(httpx.ProxyError("503 Service Unavailable")))
        self.assertTrue(net_retry.is_transient(httpx.RemoteProtocolError("Server disconnected")))
        self.assertTrue(net_retry.is_transient(httpx.ReadError("[Errno 54] Connection reset by peer")))
        self.assertTrue(net_retry.is_transient(RuntimeError("canceling statement due to statement timeout (57014)")))
        self.assertFalse(net_retry.is_transient(KeyError("SUPABASE_URL")))
        self.assertFalse(net_retry.is_transient(RuntimeError("column is_bonus does not exist")))
        self.assertTrue(net_retry.text_is_transient("httpx.ProxyError: 503 Service Unavailable")); self.assertFalse(net_retry.text_is_transient("KeyError: x"))

    def test_call_retries_three_times_with_twenty_second_pauses_then_raises(self):
        sleeps, logs = [], []
        q = FlakyQuery(2, [1])
        self.assertEqual(net_retry.execute(q, "t", sleep_fn=sleeps.append, log=logs.append).data, [1])
        self.assertEqual((q.calls, sleeps), (3, [20, 20])); self.assertEqual(len(logs), 2); self.assertIn("повтор 1 из 2", logs[0])
        q = FlakyQuery(3, [1])
        with self.assertRaises(httpx.ProxyError):
            net_retry.execute(q, "t", sleep_fn=sleeps.append, log=logs.append)
        self.assertEqual(q.calls, 3, "три попытки, не больше")

    def test_non_transient_error_is_raised_at_once(self):
        q = FlakyQuery(5, [1], exc=KeyError("x"))
        with self.assertRaises(KeyError):
            net_retry.call(q.execute, "t", sleep_fn=lambda s: self.fail("не должно ждать"))
        self.assertEqual(q.calls, 1)


class ReadersTests(unittest.TestCase):
    def test_book_fetch_survives_a_flaky_page(self):
        q = FlakyQuery(1, [{"id": 1}])
        sb = mock.Mock(); sb.table.return_value = q
        with mock.patch.object(net_retry, "PAUSE_SECONDS", 0), mock.patch("loaders.net_retry.time.sleep"), mock.patch("builtins.print"):
            rows = rep.fetch(sb, "t", "id", [("eq", "a", 1)], ["id"])
        self.assertEqual(rows, [{"id": 1}]); self.assertEqual(q.calls, 2)

    def test_bonus_reader_survives_a_flaky_page(self):
        q = FlakyQuery(1, [{"sale_date": "2026-10-21", "marketplace_sku": "1", "campaign_id": "c", "ad_spend": "1"}])
        sb = mock.Mock(); sb.table.return_value = q
        with mock.patch("loaders.net_retry.time.sleep"), mock.patch("builtins.print"):
            rows = ozon_ad_bonus.read_bonus_rows(sb, "2026-10-01", "2026-10-21")
        self.assertEqual(len(rows), 1); self.assertEqual(q.calls, 2)

    def test_rates_reader_failure_does_not_crash_the_book(self):
        printed = []
        with mock.patch.object(rep, "COST_RATE_K", {"585": 1}), mock.patch.object(uch, "load_rates", side_effect=RuntimeError("metal_rates_1c: таблица курсов 1С не прочиталась (ProxyError: 503)")), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            self.assertIsNone(rep.load_rates_for(mock.Mock(), None))
        self.assertTrue(any("курс 1С не прочитан" in l for l in printed), printed)

    def test_nightly_script_retries_on_network_markers(self):
        sh = open(os.path.join(ROOT, "scripts", "finrez_nightly.sh"), encoding="utf-8").read()
        self.assertIn('[ "$ATTEMPT" -ge "$MAX_ATTEMPTS" ]', sh); self.assertIn("FINREZ_RETRY_PAUSES:-120 300 600", sh)   # 26.10: 2 / 5 / 10 мин
        self.assertIn("ProxyError|RemoteProtocolError|ReadError", sh); self.assertIn("Connection refused", sh)
        self.assertNotIn("env -u", sh, "путь сети (прокси) не меняется без слова владельца")


if __name__ == "__main__":
    unittest.main()


class FalseMarkerTests(unittest.TestCase):
    """Довесок 54-й: голые 503 / 502 / 504 в суммах лога — не сеть; статус ответа решает по коду, не по тексту."""

    def test_amounts_with_503_in_them_are_not_transient(self):
        for text in ("Ozon  Общий итог  Себестоимость  свод  1 503,00 данные 1 503,00 разница 0,00", "разница 1 503,00", "строк 2504", "−2 504,17",
                     "rows=2504", "Оборот 10,502.5", "KeyError: 'x'", "строк 57014,00"):
            self.assertFalse(net_retry.text_is_transient(text), text)
        self.assertFalse(net_retry.is_transient(RuntimeError("rows=2504")))
        self.assertFalse(net_retry.is_transient(RuntimeError("итого 1 503,00")))

    def test_status_forms_are_transient(self):
        for text in ("httpx.ProxyError: 503 Service Unavailable", "HTTP 503", "HTTP/1.1 502 Bad Gateway", "status_code=504", "status 503",
                     "report/info: HTTP 503 {\"code\":...}", "canceling statement due to statement timeout", "SQLSTATE 57014"):
            self.assertTrue(net_retry.text_is_transient(text), text)
        self.assertTrue(net_retry.is_transient(httpx.ProxyError("503 Service Unavailable")))
        self.assertFalse(net_retry.text_is_transient("HTTP 404 Not Found")); self.assertFalse(net_retry.text_is_transient("HTTP 500"))

    def test_http_status_errors_decide_by_response_code(self):
        req = httpx.Request("GET", "https://x")
        for code, expected in ((503, True), (502, True), (504, True), (404, False), (500, False), (401, False)):
            exc = httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))
            self.assertEqual(net_retry.is_transient(exc), expected, code)
        import requests
        r = requests.Response(); r.status_code = 503
        self.assertTrue(net_retry.is_transient(requests.HTTPError("boom", response=r)))
        r.status_code = 400
        self.assertFalse(net_retry.is_transient(requests.HTTPError("boom 503 in text only", response=r)))

    def test_nightly_script_pattern_ignores_amounts(self):
        import re, subprocess
        sh = open(os.path.join(ROOT, "scripts", "finrez_nightly.sh"), encoding="utf-8").read()
        m = re.search(r'grep -qE "([^"]+)"', sh); self.assertIsNotNone(m)
        pattern = m.group(1).replace("\\\\", "\\")
        def hits(line):
            return subprocess.run(["grep", "-qE", pattern], input=line, text=True).returncode == 0
        for line in ("   Ozon  Общий итог   Себестоимость    свод   1 503,00 данные 1 503,00 разница 0.0000", "строк 2504", "−2 504,17", "Оборот 10,502.5"):
            self.assertFalse(hits(line), line)
        for line in ("httpx.ProxyError: 503 Service Unavailable", "WB-приёмка вернула код 1: HTTP 502", "httpx.RemoteProtocolError: Server disconnected", "status_code=504"):
            self.assertTrue(hits(line), line)


class NightlyLoopTests(unittest.TestCase):
    """Дополнение РБХ 26.10: книга rbh2 упала трижды за минуту на `ConnectError: [Errno 61] Connection refused` (прокси VPN не слушал
    порт). Скрипт гоняется по-настоящему в пустом каталоге: python3 подменён заглушкой, сборка падает отказом соединения заданное
    число раз, паузы — FINREZ_RETRY_PAUSES="0 0 0", итог в базу не пишется (FINREZ_NO_STATUS)."""

    STUB = """#!/bin/zsh
case "$1" in
  cabinet.py) print -r -- "export MP_CABINET=rbh2 MP_CABINET_NAME=TEST MP_DATA_DIR=$PWD/data MP_LOGS_DIR=$PWD/logs MP_REPORT_PREFIX=t_ MP_BOOK_MONTH_FROM=2026-09"; exit 0 ;;
  scripts/report_finrez.py)
    n=$(( $(cat calls 2>/dev/null || echo 0) + 1 )); echo $n > calls
    if [ $n -le $FAILS ]; then echo "httpx.ConnectError: [Errno 61] Connection refused"; exit 1; fi
    for a in "$@"; do if [ "$prev" = "--out" ]; then echo book > "$a"; fi; prev="$a"; done; exit 0 ;;
  *) exit 0 ;;
esac
"""

    def run_script(self, fails, pauses="0 0 0"):
        import shutil, subprocess, tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "scripts")); os.makedirs(os.path.join(d, "venv", "bin"))
        shutil.copy(os.path.join(ROOT, "scripts", "finrez_nightly.sh"), os.path.join(d, "scripts", "finrez_nightly.sh"))
        stub = os.path.join(d, "venv", "bin", "python3")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(self.STUB)
        os.chmod(stub, 0o755)
        env = {k: v for k, v in os.environ.items() if k not in ("FINREZ_COPY_DIR", "FINREZ_RETRY_PAUSES")}
        env.update({"FAILS": str(fails), "FINREZ_RETRY_PAUSES": pauses, "FINREZ_NO_STATUS": "1"})
        p = subprocess.run(["zsh", os.path.join(d, "scripts", "finrez_nightly.sh")], cwd=d, env=env, capture_output=True, text=True, timeout=60)
        calls = int(open(os.path.join(d, "calls")).read())
        return p.returncode, calls, p.stdout

    def test_connection_refused_three_times_then_the_fourth_attempt_builds_the_book(self):
        rc, calls, out = self.run_script(fails=3)
        self.assertEqual((rc, calls), (0, 4), out)
        self.assertIn("попытка 3 из 4 упала от сети", out)

    def test_four_refusals_stop_with_the_build_code(self):
        rc, calls, out = self.run_script(fails=9)
        self.assertEqual((rc, calls), (1, 4), out)

    def test_pauses_set_the_number_of_attempts(self):
        rc, calls, _out = self.run_script(fails=9, pauses="0")
        self.assertEqual((rc, calls), (1, 2))

    def test_connection_refused_is_transient_in_process_too(self):
        self.assertTrue(net_retry.is_transient(ConnectionRefusedError(61, "Connection refused")))
        self.assertTrue(net_retry.is_transient(httpx.ConnectError("[Errno 61] Connection refused")))
