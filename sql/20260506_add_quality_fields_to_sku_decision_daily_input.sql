alter table if exists sku_decision_daily_input
    add column if not exists stock_status text;

alter table if exists sku_decision_daily_input
    add column if not exists stock_issue_type text;

alter table if exists sku_decision_daily_input
    add column if not exists organic_reconciliation_status text;

alter table if exists sku_decision_daily_input
    add column if not exists unreconciled_revenue numeric;
