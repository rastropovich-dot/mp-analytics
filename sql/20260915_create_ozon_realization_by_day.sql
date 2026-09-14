-- Отчёт о реализации за день по SKU: /v1/finance/realization/by-day.
--
-- Зачем: единственный наш источник с настоящими ШТУКАМИ (quantity), а не
-- позициями начислений, и с разбивкой bonus / bank_coinvestment / standard_fee /
-- compensation / stars / pick_up_point_coinvestment, которой нет ни в accrual,
-- ни в старом transaction/list. Продажи и возвраты — раздельно.
--
-- Единица хранения — одна сторона одной строки отчёта: (дата, номер строки,
-- сторона). В одной строке отчёта у Ozon лежат два блока, delivery_commission и
-- return_commission; здесь они разложены в две записи с side = 'sale' / 'return',
-- чтобы суммы по продажам и по возвратам считались фильтром, а не арифметикой
-- над парами колонок. Один SKU может встречаться в отчёте несколько раз с
-- разной ценой или долей комиссии (31.08: 173 SKU на 209 строк), поэтому
-- ключ — номер строки, а не SKU. Номер строки стабилен только внутри одного
-- ответа: при повторной выгрузке дата ПЕРЕЗАПИСЫВАЕТСЯ целиком (delete + insert
-- в скрипте), а не upsert по номеру.
--
-- Глубина у Ozon — 32 календарных дня (проверено опытом 2026-09-14: 08-13 —
-- отказ, 08-14 — данные), поэтому таблица и есть наша единственная история.
-- Применять руками в SQL-редакторе Supabase или через MCP, после слова владельца.
-- Загрузка: scripts/load_ozon_realization_by_day.py. Разбор: docs/ozon_realization_by_day.md.

create table if not exists public.ozon_realization_by_day (
    marketplace_code             text        not null default 'ozon',
    realization_date             date        not null,  -- день отчёта (параметры day/month/year запроса)
    row_number                   integer     not null,  -- rowNumber из ответа, уникален внутри дня
    side                         text        not null,  -- 'sale' = delivery_commission, 'return' = return_commission
    sku                          bigint      not null,  -- item.sku
    offer_id                     text        not null,  -- item.offer_id, как в кабинете
    offer_id_norm                text        not null,  -- lower(offer_id), для стыковки с article_unit_costs
    barcode                      text,
    product_name                 text,
    seller_price_per_instance    numeric(14,2),         -- цена продавца с учётом скидки; = (amount + bonus + bank_coinvestment) / quantity у продаж
    commission_ratio             numeric(6,4),          -- доля комиссии по категории
    price_per_instance           numeric(14,2) not null,
    quantity                     integer     not null,  -- ШТУКИ
    amount                       numeric(14,2) not null, -- = price_per_instance × quantity
    bonus                        numeric(14,2) not null, -- баллы за скидки
    commission                   numeric(14,2) not null, -- итоговая комиссия с учётом скидок и наценки
    compensation                 numeric(14,2) not null, -- доплата за счёт Ozon
    standard_fee                 numeric(14,2) not null, -- базовое вознаграждение Ozon
    bank_coinvestment            numeric(14,2) not null, -- зелёные цены
    stars                        numeric(14,2) not null, -- звёзды
    pick_up_point_coinvestment   numeric(14,2) not null, -- АПВЗ
    total                        numeric(14,2) not null, -- итого к начислению
    raw_row                      jsonb       not null,  -- строка ответа целиком, обе стороны
    source                       text        not null default '/v1/finance/realization/by-day',
    fetched_at                   timestamptz not null,  -- когда снят ответ
    loaded_at                    timestamptz not null default now(),
    primary key (realization_date, row_number, side),
    constraint ozon_realization_by_day_side check (side in ('sale', 'return')),
    constraint ozon_realization_by_day_quantity_positive check (quantity > 0),
    constraint ozon_realization_by_day_offer_id_norm_lower check (offer_id_norm = lower(offer_id))
);

comment on table public.ozon_realization_by_day is
    'Реализация за день по SKU из /v1/finance/realization/by-day. Одна запись = одна сторона (продажа/возврат) одной строки отчёта. Дата перезаписывается целиком.';

create index if not exists idx_ozon_realization_by_day_sku
    on public.ozon_realization_by_day (realization_date, sku);

create index if not exists idx_ozon_realization_by_day_offer
    on public.ozon_realization_by_day (offer_id_norm, realization_date);
