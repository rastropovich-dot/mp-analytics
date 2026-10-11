-- Права service_role на объекты схемы public (RBH §5.2, 2026-10-11; решение владельца 10-11: только service_role, anon / authenticated —
-- ничего). У проекта KARATOV (апрель 2026) умолчания Supabase для роли postgres дают API-ролям полный DML (arwdDxtm) на новые таблицы;
-- у проектов, созданных в октябре 2026 (rbh1 / rbh2), умолчание — только Dxtm (truncate / references / trigger / maintain): таблицы из
-- датированных файлов остались без select / insert / update / delete для service_role, и PostgREST кабинета получил бы permission denied.
-- Наш код ходит только service_role. В KARATOV все операторы пустые (права уже есть); revoke у anon / authenticated там — отдельным файлом.
grant select, insert, update, delete on all tables in schema public to service_role;
grant usage, select on all sequences in schema public to service_role;
grant execute on all functions in schema public to service_role;
alter default privileges for role postgres in schema public grant select, insert, update, delete on tables to service_role;
alter default privileges for role postgres in schema public grant usage, select on sequences to service_role;
alter default privileges for role postgres in schema public grant execute on functions to service_role;
