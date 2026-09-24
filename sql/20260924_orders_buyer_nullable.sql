-- Тридцать шестая задача, §3.1: цена покупателя в заказах Ozon — «не измерено» должно быть null, а не нулём и не ценой продавца.
--
-- В marketplace_orders колонка cancelled_orders_amount_buyer создана NOT NULL default 0 (проверено 2026-09-24 по
-- information_schema), orders_amount_buyer — nullable. Загрузчик с мержем coinvest-columns пишет Σ customer_price × кол-во
-- (FBS — из ночного /v4-списка, FBO — из отчёта ЛК /v1/report/postings/create) и null у ключа, где цены нет хоть у одного
-- товара (отчёт ЛК не готов за 3 минуты, отправление не попало в отчёт). Без этой миграции null в отменённых упирается в
-- NOT NULL; загрузчик тогда пишет строки без колонок покупателя и говорит об этом вслух (loaders/ozon_orders_rows.py,
-- save_rows_with_buyer_fallback) — заказы не теряются, но цена покупателя за ночь не записана.
--
-- Порядок: сначала эта миграция (по слову владельца, через MCP), потом мерж ветки coinvest-columns.
-- WB пишет обе колонки всегда (finishedPrice) — на WB миграция не влияет.

alter table public.marketplace_orders
    alter column cancelled_orders_amount_buyer drop not null,
    alter column cancelled_orders_amount_buyer drop default,
    alter column orders_amount_buyer drop default;

comment on column public.marketplace_orders.orders_amount_buyer is
    'Оплачено покупателем по подтверждённым, Σ цена покупателя × кол-во. Ozon с 2026-09-24: customer_price (FBS /v4) или «Оплачено покупателем» отчёта ЛК (FBO); null — не измерено; до — дубль orders_amount_seller. WB — finishedPrice';
comment on column public.marketplace_orders.cancelled_orders_amount_buyer is
    'Оплачено покупателем по отменённым, та же семантика, что у orders_amount_buyer; null — не измерено (Ozon с 2026-09-24)';
