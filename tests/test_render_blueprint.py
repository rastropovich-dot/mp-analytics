"""render.yaml для РБХ (§5.5): состав сервисов и групп, команды = команде KARATOV, параметры и имена переменных = KARATOV по API 21.10,
секреты без значений, KARATOV в Blueprint нет; файл = генератору."""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import cabinet  # noqa: E402
import render_blueprint as rb  # noqa: E402


class BlueprintCompositionTests(unittest.TestCase):
    def test_four_cron_services_two_per_cabinet_with_shifted_schedules(self):
        s = rb.services()
        self.assertEqual([x["name"] for x in s], ["mp-analytics-rbh1", "mp-analytics-rbh1-telegram-report", "mp-analytics-rbh2", "mp-analytics-rbh2-telegram-report"])
        self.assertEqual([x["schedule"] for x in s], ["20 0 * * *", "35 7 * * *", "25 0 * * *", "40 7 * * *"])   # KARATOV: 15 0 и 30 7
        for x in s:
            self.assertIn(x["cabinet"], cabinet.KNOWN_CODES); self.assertNotEqual(x["cabinet"], "karatov")
            self.assertEqual(x["group"], x["cabinet"])
            self.assertEqual((x["region"], x["runtime"], x["buildCommand"]), ("oregon", "python", "pip install -r requirements.txt"))
        self.assertEqual({x["startCommand"] for x in s if x["kind"] == "night"}, {rb.NIGHT_COMMAND})
        self.assertEqual({x["startCommand"] for x in s if x["kind"] == "alert"}, {rb.ALERT_COMMAND})
        self.assertEqual([x["plan"] for x in s], ["standard", "starter", "standard", "starter"])   # KARATOV по API 21.10

    def test_night_command_equals_the_karatov_command_in_claude_md(self):
        text = open(os.path.join(ROOT, "CLAUDE.md"), encoding="utf-8").read()
        self.assertIn(rb.NIGHT_COMMAND, text)                      # §1 «Живая команда mp-analytics»
        self.assertIn("`python3 alerts_telegram.py`", text)

    def test_env_keys_equal_karatov_services_plus_mp_cabinet(self):
        self.assertEqual(len(rb.MAIN_NIGHT_ENV_KEYS), 13); self.assertEqual(len(rb.MAIN_ALERT_ENV_KEYS), 11)
        for s in rb.services():
            expected = set(rb.MAIN_NIGHT_ENV_KEYS if s["kind"] == "night" else rb.MAIN_ALERT_ENV_KEYS) | {"MP_CABINET"}
            self.assertEqual(set(rb.env_keys(s)), expected, s["name"])
            self.assertEqual(dict(s["own_values"])["MP_CABINET"], s["cabinet"])
            if s["kind"] == "night":
                self.assertEqual(dict(s["own_values"])["ENABLE_OZON_SELECTED_CPO_DAILY"], "false")
                self.assertEqual(dict(s["own_values"])["APPROVE_OZON_SELECTED_CPO_DAILY_WRITE"], "false")
        # на Render KARATOV этих переменных нет — код берёт умолчания; PYTHON_VERSION не задан
        for absent in ("APP_TIMEZONE", "PYTHON_VERSION", "OZON_PERFORMANCE_CAMPAIGN_BATCH_SIZE", "OZON_ADS_CLICK_CAMPAIGNS"):
            for s in rb.services():
                self.assertNotIn(absent, rb.env_keys(s))

    def test_groups_hold_secrets_without_values_and_cover_env_example(self):
        g = rb.groups()
        self.assertEqual([x["name"] for x in g], ["rbh1", "rbh2"])
        example = {line.split("=", 1)[0] for line in open(os.path.join(ROOT, ".env.example"), encoding="utf-8") if re.match(r"^[A-Z_]+=", line)}
        for grp in g:
            keys = set(grp["secrets"]) | {k for k, _v in grp["settings"]}
            self.assertTrue(keys <= example, keys - example)       # ничего, чего код не читает
            for k, v in grp["settings"]:
                self.assertFalse(re.search(r"(?i)key|secret|token|password", k), f"{k} похож на секрет, а задан значением")
            for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "TELEGRAM_CHAT_ID", "WB_API_KEY", "OPENAI_API_KEY"):
                self.assertIn(k, grp["secrets"])

    def test_rendered_yaml_has_no_karatov_services_and_no_secret_values(self):
        text = rb.render()
        self.assertNotRegex(text, r"name: mp-analytics\n")                       # сервисы KARATOV — не в Blueprint (поправка советника 2)
        self.assertNotIn("name: mp-analytics-telegram-report\n", text)
        self.assertEqual(text.count("- type: cron"), 4)
        self.assertEqual(text.count("- fromGroup: rbh1"), 2); self.assertEqual(text.count("- fromGroup: rbh2"), 2)
        self.assertEqual(text.count("value: rbh1"), 2); self.assertEqual(text.count("value: rbh2"), 2)   # MP_CABINET у ночи и у алерта
        self.assertEqual(text.count("region: oregon"), 4); self.assertEqual(text.count("plan: standard"), 2); self.assertEqual(text.count("plan: starter"), 2)
        self.assertNotIn("PYTHON_VERSION", text); self.assertNotIn("APP_TIMEZONE", text); self.assertNotIn("frankfurt", text)
        for k in rb.SECRET_KEYS:
            self.assertEqual(text.count(f"- key: {k}\n        sync: false"), 2, k)
        self.assertEqual(text.count("- key: ENABLE_OZON_SELECTED_CPO_DAILY\n        value: false"), 2)
        self.assertNotRegex(text, r"(?i)(eyJ[A-Za-z0-9_-]{20,}|sbp_[A-Za-z0-9]{10,})")
        self.assertIn('schedule: "20 0 * * *"', text)
        self.assertRegex(text, r"startCommand: python3 run_daily_pipeline\.py --skip-recovery")

    def test_render_yaml_on_disk_equals_the_generator(self):
        path = os.path.join(ROOT, "render.yaml")
        self.assertTrue(os.path.exists(path), "render.yaml нет — scripts/render_blueprint.py --write")
        self.assertEqual(open(path, encoding="utf-8").read(), rb.render(), "render.yaml отличается от генератора — scripts/render_blueprint.py --write")


if __name__ == "__main__":
    unittest.main()
