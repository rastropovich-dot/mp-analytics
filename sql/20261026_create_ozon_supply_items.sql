-- Пятьдесят седьмая §2 п. 3: состав поставок Ozon (FBO / кросс-докинг) для разнесения логистики без SKU (тип 12 CrossDock) по товарам.
-- Разведка 2026-10-26 (РБХ-1): unit_number начисления типа 12 = supply_id поставки; состав — /v3/supply-order/list
-- (order_number_search) → /v3/supply-order/get (supplies[].bundle_id) → /v1/supply-order/bundle (items: sku, quantity,
-- volume_in_litres, total_volume_in_litres). Одна заявка — 10 … 17 поставок, начисление — на каждую поставку.
-- Решение владельца 2026-10-26: сбор поставки делится между её SKU по объёму (total_volume_in_litres).
-- Применяется только через scripts/db_migrate.py (план → слово → KARATOV с --allow-karatov → --cabinet all), до мержа кода шага.
create table if not exists public.ozon_supply_items (
    supply_id              bigint      not null,
    sku                    bigint      not null,
    offer_id               text,
    quantity               integer     not null default 0,
    volume_in_litres       numeric(14, 6),
    total_volume_in_litres numeric(14, 6),
    order_id               bigint,
    order_number           text,
    bundle_id              text,
    is_crossdock           boolean,
    loaded_at              timestamptz not null default now(),
    primary key (supply_id, sku)
);

comment on table public.ozon_supply_items is
    'Состав поставки Ozon: SKU, штуки, литры из /v1/supply-order/bundle; ключ supply_id = unit_number начисления кросс-докинга (57-я)';

create index if not exists idx_ozon_supply_items_sku on public.ozon_supply_items (sku);

grant select, insert, update, delete on public.ozon_supply_items to service_role;
