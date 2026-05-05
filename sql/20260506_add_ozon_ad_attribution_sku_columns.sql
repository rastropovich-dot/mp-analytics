alter table if exists public.ozon_daily_sku_ad_attribution
    add column if not exists order_sku text,
    add column if not exists promoted_sku text,
    add column if not exists promoted_article text,
    add column if not exists raw_sku text,
    add column if not exists raw_promoted_sku text;
