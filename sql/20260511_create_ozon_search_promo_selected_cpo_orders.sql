create table if not exists public.ozon_search_promo_selected_cpo_orders (
    id bigserial primary key,
    sale_date date not null,
    marketplace_code text not null default 'ozon',
    order_id text not null,
    posting_number text not null,
    ordered_sku text not null,
    promoted_sku text not null,
    attribution_sku text not null,
    attribution_sku_basis text not null,
    offer_id text,
    promoted_article text,
    order_source_raw text,
    product_name text,
    quantity numeric,
    sale_amount numeric(14,2),
    item_amount numeric(14,2),
    bid_percent numeric(10,4),
    bid_amount numeric(14,2),
    spend numeric(14,2) not null,
    source_report text not null,
    promotion_type text not null,
    scope text not null,
    source_kind text not null,
    source_uuid text,
    raw_row jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create unique index if not exists uq_ozon_search_promo_selected_cpo_orders_idem
    on public.ozon_search_promo_selected_cpo_orders (
        sale_date,
        marketplace_code,
        source_report,
        promotion_type,
        order_id,
        posting_number,
        ordered_sku,
        promoted_sku
    );

create index if not exists idx_ozon_search_promo_selected_cpo_orders_sale_date
    on public.ozon_search_promo_selected_cpo_orders (sale_date);

create index if not exists idx_ozon_search_promo_selected_cpo_orders_promoted_sku
    on public.ozon_search_promo_selected_cpo_orders (promoted_sku);

create index if not exists idx_ozon_search_promo_selected_cpo_orders_ordered_sku
    on public.ozon_search_promo_selected_cpo_orders (ordered_sku);

create index if not exists idx_ozon_search_promo_selected_cpo_orders_source
    on public.ozon_search_promo_selected_cpo_orders (source_report, promotion_type);

create index if not exists idx_ozon_search_promo_selected_cpo_orders_sale_date_promoted_sku
    on public.ozon_search_promo_selected_cpo_orders (sale_date, promoted_sku);

create index if not exists idx_ozon_search_promo_selected_cpo_orders_sale_date_ordered_sku
    on public.ozon_search_promo_selected_cpo_orders (sale_date, ordered_sku);
