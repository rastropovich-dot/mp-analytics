-- Ledger писал только submit POST /api/client/statistics/json.
-- Опросы GET /api/client/statistics/{uuid} и скачивания GET /api/client/statistics/report
-- не учитывались вовсе, поэтому расход против лимита 2000 запросов был не виден.
--
-- request_kind  — тип запроса: submit | poll | download.
-- request_count — сколько РЕАЛЬНЫХ HTTP-запросов представляет строка.
--                 Опросы агрегируются: одна строка на wait_statistics, а не 300+ записей
--                 в БД внутри цикла ожидания. Поэтому строка больше не равна одному запросу,
--                 и суммарный расход считается через sum(request_count), а не через count(*).
--
-- Применено через Supabase MCP 2026-08-31 (8336 существующих строк размечены как submit).

ALTER TABLE ozon_performance_statistics_json_usage
    ADD COLUMN IF NOT EXISTS request_kind text,
    ADD COLUMN IF NOT EXISTS request_count int NOT NULL DEFAULT 1;

UPDATE ozon_performance_statistics_json_usage
SET request_kind = 'submit'
WHERE request_kind IS NULL;

ALTER TABLE ozon_performance_statistics_json_usage
    ALTER COLUMN request_kind SET DEFAULT 'submit';

CREATE INDEX IF NOT EXISTS idx_ozon_stats_json_usage_event_at
    ON ozon_performance_statistics_json_usage (event_at DESC);

CREATE INDEX IF NOT EXISTS idx_ozon_stats_json_usage_account_event_at
    ON ozon_performance_statistics_json_usage (account_signature, event_at DESC);
