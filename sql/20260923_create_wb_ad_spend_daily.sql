-- Реклама Wildberries: фактические списания advert-api GET /adv/v1/upd, одна строка на списание.
--
-- Зачем: реклама WB нигде не хранилась — adv/v1/upd звал только генератор листа при живом
-- запуске; лист владельца показывает июль 1 694 178 ₽ и август 835 968 ₽ (без НДС), а с
-- 18.08 реклама WB остановлена (сентябрь — ноль, и это правда, не дыра). История нужна для
-- листов прошлых месяцев и для ДРР. Ночной загрузчик — loaders/wb_ads_loader.py (окно 31
-- день до вчера, одно обращение); история с 2026-02-01 — scripts/wb_ads_backfill.py.
--
-- Зерно — списание (advertId, updTime): на сырье 02-01…09-22 (33 781 строка) пара уникальна,
-- а updNum («номер документа») — нет: 37 различных значений, 5 610 нулей. Имя таблицы —
-- по задаче (WB-6 §3а); суточные суммы берутся по upd_day (updTime в московском времени —
-- так Σ / 1,22 по месяцам сходится с листом владельца до рубля). upd_sum — с НДС, как отдаёт WB.
--
-- Применять руками (MCP) по слову владельца.

create table if not exists public.wb_ad_spend_daily (
    advert_id      bigint        not null,                 -- advertId — ID кампании
    upd_time       timestamptz   not null,                 -- updTime — время списания (WB отдаёт с +03:00)
    upd_day        date          not null,                 -- день списания по МСК — для сумм по дням
    upd_num        bigint,                                 -- updNum — «номер документа», не уникален, справочно
    upd_sum        numeric       not null,                 -- updSum — выставленная сумма, с НДС
    payment_type   text,                                   -- paymentType — Баланс / Бонусы / Счёт / Кэшбэк
    advert_type    integer,                                -- advertType — тип кампании (в сырье только 9)
    advert_status  integer,                                -- advertStatus — статус кампании на момент ответа
    camp_name      text,                                   -- campName
    currency       text,                                   -- currency
    observed_at    timestamptz   not null,                 -- момент сбора, подтвердившего строку
    loaded_at      timestamptz   not null default now(),
    primary key (advert_id, upd_time)
);

comment on table public.wb_ad_spend_daily is
    'Списания за рекламу WB (advert-api /adv/v1/upd), зерно (advertId, updTime); upd_day — день по МСК; суммы с НДС. Загрузчик loaders/wb_ads_loader.py.';

create index if not exists idx_wb_ad_spend_daily_day
    on public.wb_ad_spend_daily (upd_day);

create index if not exists idx_wb_ad_spend_daily_advert
    on public.wb_ad_spend_daily (advert_id, upd_day);
