create table if not exists public.ozon_finance_advertising_reconciliation_raw (
    id bigserial primary key,
    transaction_date date not null,
    marketplace_code text not null default 'ozon',
    operation_type text,
    service_name text,
    amount numeric not null default 0,
    currency text,
    posting_number text,
    marketplace_sku text,
    article text,
    raw_json jsonb not null default '{}'::jsonb,
    source text not null default 'finance_api',
    imported_at timestamptz not null default now()
);

create index if not exists ix_ozon_finance_advertising_reconciliation_raw_date
on public.ozon_finance_advertising_reconciliation_raw (transaction_date, marketplace_code);

create index if not exists ix_ozon_finance_advertising_reconciliation_raw_operation
on public.ozon_finance_advertising_reconciliation_raw (operation_type);

create index if not exists ix_ozon_finance_advertising_reconciliation_raw_service
on public.ozon_finance_advertising_reconciliation_raw (service_name);
