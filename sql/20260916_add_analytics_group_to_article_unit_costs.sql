-- «Аналитическая группа» из файла 1С не грузилась в article_unit_costs
-- (замечено 2026-09-15, docs/reports_model.md §6). Колонка для среза по категории;
-- заполнится при следующем снимке 1С: scripts/load_article_unit_costs.py --analytics-group.
-- Применять по слову владельца.
alter table public.article_unit_costs add column if not exists analytics_group text;
comment on column public.article_unit_costs.analytics_group is '«Аналитическая группа» из файла 1С; null у снимков, загруженных до 2026-09-16';
