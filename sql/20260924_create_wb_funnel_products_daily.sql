-- Воронка продаж Wildberries по товарам и дням: одна строка на (день, nmId).
--
-- Зачем: лист «Заказы WB» владельца (WB-6 §2, WB-7 §3) считает выручку по товару и
-- площадке (буква артикула: `t` — «WB Дискаунтер», остальное — Standard) от
-- orderSum воронки продаж (× 0,58 / 1,22), а воронка пересматривает orderSum
-- задним числом (09-14: −16 % за 9 дней). Прежде хранилась только сводка дня в
-- marketplace_orders_analytics (она остаётся — из тех же ответов, одним проходом).
--
-- Источник: POST seller-analytics-api /api/analytics/v3/sales-funnel/products,
-- selectedPeriod день…день, timezone Europe/Moscow, все карточки продавца
-- (spec/wb/11-analytics.yaml). Выкупы, отмены и возвраты воронка относит к дню
-- ЗАКАЗА (спека) — buyout_* здесь не выкупы по дате продажи (те — в
-- marketplace_buyouts из отчёта реализации).
--
-- Числа — как в ответе (numeric без точности: суммы у WB целые рубли, рейтинги —
-- десятичные). Нет в ответе — null. Строки за день переписываются каждую ночь окном
-- 14 дней; карточка, исчезнувшая из полного ответа за день, снимается по правилу
-- loaders/stale_keys.py.
--
-- Применять руками (SQL-редактор / MCP) по слову владельца. Ночная запись —
-- loaders/wb_sales_funnel_orders_loader.py; история 09-01 … —
-- scripts/wb_funnel_products_backfill.py (только по слову, после --plan).

create table if not exists public.wb_funnel_products_daily (
    day               date          not null,             -- день заказа (Europe/Moscow)
    nm_id             bigint        not null,             -- nmId — артикул WB
    vendor_code       text,                               -- vendorCode — наш артикул; первая буква = площадка
    title             text,                               -- title — название карточки
    brand             text,                               -- brandName (KARATOV / КОЮЗ Топаз)
    subject_id        integer,                            -- subjectId
    subject_name      text,                               -- subjectName — предмет
    product_rating    numeric,                            -- productRating
    feedback_rating   numeric,                            -- feedbackRating
    stocks_wb         integer,                            -- stocks.wb — остаток на складах WB на момент ответа
    stocks_mp         integer,                            -- stocks.mp — остаток на складах продавца
    balance_sum       numeric,                            -- stocks.balanceSum — стоимость остатка
    open_count        integer,                            -- openCount — переходы в карточку
    cart_count        integer,                            -- cartCount — положили в корзину
    order_count       integer,                            -- orderCount — заказали, шт
    order_sum         numeric,                            -- orderSum — заказали на сумму (лист владельца: × 0,58 / 1,22)
    buyout_count      integer,                            -- buyoutCount — выкупили, шт (по дню заказа)
    buyout_sum        numeric,                            -- buyoutSum — выкупили на сумму (по дню заказа)
    cancel_count      integer,                            -- cancelCount — отменили и вернули, шт
    cancel_sum        numeric,                            -- cancelSum — отменили и вернули на сумму
    avg_price         numeric,                            -- avgPrice — средняя цена
    add_to_wishlist   integer,                            -- addToWishlist
    observed_at       timestamptz   not null,             -- момент сбора, подтвердившего строку
    loaded_at         timestamptz   not null default now(),
    primary key (day, nm_id)
);

comment on table public.wb_funnel_products_daily is
    'Воронка продаж WB по товарам и дням (sales-funnel/products, день заказа, МСК). Сводка дня — marketplace_orders_analytics (source wb_sales_funnel). Лист «Заказы WB»: docs/wb_report_model.md §8, §11.';

create index if not exists idx_wb_funnel_products_daily_day
    on public.wb_funnel_products_daily (day);

create index if not exists idx_wb_funnel_products_daily_vendor
    on public.wb_funnel_products_daily (vendor_code);
