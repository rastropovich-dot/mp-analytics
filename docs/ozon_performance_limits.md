# Ozon Performance Limits and Production Schedule

## Confirmed Ozon Performance limits

- Ozon confirmed the `statistics/json` export limit is `2000 campaigns/day`.
- One campaign consumes one unit of that daily export limit.
- A batch of `10` campaigns consumes `10 campaign units`.
- The general API limit `100000` does not apply to `statistics/json` export jobs.
- The `statistics/json` export limit cannot be increased.
- There is no alternative endpoint that bypasses this export limit for CPC statistics.

## Production schedule

Moscow is `UTC+3`, without DST.

- Daily load: `06:10 MSK` = `03:10 UTC`
  - Render cron: `10 3 * * *`
- Telegram executive report: `09:00 MSK` = `06:00 UTC`
  - Render cron: `0 6 * * *`

## Why daily-yesterday beats a rolling 30-day window

- Executive and pricing decisions are made on the freshest complete day, not on a wide historical CPC window.
- Ozon counts `statistics/json` quota in campaign units, so a daily D-1 run is predictable:
  - current campaign count is about `785`;
  - full D-1 CPC fits into the confirmed `2000 campaigns/day` quota;
  - the production budget keeps a reserve and targets at most `1800` campaign units per daily run.
- Historical windows are still useful, but they belong to bounded backfill jobs, not to the daily production cron.

## Production Ozon Performance mode

- `run_daily_pipeline.py --skip-telegram` now calls Ozon Performance in `daily-yesterday` mode.
- For a morning run on `2026-05-06 MSK`, the target Ozon Performance window is:
  - `2026-05-05..2026-05-05`
- Daily D-1 CPC selection is now completeness-first by default:
  - keep all CPC campaigns whose campaign dates overlap the target date;
  - do not exclude a campaign only because it is currently inactive or not updated in that day;
  - use `recent` mode only as an explicit fallback, because it may miss D-1 spend needed for management decisions.
- Historical and recovery runs stay separate:
  - `--mode full` for explicit historical ranges;
  - `--mode cpc-backfill` for pending CPC batches after the main daily run.
- `cpc-backfill` should resume only canonical D-1 progress created with:
  - `selection_mode = complete`
  - saved `ordered_campaign_ids`
  - saved `campaign_list_hash`
- Legacy CPC progress created before ordered batch persistence should be treated as `legacy partial`:
  - do not resume it automatically;
  - do not assume batch indexes still point to the same campaign set;
  - wait for the next scheduled D-1 run to create a canonical resumable progress key.

## Quota model

- The main scarce resource is not batch count but campaign units:
  - `1 campaign = 1 statistics/json unit`
  - a batch of `10` campaigns consumes `10` units
- Production env defaults:
  - `OZON_PERFORMANCE_STATS_DAILY_CAMPAIGN_LIMIT=2000`
  - `OZON_PERFORMANCE_STATS_DAILY_CAMPAIGN_RESERVE=200`
  - `OZON_PERFORMANCE_MAX_STATS_CAMPAIGNS_PER_DAILY_RUN=1800`
  - `OZON_PERFORMANCE_DAILY_CPC_SELECTION_MODE=complete`
- Confirmed planning dry-run example for `2026-05-05`:
  - `raw_campaign_count = 948`
  - `filtered_recent_count = 185`
  - `raw_cpc_count = 948`
  - `date_overlap_cpc_count = 899`
  - `selected_cpc_count = 899`
  - `excluded_by_recent_filter_count = 714`
  - `cpc_campaign_count = 899`
  - `batch_size = 10`
  - `total_batches = 90`
  - `campaign_units = 899`
  - `usable_limit = 1800`
  - `would_fit_daily_limit = yes`
- This confirms the production D-1 CPC path is low-risk by quota:
  - rolling 30-day daily mode is removed;
  - the morning daily load now works on one day only;
  - the D-1 CPC run leaves a quota buffer of `901 campaign units`.
- If daily CPC cannot fit into the remaining budget:
  - CPC status becomes `pending_quota`
  - overall run status becomes `partial_quota`
  - remaining campaigns stay in DB-backed progress and can be retried by bounded backfill later
- `--plan-only` prints a safe planning summary before any report jobs are created:
  - `target_date`
  - `raw_campaign_count`
  - `raw_cpc_count`
  - `filtered_recent_count`
  - `date_overlap_cpc_count`
  - `selected_cpc_count`
  - `excluded_by_recent_filter_count`
  - `excluded_by_quota_count`
  - `campaign_units`
  - `daily_limit`
  - `reserve`
  - `usable_limit`
  - `would_fit_daily_limit`
- DB-backed state in `pipeline_runtime_state` remains the source of truth for:
  - `cpc_progress`
  - `statistics/json` job cache
  - `cooldowns`
  - `batch_recommendations`
- For CPC resume safety, `cpc_progress` should contain:
  - `ordered_campaign_ids`
  - `campaign_list_hash`
  - `selection_mode = complete`

## Safe cron split

Current `run_daily_pipeline.py` can now skip the Telegram step.

- Load cron command:

```bash
python3 run_daily_pipeline.py --skip-telegram
```

- Report cron command:

```bash
python3 alerts_telegram.py
```

This split is safer than running one combined cron because:

- the Ozon load can start right after the assumed safe window after the overnight limit reset;
- the executive Telegram report is delayed until the morning, when data loading is expected to be complete;
- a retry or bounded Ozon backfill does not need to resend the executive report.

## Operational guidance

- Do not run manual full pipeline loads during the day unless necessary.
- Prefer `--plan-only` when you need to confirm D-1 campaign units without creating any Ozon report jobs.
- If `daily-yesterday` is forced into `recent` selection mode, treat that as a warning state:
  - it may miss real D-1 CPC spend;
  - it is unsuitable for management decisions that need full yesterday attribution.
- For manual checks, prefer:
  - bounded `cpc-backfill`, or
  - Ozon-only runs with explicit CPC batch limits.
- Before any live `cpc-backfill`, confirm in read-only mode:
  - `ordered_campaign_ids present = yes`
  - `campaign_list_hash present = yes`
  - `selection_mode = complete`
  - `completed_batches / pending_batches`
  - `cpc_status / run_status`
- If resume falls back to `deterministic_sort_fallback`, do not auto-resume that progress.
- Treat `statistics/json` campaign units as the scarce resource.
