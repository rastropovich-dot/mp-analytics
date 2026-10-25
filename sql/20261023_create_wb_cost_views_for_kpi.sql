-- Двадцатая WB §1 (д) п. 3, слово владельца 2026-10-23 (развилка В): реклама и хранение WB по артикулам — в витрину daily_sku_kpi.
-- Витрина пересобирается целиком каждую ночь и читает источники страницами по 1 000 строк через PostgREST (агрегаты на базе
-- выключены — PGRST123). Сырые таблицы для неё велики: wb_paid_storage_daily — 1 220 060 строк (1 221 страница, 474 с в сборке
-- книги WB-18), wb_ad_spend_nm_daily — 361 222 (362 страницы). Две обычные вьюхи-свёртки отдают то же в зерне витрины:
--   * wb_paid_storage_nm_daily       — (день, nmId): Σ warehouse_price, Σ barcodes_count, артикул продавца — 226 083 строки, 227 страниц;
--   * wb_ad_spend_nm_campaign_daily  — (день, кампания, nmId) с sum > 0 — зерно, которое нужно правилу книги (доля «Баланса»
--                                       кампании в день × доля номенклатуры); 94 068 строк, 95 страниц; campaign_nm_key — один
--                                       bigint на пару (кампания, nmId) для постраничного чтения по (day, id) loaders/keyset.py
--                                       (advert_id ≤ 39 161 827, nm_id ≤ 1 207 756 342 < 10^10 — замер 2026-10-23).
-- Чтение по (day, id) с LIMIT останавливается рано: GroupAggregate поверх Index Scan по day (EXPLAIN 2026-10-23: 8 800 строк, 357 мс
-- на первую страницу диапазона 04-28 … 10-23). Обычные вьюхи, не материализованные: таблицы под ними пишутся каждую ночь.
-- Частичный индекс — для строк отчёта реализации, из которых витрина берёт «прочее» по артикулу (штрафы с nmId, доплаты, «Списание
-- за отзыв … товар <nmId>»): таких строк ~3 400 из 332 219, без индекса первая страница — 4,9 с с холодного кэша (потолок 8 с).
-- Применяется только через scripts/db_migrate.py (план → --apply по слову → --cabinet all), до мержа ветки (guard ночи).

create or replace view public.wb_paid_storage_nm_daily as
select day,
       nm_id,
       sum(warehouse_price)  as warehouse_price,    -- ₽ с НДС, сторно со знаком минус (как в таблице)
       sum(barcodes_count)   as barcodes_count,
       sum(rows_count)       as rows_count,
       count(*)              as keys_count,         -- сколько ключей таблицы (размер × склад × поставка × способ × баркод) свёрнуто
       max(vendor_code)      as vendor_code         -- артикул продавца: у одного nmId один артикул (ключ WB в 1С — без размера)
from public.wb_paid_storage_daily
group by day, nm_id;

comment on view public.wb_paid_storage_nm_daily is
  'WB платное хранение по (день, nmId) — свёртка wb_paid_storage_daily для витрины daily_sku_kpi.storage_cost (20-я WB); Σ дня = paid_storage отчёта реализации';

create or replace view public.wb_ad_spend_nm_campaign_daily as
select day,
       advert_id,
       nm_id,
       (advert_id::numeric * 10000000000 + nm_id)::bigint as campaign_nm_key,   -- один id на (кампания, nmId) для keyset по (day, id)
       sum(sum)    as sum,                           -- затраты по статистике, с НДС (как updSum)
       sum(views)  as views,
       sum(clicks) as clicks,
       sum(orders) as orders
from public.wb_ad_spend_nm_daily
where sum > 0
group by day, advert_id, nm_id;

comment on view public.wb_ad_spend_nm_campaign_daily is
  'WB реклама по (день, кампания, nmId) с sum > 0 — свёртка wb_ad_spend_nm_daily по площадкам показа для разнесения списаний «Баланс» по артикулам в витрине (20-я WB)';

create index if not exists idx_wb_sales_report_rows_sku_other
    on public.wb_sales_report_rows (rr_date, rrd_id)
    where penalty <> 0 or additional_payment <> 0 or bonus_type_name like 'Списание за отзыв%';

comment on index public.idx_wb_sales_report_rows_sku_other is
  'строки отчёта реализации, дающие «прочее» по артикулу в витрине: штрафы, доплаты, списания за отзыв (20-я WB)';
