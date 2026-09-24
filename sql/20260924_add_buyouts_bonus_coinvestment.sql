-- Тридцать шестая задача, §3.2: соинвест по выкупам — из того же ответа accrual/by-day, что пишет выкупы.
-- posting.products[].commission: seller_price (цена продавца за единицу), sale_price (оплачено покупателем — ЗА СТРОКУ,
-- не за единицу: проверено 2026-09-24 на 64 строках с количеством > 1), bonus (баллы за скидки), coinvestment (зелёные цены).
-- Тождество построчно: sale_amount − sale_price = bonus + coinvestment.
--
-- buyouts_amount_buyer уже есть: до этой задачи в неё писалась цена продавца (дубль buyouts_amount_seller);
-- с мержем ветки coinvest-columns — Σ sale_price. Две колонки ниже — новые; null = не измерено (строка записана до колонок).
-- Применяется владельцем / по слову владельца через MCP; загрузчик без колонок пишет строки без них и говорит об этом.

alter table public.marketplace_buyouts
    add column if not exists bonus_amount numeric null,
    add column if not exists coinvestment_amount numeric null;

comment on column public.marketplace_buyouts.bonus_amount is
    'Ozon: Σ commission.bonus (баллы за скидки) по строкам продаж/возвратов дня и SKU, знак Ozon как у sale_amount; null — не измерено';
comment on column public.marketplace_buyouts.coinvestment_amount is
    'Ozon: Σ commission.coinvestment (зелёные цены / соинвест Ozon) по строкам продаж/возвратов дня и SKU; null — не измерено';
comment on column public.marketplace_buyouts.buyouts_amount_buyer is
    'Ozon с 2026-09-24: Σ commission.sale_price (оплачено покупателем, за строку); до — дубль buyouts_amount_seller. WB — своя семантика';
