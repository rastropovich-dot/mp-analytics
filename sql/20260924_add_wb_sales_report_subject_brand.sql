-- Предмет и бренд в строках отчёта реализации WB (WB-8 §2, решение советника 2026-09-24).
--
-- Зачем: категория листа «Выкупы WB» книги «Фин рез» строится по предмету карточки; отчёт реализации
-- несёт subjectName и brandName в каждой строке (проверено по сырью 09-24), а таблица их не хранила —
-- категория зависела от карточек воронки (покрытие 1 614 из 1 636 nmId). Теперь — из самой строки,
-- воронка остаётся откатом.
--
-- Посев истории 02-01 … 09-22 — scripts/seed_wb_sales_report_subjects.py из сырья на диске
-- (data/wb_sales_report_raw/); ночной загрузчик (loaders/wb_sales_report_loader.py) пишет обе колонки
-- с первой ночи после мержа, окно 21 день доберёт строки с 09-23. Применять через MCP по слову.

alter table public.wb_sales_report_rows
    add column if not exists subject_name text,   -- subjectName — предмет карточки («Ювелирные кольца» …); пусто у операций без товара
    add column if not exists brand_name   text;   -- brandName — бренд карточки (KARATOV / КОЮЗ Топаз)

comment on column public.wb_sales_report_rows.subject_name is 'subjectName отчёта реализации — предмет карточки; категория листа «Выкупы WB»';
comment on column public.wb_sales_report_rows.brand_name is 'brandName отчёта реализации — бренд карточки';
