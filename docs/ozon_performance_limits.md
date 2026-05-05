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
