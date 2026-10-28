-- Пятьдесят девятая, органика шаги 1–2 (слово владельца 2026-10-28). Применяется только через scripts/db_migrate.py
-- (план → слово → KARATOV с --allow-karatov → --cabinet all), ДО мержа ветки: guard cabinet.assert_schema остановит ночь,
-- если файл в main, а строки версии в базе нет.
--
-- 1) Строки отчёта ОЗЗ «Все» (all_sku_promo/orders) с номером и датой создания заказа. Отчёт датирован днём СПИСАНИЯ
--    (разведка 28.10: за 23.10 133 строки из 252 — заказы, созданные раньше); органика берёт ОЗЗ отсюда по дню создания,
--    расходы и атрибуция остаются по дню списания. Пишет loaders/ozon_cpo_order_lines.py (ночной шаг рекламы).
create table if not exists public.ozon_cpo_order_lines (
    charge_date          date        not null,            -- «Дата» отчёта: день списания
    order_number         text        not null,            -- «Номер заказа»; номер отправления = он + «-N»
    marketplace_sku      text        not null,            -- «SKU» заказанного товара (order_sku атрибуции)
    promoted_sku         text        not null default '', -- «SKU продвигаемого товара»
    order_id             text,                            -- «ID заказа»
    article              text,
    qty                  numeric     not null default 0,  -- «Количество»
    sale_amount          numeric     not null default 0,  -- «Стоимость продажи, ₽» — то же поле, что ad_orders_revenue атрибуции
    rate_percent         numeric,                         -- «Ставка, %»
    spend                numeric     not null default 0,  -- «Расход, ₽»
    order_created_date   date,                            -- день создания заказа по МСК из ozon_posting_status_log; null — не нашли
    created_date_source  text,                            -- 'status_log' | null (история — 'postings_raw', если понадобится)
    loaded_at            timestamptz not null default now(),
    primary key (charge_date, order_number, marketplace_sku, promoted_sku)
);

comment on table public.ozon_cpo_order_lines is
    'Строки отчёта ОЗЗ «Все» с номером и датой создания заказа (59-я): органика ставит ОЗЗ на день создания, расходы — на день списания';

create index if not exists idx_ozon_cpo_order_lines_created
    on public.ozon_cpo_order_lines (order_created_date, marketplace_sku);

grant select, insert, update, delete on public.ozon_cpo_order_lines to service_role;

-- 2) Органика без молчаливого нуля: organic_* = всего − реклама как есть (может быть < 0), флаг — реклама больше всех заказов.
alter table if exists public.ozon_daily_sku_organic
    add column if not exists ad_exceeds_total boolean not null default false;

comment on column public.ozon_daily_sku_organic.ad_exceeds_total is
    'Рекламных заказов больше, чем всех заказов SKU за день (шт): органика отрицательная, не обрезана (59-я)';
