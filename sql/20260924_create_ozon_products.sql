-- Тридцать седьмая задача, §3: категория товара из карточки Ozon — одной таблицей.
--
-- Источник — /v3/product/info/list по SKU (description_category_id, type_id, name, offer_id, статус) и
-- /v1/description-category/tree (имена категорий и типов). Категория владельца (кольца, серьги, подвески, цепочки, браслеты,
-- пирсинг, колье, броши) — нормализация type_name словарём в scripts/ozon_product_catalog.py; что не легло — «прочее».
-- SKU без карточки (удалена/архив без ответа) — категория по названию из заказов, category_source = 'name'.
-- Обновление — скриптом по слову (раз в неделю), ночного шага нет. Пишет только scripts/ozon_product_catalog.py --apply.

create table if not exists public.ozon_products (
    sku                      text primary key,
    product_id               bigint,
    offer_id                 text,
    name                     text,
    description_category_id  bigint,
    category_path            text,
    type_id                  bigint,
    type_name                text,
    category                 text not null,                  -- категория владельца: кольца / серьги / … / прочее
    category_source          text not null,                 -- card — по type_id карточки; name — по названию (карточки нет)
    brand                    text,                          -- KARATOV / Топаз — по первой букве артикула (T — Топаз)
    metal                    text,                          -- серебро / золото / null — по названию, как в листах «Серебро» / «Золото»
    is_archived              boolean,
    status                   text,                          -- statuses.status_name карточки («Продается», …)
    observed_at              timestamptz not null,          -- когда снята карточка
    updated_at               timestamptz not null default now(),
    constraint ozon_products_source_check check (category_source in ('card', 'name'))
);

create index if not exists ozon_products_category_idx on public.ozon_products (category);
create index if not exists ozon_products_offer_id_idx on public.ozon_products (offer_id);

comment on table public.ozon_products is
    'Карточки Ozon по SKU базы (заказы ∪ выкупы): категория/тип из /v3/product/info/list + дерево категорий; category — нормализация к списку владельца. Обновляется scripts/ozon_product_catalog.py --apply по слову';
comment on column public.ozon_products.category is 'Категория владельца: кольца, серьги, подвески, цепочки, браслеты, пирсинг, колье, броши, прочее';
comment on column public.ozon_products.category_source is 'card — из type_id карточки; name — по названию товара (карточки в ответе нет)';
