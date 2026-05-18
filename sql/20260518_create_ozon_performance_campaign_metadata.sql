create table if not exists public.ozon_performance_campaign_metadata (
    snapshot_date date not null,
    marketplace_code text not null,
    campaign_id text not null,
    title text null,
    state text null,
    adv_object_type text null,
    payment_type text null,
    placement jsonb null,
    budget text null,
    daily_budget text null,
    weekly_budget text null,
    budget_type text null,
    expense_strategy text null,
    product_campaign_mode text null,
    product_autopilot_strategy text null,
    created_at timestamptz null,
    updated_at timestamptz null,
    raw_campaign_json jsonb not null,
    captured_at timestamptz not null default now(),
    primary key (snapshot_date, marketplace_code, campaign_id)
);

create index if not exists idx_ozon_performance_campaign_metadata_campaign
    on public.ozon_performance_campaign_metadata (campaign_id, snapshot_date desc);
