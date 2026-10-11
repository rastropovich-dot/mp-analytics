"""Миграции схемы базы кабинета по файлам sql/ с таблицей версий `schema_migrations` (§5.2б первой задачи RBH).

    venv/bin/python3 scripts/db_migrate.py --cabinet rbh1                 план: что применено, что ждёт (к базе — одно чтение), db_writes 0
    venv/bin/python3 scripts/db_migrate.py --cabinet rbh1 --apply         применить ожидающие файлы по порядку (по слову владельца)
    venv/bin/python3 scripts/db_migrate.py --cabinet all --apply          rbh1 + rbh2 (KARATOV в «all» не входит — поправка советника 3)
    venv/bin/python3 scripts/db_migrate.py --cabinet karatov --mark-applied --allow-karatov
                                                                          засев таблицы версий в KARATOV: записать все файлы как применённые,
                                                                          НЕ выполняя их (схема там уже есть; по слову владельца)
    venv/bin/python3 scripts/db_migrate.py --cabinet karatov --allow-karatov --against data/schema/karatov_<UTC>.json [--offline] [--apply]
                                                                          план засева против снимка схемы: файл, чьих объектов в снимке нет, —
                                                                          «применить», остальные — «пустой»; с --apply выполняются только первые,
                                                                          вторые записываются без выполнения (по слову владельца)

Порядок файлов — по имени: `00000000_baseline_public_schema.sql` (объекты, которых датированные файлы не создают) первым, затем датированные.
Внутри одной даты порядок тоже по имени; файл, который должен идти ПОСЛЕ соседей той же даты (alter после create, функция после колонок),
называется `<дата>_z_<имя>.sql` — четыре таких файла переименованы 2026-10-11 после первого прогона на пустой базе rbh1 (три `alter table
if exists` 20260506 молча пропускались, функция 20260831 падала на несуществующей колонке).
Таблица `schema_migrations(filename primary key, sha256, applied_at, applied_by, note)` создаётся самой командой при первом `--apply` /
`--mark-applied`. Применённый файл с другим sha256 в репозитории — стоп с именем файла (историю не переписываем, нужен новый файл).
Канал — Supabase Management API `POST /v1/projects/{ref}/database/query` (токен `SUPABASE_ACCESS_TOKEN` из общего .env, ref — из профиля
кабинета); каждый файл уходит одним запросом внутри `begin … commit` вместе со строкой `schema_migrations` — либо применён и записан, либо ни
то ни другое. Файл со своим `begin;`/`commit;` на верхнем уровне не оборачивается, строка версии пишется вторым запросом (таких файлов в
репозитории нет: `BEGIN` в 20260531 — внутри `DO $$`). Guard ночи и алерта — `cabinet.schema_status` / `assert_schema`: база отстаёт от
файлов репозитория → ночь останавливается на второй строке лога, алерт шлёт одну строку; таблицы версий нет — молчит (до засева KARATOV).
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import cabinet  # noqa: E402
import db_schema_snapshot as dss  # noqa: E402  — management_runner, project_ref

TABLE = cabinet.MIGRATIONS_TABLE
ALL_CODES = ("rbh1", "rbh2")       # «all» без KARATOV, пока команда не прошла на обоих кабинетах РБХ (поправка советника 3)
CREATE_TABLE = (f"create table if not exists public.{TABLE} (filename text primary key, sha256 text not null, "
                "applied_at timestamptz not null default now(), applied_by text not null, note text)")
TXN_RE = re.compile(r"(?im)^\s*(begin|commit|start\s+transaction|end)\s*;")


def read_applied(run):
    """{filename: sha256}; таблицы нет → {} (и флаг, что её нет)."""
    try:
        rows = run(f"select filename, sha256 from public.{TABLE} order by filename")
    except RuntimeError as exc:
        text = str(exc)
        if "does not exist" in text or "42P01" in text:
            return {}, False
        raise
    return {r["filename"]: r["sha256"] for r in rows}, True


def plan(sql_dir, applied):
    """(pending [(file, sha)], mismatched [(file, sha_repo, sha_db)], applied_ok [file])."""
    pending, mismatched, ok = [], [], []
    for fn in cabinet.sql_files(sql_dir):
        sha = cabinet.file_sha256(os.path.join(sql_dir, fn))
        if fn in applied:
            (ok if applied[fn] == sha else mismatched).append(fn if applied[fn] == sha else (fn, sha, applied[fn]))
        else:
            pending.append((fn, sha))
    return pending, mismatched, ok


def record_sql(fn, sha, applied_by, note):
    esc = lambda v: str(v).replace("'", "''")  # noqa: E731
    return (f"insert into public.{TABLE} (filename, sha256, applied_by, note) values ('{esc(fn)}', '{esc(sha)}', '{esc(applied_by)}', "
            f"{'null' if note is None else chr(39) + esc(note) + chr(39)})")


def apply_query(text, fn, sha, applied_by):
    """(запрос применения, отдельный запрос записи версии | None): файл без своей транзакции — одним запросом вместе со строкой версии."""
    if TXN_RE.search(text):
        return text, record_sql(fn, sha, applied_by, "файл со своей транзакцией; версия записана отдельным запросом")
    return "begin;\n" + text.rstrip().rstrip(";") + ";\n" + record_sql(fn, sha, applied_by, None) + ";\ncommit;", None


ADD_COLUMN_RE = re.compile(r"(?is)alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?(?:public\.)?\"?([a-z_][a-z0-9_]*)\"?\s+(.*?);")
ADD_COL_ITEM_RE = re.compile(r"(?i)add\s+column\s+(?:if\s+not\s+exists\s+)?\"?([a-z_][a-z0-9_]*)")
STMT_RE = re.compile(r";\s*(?:\n|$)")


def file_objects(text):
    """{вид: {имена}} объектов, которые файл создаёт: table / view / function / index и column как (таблица, колонка)."""
    text = re.sub(r"--[^\n]*", "", text)
    out = {"table": set(), "view": set(), "function": set(), "index": set(), "column": set()}
    for kind, name in dss.CREATE_RE.findall(text):
        kind = kind.lower().replace("unique index", "index").replace("materialized view", "view")
        out[kind].add(name.lower())
    for table, body in ADD_COLUMN_RE.findall(text):
        for col in ADD_COL_ITEM_RE.findall(body):
            out["column"].add((table.lower(), col.lower()))
    return out


def classify_against(snapshot, pending, sql_dir):
    """Для плана засева KARATOV: какие ожидающие файлы создадут в базе что-то новое («применить»), а какие уже целиком в базе
    («пустой» → --mark-applied). [(файл, операторов, 'применить' | 'пустой', [чего не хватает])]."""
    ident = lambda n: n.lower()[:63]   # noqa: E731  — Postgres усекает идентификаторы до 63 байт (NAMEDATALEN − 1): индекс 20260511 в файле 64 символа
    have = {"table": {ident(t["table_name"]) for t in snapshot["tables"]}, "view": {ident(t["table_name"]) for t in snapshot["tables"]},
            "function": {ident(f["proname"]) for f in snapshot["functions"]}, "index": {ident(i["indexname"]) for i in snapshot["indexes"]},
            "column": {(ident(c["table_name"]), ident(c["column_name"])) for c in snapshot["columns"]}}
    rows = []
    for fn, _sha in pending:
        text = open(os.path.join(sql_dir, fn), encoding="utf-8").read()
        objs = file_objects(text)
        missing = [f"{k} {n if isinstance(n, str) else '.'.join(n)}" for k, names in objs.items() for n in sorted(names)
                   if (ident(n) if isinstance(n, str) else (ident(n[0]), ident(n[1]))) not in have[k]]
        n_stmts = len([s for s in STMT_RE.split(re.sub(r"--[^\n]*", "", text)) if s.strip()])
        rows.append((fn, n_stmts, "применить" if missing else "пустой", missing))
    return rows


def migrate(code, run, sql_dir, apply=False, mark_applied=False, applied_by=None, out=print, apply_only=None):
    """Один кабинет. Возвращает dict со счётчиками; при --apply останавливается на первом отказе.
    apply_only — множество файлов, которые выполняются; остальные ожидающие записываются как применённые без выполнения
    (засев кабинета, где схема уже есть: --against снимок + --apply)."""
    prof = cabinet.profile(code)
    applied_by = applied_by or f"db_migrate@{os.uname().nodename}"
    applied, table_exists = read_applied(run)
    pending, mismatched, ok = plan(sql_dir, applied)
    out(f"=== {prof.DISPLAY_NAME} ({code}): таблица {TABLE} {'есть' if table_exists else 'НЕТ'}, применено {len(ok)}, "
        f"ждут {len(pending)}, расходятся по sha256 {len(mismatched)}")
    for fn, sha_repo, sha_db in mismatched:
        out(f"  ✗ {fn}: в репозитории {sha_repo[:12]}…, в базе {sha_db[:12]}… — применённый файл изменён; стоп, нужен новый файл")
    for fn, _sha in pending:
        size = os.path.getsize(os.path.join(sql_dir, fn))
        out(f"  … {fn} ({size} байт)")
    result = {"code": code, "applied_before": len(ok), "pending": len(pending), "mismatched": len(mismatched), "done": 0, "writes": 0, "error": None}
    if mismatched:
        result["error"] = "sha256 mismatch"
        return result
    if not (apply or mark_applied):
        out("  план; без --apply ничего не пишется (db_writes 0)")
        return result
    if not table_exists:
        run(CREATE_TABLE)
        result["writes"] += 1
        out(f"  + создана таблица {TABLE}")
    for fn, sha in pending:
        path = os.path.join(sql_dir, fn)
        t0 = time.time()
        mark_this = mark_applied or (apply_only is not None and fn not in apply_only)
        try:
            if mark_this:
                run(record_sql(fn, sha, applied_by, "засев: схема уже была в базе, файл не выполнялся"))
                result["writes"] += 1
            else:
                q, record = apply_query(open(path, encoding="utf-8").read(), fn, sha, applied_by)
                run(q)
                result["writes"] += 1
                if record:
                    run(record)
                    result["writes"] += 1
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{fn}: {type(exc).__name__}: {str(exc)[:400]}"
            out(f"  ✗ {fn}: {result['error']} — стоп, следующие файлы не трогались")
            return result
        result["done"] += 1
        out(f"  ✓ {fn} {'записан как применённый (засев)' if mark_this else 'применён'} за {time.time() - t0:.1f} с")
    out(f"  итого: {'записано' if mark_applied else 'применено / записано'} {result['done']} из {len(pending)}; запросов на запись {result['writes']}")
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cabinet", required=True, help="rbh1 | rbh2 | all (= rbh1 + rbh2) | karatov (только с --allow-karatov)")
    ap.add_argument("--apply", action="store_true", help="применить ожидающие файлы (по слову владельца)")
    ap.add_argument("--mark-applied", action="store_true", help="записать ожидающие файлы как применённые, не выполняя (засев кабинета по умолчанию, по слову)")
    ap.add_argument("--allow-karatov", action="store_true", help="разрешить --cabinet karatov (миграции кабинета по умолчанию — отдельным словом)")
    ap.add_argument("--sql-dir", default=os.path.join(ROOT, "sql"))
    ap.add_argument("--applied-by", help="кто применил (по умолчанию db_migrate@<хост>)")
    ap.add_argument("--against", metavar="SNAPSHOT.json", help="план засева: классифицировать ожидающие файлы против снимка схемы (db_schema_snapshot): «применить» / «пустой»")
    ap.add_argument("--offline", action="store_true", help="только с --against и без --apply: к базе не ходить, считать таблицу версий пустой")
    args = ap.parse_args(argv)
    if args.apply and args.mark_applied:
        raise SystemExit("--apply и --mark-applied вместе не имеют смысла")
    codes = list(ALL_CODES) if args.cabinet.lower() == "all" else [args.cabinet.lower()]
    if "karatov" in codes and not args.allow_karatov:
        raise SystemExit("кабинет karatov не входит в «all» и не берётся без --allow-karatov: его миграции — через MCP по слову, пока команда не прошла на rbh1 и rbh2")
    if args.offline and (args.apply or args.mark_applied or not args.against):
        raise SystemExit("--offline — только план с --against, без --apply / --mark-applied")
    snapshot = json.load(open(args.against, encoding="utf-8")) if args.against else None
    if args.offline:
        def offline_run(query):
            raise RuntimeError('offline: relation "public.schema_migrations" does not exist')
        token = None
    else:
        cabinet.load_base()
        token = (os.environ.get("SUPABASE_ACCESS_TOKEN") or "").strip()
        if not token:
            raise SystemExit("SUPABASE_ACCESS_TOKEN не задан — персональный токен Management API в общем .env")
    rc = 0
    for code in codes:
        prof = cabinet.profile(code)
        run = offline_run if args.offline else dss.management_runner(token, dss.project_ref(prof), read_only=not (args.apply or args.mark_applied))
        apply_only = None
        if snapshot is not None and args.apply:   # засев: выполняются только файлы, чьих объектов в снимке нет; остальные — записью
            applied0, _e = read_applied(run)
            pending0, _m0, _o0 = plan(args.sql_dir, applied0)
            rows0 = classify_against(snapshot, pending0, args.sql_dir)
            apply_only = {fn for fn, _n, verdict, _m in rows0 if verdict == "применить"}
            print(f"  --against + --apply: выполнить {len(apply_only)} файлов, записать без выполнения {len(rows0) - len(apply_only)}")
        res = migrate(code, run, args.sql_dir, apply=args.apply, mark_applied=args.mark_applied, applied_by=args.applied_by, apply_only=apply_only)
        if snapshot is not None and not (args.apply or args.mark_applied):
            applied, _exists = read_applied(run)
            pending, _m, _ok = plan(args.sql_dir, applied)
            rows = classify_against(snapshot, pending, args.sql_dir)
            n_apply = sum(1 for r in rows if r[2] == "применить")
            print(f"  против снимка {os.path.basename(args.against)} ({snapshot['meta'].get('cabinet')} {snapshot['meta'].get('taken_at')}): "
                  f"применить {n_apply} файлов, пустых (→ --mark-applied) {len(rows) - n_apply}, строк schema_migrations после засева {len(rows) + len(applied)}")
            for fn, n_stmts, verdict, missing in rows:
                print(f"    {'✎' if verdict == 'применить' else '·'} {fn}: операторов {n_stmts}, {verdict}" + (f" — нет: {', '.join(missing[:6])}{' …' if len(missing) > 6 else ''}" if missing else ""))
        if res["error"]:
            rc = 1
            break
    print(f"db_writes = {'см. «запросов на запись» выше' if (args.apply or args.mark_applied) else 0}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
