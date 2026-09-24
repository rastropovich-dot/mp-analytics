-- Реклама Wildberries по номенклатурам: статистика кампаний за день по артикулу WB и площадке показа.
--
-- Зачем: wb_ad_spend_daily хранит списания по кампаниям (adv/v1/upd), а листу «Данные WB выкупы»
-- книги «Фин рез» нужна реклама по артикулу (WB-8 §2–§3). Источник — GET advert-api /adv/v3/fullstats
-- (spec/wb/08-promotion.yaml): ids ≤ 50 кампаний, период ≤ 31 день, 3 запроса/мин; ответ —
-- кампания → days[] → apps[] (appType) → nms[] (nmId, sum, views, clicks, orders, …).
--
-- Зерно — как в ответе: (день, кампания, площадка показа appType, nmId). Суммы по дню и артикулу —
-- дело читателя (Σ по app_type). Проба 2026-07-01 … 07-07 (78 кампаний июля, 2 обращения): Σ nms.sum
-- по дню = Σ days.sum до копейки, против updSum того же дня — +1,6 … 1,8 % (не объяснено, WB-8 §3).
--
-- Числа — как в ответе (numeric без точности); observed_at — момент сбора. Строки окна, которых в свежем
-- полном ответе нет, снимает loaders/stale_keys.py (только при полном сборе).
--
-- Применять руками (MCP) по слову владельца. Ночная запись — loaders/wb_ads_nm_loader.py (внутри шага
-- «WB: реклама»); история февраль–август — scripts/wb_ads_nm_backfill.py (только по слову, после --plan).

create table if not exists public.wb_ad_spend_nm_daily (
    day               date          not null,             -- days[].date
    advert_id         bigint        not null,             -- advertId
    app_type          integer       not null,             -- apps[].appType (1 — сайт, 32 — Android, 64 — iOS; спека)
    nm_id             bigint        not null,             -- nms[].nmId
    nm_name           text,                               -- nms[].name
    views             integer,                            -- показы
    clicks            integer,                            -- клики
    ctr               numeric,
    cpc               numeric,
    sum               numeric,                            -- затраты, ₽ (с НДС, как updSum)
    atbs              integer,                            -- добавления в корзину
    orders            integer,                            -- заказы
    cr                numeric,
    shks              integer,                            -- заказано товаров, шт
    sum_price         numeric,                            -- заказов на сумму, ₽
    canceled          integer,
    observed_at       timestamptz   not null,
    loaded_at         timestamptz   not null default now(),
    primary key (day, advert_id, app_type, nm_id)
);

comment on table public.wb_ad_spend_nm_daily is
    'Реклама WB по номенклатурам (adv/v3/fullstats): день × кампания × площадка показа × nmId; sum — затраты с НДС. Списания по кампаниям — wb_ad_spend_daily. WB-8 §3.';

create index if not exists idx_wb_ad_spend_nm_daily_day_nm
    on public.wb_ad_spend_nm_daily (day, nm_id);

create index if not exists idx_wb_ad_spend_nm_daily_advert
    on public.wb_ad_spend_nm_daily (advert_id, day);
