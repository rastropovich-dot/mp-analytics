-- Снимок обоих окон квоты в момент 429 daily_quota_exhausted.
--
-- Эксперимент "календарное окно или скользящее" осмыслен только если обе суммы
-- сняты в одну секунду, а 429 приходит около 05:50 UTC — руками туда не успеть.
-- Замер снимается автоматически и ложится отдельной строкой ledger
-- с request_kind = 'quota_snapshot' и request_count = 0: она не участвует
-- ни в расходе (sum(request_count)), ни в бюджете units.
--
-- Применено через Supabase MCP 2026-08-31.

ALTER TABLE ozon_performance_statistics_json_usage
    ADD COLUMN IF NOT EXISTS quota_window_rolling_24h int,
    ADD COLUMN IF NOT EXISTS quota_window_since_utc_midnight int;

-- Точные суммы одним round-trip. Считать выборкой строк нельзя:
-- PostgREST режет ответ на 1000 строк, и расход молча занизился бы.
CREATE OR REPLACE FUNCTION ozon_statistics_json_usage_quota_windows(
    p_account_signature text DEFAULT NULL
)
RETURNS TABLE (
    rolling_24h bigint,
    since_utc_midnight bigint,
    utc_day_started_at timestamptz,
    next_utc_reset_at timestamptz
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        coalesce(sum(request_count) FILTER (
            WHERE event_at >= now() - interval '24 hours'
        ), 0)::bigint,
        coalesce(sum(request_count) FILTER (
            WHERE event_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'
        ), 0)::bigint,
        (date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'),
        ((date_trunc('day', now() AT TIME ZONE 'utc') + interval '1 day') AT TIME ZONE 'utc')
    FROM ozon_performance_statistics_json_usage
    WHERE request_kind IN ('submit', 'poll', 'download')
      -- оба окна лежат внутри 24 часов; отсечка по 48 часам держит скан коротким
      AND event_at >= now() - interval '48 hours'
      AND (p_account_signature IS NULL OR account_signature = p_account_signature);
$$;
