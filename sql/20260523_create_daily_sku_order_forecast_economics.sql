create table if not exists public.daily_sku_order_forecast_economics (
    id bigserial primary key,
    order_date date not null,
    marketplace_code text not null,
    marketplace_sku text not null,
    article text null,
    product_name text null,

    orders_qty numeric null,
    orders_revenue numeric null,
    ad_orders_qty numeric null,
    ad_orders_revenue numeric null,
    organic_orders_qty numeric null,
    organic_orders_revenue numeric null,

    cpc_spend numeric null,
    cpo_all_spend numeric null,
    selected_cpo_spend numeric null,
    total_ad_spend numeric null,

    cpc_acos numeric null,
    total_order_tacos numeric null,
    cpc_order_tacos numeric null,
    selected_cpo_order_tacos numeric null,

    expected_buyout_rate_qty numeric null,
    expected_buyout_rate_amount numeric null,
    expected_buyout_rate_source text null,
    expected_buyout_rate_sample_orders numeric null,
    expected_buyout_rate_confidence text null,

    expected_buyouts_qty numeric null,
    expected_buyouts_revenue numeric null,
    unit_cost numeric null,
    cogs_source text null,
    expected_cogs numeric null,
    commission_rate numeric null,
    commission_rate_source text null,
    expected_commission numeric null,
    acquiring_rate numeric null,
    acquiring_rate_source text null,
    expected_acquiring numeric null,
    logistics_per_unit numeric null,
    logistics_rate_source text null,
    expected_logistics numeric null,
    other_rate numeric null,
    other_rate_source text null,
    expected_other numeric null,
    expected_gross_margin numeric null,
    expected_fin_result numeric null,
    expected_fin_result_margin numeric null,

    target_profit_amount numeric null,
    target_profit_rate numeric null,
    max_affordable_ad_spend numeric null,
    ad_spend_headroom numeric null,

    data_quality_status text null,
    decision_status text null,
    organic_reconciliation_status text null,
    assumption_flags jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create unique index if not exists idx_daily_sku_order_forecast_economics_unique
    on public.daily_sku_order_forecast_economics (order_date, marketplace_code, marketplace_sku);

create index if not exists idx_daily_sku_order_forecast_economics_lookup
    on public.daily_sku_order_forecast_economics (marketplace_code, marketplace_sku, order_date desc);
