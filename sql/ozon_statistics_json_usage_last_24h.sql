-- Расход запросов к Ozon statistics за скользящие 24 часа, против лимита 2000.
--
-- Строка ledger больше не равна одному запросу: опросы агрегируются одной строкой
-- на wait_statistics. Поэтому расход считается через sum(request_count),
-- а count(*) показан отдельно как число записей.
--
-- Фильтр по семейству statistics: submit + poll + download.
-- token и campaign_list в лимит не входят и в ledger не пишутся;
-- CPO (all_sku_promo) в лимит тоже не входит.

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
select request_kind,
       requests,
       ledger_rows,
       round(100.0 * requests / nullif(sum(requests) over (), 0), 1) as pct_of_total,
       sum(requests) over ()                                        as requests_total,
       2000 - sum(requests) over ()                                 as headroom_vs_2000
from by_kind
order by requests desc;


-- Только суммарный расход, одной строкой:
--
-- select coalesce(sum(request_count), 0) as requests_last_24h
-- from ozon_performance_statistics_json_usage
-- where event_at >= now() - interval '24 hours'
--   and request_kind in ('submit', 'poll', 'download');


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
