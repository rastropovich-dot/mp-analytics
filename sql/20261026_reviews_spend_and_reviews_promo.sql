-- «Отзывы» по SKU до витрины (слово владельца 2026-10-26): типы начислений Ozon 95 CustomerReviews «Отзывы за баллы» и 96
-- AcceleratedReviewCollection «Ускоренный сбор отзывов» — статья reviews в marketplace_expenses (TYPE_TO_EXPENSE), в витрине — своя
-- колонка reviews_spend (с НДС, расход > 0, как ad_spend), в ad_spend не кладётся. ДРР «(реклама + отзывы)» = (ad_spend + reviews_spend) /
-- orders_amount_seller — ads_reviews_share_of_orders; ad_share_of_orders остаётся «только реклама» (справочно).
-- 96 приходит без SKU: у РБХ витрина разносит его по SKU, подключённым к услуге (ozon_reviews_promo_daily, поле promotions[REVIEWS_PROMO]
-- ответа /v3/product/info/list), пропорционально заказам дня; у KARATOV — строкой без SKU (правило профиля REVIEWS_96_RULE).
-- Применяется только через scripts/db_migrate.py (план → слово → KARATOV с --allow-karatov → --cabinet all), до мержа ветки.
alter table if exists public.daily_sku_kpi
    add column if not exists reviews_spend numeric not null default 0;
alter table if exists public.daily_sku_kpi
    add column if not exists ads_reviews_share_of_orders numeric not null default 0;

comment on column public.daily_sku_kpi.reviews_spend is
    'Отзывы Ozon за день по SKU: типы 95 (у товара) + 96 (по правилу профиля), с НДС, положительно; в ad_spend не входит (26.10)';
comment on column public.daily_sku_kpi.ads_reviews_share_of_orders is
    'ДРР (реклама + отзывы) = (ad_spend + reviews_spend) / orders_amount_seller; ad_share_of_orders — только реклама (26.10)';

alter table if exists public.daily_marketplace_kpi
    add column if not exists reviews_spend numeric not null default 0;
alter table if exists public.daily_marketplace_kpi
    add column if not exists ads_reviews_share_of_orders numeric not null default 0;

comment on column public.daily_marketplace_kpi.reviews_spend is
    'Отзывы Ozon за день: Σ reviews_spend витрины SKU (типы 95 + 96), с НДС (26.10)';
comment on column public.daily_marketplace_kpi.ads_reviews_share_of_orders is
    'ДРР (реклама + отзывы) = (ad_spend + reviews_spend) / orders_amount_seller (26.10)';

-- Участники услуги «Ускоренный сбор отзывов» по дате снимка: ночной шаг снимает promotions[type = REVIEWS_PROMO] всех SKU каталога
-- (1 обращение на 1 000 SKU); история до первой ночи — снимки каталога 21–22.10 (посев по слову).
create table if not exists public.ozon_reviews_promo_daily (
    observed_date   date        not null,
    sku             bigint      not null,
    offer_id        text,
    is_enabled      boolean     not null,
    observed_at     timestamptz not null default now(),
    primary key (observed_date, sku)
);

comment on table public.ozon_reviews_promo_daily is
    'Ozon: подключён ли SKU к «Ускоренному сбору отзывов» (promotions REVIEWS_PROMO.is_enabled) на дату снимка — база разнесения типа 96 (26.10)';

alter table public.ozon_reviews_promo_daily enable row level security;
grant select, insert, update, delete on public.ozon_reviews_promo_daily to service_role;
