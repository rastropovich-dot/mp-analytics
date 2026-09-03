"""--ozon-recovery-current-day-only: хвост вчерашнего дня без исторического бэкфилла.

Выбор дат у воркера сейчас неисправен: бэклог строится по статусным строкам и
расходится с детектором дыр (30 дат против 28, пересечение 11). Пока это не
починено, бэкфилл включать рано — а хвост текущего дня копится по 10 юнитов за
ночь и нужен.

Ключевая тонкость, ради которой этот флаг вообще существует: pre-фаза — НЕ
подбор вчерашнего хвоста. Она стоит под гейтом is_yesterday_cpc_loaded и
запускается только когда вчерашняя дата уже success, то есть всегда работает со
старыми датами. Хвост подбирает post-фаза. Поэтому флаг выключает pre и сужает
post, а не наоборот.
"""

import types
import unittest
from unittest import mock

import run_daily_pipeline as pipeline


def _args(current_day_only=False, skip_recovery=False):
    return types.SimpleNamespace(
        ozon_recovery_current_day_only=current_day_only,
        skip_recovery=skip_recovery,
        skip_excel=False,
        skip_decision=False,
        skip_telegram=False,
        ozon_campaign_selection=None,
        ozon_recent_activity_days=None,
        ozon_dormant_probe_size=None,
        ozon_max_daily_cpc_units=None,
        ozon_allow_staged_cpc_partial=False,
    )


class PostRecoveryCommandTests(unittest.TestCase):
    def test_default_command_is_unchanged(self):
        command = pipeline.build_post_recovery_command(None)
        self.assertIn("--max-batches-per-run 26", command)
        self.assertIn("--stop-when-complete", command)
        self.assertNotIn("--date", command)

    def test_current_day_only_restricts_to_yesterday(self):
        with mock.patch.object(pipeline, "pipeline_yesterday", return_value="2026-09-02"):
            command = pipeline.build_post_recovery_command(_args(current_day_only=True))
        self.assertIn("--date 2026-09-02", command)
        self.assertIn(f"--max-batches-per-run {pipeline.CURRENT_DAY_TAIL_MAX_BATCHES}", command)
        self.assertNotIn("--max-batches-per-run 26", command)
        # --stop-when-complete гонит воркер по всему бэклогу; в этом режиме он не нужен.
        self.assertNotIn("--stop-when-complete", command)

    def test_batch_cap_matches_a_real_tail(self):
        """Хвост — один-два батча по 10 кампаний, а не десятки."""
        self.assertGreaterEqual(pipeline.CURRENT_DAY_TAIL_MAX_BATCHES, 1)
        self.assertLessEqual(pipeline.CURRENT_DAY_TAIL_MAX_BATCHES, 3)


class PrePhaseTests(unittest.TestCase):
    def test_pre_phase_is_skipped_in_current_day_only(self):
        skip, message = pipeline.should_skip_pipeline_step(
            "Ozon Performance: CPC recovery before daily",
            _args(current_day_only=True),
            ozon_downstream_allowed=None,
        )
        self.assertTrue(skip)
        self.assertIn("только текущего дня", message)

    def test_post_phase_is_not_skipped_in_current_day_only(self):
        skip, _message = pipeline.should_skip_pipeline_step(
            "Ozon Performance: CPC recovery after daily",
            _args(current_day_only=True),
            ozon_downstream_allowed=None,
        )
        self.assertFalse(skip)

    def test_pre_phase_still_runs_without_the_flag(self):
        skip, _message = pipeline.should_skip_pipeline_step(
            "Ozon Performance: CPC recovery before daily",
            _args(current_day_only=False),
            ozon_downstream_allowed=None,
            yesterday_cpc_complete=True,
        )
        self.assertFalse(skip)

    def test_skip_recovery_still_wins_over_the_flag(self):
        for title in ("Ozon Performance: CPC recovery before daily",
                      "Ozon Performance: CPC recovery after daily"):
            skip, _message = pipeline.should_skip_pipeline_step(
                title, _args(current_day_only=True, skip_recovery=True), ozon_downstream_allowed=None
            )
            self.assertTrue(skip, title)

    def test_other_steps_are_untouched_by_the_flag(self):
        for title in ("KPI: расчет SKU", "WB: загрузка заказов", "Ozon: реклама Performance API"):
            skip, _message = pipeline.should_skip_pipeline_step(
                title, _args(current_day_only=True), ozon_downstream_allowed=None
            )
            self.assertFalse(skip, title)


class YesterdayTests(unittest.TestCase):
    def test_falls_back_when_the_loader_is_unavailable(self):
        """build_steps зовётся на импорте — падать из-за лоадера здесь нельзя."""
        with mock.patch.dict("sys.modules", {"loaders.ozon_performance_ads_loader": None}):
            value = pipeline.pipeline_yesterday()
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}$")

    def test_flag_is_off_by_default(self):
        parser_args = pipeline.parse_args.__wrapped__ if hasattr(pipeline.parse_args, "__wrapped__") else None
        with mock.patch("sys.argv", ["run_daily_pipeline.py"]):
            args = pipeline.parse_args()
        self.assertFalse(args.ozon_recovery_current_day_only)
        self.assertFalse(args.skip_recovery)


if __name__ == "__main__":
    unittest.main()
