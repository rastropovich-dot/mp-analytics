-- WB-12 §3: «последняя карточка по nmId» — таблица, которую пишет сам шаг воронки (одна запись в ночи), вместо чтения
-- вьюхи wb_funnel_products_latest поверх ~500 тыс. строк wb_funnel_products_daily. Замер 2026-09-28: холодная вьюха не
-- укладывается в statement_timeout PostgREST 8 с — первая страница 57014 через 10,4 с (explain: index scan 79 874 записей
-- и 81 150 буферов ради 1 000 nmId), тёплая 0,4–0,5 с; в сборках книги это уже ловил откат на таблицу окном (WB-10 §3.1).
-- Таблица — ~5 200 строк, первичный ключ nm_id: чтение по ключу мгновенное и от кэша не зависит.
-- Посев — из вьюхи (тот же distinct on). Дальше пишет loaders/wb_sales_funnel_orders_loader.py: в конце окна upsert по
-- nm_id последней карточки каждого nmId за окно (окно кончается вчера, поэтому «последняя за окно» = последняя вообще).
-- Читатель — report_finrez_wb.read_latest_cards: таблица → вьюха (откат) → таблица воронки окном (второй откат).
-- Применяется по слову владельца через MCP. Обратимо: drop table.
create table if not exists public.wb_funnel_products_current (
  nm_id        bigint primary key,
  day          date not null,
  vendor_code  text,
  title        text,
  brand        text,
  subject_id   integer,
  subject_name text,
  observed_at  timestamptz
);

comment on table public.wb_funnel_products_current is
  'WB: последняя по дню карточка воронки на nmId (vendor_code, title, brand, subject) — словарь товаров для книги «Фин рез»; пишет шаг воронки в конце окна; WB-12 §3';

insert into public.wb_funnel_products_current (nm_id, day, vendor_code, title, brand, subject_id, subject_name, observed_at)
select nm_id, day, vendor_code, title, brand, subject_id, subject_name, observed_at
from public.wb_funnel_products_latest
on conflict (nm_id) do update
  set day = excluded.day, vendor_code = excluded.vendor_code, title = excluded.title, brand = excluded.brand,
      subject_id = excluded.subject_id, subject_name = excluded.subject_name, observed_at = excluded.observed_at
  where excluded.day >= public.wb_funnel_products_current.day;
