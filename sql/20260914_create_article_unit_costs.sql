-- Себестоимость по варианту offer_id из снимков 1С.
--
-- Заменяет sql/20260518_create_article_unit_costs.sql, которая никогда не
-- применялась (проверено 2026-09-13) и имела другую модель: article + valid_from/valid_to.
-- Здесь единица хранения — снимок: (offer_id, snapshot_date). Второй файл из 1С
-- добавит строки с новой датой, а не затрёт первые, поэтому маржа прошлых
-- периодов не пересчитается молча по новой цене (этот механизм уже видели на
-- marketplace_expenses). Обоснование и стыковка: docs/cost_of_goods.md.
--
-- Применять руками в SQL-редакторе Supabase, после слова владельца.
-- Загрузка данных: scripts/load_article_unit_costs.py.

create table if not exists public.article_unit_costs (
    marketplace_code   text        not null default 'ozon',
    offer_id           text        not null,  -- как в кабинете и в файле, с регистром
    offer_id_norm      text        not null,  -- lower(offer_id): стыковка регистронезависимая
    snapshot_date      date        not null,  -- дата снимка 1С (для cost_20260520.xlsx — 2026-05-20)
    unit_cost          numeric(14,2) not null, -- колонка «Себестоимость»: по варианту артикул+размер+вставка
    unit_cost_uniform  numeric(14,2),          -- колонка «единая»: одна на артикул 1С; справочно, в расчётах НЕ используется
    currency           text        not null default 'RUB',
    variant_type       text        not null,  -- main | select | discount — префикс F / S / T
    article_1c         text        not null,  -- «Артикул» 1С, например Т14702В909-02-03
    size               text,                  -- «Размер»; '0' — безразмерное изделие
    insert_category    text,                  -- «КатегорияКамней»
    product_kind       text,                  -- «ВидИзделия»
    metal_fineness     text,                  -- «Проба»
    weight_g           numeric(10,3),         -- «Вес»
    discontinued       boolean,               -- «Непроизводится» = Да
    source_file        text        not null,  -- имя файла-снимка
    source_row         integer     not null,  -- номер строки в файле, как в Excel (заголовок = 1)
    loaded_at          timestamptz not null default now(),
    primary key (offer_id_norm, snapshot_date),
    constraint article_unit_costs_unit_cost_positive check (unit_cost > 0),
    constraint article_unit_costs_variant_type check (variant_type in ('main', 'select', 'discount')),
    constraint article_unit_costs_offer_id_norm_lower check (offer_id_norm = lower(offer_id))
);

comment on table public.article_unit_costs is
    'Себестоимость по варианту offer_id из снимков 1С. Ключ (offer_id_norm, snapshot_date); один снимок — одна дата, второй снимок добавляет строки.';

create index if not exists idx_article_unit_costs_lookup
    on public.article_unit_costs (marketplace_code, offer_id_norm, snapshot_date desc);

create index if not exists idx_article_unit_costs_article_1c
    on public.article_unit_costs (article_1c, snapshot_date);
