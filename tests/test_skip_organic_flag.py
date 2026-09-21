"""--skip-organic: органика выключена намеренно, а не по случайности.

Шаг органики до сих пор молчал только потому, что гейт ozon_downstream_allowed
закрывался — а закрывается он лишь когда у дневного сбора остался хвост, то есть
по случайному HTTP 500 на одном батче. В чистую ночь шаг запустился бы сам и
записал бы органику, завышенную примерно на четверть: Selected CPO не собирается
с 2026-05-21, а organic = total_orders - ad_attributed.

Флаг превращает случайное молчание в названное решение с условием снятия.
"""

import types
import unittest
from unittest import mock

import run_daily_pipeline as pipeline

ORGANIC = "Ozon: расчет organic sales по SKU"


def _args(skip_organic=False, **over):
    base = dict(
        skip_organic=skip_organic,
        skip_recovery=False,
        skip_excel=False,
        skip_decision=False,
        skip_telegram=False,
        ozon_recovery_current_day_only=False,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


class SkipOrganicTests(unittest.TestCase):
    def test_flag_skips_the_organic_step(self):
        skip, message = pipeline.should_skip_pipeline_step(
            ORGANIC, _args(skip_organic=True), ozon_downstream_allowed=True
        )
        self.assertTrue(skip)
        self.assertIn("Selected CPO", message)

    def test_flag_is_independent_of_ozon_downstream_allowed(self):
        """Главное свойство: работает при любом значении гейта, в том числе True."""
        for allowed in (True, False, None):
            skip, _message = pipeline.should_skip_pipeline_step(
                ORGANIC, _args(skip_organic=True), ozon_downstream_allowed=allowed
            )
            self.assertTrue(skip, f"ozon_downstream_allowed={allowed}")

    def test_without_the_flag_a_clean_night_still_runs_organic(self):
        """Ровно тот случай, ради которого флаг и заведён."""
        skip, message = pipeline.should_skip_pipeline_step(
            ORGANIC, _args(skip_organic=False), ozon_downstream_allowed=True
        )
        self.assertFalse(skip)
        self.assertIsNone(message)

    def test_gate_still_closes_organic_on_a_partial_night(self):
        skip, _message = pipeline.should_skip_pipeline_step(
            ORGANIC, _args(skip_organic=False), ozon_downstream_allowed=False
        )
        self.assertTrue(skip)

    def test_flag_touches_no_other_step(self):
        others = [
            "Ozon Performance: CPC recovery before daily",
            "Ozon Performance: CPC recovery after daily",
            "Ozon: реклама Performance API",
            "Ozon: total orders analytics по SKU",
            "Ozon: загрузка остатков",
            "KPI: расчет SKU",
            "KPI: расчет маркетплейсов",
            "WB: загрузка заказов",
        ]
        for title in others:
            skip, _message = pipeline.should_skip_pipeline_step(
                title, _args(skip_organic=True), ozon_downstream_allowed=True
            )
            self.assertFalse(skip, title)

    def test_flag_is_off_by_default(self):
        with mock.patch("sys.argv", ["run_daily_pipeline.py"]):
            args = pipeline.parse_args()
        self.assertFalse(args.skip_organic)

    def test_flag_parses_and_turns_on(self):
        with mock.patch("sys.argv", ["run_daily_pipeline.py", "--skip-organic"]):
            args = pipeline.parse_args()
        self.assertTrue(args.skip_organic)

    def test_help_states_the_reason_and_the_release_condition(self):
        """Флаг должен объяснять себя тому, кто найдёт его через полгода."""
        with mock.patch("sys.argv", ["run_daily_pipeline.py"]):
            parser_help = pipeline.__dict__  # доступ к модулю ради argparse ниже
        import argparse
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch("sys.argv", ["x", "--help"]):
            with self.assertRaises(SystemExit):
                pipeline.parse_args()
        text = buf.getvalue()
        self.assertIn("--skip-organic", text)
        self.assertIn("Selected CPO", text)


if __name__ == "__main__":
    unittest.main()
