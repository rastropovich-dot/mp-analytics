-- Шестьдесят первая (пятая задача RBH): отправление × товар Ozon — связь «отправление → SKU» для когорт и модели
-- docs/reports_model.md («отправление × товар»). До неё ночные шаги заказов получали products[] каждого отправления, но в базу
-- писали только день × SKU (marketplace_orders), а ozon_posting_status_log — отправление без товаров; сырьё на Render не живёт.
--
-- Источник — тот же ответ ночных шагов заказов (FBO /v3/posting/fbo/list, FBS /v4/posting/fbs/list), ни одного нового
-- обращения к Ozon. Разведка 2026-11-06 по сырью трёх кабинетов (454 тыс. отправлений): повторов sku внутри отправления 0,
-- родителей разделённых (cancelled_from_split_pending) 0 — ключ (posting_number, sku) однозначен.
--
-- Тождество: Σ quantity × seller_price по (order_date, sku, schema) = orders_amount_seller + cancelled_orders_amount_seller
-- marketplace_orders той же ночи; Σ quantity — orders_qty + cancelled_orders_qty (то же правило строк, loaders/ozon_orders_rows.py).
--
-- Применяется только через scripts/db_migrate.py (план → слово → KARATOV с --allow-karatov → --cabinet all), до мержа ветки.
create table if not exists public.ozon_posting_items (
    posting_number  text        not null,
    sku             bigint      not null,
    offer_id        text,
    schema          text        not null,   -- fbo | fbs
    order_date      date        not null,   -- день заказа по МСК — когорта, как order_date marketplace_orders
    ordered_at      timestamptz,            -- исходная метка Ozon: FBO created_at, FBS in_process_at (created_at в /v4 нет)
    quantity        integer     not null,
    seller_price    numeric(14, 2) not null, -- цена продавца за единицу (products[].price)
    buyer_price     numeric(14, 2),          -- оплачено покупателем за единицу; null = не измерено (FBO без отчёта ЛК, чужая валюта)
    status          text        not null,   -- статус отправления на момент сбора (observed_at)
    observed_at     timestamptz not null,   -- момент сбора, из которого взята строка
    source          text        not null,   -- 'nightly' | 'history:<файл сырья>'
    loaded_at       timestamptz not null default now(),
    primary key (posting_number, sku),
    constraint ozon_posting_items_schema check (schema in ('fbo', 'fbs')),
    constraint ozon_posting_items_quantity check (quantity > 0)
);

comment on table public.ozon_posting_items is
    'Отправление × товар Ozon из ночных списков FBO /v3 и FBS /v4: штуки, цена продавца и покупателя за единицу, статус на момент сбора (61-я)';

create index if not exists idx_ozon_posting_items_day
    on public.ozon_posting_items (order_date, schema);

create index if not exists idx_ozon_posting_items_sku_day
    on public.ozon_posting_items (sku, order_date);

grant select, insert, update, delete on public.ozon_posting_items to service_role;
