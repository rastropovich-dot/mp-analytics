create table if not exists public.ozon_selected_cpo_xlsx_raw (
    id bigserial primary key,
    report_date date not null,
    marketplace_code text not null default 'ozon',
    marketplace_sku text,
    article text,
    product_name text,
    campaign_id text,
    campaign_name text,
    orders_qty numeric not null default 0,
    orders_revenue numeric not null default 0,
    rate_percent numeric,
    ad_spend numeric not null default 0,
    source_file text,
    source_file_hash text,
    sheet_name text,
    raw_row jsonb not null default '{}'::jsonb,
    imported_at timestamptz not null default now()
);

create index if not exists ix_ozon_selected_cpo_xlsx_raw_date
on public.ozon_selected_cpo_xlsx_raw (report_date, marketplace_code);

create index if not exists ix_ozon_selected_cpo_xlsx_raw_file_hash
on public.ozon_selected_cpo_xlsx_raw (source_file_hash);

create index if not exists ix_ozon_selected_cpo_xlsx_raw_campaign
on public.ozon_selected_cpo_xlsx_raw (campaign_id, campaign_name);
