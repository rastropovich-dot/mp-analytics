"""render.yaml для РБХ (§5.5): состав сервисов и групп, команды = команде KARATOV, секреты без значений, KARATOV в Blueprint нет; файл = генератору."""
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
        self.assertEqual({x["startCommand"] for x in s if x["kind"] == "night"}, {rb.NIGHT_COMMAND})
        self.assertEqual({x["startCommand"] for x in s if x["kind"] == "alert"}, {rb.ALERT_COMMAND})

    def test_night_command_equals_the_karatov_command_in_claude_md(self):
        text = open(os.path.join(ROOT, "CLAUDE.md"), encoding="utf-8").read()
        self.assertIn(rb.NIGHT_COMMAND, text)                      # §1 «Живая команда mp-analytics»
        self.assertIn("`python3 alerts_telegram.py`", text)

    def test_groups_hold_every_variable_the_code_reads_and_no_secret_values(self):
        g = rb.groups()
        self.assertEqual([x["name"] for x in g], ["rbh1", "rbh2"])
        example = {line.split("=", 1)[0] for line in open(os.path.join(ROOT, ".env.example"), encoding="utf-8") if re.match(r"^[A-Z_]+=", line)}
        for grp in g:
            keys = set(grp["secrets"]) | {k for k, _v in grp["settings"]} | set(grp["unsynced"])
            missing = example - keys - {"OPENAI_API_KEY", "OZON_ADS_CLICK_CAMPAIGNS"}   # decision layer выключен; список кампаний пуст и у KARATOV
            self.assertEqual(missing, set(), f"{grp['name']}: нет в группе {missing}")
            for k, v in grp["settings"]:
                self.assertFalse(re.search(r"(?i)key|secret|token|password", k), f"{k} похож на секрет, а задан значением")
            self.assertIn("SUPABASE_URL", grp["secrets"]); self.assertIn("SUPABASE_SERVICE_KEY", grp["secrets"]); self.assertIn("TELEGRAM_CHAT_ID", grp["secrets"])
            self.assertEqual(dict(grp["settings"])["ENABLE_OZON_SELECTED_CPO_DAILY"], "false")
            self.assertEqual(dict(grp["settings"])["APPROVE_OZON_SELECTED_CPO_DAILY_WRITE"], "false")

    def test_rendered_yaml_has_no_karatov_services_and_no_secret_values(self):
        text = rb.render()
        self.assertNotRegex(text, r"name: mp-analytics\n")                       # сервисы KARATOV — не в Blueprint (поправка советника 2)
        self.assertNotIn("name: mp-analytics-telegram-report\n", text)
        self.assertEqual(text.count("- type: cron"), 4)
        self.assertEqual(text.count("- fromGroup: rbh1"), 2); self.assertEqual(text.count("- fromGroup: rbh2"), 2)
        self.assertEqual(text.count("value: rbh1"), 2); self.assertEqual(text.count("value: rbh2"), 2)   # MP_CABINET у ночи и у алерта
        for k in rb.SECRET_KEYS:
            self.assertEqual(text.count(f"- key: {k}\n        sync: false"), 2, k)
        self.assertNotRegex(text, r"(?i)(eyJ[A-Za-z0-9_-]{20,}|sbp_[A-Za-z0-9]{10,})")
        self.assertIn('schedule: "20 0 * * *"', text)
        self.assertRegex(text, r"startCommand: python3 run_daily_pipeline\.py --skip-recovery")

    def test_render_yaml_on_disk_equals_the_generator(self):
        path = os.path.join(ROOT, "render.yaml")
        self.assertTrue(os.path.exists(path), "render.yaml нет — scripts/render_blueprint.py --write")
        self.assertEqual(open(path, encoding="utf-8").read(), rb.render(), "render.yaml отличается от генератора — scripts/render_blueprint.py --write")


if __name__ == "__main__":
    unittest.main()
