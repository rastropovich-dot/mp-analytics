-- WB-11 §3 (по слову, необязательно): признак источника buyout_rate в витринах.
-- С WB-11 строки WB витрин несут выкуп по когорте дня заказа (wb_buyout_cohort_*): зрелый день — ставка когорты,
-- незрелый — прогноз по последним 30 зрелым дням, дни раньше 2026-04-01 — прежнее календарное правило. Без этой колонки
-- код пишет только buyout_rate; с ней — ещё источник: 'cohort' | 'cohort_forecast' | 'calendar' (Ozon-строки — 'calendar').
-- Код проверяет наличие колонки одним select и без неё ничего не ломает. Обратимо (drop column).
alter table public.daily_marketplace_kpi add column if not exists buyout_rate_source text;
alter table public.daily_sku_kpi          add column if not exists buyout_rate_source text;
comment on column public.daily_marketplace_kpi.buyout_rate_source is 'источник buyout_rate: cohort | cohort_forecast | calendar (WB-11 §3)';
comment on column public.daily_sku_kpi.buyout_rate_source is 'источник buyout_rate: cohort | cohort_forecast | calendar (WB-11 §3)';
