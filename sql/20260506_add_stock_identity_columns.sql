alter table if exists stock_daily
    add column if not exists product_id text,
    add column if not exists stock_marketplace_sku text,
    add column if not exists decision_marketplace_sku text,
    add column if not exists stock_identity_status text;
