-- Replace unique(load_date, target_date, marketplace_code) with
-- unique(load_date, target_date, marketplace_code, mode) so that
-- daily-yesterday and cpc-backfill rows for the same target date
-- can coexist without one overwriting the other.

-- Step 1: find and drop the old 3-column unique constraint
DO $$
DECLARE
    v_constraint_name text;
BEGIN
    SELECT c.conname INTO v_constraint_name
    FROM pg_constraint c
    WHERE c.conrelid = 'ozon_performance_daily_load_status'::regclass
      AND c.contype = 'u'
      AND NOT EXISTS (
          SELECT 1
          FROM unnest(c.conkey) AS k
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k
          WHERE a.attname = 'mode'
      )
      AND (
          SELECT COUNT(*)
          FROM unnest(c.conkey) AS k
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k
          WHERE a.attname IN ('load_date', 'target_date', 'marketplace_code')
      ) = 3;

    IF v_constraint_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE ozon_performance_daily_load_status DROP CONSTRAINT %I',
            v_constraint_name
        );
        RAISE NOTICE 'Dropped old constraint: %', v_constraint_name;
    ELSE
        RAISE NOTICE 'No matching 3-column unique constraint found — skipping drop';
    END IF;
END $$;

-- Step 2: add new 4-column unique constraint
ALTER TABLE ozon_performance_daily_load_status
    ADD CONSTRAINT ozon_performance_daily_load_status_load_target_marketplace_mode_key
    UNIQUE (load_date, target_date, marketplace_code, mode);
