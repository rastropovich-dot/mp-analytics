-- WB-10 §2: выкуп WB по когорте дня заказа — то же определение, что у коэффициента листа «Коэффициенты»
-- (report_finrez_wb.buyout_rate_for_finrez): числитель — продажи отчёта реализации (Продажа − Возврат) по дню orderDt МСК,
-- знаменатель — заказы воронки того дня (wb_funnel_products_daily: order_count / order_sum). Зрелый день — возраст
-- ≥ BUYOUT_RATE_MATURE_DAYS (25) суток: когорта дособрана на ≥ 99,9 % ₽ (WB-9 §3); незрелый — прогноз по зрелым дням
-- последних 30 суток, с пометкой. Старые поля витрин (daily_sku_kpi.buyouts_qty — по дню продажи, buyout_rate — выкупы
-- по продаже / созданные из marketplace_orders) читают алерт, отчёты 7/60 дней, Excel и слой решений как календарные
-- (WB-10 §2.1), поэтому когорта живёт в своих таблицах. Пишет только scripts/wb_buyout_cohort_step.py --apply.
-- Применяется по слову владельца через MCP (apply_migration).

create table if not exists public.wb_buyout_cohort_sku_daily (
    day                 date        not null,             -- день заказа (МСК, как в воронке)
    nm_id               bigint      not null,
    vendor_code         text,
    created_qty         integer     not null default 0,   -- orderCount воронки
    created_sum         numeric     not null default 0,   -- orderSum воронки, ₽ с НДС
    sold_qty            integer     not null default 0,   -- Продажа − Возврат отчёта по orderDt, штуки
    sold_sum            numeric     not null default 0,   -- то же в ₽ (retailPriceWithDisc)
    age_days            integer     not null,             -- на момент записи: сегодня − день
    mature              boolean     not null,             -- age_days ≥ 25
    rate_qty            numeric,                          -- sold_qty / created_qty (зрелый день — факт; незрелый — null)
    rate_sum            numeric,                          -- sold_sum / created_sum
    observed_at         timestamptz not null default now(),
    primary key (day, nm_id)
);
create index if not exists wb_buyout_cohort_sku_daily_day_idx on public.wb_buyout_cohort_sku_daily (day);
comment on table public.wb_buyout_cohort_sku_daily is
  'WB: выкуп по когорте дня заказа на (день, nmId): заказы воронки и продажи отчёта реализации по orderDt; WB-10 §2';

create table if not exists public.wb_buyout_cohort_daily (
    day                 date        not null primary key, -- день заказа
    created_qty         integer     not null default 0,
    created_sum         numeric     not null default 0,
    sold_qty            integer     not null default 0,
    sold_sum            numeric     not null default 0,
    age_days            integer     not null,
    mature              boolean     not null,
    rate_qty            numeric,                          -- факт по зрелому дню
    rate_sum            numeric,
    forecast_rate_qty   numeric,                          -- прогноз для незрелого дня: Σ sold / Σ created по зрелым дням последних 30 суток
    forecast_rate_sum   numeric,
    forecast_window     text,                             -- «YYYY-MM-DD … YYYY-MM-DD» зрелых дней прогноза
    observed_at         timestamptz not null default now()
);
comment on table public.wb_buyout_cohort_daily is
  'WB: выкуп по когорте дня заказа по дням (все nmId): факт по зрелым дням (возраст ≥ 25 сут.), прогноз по последним 30 зрелым дням для незрелых; WB-10 §2';
