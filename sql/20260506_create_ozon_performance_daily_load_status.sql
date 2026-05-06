create table if not exists public.ozon_performance_daily_load_status (
    load_date date not null,
    target_date date not null,
    marketplace_code text not null default 'ozon',
    account_signature text,
    mode text not null,
    cpc_campaign_count integer not null default 0,
    cpc_campaign_units_attempted integer not null default 0,
    cpc_campaign_units_completed integer not null default 0,
    cpc_pending_campaigns integer not null default 0,
    cpc_status text,
    cpo_status text,
    run_status text,
    ad_spend_loaded numeric not null default 0,
    ad_attribution_loaded numeric not null default 0,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (load_date, target_date, marketplace_code)
);

create index if not exists idx_ozon_perf_daily_load_status_target
    on public.ozon_performance_daily_load_status (target_date, marketplace_code, updated_at desc);

create index if not exists idx_ozon_perf_daily_load_status_account
    on public.ozon_performance_daily_load_status (load_date, account_signature, updated_at desc);
