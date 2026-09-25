-- WB-10 §3.1 (после слова 09-25): индекс под вьюху wb_funnel_products_latest.
-- Вьюха `select distinct on (nm_id) … order by nm_id, day desc` без такого индекса сортирует все ~492 тыс. строк
-- на каждый запрос и через PostgREST (statement_timeout 8 с) отдаёт 57014 — модуль книги откатывается на чтение
-- таблицы окном (252 … 301 с). С индексом (nm_id, day desc) сервер идёт по индексу в нужном порядке и берёт
-- первую строку каждого nmId без сортировки; ORDER BY nm_id LIMIT 1000 останавливается после 1 000 nmId.
-- Обратимо (drop index). Применяется по слову владельца через MCP.
create index if not exists wb_funnel_products_daily_nm_day_desc_idx
    on public.wb_funnel_products_daily (nm_id, day desc);
