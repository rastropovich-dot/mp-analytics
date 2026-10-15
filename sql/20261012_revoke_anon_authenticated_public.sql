-- Роли anon / authenticated теряют все права на схему public (решение владельца 10-10 / 10-11; RBH §5.2 (в)).
-- У KARATOV (апрель 2026) умолчания Supabase дали им полный DML на все 40+ таблиц, RLS включён лишь на 13 таблицах панели без политик:
-- публичный anon-ключ читал и писал 27+ таблиц через PostgREST. Клиентов с anon / authenticated в коде нет — все 38 create_client на
-- SUPABASE_SERVICE_KEY (инвентаризация §1, 09-28); service_role и postgres не меняются, RLS не трогается. В rbh1 / rbh2 снимает 30 грантов,
-- которые базовый файл дал 15 таблицам панели (копия KARATOV 1:1), и умолчания на будущее — после этого гранты трёх баз равны.
revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all functions in schema public from anon, authenticated;
alter default privileges for role postgres in schema public revoke all on tables from anon, authenticated;
alter default privileges for role postgres in schema public revoke all on sequences from anon, authenticated;
alter default privileges for role postgres in schema public revoke all on functions from anon, authenticated;
