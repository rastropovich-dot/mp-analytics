-- ozon_performance_daily_load_status: unique (load_date, target_date, marketplace_code, mode) из 20260531 дублирует первичный ключ,
-- который 20260602 поставил на те же четыре колонки. В KARATOV этого unique нет (снят руками, 20260602 удаляет constraint с другим
-- именем «…_mode_unique»); на пустой базе rbh1 после обоих файлов он остался (снимок 2026-10-10, --compare). Снимаем в обеих базах.
alter table if exists public.ozon_performance_daily_load_status
    drop constraint if exists ozon_performance_daily_load_status_load_target_marketplace_mode_key;
