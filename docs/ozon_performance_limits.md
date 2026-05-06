# Ozon Performance Limits and Production Schedule

## Confirmed Ozon Performance limits

- Ozon confirmed the `statistics/json` export limit is `2000 campaigns/day`.
- One campaign consumes one unit of that daily export limit.
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
- Historical and recovery runs stay separate:
  - `--mode full` for explicit historical ranges;
  - `--mode cpc-backfill` for pending CPC batches after the main daily run.

## Quota model

- The main scarce resource is not batch count but campaign units:
  - `1 campaign = 1 statistics/json unit`
  - a batch of `10` campaigns consumes `10` units
- Production env defaults:
  - `OZON_PERFORMANCE_STATS_DAILY_CAMPAIGN_LIMIT=2000`
  - `OZON_PERFORMANCE_STATS_DAILY_CAMPAIGN_RESERVE=200`
  - `OZON_PERFORMANCE_MAX_STATS_CAMPAIGNS_PER_DAILY_RUN=1800`
- If daily CPC cannot fit into the remaining budget:
  - CPC status becomes `pending_quota`
  - overall run status becomes `partial_quota`
  - remaining campaigns stay in DB-backed progress and can be retried by bounded backfill later
- DB-backed state in `pipeline_runtime_state` remains the source of truth for:
  - `cpc_progress`
  - `statistics/json` job cache
  - `cooldowns`
  - `batch_recommendations`

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
- For manual checks, prefer:
  - bounded `cpc-backfill`, or
  - Ozon-only runs with explicit CPC batch limits.
- Treat `statistics/json` campaign units as the scarce resource.
