-- Порядок применения (db_migrate, 2026-10-11): `_z_` ставит файл ПОСЛЕ `20260506_create_ozon_performance_daily_load_status.sql`;
-- с прежним именем `alter table if exists` на пустой базе молча пропускался.
alter table if exists public.ozon_performance_daily_load_status
    add column if not exists cpc_campaign_units_planned_total integer not null default 0,
    add column if not exists cpc_campaign_units_completed_total integer not null default 0,
    add column if not exists cpc_campaign_units_pending_total integer not null default 0,
    add column if not exists cpc_campaign_units_attempted_this_run integer not null default 0,
    add column if not exists cpc_campaign_units_completed_this_run integer not null default 0,
    add column if not exists cpc_campaign_units_failed_429_this_run integer not null default 0,
    add column if not exists cpc_stop_batch_index integer,
    add column if not exists cpc_stop_reason text;
