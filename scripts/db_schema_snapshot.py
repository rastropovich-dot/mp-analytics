"""Снимок схемы `public` базы кабинета через Supabase Management API — только чтение (§5.2а первой задачи RBH).

    venv/bin/python3 scripts/db_schema_snapshot.py --cabinet karatov                       снимок → data/schema/karatov_<UTC>.json + сводка объектов
    venv/bin/python3 scripts/db_schema_snapshot.py --cabinet karatov --baseline-sql sql/00000000_baseline_public_schema.sql
                                                                                            + DDL объектов, которых датированные файлы sql/ не создают
    venv/bin/python3 scripts/db_schema_snapshot.py --compare data/schema/karatov_A.json data/schema/rbh1_B.json      разница двух снимков, без сети
    venv/bin/python3 scripts/db_schema_snapshot.py --cabinet karatov --from-file data/schema/karatov_A.json --baseline-sql …   DDL из снимка, без сети

Зачем: `sql/` не собирает базу с нуля — 11 объектов кода созданы в панели до первого датированного файла (outbox RBH, 09-30), поэтому пустой
проект РБХ получает схему KARATOV так: базовый файл (из этого снимка) + 42 датированных файла → снимок базы РБХ → `--compare` с KARATOV = 0.

Доступ: `POST https://api.supabase.com/v1/projects/{ref}/database/query` с `read_only: true` (тот же канал, что у MCP), токен —
`SUPABASE_ACCESS_TOKEN` из общего `.env` (владелец создаёт в Account → Access Tokens и отзывает после работ со схемой), `ref` — первая метка
`SUPABASE_HOST` профиля кабинета. Запросов к базе — по числу QUERIES (+1 на справочник `marketplaces`, если таблица есть); таблицы данных не
читаются, кроме `marketplaces` (справочник ≤ 100 строк) и оценок `reltuples`. db_writes = 0 всегда; ключи и токен не печатаются.
Расшифровка типов колонок для DDL — по `information_schema` (varchar(n), numeric(p,s), массивы, identity, nextval → CREATE SEQUENCE);
единственная проверка её правильности — `--compare` после применения к пустой базе, не чтение глазами.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import cabinet  # noqa: E402  — только профиль (ref и каталоги); клиента PostgREST нет, guard по SUPABASE_URL не нужен

API = "https://api.supabase.com/v1/projects/{ref}/database/query"
SQL_DIR = os.path.join(ROOT, "sql")
DATED_SQL = re.compile(r"^\d{8}_.*\.sql$")
ROLES = ("anon", "authenticated", "service_role")

QUERIES = {
    "tables": "select table_name, table_type from information_schema.tables where table_schema = 'public' order by 1",
    "columns": ("select table_name, column_name, ordinal_position, data_type, udt_name, character_maximum_length, numeric_precision, "
                "numeric_scale, is_nullable, column_default, is_identity, identity_generation "
                "from information_schema.columns where table_schema = 'public' order by table_name, ordinal_position"),
    "constraints": ("select conrelid::regclass::text as table_name, conname, contype, pg_get_constraintdef(oid) as definition "
                    "from pg_constraint where connamespace = 'public'::regnamespace order by 1, 2"),
    "indexes": "select tablename as table_name, indexname, indexdef from pg_indexes where schemaname = 'public' order by 1, 2",
    "views": "select viewname, definition from pg_views where schemaname = 'public' order by 1",
    "matviews": "select matviewname, definition from pg_matviews where schemaname = 'public' order by 1",
    "functions": ("select p.proname, pg_get_function_identity_arguments(p.oid) as args, pg_get_functiondef(p.oid) as definition "
                  "from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public' and p.prokind in ('f', 'p') order by 1, 2"),
    "sequences": "select sequence_name, data_type, start_value, increment from information_schema.sequences where sequence_schema = 'public' order by 1",
    "triggers": ("select c.relname as table_name, t.tgname, pg_get_triggerdef(t.oid) as definition from pg_trigger t "
                 "join pg_class c on c.oid = t.tgrelid join pg_namespace n on n.oid = c.relnamespace "
                 "where n.nspname = 'public' and not t.tgisinternal order by 1, 2"),
    "rls": ("select c.relname as table_name, c.relrowsecurity as rls_enabled, c.relforcerowsecurity as rls_forced from pg_class c "
            "join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'public' and c.relkind = 'r' order by 1"),
    "policies": ("select tablename as table_name, policyname, cmd, permissive, roles::text as roles, qual, with_check "
                 "from pg_policies where schemaname = 'public' order by 1, 2"),
    "grants": ("select table_name, grantee, string_agg(privilege_type, ',' order by privilege_type) as privileges "
               "from information_schema.role_table_grants where table_schema = 'public' and grantee in ('anon', 'authenticated', 'service_role') "
               "group by 1, 2 order by 1, 2"),
    "extensions": "select extname, extversion from pg_extension order by 1",
    "row_estimates": ("select c.relname as table_name, c.reltuples::bigint as rows_estimate from pg_class c "
                      "join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'public' and c.relkind = 'r' order by 1"),
}
MARKETPLACES_QUERY = "select * from public.marketplaces order by 1 limit 100"


# ---------- доступ ----------

def project_ref(prof):
    host = (prof.SUPABASE_HOST or "").strip()
    if not host:
        raise SystemExit(f"кабинет {prof.DISPLAY_NAME}: в cabinets/{prof.CODE}.py не задан SUPABASE_HOST — ref проекта взять неоткуда")
    return host.split(".")[0]


def management_runner(token, ref, timeout=120):
    """Функция query → список строк (dict). Каждый запрос — read_only; ошибка API — именованная, тело ответа без токена."""
    def run(query):
        body = json.dumps({"query": query, "read_only": True}).encode("utf-8")
        req = urllib.request.Request(API.format(ref=ref), data=body, method="POST",
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Management API HTTP {e.code} на запросе «{query[:60]}…»: {e.read()[:300]!r}") from None
        if isinstance(data, dict) and "error" in data:
            raise RuntimeError(f"Management API: {str(data['error'])[:300]} на запросе «{query[:60]}…»")
        return data if isinstance(data, list) else data.get("rows", data.get("result", []))
    return run


# ---------- снимок ----------

def take_snapshot(run, cabinet_code, ref):
    """Все QUERIES + справочник marketplaces (если таблица есть). Возвращает dict с meta."""
    snap = {"meta": {"taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "cabinet": cabinet_code, "ref": ref,
                     "schema": "public", "queries": 0}}
    for name, query in QUERIES.items():
        snap[name] = run(query)
        snap["meta"]["queries"] += 1
    table_names = {r["table_name"] for r in snap["tables"]}
    if "marketplaces" in table_names:
        snap["marketplaces_rows"] = run(MARKETPLACES_QUERY)
        snap["meta"]["queries"] += 1
    else:
        snap["marketplaces_rows"] = []
    return snap


def columns_by_table(snap):
    out = {}
    for c in snap["columns"]:
        out.setdefault(c["table_name"], []).append(c)
    for cols in out.values():
        cols.sort(key=lambda c: int(c["ordinal_position"]))
    return out


# ---------- что создают датированные файлы sql/ ----------

CREATE_RE = re.compile(r"create\s+(?:or\s+replace\s+)?(unique\s+index|index|table|view|materialized\s+view|function)(?:\s+if\s+not\s+exists)?\s+"
                       r"(?:public\.)?\"?([a-z_][a-z0-9_]*)", re.I)


def sql_dir_objects(sql_dir=SQL_DIR):
    """{вид: {имя: файл}} по датированным файлам (без даты в имени — не в счёт: он не встаёт в порядок применения)."""
    out = {"table": {}, "view": {}, "function": {}, "index": {}}
    for fn in sorted(os.listdir(sql_dir)):
        if not DATED_SQL.match(fn):
            continue
        text = open(os.path.join(sql_dir, fn), encoding="utf-8").read()
        for kind, name in CREATE_RE.findall(text):
            kind = kind.lower().replace("unique index", "index").replace("materialized view", "view")
            out[kind].setdefault(name.lower(), fn)
    return out


def coverage(snap, created):
    """[(вид, имя, колонок/аргументов, файл sql/ или '—')] по объектам снимка."""
    cols = columns_by_table(snap)
    rows = []
    for t in snap["tables"]:
        name = t["table_name"]
        kind = "view" if t["table_type"] == "VIEW" else "table"
        rows.append((kind, name, len(cols.get(name, [])), created[kind].get(name.lower(), "—")))
    for v in snap.get("matviews", []):
        rows.append(("matview", v["matviewname"], 0, created["view"].get(v["matviewname"].lower(), "—")))
    for f in snap["functions"]:
        rows.append(("function", f"{f['proname']}({f['args']})", 0, created["function"].get(f["proname"].lower(), "—")))
    for i in snap["indexes"]:
        rows.append(("index", i["indexname"], 0, created["index"].get(i["indexname"].lower(), "—")))
    return rows


# ---------- DDL базового файла ----------

def column_type(c):
    dt, udt = c["data_type"], (c.get("udt_name") or "")
    if dt == "ARRAY":
        return (udt[1:] if udt.startswith("_") else udt) + "[]"
    if dt == "USER-DEFINED":
        return udt
    if dt == "character varying":
        return f"varchar({c['character_maximum_length']})" if c.get("character_maximum_length") else "varchar"
    if dt == "character":
        return f"char({c['character_maximum_length']})" if c.get("character_maximum_length") else "char"
    if dt == "numeric":
        return f"numeric({c['numeric_precision']},{c['numeric_scale']})" if c.get("numeric_precision") is not None else "numeric"
    return dt


NEXTVAL_RE = re.compile(r"nextval\('(?:public\.)?\"?([a-z0-9_]+)\"?'::regclass\)", re.I)


def column_ddl(c):
    parts = [f"    \"{c['column_name']}\" {column_type(c)}"]
    if str(c.get("is_identity")) == "YES":
        parts.append(f"generated {'always' if c.get('identity_generation') == 'ALWAYS' else 'by default'} as identity")
    elif c.get("column_default") not in (None, ""):
        parts.append(f"default {c['column_default']}")
    if c.get("is_nullable") == "NO":
        parts.append("not null")
    return " ".join(parts)


def constraint_name_set(snap):
    return {r["conname"] for r in snap["constraints"]}


def baseline_sql(snap, created, title="базовый снимок схемы public"):
    """DDL объектов, которых датированные файлы не создают: последовательности → таблицы (PK / unique / check внутри) → внешние ключи →
    индексы → представления → функции → RLS и политики → гранты. Идемпотентно (if not exists / or replace), порядок — для пустой базы."""
    cols = columns_by_table(snap)
    cons = {}
    for r in snap["constraints"]:
        cons.setdefault(r["table_name"].replace("public.", "").strip('"'), []).append(r)
    con_names = constraint_name_set(snap)
    lines = [f"-- {title}: сгенерировано scripts/db_schema_snapshot.py из снимка {snap['meta'].get('cabinet')} {snap['meta'].get('taken_at')}",
             "-- Объекты, которых датированные файлы sql/ не создают (созданы в панели до 2026-05-05 + дрейф). Применять ПЕРВЫМ, до датированных.",
             "begin;", ""]
    tables = [t["table_name"] for t in snap["tables"] if t["table_type"] != "VIEW" and t["table_name"].lower() not in created["table"]]
    seqs = set()
    for t in tables:
        for c in cols.get(t, []):
            m = NEXTVAL_RE.search(str(c.get("column_default") or ""))
            if m and str(c.get("is_identity")) != "YES":
                seqs.add(m.group(1))
    for s in sorted(seqs):
        lines.append(f"create sequence if not exists public.\"{s}\";")
    fks = []
    for t in tables:
        body = [column_ddl(c) for c in cols.get(t, [])]
        for r in sorted(cons.get(t, []), key=lambda r: ({"p": 0, "u": 1, "c": 2}.get(r["contype"], 3), r["conname"])):
            if r["contype"] in ("p", "u", "c"):
                body.append(f"    constraint \"{r['conname']}\" {r['definition']}")
            elif r["contype"] == "f":
                fks.append((t, r))
        lines += ["", f"create table if not exists public.\"{t}\" (", ",\n".join(body), ");"]
    for t, r in fks:
        lines.append(f"alter table public.\"{t}\" add constraint \"{r['conname']}\" {r['definition']};")
    for i in snap["indexes"]:
        if i["indexname"] in con_names or i["indexname"].lower() in created["index"] or i["table_name"].lower() in created["table"]:
            continue   # индекс ограничения (PK / unique) создан вместе с таблицей; индексы таблиц из sql/ — там же
        lines.append(re.sub(r"^create (unique )?index ", lambda m: f"create {m.group(1) or ''}index if not exists ", i["indexdef"], flags=re.I) + ";")
    for v in snap["views"]:
        if v["viewname"].lower() not in created["view"]:
            lines += ["", f"create or replace view public.\"{v['viewname']}\" as", v["definition"].rstrip().rstrip(";") + ";"]
    for v in snap.get("matviews", []):
        if v["matviewname"].lower() not in created["view"]:
            lines += ["", f"create materialized view if not exists public.\"{v['matviewname']}\" as", v["definition"].rstrip().rstrip(";") + ";"]
    for f in snap["functions"]:
        if f["proname"].lower() not in created["function"]:
            lines += ["", f["definition"].rstrip().rstrip(";") + ";"]
    for t in snap["triggers"]:
        if t["table_name"].lower() not in created["table"]:
            lines.append(t["definition"].rstrip().rstrip(";") + ";")
    for r in snap["rls"]:
        if r["table_name"] in tables and r.get("rls_enabled"):
            lines.append(f"alter table public.\"{r['table_name']}\" enable row level security;")
    for p in snap["policies"]:
        if p["table_name"] in tables:
            roles = str(p.get("roles") or "").strip("{}") or "public"
            stmt = f"create policy \"{p['policyname']}\" on public.\"{p['table_name']}\" as {'permissive' if str(p.get('permissive', 'PERMISSIVE')).upper() == 'PERMISSIVE' else 'restrictive'} for {p['cmd'].lower()} to {roles}"
            if p.get("qual"):
                stmt += f" using ({p['qual']})"
            if p.get("with_check"):
                stmt += f" with check ({p['with_check']})"
            lines.append(stmt + ";")
    for g in snap["grants"]:
        if g["table_name"] in tables:
            lines.append(f"grant {g['privileges'].replace(',', ', ')} on public.\"{g['table_name']}\" to {g['grantee']};")
    lines += ["", "commit;", ""]
    return "\n".join(lines)


# ---------- сравнение снимков ----------

def _key_rows(snap, kind):
    if kind == "columns":
        return {(r["table_name"], r["column_name"]): (column_type(r), r["is_nullable"], r.get("column_default"), str(r.get("is_identity"))) for r in snap["columns"]}
    if kind == "tables":
        return {r["table_name"]: r["table_type"] for r in snap["tables"]}
    if kind == "constraints":
        return {(r["table_name"].replace("public.", ""), r["conname"]): (r["contype"], r["definition"]) for r in snap["constraints"]}
    if kind == "indexes":
        return {(r["table_name"], r["indexname"]): re.sub(r"\s+", " ", r["indexdef"]) for r in snap["indexes"]}
    if kind == "views":
        return {r["viewname"]: re.sub(r"\s+", " ", r["definition"]).strip() for r in snap["views"]}
    if kind == "functions":
        return {(r["proname"], r["args"]): re.sub(r"\s+", " ", r["definition"]).strip() for r in snap["functions"]}
    if kind == "triggers":
        return {(r["table_name"], r["tgname"]): r["definition"] for r in snap["triggers"]}
    if kind == "rls":
        return {r["table_name"]: (bool(r["rls_enabled"]), bool(r["rls_forced"])) for r in snap["rls"]}
    if kind == "policies":
        return {(r["table_name"], r["policyname"]): (r["cmd"], r.get("roles"), r.get("qual"), r.get("with_check")) for r in snap["policies"]}
    if kind == "grants":
        return {(r["table_name"], r["grantee"]): r["privileges"] for r in snap["grants"]}
    if kind == "sequences":
        return {r["sequence_name"]: (r["data_type"], str(r["start_value"]), str(r["increment"])) for r in snap["sequences"]}
    if kind == "extensions":
        return {r["extname"]: r["extversion"] for r in snap["extensions"]}
    raise KeyError(kind)


COMPARE_KINDS = ("tables", "columns", "constraints", "indexes", "views", "functions", "triggers", "sequences", "rls", "policies", "grants", "extensions")


def compare(a, b, kinds=COMPARE_KINDS):
    """{вид: {"only_a": [...], "only_b": [...], "differ": [(ключ, A, B)]}} — только виды с разницей."""
    out = {}
    for kind in kinds:
        ka, kb = _key_rows(a, kind), _key_rows(b, kind)
        only_a = sorted(k for k in ka if k not in kb)
        only_b = sorted(k for k in kb if k not in ka)
        differ = sorted((k, ka[k], kb[k]) for k in ka if k in kb and ka[k] != kb[k])
        if only_a or only_b or differ:
            out[kind] = {"only_a": only_a, "only_b": only_b, "differ": differ}
    return out


def compare_total(diff):
    return sum(len(v["only_a"]) + len(v["only_b"]) + len(v["differ"]) for v in diff.values())


# ---------- вывод ----------

def print_summary(snap, created):
    m = snap["meta"]
    print(f"снимок схемы public: кабинет {m.get('cabinet')}, ref {m.get('ref')}, {m.get('taken_at')}, запросов {m.get('queries')}")
    counts = {k: len(snap.get(k, [])) for k in ("tables", "columns", "constraints", "indexes", "views", "matviews", "functions", "sequences", "triggers", "policies", "grants", "extensions")}
    print("объектов: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    rows = coverage(snap, created)
    missing = [r for r in rows if r[3] == "—"]
    print(f"\n{'вид':9} {'имя':58} {'колонок':>7}  в sql/ (датированные файлы)")
    for kind, name, n, src in rows:
        if kind == "index" and src != "—":
            continue
        print(f"{kind:9} {name[:58]:58} {n:>7}  {src}")
    print(f"\nне создаются датированными файлами sql/: {len(missing)} объектов "
          f"({', '.join(sorted({r[0] for r in missing}))}); таблицы: {', '.join(r[1] for r in missing if r[0] == 'table') or '—'}")
    est = {r["table_name"]: r["rows_estimate"] for r in snap.get("row_estimates", [])}
    big = sorted(est.items(), key=lambda kv: -int(kv[1] or 0))[:8]
    print("оценка строк (reltuples) у крупнейших таблиц: " + ", ".join(f"{t} {int(n or 0):,}".replace(",", " ") for t, n in big))
    if snap.get("marketplaces_rows"):
        print(f"справочник marketplaces: {len(snap['marketplaces_rows'])} строк — в снимке")


def print_compare(diff, label_a, label_b):
    if not diff:
        print(f"{label_a} = {label_b}: различий 0 по {len(COMPARE_KINDS)} видам объектов")
        return
    for kind, d in diff.items():
        print(f"{kind}: только в A {len(d['only_a'])}, только в B {len(d['only_b'])}, отличаются {len(d['differ'])}")
        for k in d["only_a"][:20]:
            print(f"   только A: {k}")
        for k in d["only_b"][:20]:
            print(f"   только B: {k}")
        for k, va, vb in d["differ"][:20]:
            print(f"   {k}: A={str(va)[:90]!r} | B={str(vb)[:90]!r}")
    print(f"итого различий: {compare_total(diff)}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cabinet", help="код кабинета (по умолчанию MP_CABINET / karatov)")
    ap.add_argument("--out", help="куда писать снимок JSON (по умолчанию <data кабинета>/schema/<код>_<UTC>.json)")
    ap.add_argument("--from-file", help="готовый снимок JSON вместо обращения к базе (сводка / базовый файл без сети)")
    ap.add_argument("--baseline-sql", help="путь для DDL объектов, которых sql/ не создаёт (sql/00000000_baseline_public_schema.sql)")
    ap.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"), help="сравнить два снимка; к базе не ходит")
    args = ap.parse_args(argv)

    if args.compare:
        a, b = (json.load(open(p, encoding="utf-8")) for p in args.compare)
        diff = compare(a, b)
        print_compare(diff, os.path.basename(args.compare[0]), os.path.basename(args.compare[1]))
        return 1 if diff else 0

    code = (args.cabinet or cabinet.cabinet_code()).lower()
    prof = cabinet.profile(code)
    created = sql_dir_objects()
    if args.from_file:
        snap = json.load(open(args.from_file, encoding="utf-8"))
    else:
        cabinet.load_base()   # общий .env: SUPABASE_ACCESS_TOKEN
        cabinet.load_overlay(code)
        token = (os.environ.get("SUPABASE_ACCESS_TOKEN") or "").strip()
        if not token:
            raise SystemExit("SUPABASE_ACCESS_TOKEN не задан — персональный токен Management API в общем .env (Account → Access Tokens); без него снимок невозможен")
        ref = project_ref(prof)
        snap = take_snapshot(management_runner(token, ref), code, ref)
        out = args.out or cabinet.data_path("schema", f"{code}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json", prof=prof)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=1, default=str)
        print(f"снимок записан: {os.path.relpath(out, ROOT)}")
    print_summary(snap, created)
    if args.baseline_sql:
        text = baseline_sql(snap, created)
        with open(args.baseline_sql, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"базовый файл записан: {args.baseline_sql} ({len(text.splitlines())} строк, {text.count('create table')} таблиц, "
              f"{text.count('create or replace view')} представлений, {text.count('CREATE OR REPLACE FUNCTION') + text.count('create or replace function')} функций)")
    print("db_writes = 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
