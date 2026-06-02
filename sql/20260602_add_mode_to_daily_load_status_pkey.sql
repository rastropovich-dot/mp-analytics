-- Add mode to primary key of ozon_performance_daily_load_status so that
-- daily-yesterday and cpc-backfill rows for the same target date can coexist.
-- The on_conflict="load_date,target_date,marketplace_code,mode" upsert was failing
-- because the INSERT violated the old 3-column PK before ON CONFLICT could handle it.

-- Step 1: drop old 3-column primary key
ALTER TABLE ozon_performance_daily_load_status
    DROP CONSTRAINT ozon_performance_daily_load_status_pkey;

-- Step 2: add new 4-column primary key
ALTER TABLE ozon_performance_daily_load_status
    ADD CONSTRAINT ozon_performance_daily_load_status_pkey
    PRIMARY KEY (load_date, target_date, marketplace_code, mode);

-- Step 3: drop the unique constraint added by 20260531 migration (now redundant — covered by PK)
ALTER TABLE ozon_performance_daily_load_status
    DROP CONSTRAINT IF EXISTS ozon_performance_daily_load_status_mode_unique;
