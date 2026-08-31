-- Расход запросов к Ozon statistics против лимита 2000.
--
-- Строка ledger больше не равна одному запросу: опросы агрегируются одной строкой
-- на wait_statistics. Поэтому расход считается через sum(request_count),
-- а count(*) показан отдельно как число записей.
--
-- Фильтр по семейству statistics: submit + poll + download.
-- token и campaign_list в лимит не входят и в ledger не пишутся;
-- CPO (all_sku_promo) в лимит тоже не входит.
--
-- Ниже ДВА окна, и держать их надо рядом. Запущенные в момент 429 они отвечают
-- на два разных вопроса:
--
--   * Если календарное окно (от полуночи UTC) близко к 2000, а скользящее —
--     заметно больше, значит квота календарная и сбросится в 00:00 UTC.
--   * Если скользящее окно близко к 2000, а календарное сильно меньше, значит
--     окно скользящее и ждать полуночи бесполезно.
--   * Если ОБА заметно ниже 2000, а 429 всё равно прилетает — лимит выбирают не
--     мы: в кабинете есть посторонний потребитель, которого ledger не видит.
--
-- Смысл именно в паре чисел; по одному окну эти три случая неразличимы.


-- ── Окно 1: скользящие 24 часа ────────────────────────────────────────────────
with window_usage as (
    select request_kind, request_count
    from ozon_performance_statistics_json_usage
    where event_at >= now() - interval '24 hours'
      and request_kind in ('submit', 'poll', 'download')
      -- and account_signature = :account_signature
),
by_kind as (
    select request_kind,
           sum(request_count) as requests,
           count(*)           as ledger_rows
    from window_usage
    group by request_kind
)
select 'rolling_24h'                                                as window,
       request_kind,
       requests,
       ledger_rows,
       round(100.0 * requests / nullif(sum(requests) over (), 0), 1) as pct_of_total,
       sum(requests) over ()                                        as requests_total,
       2000 - sum(requests) over ()                                 as headroom_vs_2000
from by_kind
order by requests desc;


-- ── Окно 2: от полуночи UTC (календарное окно квоты) ──────────────────────────
with window_usage as (
    select request_kind, request_count
    from ozon_performance_statistics_json_usage
    where event_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc'
      and request_kind in ('submit', 'poll', 'download')
      -- and account_signature = :account_signature
),
by_kind as (
    select request_kind,
           sum(request_count) as requests,
           count(*)           as ledger_rows
    from window_usage
    group by request_kind
)
select 'since_utc_midnight'                                         as window,
       request_kind,
       requests,
       ledger_rows,
       round(100.0 * requests / nullif(sum(requests) over (), 0), 1) as pct_of_total,
       sum(requests) over ()                                        as requests_total,
       2000 - sum(requests) over ()                                 as headroom_vs_2000
from by_kind
order by requests desc;


-- ── Оба окна одной строкой: то, что нужно смотреть в момент 429 ───────────────
select
    (select coalesce(sum(request_count), 0)
     from ozon_performance_statistics_json_usage
     where event_at >= now() - interval '24 hours'
       and request_kind in ('submit', 'poll', 'download'))              as rolling_24h,
    (select coalesce(sum(request_count), 0)
     from ozon_performance_statistics_json_usage
     where event_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc'
       and request_kind in ('submit', 'poll', 'download'))              as since_utc_midnight,
    2000                                                                as limit_hint,
    date_trunc('day', now() at time zone 'utc') at time zone 'utc'      as utc_day_started_at,
    (date_trunc('day', now() at time zone 'utc') + interval '1 day')
        at time zone 'utc'                                              as next_utc_reset_at;


-- ── Утреннее чтение: чем всё кончилось в момент 429 ───────────────────────────
-- Снимок снимается автоматически при 429 daily_quota_exhausted, руками к 05:50
-- UTC успевать не нужно. Обе суммы в строке сняты в одну секунду.
--
--   rolling_24h у 2000, since_utc_midnight заметно меньше  -> окно скользящее
--   since_utc_midnight у 2000, rolling_24h больше          -> окно календарное
--   оба заметно ниже 2000                                  -> посторонний потребитель
--
-- select event_at,
--        quota_window_rolling_24h        as rolling_24h,
--        quota_window_since_utc_midnight as since_utc_midnight,
--        mode, load_date, account_signature
-- from ozon_performance_statistics_json_usage
-- where request_kind = 'quota_snapshot'
-- order by event_at desc
-- limit 20;


-- 429, прилетевшие именно на опросе (раньше были полностью невидимы):
--
-- select date_trunc('hour', event_at) as hour, response_kind,
--        sum(request_count) as requests
-- from ozon_performance_statistics_json_usage
-- where event_at >= now() - interval '24 hours'
--   and request_kind = 'poll'
--   and response_kind in ('poll_429', 'poll_daily_quota_exhausted')
-- group by 1, 2
-- order by 1 desc;
