-- Пятьдесят третья §2 (решение советника 2026-10-21): трата бонусных кампаний Ozon (автосозданных, isAutocreated = true в списке
-- кампаний Performance) — не расход продавца: в marketplace_expenses не пишется, в ozon_daily_sku_ad_attribution помечается флагом.
-- Читатели (утренний лист, алерт, книга «Фин рез») берут строку «реклама за счёт бонусов Ozon (не расход)» по is_bonus.
-- Применяется только через scripts/db_migrate.py (план → слово → KARATOV с --allow-karatov → --cabinet all).
alter table if exists public.ozon_daily_sku_ad_attribution
    add column if not exists is_bonus boolean not null default false;

comment on column public.ozon_daily_sku_ad_attribution.is_bonus is
    'true — строка бонусной (автосозданной Ozon) кампании: трата бонусов продвижения, не деньги продавца; в расходы и ДРР не входит (53-я)';

create index if not exists idx_ozon_daily_sku_ad_attribution_is_bonus
    on public.ozon_daily_sku_ad_attribution (sale_date)
    where is_bonus;
