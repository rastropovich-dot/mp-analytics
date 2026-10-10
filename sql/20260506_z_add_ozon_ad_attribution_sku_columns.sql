-- Порядок применения (db_migrate, 2026-10-11): `_z_` в имени ставит файл ПОСЛЕ `20260506_create_ozon_daily_sku_organic.sql`,
-- который создаёт ozon_daily_sku_ad_attribution; с прежним именем `alter table if exists` на пустой базе молча пропускался.
alter table if exists public.ozon_daily_sku_ad_attribution
    add column if not exists order_sku text,
    add column if not exists promoted_sku text,
    add column if not exists promoted_article text,
    add column if not exists raw_sku text,
    add column if not exists raw_promoted_sku text;
