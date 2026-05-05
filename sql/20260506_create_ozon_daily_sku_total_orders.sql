create table if not exists public.ozon_daily_sku_total_orders (
    sale_date date not null,
    marketplace_code text not null default 'ozon',
    marketplace_sku text not null,
    article text,
    product_name text,
    total_orders_qty numeric not null default 0,
    total_orders_revenue numeric not null default 0,
    total_revenue_source text not null,
    updated_at timestamptz not null default now(),
    primary key (sale_date, marketplace_code, marketplace_sku, total_revenue_source)
);

create index if not exists idx_ozon_daily_sku_total_orders_date
    on public.ozon_daily_sku_total_orders (sale_date, marketplace_code, total_revenue_source);
