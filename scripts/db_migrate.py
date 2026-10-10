"""Миграции схемы базы кабинета по файлам sql/ с таблицей версий `schema_migrations` (§5.2б первой задачи RBH).

    venv/bin/python3 scripts/db_migrate.py --cabinet rbh1                 план: что применено, что ждёт (к базе — одно чтение), db_writes 0
    venv/bin/python3 scripts/db_migrate.py --cabinet rbh1 --apply         применить ожидающие файлы по порядку (по слову владельца)
    venv/bin/python3 scripts/db_migrate.py --cabinet all --apply          rbh1 + rbh2 (KARATOV в «all» не входит — поправка советника 3)
    venv/bin/python3 scripts/db_migrate.py --cabinet karatov --mark-applied --allow-karatov
                                                                          засев таблицы версий в KARATOV: записать все файлы как применённые,
                                                                          НЕ выполняя их (схема там уже есть; по слову владельца)

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


def migrate(code, run, sql_dir, apply=False, mark_applied=False, applied_by=None, out=print):
    """Один кабинет. Возвращает dict со счётчиками; при --apply останавливается на первом отказе."""
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
        try:
            if mark_applied:
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
        out(f"  ✓ {fn} {'записан как применённый' if mark_applied else 'применён'} за {time.time() - t0:.1f} с")
    out(f"  итого: {'записано' if mark_applied else 'применено'} {result['done']} из {len(pending)}; запросов на запись {result['writes']}")
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cabinet", required=True, help="rbh1 | rbh2 | all (= rbh1 + rbh2) | karatov (только с --allow-karatov)")
    ap.add_argument("--apply", action="store_true", help="применить ожидающие файлы (по слову владельца)")
    ap.add_argument("--mark-applied", action="store_true", help="записать ожидающие файлы как применённые, не выполняя (засев кабинета по умолчанию, по слову)")
    ap.add_argument("--allow-karatov", action="store_true", help="разрешить --cabinet karatov (миграции кабинета по умолчанию — отдельным словом)")
    ap.add_argument("--sql-dir", default=os.path.join(ROOT, "sql"))
    ap.add_argument("--applied-by", help="кто применил (по умолчанию db_migrate@<хост>)")
    args = ap.parse_args(argv)
    if args.apply and args.mark_applied:
        raise SystemExit("--apply и --mark-applied вместе не имеют смысла")
    codes = list(ALL_CODES) if args.cabinet.lower() == "all" else [args.cabinet.lower()]
    if "karatov" in codes and not args.allow_karatov:
        raise SystemExit("кабинет karatov не входит в «all» и не берётся без --allow-karatov: его миграции — через MCP по слову, пока команда не прошла на rbh1 и rbh2")
    cabinet.load_base()
    token = (os.environ.get("SUPABASE_ACCESS_TOKEN") or "").strip()
    if not token:
        raise SystemExit("SUPABASE_ACCESS_TOKEN не задан — персональный токен Management API в общем .env")
    rc = 0
    for code in codes:
        prof = cabinet.profile(code)
        ref = dss.project_ref(prof)
        run = dss.management_runner(token, ref, read_only=not (args.apply or args.mark_applied))
        res = migrate(code, run, args.sql_dir, apply=args.apply, mark_applied=args.mark_applied, applied_by=args.applied_by)
        if res["error"]:
            rc = 1
            break
    print(f"db_writes = {'см. «запросов на запись» выше' if (args.apply or args.mark_applied) else 0}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
