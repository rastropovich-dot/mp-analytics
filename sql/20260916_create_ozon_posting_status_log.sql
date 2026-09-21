-- Лог переходов статусов отправлений Ozon. Минимум для кривой дозревания.
--
-- Зачем: даты отмены у Ozon нет ни в одном ответе Seller API (проверено
-- 2026-09-15, docs/reports_model.md §3). Единственный способ узнать, КОГДА
-- отправление стало cancelled (или delivered), — наблюдать статус каждым сбором
-- и записывать изменения. Продольная кривая дозревания появится через ~30 дней
-- после включения; каждый день без лога не восстановить.
--
-- Что пишется: одна строка на каждое ЗАМЕЧЕННОЕ изменение статуса отправления.
-- Первое наблюдение отправления — тоже строка (previous_status = null). Если
-- статус не изменился с прошлого наблюдения — строки нет. Источник — те же
-- ответы /v3/posting/fbo/list и /v3/posting/fbs/list, что ночной сбор уже
-- получает; ни одного дополнительного обращения к API.
--
-- Точность даты отмены — сутки (сбор ночной): observed_at — момент сбора, не
-- момент отмены у Ozon. Не принимать за дату Ozon.
--
-- Применять руками (SQL-редактор / MCP) по слову владельца. Загрузка:
-- scripts/ozon_posting_status_log.py.

create table if not exists public.ozon_posting_status_log (
    posting_number      text        not null,
    observed_at         timestamptz not null,   -- момент сбора, из которого взят статус
    schema              text        not null,   -- fbo | fbs
    order_date          date        not null,   -- день заказа по МСК: FBO created_at, FBS in_process_at — ключ когорты
    ordered_at          timestamptz,            -- исходная метка Ozon
    status              text        not null,   -- статус Ozon на момент сбора
    substatus           text,
    previous_status     text,                   -- статус в прошлом наблюдении; null = первое наблюдение
    cancel_reason_id    integer,                -- заполняется при status = cancelled
    cancellation_type   text,                   -- client | seller | … (как у Ozon)
    cancelled_after_ship boolean,               -- только FBS
    amount              numeric(14,2),          -- Σ price × quantity по товарам отправления на момент сбора, для взвешивания кривой
    source              text        not null,   -- 'nightly' | 'snapshot:<файл>'
    primary key (posting_number, observed_at),
    constraint ozon_posting_status_log_schema check (schema in ('fbo', 'fbs'))
);

comment on table public.ozon_posting_status_log is
    'Изменения статусов отправлений Ozon, замеченные сборами. observed_at — момент сбора, не момент события у Ozon (точность сутки).';

create index if not exists idx_ozon_posting_status_log_posting
    on public.ozon_posting_status_log (posting_number, observed_at desc);

create index if not exists idx_ozon_posting_status_log_cohort
    on public.ozon_posting_status_log (schema, order_date, status);
