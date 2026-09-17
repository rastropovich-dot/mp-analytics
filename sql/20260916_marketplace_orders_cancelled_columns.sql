-- Отменённые заказы рядом с подтверждёнными, тот же ключ (order_date, sku, schema).
--
-- Зачем: «созданные» и «подтверждённые» — фильтр при чтении, а не два правила
-- записи (решение владельца, 2026-09-16). Старые колонки orders_* сохраняют
-- смысл «подтверждённые», читатели не меняются; сумма пар — «созданные».
-- Правило записи одно на FBO и FBS: loaders/ozon_orders_rows.py.
--
-- Дозревание отмен: ночной сбор строит окно заново и upsert переписывает обе
-- пары у каждого ключа окна, поэтому отмена, пришедшая через две недели,
-- перекладывает суммы из orders_* в cancelled_* той же строкой. observed_at —
-- момент сбора, подтвердившего строку; строки старше последнего полного сбора
-- внутри его окна — те, которых сбор не вернул (устаревшие ключи FBO).
--
-- НЕ применять до слова владельца. Порядок: миграция → мерж ветки
-- orders-cancelled-columns → первая ночь. Загрузчики на ветке пишут новые
-- колонки и без миграции упадут на первом upsert (громко, не молча).
-- Применять руками в SQL-редакторе Supabase или через MCP.

alter table public.marketplace_orders
    add column if not exists cancelled_orders_qty            numeric      not null default 0,
    add column if not exists cancelled_orders_amount_buyer   numeric      not null default 0,
    add column if not exists cancelled_orders_amount_seller  numeric      not null default 0,
    add column if not exists observed_at                     timestamptz;

alter table public.marketplace_orders
    drop constraint if exists marketplace_orders_cancelled_nonneg,
    add constraint marketplace_orders_cancelled_nonneg
        check (cancelled_orders_qty >= 0
               and cancelled_orders_amount_buyer >= 0
               and cancelled_orders_amount_seller >= 0);

comment on column public.marketplace_orders.orders_qty is
    'Подтверждённые (не отменённые) заказы, штук; созданные = orders_qty + cancelled_orders_qty';
comment on column public.marketplace_orders.cancelled_orders_qty is
    'Отменённые заказы той же когорты (order_date — дата заказа, не отмены), штук';
comment on column public.marketplace_orders.cancelled_orders_amount_buyer is
    'Сумма отменённых по цене продавца, как orders_amount_buyer';
comment on column public.marketplace_orders.cancelled_orders_amount_seller is
    'Сумма отменённых по цене продавца, как orders_amount_seller';
comment on column public.marketplace_orders.observed_at is
    'Момент сбора Ozon, который последний раз подтвердил строку; null у WB и у строк до 2026-09-16';

-- Устаревшие ключи FBO (1 108 на 15 778 719,00 за 08-17…09-14, измерено 09-15)
-- миграция не трогает. Предложение — docs/ozon_orders_one_rule.md §4.
