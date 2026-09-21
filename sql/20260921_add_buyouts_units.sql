-- Штуки выкупов рядом с позициями.
--
-- marketplace_buyouts.buyouts_qty — ПОЗИЦИИ: товарные строки начислений by-day
-- (у старого API до 2026-08-17 в этой колонке лежали штуки — ряд смешанный).
-- Штук в by-day нет, но они есть в /v1/finance/accrual/postings: строка типа 69
-- SaleCommission несёт seller_price и quantity, и seller_price × quantity =
-- sale_amount построчно со знаком (проверено 2026-09-16: 46 510 строк, 0
-- промахов; штуки нетто = реализации Ozon по каждому SKU на 14 датах).
--
-- null = «не измерено», НЕ ноль: строка выкупа, по которой штуки не сведены,
-- остаётся null, и читатель (себестоимость) берёт для неё позиции и говорит об
-- этом. 0 — измеренный ноль (продажа и возврат в один день).
--
-- Знак — как у buyouts_qty: продажа +, возврат −.
--
-- Пишет нефатальный шаг scripts/ozon_buyout_units_step.py (только эту колонку,
-- только у существующих строк); посев истории — scripts/seed_buyout_units.py.
-- Ночной загрузчик выкупов колонку не трогает: её нет в его строках, upsert
-- обновляет только присланные колонки.
--
-- Применять руками (SQL-редактор / MCP) по слову владельца.

alter table public.marketplace_buyouts
    add column if not exists buyouts_units integer;

comment on column public.marketplace_buyouts.buyouts_units is
    'Штуки нетто за день по SKU из accrual/postings (тип 69, quantity со знаком). null — не измерено; buyouts_qty — позиции.';
