alter table if exists public.ozon_performance_daily_load_status
    add column if not exists cpc_campaign_units_planned_total integer not null default 0,
    add column if not exists cpc_campaign_units_completed_total integer not null default 0,
    add column if not exists cpc_campaign_units_pending_total integer not null default 0,
    add column if not exists cpc_campaign_units_attempted_this_run integer not null default 0,
    add column if not exists cpc_campaign_units_completed_this_run integer not null default 0,
    add column if not exists cpc_campaign_units_failed_429_this_run integer not null default 0,
    add column if not exists cpc_stop_batch_index integer,
    add column if not exists cpc_stop_reason text;
