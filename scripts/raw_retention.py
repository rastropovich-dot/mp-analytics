#!/usr/bin/env python3
"""Хранение сырья отправлений в data/postings_raw/ (gitignored): что оставить, что убрать. Без --apply — только план.

    venv/bin/python3 scripts/raw_retention.py              план: каждый файл — оставить / убрать и почему; итог в файлах и МБ
    venv/bin/python3 scripts/raw_retention.py --apply      то же и удаление файлов «убрать»; итог — сколько осталось

Правило (тридцать четвёртая задача, §4; папка — ~330 МБ на 2026-09-23 и растёт с каждым --check-orders и пересборкой):
  * history_<схема>_<от>_<до>.json — окна истории: по каждой схеме оставить KEEP_HISTORY_WINDOWS последних (по дате
    «до», при равенстве — по дате «от» и времени файла), остальные убрать;
  * снимки fbo_…, fbs_…, precheck_… (время снимка — в имени, YYYYMMDDTHHMMSSZ; нет в имени — время файла) —
    старше SNAPSHOT_MAX_AGE_DAYS дней убрать;
  * файл, на который ссылаются разделы docs/outbox.md за последние PROTECT_OUTBOX_DAYS дней (по дате в заголовке
    «## YYYY-MM-DD»), не трогать никогда — имя или шаблон с «*» ищется в тексте этих разделов;
  * всё прочее в папке — не наше правило: оставить и назвать.
Список к удалению печатается целиком до удаления. В БД и в API не ходит.
"""
import argparse
import fnmatch
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "data", "postings_raw")
OUTBOX = os.path.join(ROOT, "docs", "outbox.md")
KEEP_HISTORY_WINDOWS = 3
SNAPSHOT_MAX_AGE_DAYS = 30
PROTECT_OUTBOX_DAYS = 7

HISTORY_RE = re.compile(r"^history_(?P<scheme>[a-z0-9]+)_(?P<from>\d{4}-\d{2}-\d{2})_(?P<to>\d{4}-\d{2}-\d{2})\.json$")
SNAPSHOT_RE = re.compile(r"^(?:fbo|fbs|precheck)_.*?(?P<ts>\d{8}T\d{6}Z)?\.json$")
TS_RE = re.compile(r"(\d{8}T\d{6}Z)")
REF_RE = re.compile(r"(?:history|fbo|fbs|precheck)_[\w*.\-]*?\.json")
HEADER_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})", re.M)


def outbox_references(text, today, days=PROTECT_OUTBOX_DAYS):
    """Имена и шаблоны файлов сырья из разделов outbox не старше days дней."""
    since = (today - timedelta(days=days)).isoformat()
    heads = [(m.start(), m.group(1)) for m in HEADER_RE.finditer(text)]
    refs = set()
    for i, (pos, day) in enumerate(heads):
        if day < since:
            continue
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        refs.update(REF_RE.findall(text[pos:end]))
    return refs


def snapshot_time(name, mtime):
    m = TS_RE.search(name)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(mtime, tz=timezone.utc)


def plan(files, refs, now):
    """files — [(имя, байт, mtime)]. Возвращает [(имя, байт, решение 'keep'|'remove', причина)]."""
    out, history = [], {}
    for name, size, mtime in files:
        m = HISTORY_RE.match(name)
        if m:
            history.setdefault(m.group("scheme"), []).append((m.group("to"), m.group("from"), mtime, name, size))
    keep_history = set()
    for scheme, items in history.items():
        for item in sorted(items, reverse=True)[:KEEP_HISTORY_WINDOWS]:
            keep_history.add(item[3])
    for name, size, mtime in sorted(files):
        protected = any(fnmatch.fnmatch(name, ref) for ref in refs)
        if HISTORY_RE.match(name):
            decision, why = ("keep", f"окно истории — среди {KEEP_HISTORY_WINDOWS} последних своей схемы") if name in keep_history \
                else ("remove", f"окно истории — старше {KEEP_HISTORY_WINDOWS} последних своей схемы")
        elif SNAPSHOT_RE.match(name):
            age = (now - snapshot_time(name, mtime)).days
            decision, why = ("remove", f"снимок {age} дн. — старше {SNAPSHOT_MAX_AGE_DAYS}") if age > SNAPSHOT_MAX_AGE_DAYS \
                else ("keep", f"снимок {age} дн.")
        else:
            decision, why = "keep", "не наше правило — не трогаю"
        if decision == "remove" and protected:
            decision, why = "keep", why + f"; НО на него ссылается outbox за {PROTECT_OUTBOX_DAYS} дн. — не трогаю"
        out.append((name, size, decision, why))
    return out


def mb(n):
    return f"{n / 1048576:,.1f} МБ".replace(",", " ")


def main(argv=None):
    ap = argparse.ArgumentParser(description="План хранения сырья отправлений; --apply — удалить лишнее.")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dir", default=RAW_DIR)
    args = ap.parse_args(argv)
    if not os.path.isdir(args.dir):
        print(f"{args.dir}: папки нет — нечего хранить")
        return 0
    files = []
    for name in os.listdir(args.dir):
        path = os.path.join(args.dir, name)
        if os.path.isfile(path):
            st = os.stat(path)
            files.append((name, st.st_size, st.st_mtime))
    now = datetime.now(timezone.utc)
    refs = outbox_references(open(OUTBOX).read(), now.date()) if os.path.exists(OUTBOX) else set()
    rows = plan(files, refs, now)
    total = sum(r[1] for r in rows)
    print(f"{args.dir}: файлов {len(rows)}, {mb(total)}; ссылок из outbox за {PROTECT_OUTBOX_DAYS} дн.: {len(refs)}")
    for name, size, decision, why in rows:
        print(f"  {'УБРАТЬ  ' if decision == 'remove' else 'оставить'} {mb(size):>10}  {name} — {why}")
    remove = [r for r in rows if r[2] == "remove"]
    freed = sum(r[1] for r in remove)
    print(f"к удалению: {len(remove)} файлов, {mb(freed)}; останется {len(rows) - len(remove)} файлов, {mb(total - freed)}")
    if not remove:
        return 0
    if not args.apply:
        print("без --apply ничего не удаляю")
        return 0
    removed = 0
    for name, _size, _d, _w in remove:
        os.remove(os.path.join(args.dir, name))
        removed += 1
    left = [n for n in os.listdir(args.dir) if os.path.isfile(os.path.join(args.dir, n))]
    print(f"✅ удалено {removed} из {len(remove)}; в папке {len(left)} файлов, {mb(sum(os.path.getsize(os.path.join(args.dir, n)) for n in left))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
