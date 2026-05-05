create table if not exists public.ozon_daily_sku_ad_attribution (
    sale_date date not null,
    marketplace_code text not null default 'ozon',
    marketplace_sku text not null,
    ad_source text not null,
    attribution_type text not null default 'direct',
    campaign_id text not null default '',
    article text,
    product_name text,
    ad_orders_qty numeric not null default 0,
    ad_orders_revenue numeric not null default 0,
    ad_clicks numeric not null default 0,
    ad_views numeric not null default 0,
    ad_spend numeric not null default 0,
    warning text,
    updated_at timestamptz not null default now(),
    primary key (sale_date, marketplace_code, marketplace_sku, ad_source, attribution_type, campaign_id)
);

create index if not exists idx_ozon_daily_sku_ad_attribution_date
    on public.ozon_daily_sku_ad_attribution (sale_date, marketplace_code);

create table if not exists public.ozon_daily_sku_organic (
    sale_date date not null,
    marketplace_code text not null default 'ozon',
    marketplace_sku text not null,
    article text,
    product_name text,
    total_orders_qty numeric not null default 0,
    total_orders_revenue numeric not null default 0,
    ad_orders_qty numeric not null default 0,
    ad_orders_revenue numeric not null default 0,
    organic_orders_qty numeric not null default 0,
    organic_orders_revenue numeric not null default 0,
    ad_share_orders numeric,
    ad_share_revenue numeric,
    calculation_status text not null default 'ok',
    warning text,
    updated_at timestamptz not null default now(),
    primary key (sale_date, marketplace_code, marketplace_sku)
);

create index if not exists idx_ozon_daily_sku_organic_date
    on public.ozon_daily_sku_organic (sale_date, marketplace_code);

alter table if exists public.daily_sku_kpi
    add column if not exists ad_orders_qty numeric not null default 0,
    add column if not exists ad_orders_revenue numeric not null default 0,
    add column if not exists organic_orders_qty numeric not null default 0,
    add column if not exists organic_orders_revenue numeric not null default 0,
    add column if not exists ad_share_orders numeric,
    add column if not exists ad_share_revenue numeric;

alter table if exists public.daily_marketplace_kpi
    add column if not exists ad_orders_qty numeric not null default 0,
    add column if not exists ad_orders_revenue numeric not null default 0,
    add column if not exists organic_orders_qty numeric not null default 0,
    add column if not exists organic_orders_revenue numeric not null default 0,
    add column if not exists ad_share_orders numeric,
    add column if not exists ad_share_revenue numeric;
