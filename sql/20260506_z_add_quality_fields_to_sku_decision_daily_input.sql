-- Порядок применения (db_migrate, 2026-10-11): `_z_` ставит файл ПОСЛЕ `20260506_create_sku_decision_daily_input.sql`;
-- с прежним именем `alter table if exists` на пустой базе молча пропускался.
alter table if exists sku_decision_daily_input
    add column if not exists stock_status text;

alter table if exists sku_decision_daily_input
    add column if not exists stock_issue_type text;

alter table if exists sku_decision_daily_input
    add column if not exists organic_reconciliation_status text;

alter table if exists sku_decision_daily_input
    add column if not exists unreconciled_revenue numeric;
