-- Пятьдесят вторая §2: хранение Ozon по товарам — отчёт /v1/report/placement/by-products (ЛК «FBO → Стоимость размещения»),
-- зерно файла «SKU × склад × день» (разведка 2026-10-21: 5 327 строк за 20.10, 2 457 SKU, 34 склада; Σ начислений = тип 46 леджера
-- за тот же день до копейки). amount — со знаком леджера (списание < 0), как в ozon_accrual_daily_types; в файле сумма положительная.
-- Применяется только через scripts/db_migrate.py (план → слово → KARATOV с --allow-karatov → --cabinet all), до мержа ветки.
create table if not exists public.ozon_placement_by_product_daily (
    day             date        not null,
    sku             bigint      not null,
    warehouse       text        not null,
    offer_id        text,
    category        text,
    product_type    text,
    product_flag    text,
    volume_ml       integer     not null default 0,
    units           integer     not null default 0,
    paid_volume_ml  integer     not null default 0,
    paid_units      integer     not null default 0,
    amount          numeric(14, 2) not null default 0,
    report_code     text,
    loaded_at       timestamptz not null default now(),
    primary key (day, sku, warehouse)
);

comment on table public.ozon_placement_by_product_daily is
    'Стоимость размещения Ozon по товарам: строка файла отчёта placement/by-products за день (SKU × склад); amount со знаком леджера (52-я)';

create index if not exists idx_ozon_placement_by_product_daily_sku_day
    on public.ozon_placement_by_product_daily (sku, day);

-- витрина: хранение за день по SKU (Σ по складам, положительная сумма расхода, как другие колонки расходов витрины)
alter table if exists public.daily_sku_kpi
    add column if not exists storage_cost numeric default 0;

comment on column public.daily_sku_kpi.storage_cost is
    'Хранение Ozon за день по SKU — Σ по складам из ozon_placement_by_product_daily (положительно); в прибыль витрины не входит (52-я)';

grant select, insert, update, delete on public.ozon_placement_by_product_daily to service_role;
