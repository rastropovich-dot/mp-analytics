"""scripts/render_services_sync.py: состав запросов на создание сервисов РБХ = блюпринту, значения из .env.rbh<N>, отказ без
значений до обращений, создание → suspend, существующий сервис не трогается, план ничего не печатает из значений."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import render_blueprint as rb  # noqa: E402
import render_services_sync as rs  # noqa: E402

SECRET = "sb-secret-VALUE-1234567890"


def fake_env_dir(cabinets=("rbh1", "rbh2"), drop=()):
    d = tempfile.TemporaryDirectory()
    for c in cabinets:
        lines = [f"{k}={SECRET}{c}" for k in rb.SECRET_KEYS if k not in drop] + ["OZON_PERFORMANCE_STATE_BACKEND=db", "TELEGRAM_BOT_TOKEN="]
        open(os.path.join(d.name, f".env.{c}"), "w").write("\n".join(lines) + "\n")
    open(os.path.join(d.name, ".env"), "w").write(f"TELEGRAM_BOT_TOKEN={SECRET}common\nRENDER_API_KEY=rnd_test_key\n")
    return d


class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, existing=()):
        self.calls = []
        self.existing = [{"service": {"name": n, "id": f"crn-{n}", "suspended": "not_suspended"}} for n in existing]

    suspend_fail_first = 0   # сколько первых POST …/suspend отвечают 404, а GET показывает not_suspended (гонка 2026-10-23)

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw.get("params")))
        if url.endswith("/services"):
            return FakeResponse(200, self.existing)
        sid = url.rsplit("/", 1)[1]                                           # GET /services/{id} — состояние после suspend
        ok = self.suspends.get(sid, 0) > self.suspend_fail_first
        return FakeResponse(200, {"id": sid, "suspended": "suspended" if ok else "not_suspended"})

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw.get("json")))
        if url.endswith("/services"):
            return FakeResponse(201, {"service": {"id": "crn-new-" + kw["json"]["name"]}})
        sid = url.rsplit("/", 2)[1]
        self.suspends = getattr(self, "suspends", {}); self.suspends[sid] = self.suspends.get(sid, 0) + 1
        return FakeResponse(404 if self.suspends[sid] <= self.suspend_fail_first else 200, {})


class ComposeTests(unittest.TestCase):
    def test_create_body_matches_blueprint_and_render_schema(self):
        d = fake_env_dir()
        try:
            rows = rs.plan(root=d.name)
            self.assertEqual([r["service"]["name"] for r in rows], [s["name"] for s in rb.services()])
            for r in rows:
                self.assertEqual(r["missing"], [], r["service"]["name"])
                body = rs.create_body(r["service"], r["env_vars"])
                self.assertEqual(set(body), {"type", "name", "ownerId", "repo", "branch", "autoDeployTrigger", "rootDir", "envVars", "serviceDetails"})
                self.assertEqual((body["type"], body["branch"], body["autoDeployTrigger"], body["repo"]), ("cron_job", "main", "commit", rb.REPO))
                sd = body["serviceDetails"]
                self.assertEqual(set(sd), {"runtime", "schedule", "plan", "region", "envSpecificDetails"})
                self.assertEqual((sd["runtime"], sd["region"], sd["schedule"]), ("python", "oregon", r["service"]["schedule"]))
                self.assertEqual(sd["envSpecificDetails"], {"buildCommand": rb.BUILD_COMMAND, "startCommand": r["service"]["startCommand"]})
                self.assertEqual({e["key"] for e in body["envVars"]}, set(rb.env_keys(r["service"])))
                values = {e["key"]: e["value"] for e in body["envVars"]}
                self.assertEqual(values["MP_CABINET"], r["service"]["cabinet"])
                self.assertEqual(values["SUPABASE_SERVICE_KEY"], SECRET + r["service"]["cabinet"])     # из файла своего кабинета
                self.assertEqual(values["TELEGRAM_BOT_TOKEN"], SECRET + "common")                  # пустая строка кабинета → общий .env
                self.assertEqual(r["sources"]["TELEGRAM_BOT_TOKEN"], ".env")
                if r["service"]["kind"] == "night":
                    self.assertEqual(values["ENABLE_OZON_SELECTED_CPO_DAILY"], "false")
                else:
                    self.assertNotIn("ENABLE_OZON_SELECTED_CPO_DAILY", values)
            self.assertEqual([r["service"]["plan"] for r in rows], ["standard", "starter", "standard", "starter"])
        finally:
            d.cleanup()

    def test_empty_line_in_cabinet_file_counts_as_missing(self):
        d = fake_env_dir(drop=("WB_API_KEY",))
        try:
            rows = rs.plan(root=d.name)
            for r in rows:
                self.assertEqual(r["missing"], ["WB_API_KEY"])
            out = io.StringIO()
            with redirect_stdout(out):
                ok = rs.print_plan(rows)
            self.assertFalse(ok); self.assertIn("нет значения", out.getvalue()); self.assertNotIn(SECRET, out.getvalue())
        finally:
            d.cleanup()

    def test_optional_openai_key_is_skipped_not_blocking(self):
        d = fake_env_dir(drop=("OPENAI_API_KEY",))
        try:
            rows = rs.plan(root=d.name)
            for r in rows:
                self.assertEqual(r["missing"], [])
                self.assertNotIn("OPENAI_API_KEY", {e["key"] for e in r["env_vars"]})
                self.assertTrue(r["sources"]["OPENAI_API_KEY"].startswith("пусто"))
        finally:
            d.cleanup()

    def test_missing_env_file_is_reported_not_crashed(self):
        d = fake_env_dir(cabinets=("rbh1",))
        try:
            rows = rs.plan(root=d.name)
            r2 = [r for r in rows if r["service"]["cabinet"] == "rbh2"]
            self.assertTrue(all(not r["env_file_exists"] for r in r2))
        finally:
            d.cleanup()

    def test_plan_output_has_key_names_and_lengths_but_never_values(self):
        d = fake_env_dir()
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                rs.print_plan(rs.plan(root=d.name))
            text = out.getvalue()
            self.assertIn("SUPABASE_SERVICE_KEY ← .env.rbh1", text); self.assertIn("MP_CABINET ← blueprint", text)
            self.assertNotIn(SECRET, text)
            self.assertNotIn(SECRET, json.dumps(rs.masked(rs.create_body(rs.plan(root=d.name)[0]["service"], rs.plan(root=d.name)[0]["env_vars"]))))
        finally:
            d.cleanup()


class ApplyTests(unittest.TestCase):
    def test_apply_creates_missing_services_and_suspends_each(self):
        d = fake_env_dir()
        try:
            rows = rs.plan(root=d.name)
            s = FakeSession(existing=("mp-analytics-rbh1",))
            outcomes = rs.apply(rows, s, "rnd_test_key")
            self.assertEqual([(o["name"], o["action"]) for o in outcomes],
                             [("mp-analytics-rbh1", "exists"), ("mp-analytics-rbh1-telegram-report", "created"),
                              ("mp-analytics-rbh2", "created"), ("mp-analytics-rbh2-telegram-report", "created")])
            posts = [c for c in s.calls if c[0] == "POST"]
            self.assertEqual(len(posts), 6)                                    # 3 × (create + suspend)
            self.assertEqual([c[1].endswith("/suspend") for c in posts], [False, True] * 3)
            self.assertTrue(all(o["suspended"] == "suspended" for o in outcomes if o["action"] == "created"))
            self.assertEqual(s.calls[0][0], "GET")                             # сначала список, потом запись
            self.assertTrue(all(o["suspend_attempts"] == 1 for o in outcomes if o["action"] == "created"))
        finally:
            d.cleanup()

    def test_suspend_is_retried_and_verified_by_reading_the_service(self):
        # 2026-10-23: первый POST …/suspend сразу после создания отвечал 404; верим только GET /services/{id}
        s = FakeSession(); s.suspend_fail_first = 1
        slept = []
        state, tries = rs.suspend_verified(s, {}, "crn-x", attempts=3, pause_seconds=5.0, sleep_fn=slept.append)
        self.assertEqual((state, tries), ("suspended", 2))
        self.assertEqual(slept, [5.0])
        posts = [c for c in s.calls if c[0] == "POST"]; gets = [c for c in s.calls if c[0] == "GET"]
        self.assertEqual((len(posts), len(gets)), (2, 2))
        s2 = FakeSession(); s2.suspend_fail_first = 99
        state2, tries2 = rs.suspend_verified(s2, {}, "crn-y", attempts=2, pause_seconds=0.0, sleep_fn=lambda _p: None)
        self.assertTrue(state2.startswith("suspend_failed HTTP 404")); self.assertEqual(tries2, 2)

    def test_apply_refuses_before_any_call_when_values_missing(self):
        d = fake_env_dir(drop=("OZON_API_KEY",))
        try:
            s = FakeSession()
            with self.assertRaises(rs.SyncRefused):
                rs.apply(rs.plan(root=d.name), s, "rnd_test_key")
            self.assertEqual(s.calls, [])
        finally:
            d.cleanup()

    def test_apply_stops_after_a_failed_create(self):
        d = fake_env_dir()
        try:
            class Failing(FakeSession):
                def post(self, url, **kw):
                    self.calls.append(("POST", url, kw.get("json")))
                    return FakeResponse(400, {}, "bad plan")
            s = Failing()
            outcomes = rs.apply(rs.plan(root=d.name), s, "rnd_test_key")
            self.assertEqual([o["action"] for o in outcomes], ["create_failed"])
            self.assertEqual(len([c for c in s.calls if c[0] == "POST"]), 1)
        finally:
            d.cleanup()

    def test_no_resume_anywhere(self):
        src = open(os.path.join(ROOT, "scripts", "render_services_sync.py"), encoding="utf-8").read()
        self.assertNotIn("/resume", src)


if __name__ == "__main__":
    unittest.main()
