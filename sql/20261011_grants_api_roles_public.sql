-- Права API-ролей на объекты схемы public — как у KARATOV (RBH §5.2, 2026-10-11).
-- У проекта KARATOV (апрель 2026) умолчания Supabase для роли postgres дают anon / authenticated / service_role полный DML
-- (arwdDxtm) на новые таблицы; у проектов, созданных в октябре 2026 (rbh1 / rbh2), умолчание — только Dxtm (truncate / references /
-- trigger / maintain): таблицы из датированных файлов остались без select / insert / update / delete для service_role, и PostgREST
-- кабинета получил бы permission denied. Файл выравнивает оба: на KARATOV все операторы — пустые (права уже есть).
-- Решение владельца 10-10: revoke у anon / authenticated — отдельным датированным файлом по слову после §5.2, в обеих базах.
grant select, insert, update, delete on all tables in schema public to anon, authenticated, service_role;
grant usage, select on all sequences in schema public to anon, authenticated, service_role;
grant execute on all functions in schema public to anon, authenticated, service_role;
alter default privileges for role postgres in schema public grant select, insert, update, delete on tables to anon, authenticated, service_role;
alter default privileges for role postgres in schema public grant usage, select on sequences to anon, authenticated, service_role;
alter default privileges for role postgres in schema public grant execute on functions to anon, authenticated, service_role;
