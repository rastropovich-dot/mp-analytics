-- WB-10 §3.1: «последняя карточка по nmId» — обычная (не материализованная) вьюха над wb_funnel_products_daily.
-- Зачем: словарь товаров для книги «Фин рез» (report_finrez_wb.load_product_dictionary) читает ради «Наименования»
-- всё окно книги — 492 128 строк за 493 страницы по 1 000 (301 с, замер 2026-09-25); вьюха отдаёт одну строку на nmId
-- (5 202 строки, ~6 страниц). Обычная вьюха: воронка переписывается каждую ночь окном 14 дней, кэшировать нечего.
-- Применяется по слову владельца через MCP (apply_migration). Читатель — только модуль книги; при отсутствии вьюхи
-- модуль читает таблицу по-старому (откат в коде, PGRST205 / 42P01).
create or replace view public.wb_funnel_products_latest as
select distinct on (nm_id)
       nm_id, day, vendor_code, title, brand, subject_id, subject_name, observed_at
from public.wb_funnel_products_daily
order by nm_id, day desc;

comment on view public.wb_funnel_products_latest is
  'WB: последняя по дню карточка воронки на nmId (vendor_code, title, brand, subject) — словарь товаров для книги «Фин рез»; WB-10 §3.1';
