# -*- coding: utf-8 -*-
"""scripts/render_env_sync.py (WB-18, 22.10): значение переменной Render из .env — план без записи, запись по --apply,
значения токенов никогда не печатаются, exp читается из JWT, оба сервиса, подтверждение после записи."""
import base64
import io
import json
import os
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout

import scripts.render_env_sync as rs


def jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"hdr.{payload}.sig"


OLD, NEW = jwt(1792521134), jwt(1802283920)


class FakeHttp:
    def __init__(self, env, deploys=None):
        self.env, self.calls, self.deploys = env, [], deploys or {}

    def _resp(self, code, body):
        return type("R", (), {"status_code": code, "json": lambda self_=None: body})()

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url))
        sid = url.split("/services/")[1].split("/")[0]
        if url.endswith("/env-vars"):
            return self._resp(200, [{"envVar": {"key": k, "value": v}, "cursor": "c"} for k, v in self.env[sid].items()])
        if url.endswith("/deploys"):
            return self._resp(200, [{"deploy": d} for d in self.deploys.get(sid, [])])
        return self._resp(404, {})

    def put(self, url, headers=None, data=None, timeout=None):
        self.calls.append(("PUT", url))
        sid, key = url.split("/services/")[1].split("/env-vars/")
        self.env[sid][key] = json.loads(data)["value"]
        return self._resp(200, {"key": key})


class RenderEnvSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "src.env"); self.env = os.path.join(self.tmp.name, ".env")
        open(self.src, "w").write(f"OTHER=1\nWB_API_KEY={NEW}\n")
        open(self.env, "w").write("RENDER_API_KEY=rnd_secret\n")
        self.http = FakeHttp({"crn-d7n7nan7f7vs73fk70kg": {"WB_API_KEY": OLD, "X": "1"}, "crn-d7t5ed1j2pic73aiqmog": {"WB_API_KEY": OLD}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_jwt_exp_and_env_reader(self):
        from datetime import datetime, timezone
        fmt = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        self.assertEqual(rs.jwt_exp(NEW), fmt(1802283920)); self.assertEqual(rs.jwt_exp(OLD), fmt(1792521134))
        self.assertTrue(rs.jwt_exp(NEW).startswith("2027-02-10")); self.assertTrue(rs.jwt_exp(OLD).startswith("2026-10-20"))
        self.assertIsNone(rs.jwt_exp("not-a-jwt")); self.assertEqual(rs.read_env_value(self.src, "WB_API_KEY"), NEW)
        self.assertIsNone(rs.read_env_value(self.src, "MISSING"))
        open(self.src, "a").write('Q="quoted"\n'); self.assertEqual(rs.read_env_value(self.src, "Q"), "quoted")

    def run_main(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out), unittest.mock.patch.object(rs, "requests", self.http):
            rc = rs.main(["--key", "WB_API_KEY", "--from", self.src, "--env", self.env, "--wait-live", "0", *extra])
        return rc, out.getvalue()

    def test_plan_prints_exp_and_lengths_but_never_values_and_writes_nothing(self):
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("2026-10-20", out); self.assertIn("2027-02-10", out); self.assertIn("отличается", out)
        self.assertNotIn(OLD, out); self.assertNotIn(NEW, out); self.assertNotIn("rnd_secret", out)
        self.assertFalse([c for c in self.http.calls if c[0] == "PUT"])
        self.assertEqual(self.http.env["crn-d7n7nan7f7vs73fk70kg"]["WB_API_KEY"], OLD)

    def test_apply_writes_both_services_and_confirms(self):
        rc, out = self.run_main("--apply")
        self.assertEqual(rc, 0)
        puts = [c for c in self.http.calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 2)
        for sid in rs.SERVICES:
            self.assertEqual(self.http.env[sid]["WB_API_KEY"], NEW)
        self.assertEqual(self.http.env["crn-d7n7nan7f7vs73fk70kg"]["X"], "1")        # другие переменные не тронуты
        self.assertIn("= файлу", out); self.assertIn("записано сервисов: 2", out); self.assertNotIn(NEW, out)

    def test_apply_skips_a_service_that_already_matches(self):
        self.http.env["crn-d7t5ed1j2pic73aiqmog"]["WB_API_KEY"] = NEW
        rc, out = self.run_main("--apply")
        self.assertEqual(rc, 0); self.assertEqual(len([c for c in self.http.calls if c[0] == "PUT"]), 1); self.assertIn("уже совпадает", out)

    def test_wait_live_finds_the_deploy_created_after_the_write(self):
        self.http.deploys = {"crn-d7n7nan7f7vs73fk70kg": [{"status": "live", "createdAt": "2026-10-22T14:00:00Z", "finishedAt": "2026-10-22T14:01:00Z"},
                                                           {"status": "live", "createdAt": "2026-10-21T21:09:32Z"}]}
        res = rs.wait_live("crn-d7n7nan7f7vs73fk70kg", "k", "2026-10-22T13:59:00", 0, http=self.http, sleep_fn=lambda s: None)
        self.assertEqual(res[0], "live"); self.assertEqual(res[1], "2026-10-22T14:00:00Z")
        self.assertIsNone(rs.wait_live("crn-d7n7nan7f7vs73fk70kg", "k", "2026-10-22T15:00:00", 0, http=self.http, sleep_fn=lambda s: None))

    def test_missing_key_or_api_key_is_loud(self):
        open(self.env, "w").write("NOTHING=1\n")
        rc, out = self.run_main(); self.assertEqual(rc, 2); self.assertIn("RENDER_API_KEY не найден", out)


if __name__ == "__main__":
    unittest.main()
