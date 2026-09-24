#!/usr/bin/env python3
"""Дописать свежие экспорты чата разработчиков Ozon к архиву knowledge/telegram/ozon-dev-chat.json.

    venv/bin/python3 scripts/merge_telegram_export.py "~/Downloads/Telegram Desktop/ChatExport_2026-09-21" [ещё корни…]           план
    venv/bin/python3 scripts/merge_telegram_export.py … --apply                                                                     запись

Порядок — из knowledge/telegram/README.md («Обновление»): сверить `id` и `name` чата; собрать архив и экспорт в
словари по `id`; пересечение обновить свежим снимком (сообщения меняются задним числом — правки, реакции), новое
добавить; отсортировать по `id`; проверить, что ни один месяц между первым и последним не выпал; резервная копия
`ozon-dev-chat.json.bak` и инкремент `_increment_<дата>.json` рядом (оба в .gitignore); писать с `indent=1`,
`ensure_ascii=False` — как отдаёт Telegram, иначе git diff перестаёт показывать, что изменилось.

Вложения. Каждый экспорт кладёт фото в свой корень (`photos/…`), а архив знает один корень — 09-13, где фото лежат
в `chats/chat_562951630583447/topic_1/photos/`. Поэтому путь в сообщении переписывается на путь корня 09-13, а файл
копируется туда. **Нумерация фото НЕ сквозная между экспортами** (2026-09-23: одно и то же фото — `photo_2561@…` в
архиве и `photo_2817@…` в экспорте 09-21, байты совпадают): если в корне уже есть файл с тем же хвостом `@дата_время`
и тем же sha256, путь переписывается на него, а копия не делается. Столкновение имени с другим содержимым — стоп.
Файлы, которых в экспорте нет («(File not included …)»), не трогаются.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVE = os.path.join(ROOT, "knowledge", "telegram", "ozon-dev-chat.json")
PHOTO_ROOT = os.path.expanduser("~/Downloads/Telegram Desktop/ChatExport_2026-09-13")
PHOTO_DIR = "chats/chat_562951630583447/topic_1/photos"
ATTACHMENT_FIELDS = ("photo", "file")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def month_gaps(messages):
    """Месяцы между первым и последним сообщением, в которых нет ни одного сообщения."""
    seen = {m["date"][:7] for m in messages if m.get("date")}
    first, last = min(seen), max(seen)
    y, mo = int(first[:4]), int(first[5:7])
    gaps = []
    while f"{y:04d}-{mo:02d}" <= last:
        if f"{y:04d}-{mo:02d}" not in seen:
            gaps.append(f"{y:04d}-{mo:02d}")
        mo += 1
        if mo == 13:
            y, mo = y + 1, 1
    return gaps


def plan_attachments(export_root, messages, photo_root=PHOTO_ROOT, photo_dir=PHOTO_DIR):
    """Для каждого вложения экспорта: (сообщение, поле, путь в экспорте, новый путь, копировать?)."""
    twins = {}
    root_dir = os.path.join(photo_root, photo_dir)
    for name in os.listdir(root_dir) if os.path.isdir(root_dir) else []:
        if "@" in name:
            twins.setdefault(name.split("@", 1)[1], []).append(name)
    out = []
    for m in messages:
        for field in ATTACHMENT_FIELDS:
            rel = m.get(field)
            if not rel or rel.startswith("("):
                continue
            src = os.path.join(export_root, rel)
            if not os.path.isfile(src):
                raise SystemExit(f"вложение из экспорта не найдено на диске: {src}")
            base = os.path.basename(rel)
            same_tail = [n for n in twins.get(base.split("@", 1)[1], [])] if "@" in base else []
            twin = next((n for n in same_tail if sha256_of(os.path.join(root_dir, n)) == sha256_of(src)), None)
            if twin:
                out.append((m, field, rel, f"{photo_dir}/{twin}", False))
                continue
            dst = os.path.join(root_dir, base)
            if os.path.exists(dst) and sha256_of(dst) != sha256_of(src):
                raise SystemExit(f"столкновение имени с другим содержимым: {dst}")
            out.append((m, field, rel, f"{photo_dir}/{base}", not os.path.exists(dst)))
    return out


def merge(archive, exports):
    """archive, exports — загруженные JSON. Возвращает (новые сообщения, статистика по экспортам)."""
    by_id = {m["id"]: m for m in archive["messages"]}
    stats = []
    for name, exp in exports:
        if str(exp.get("id")) != str(archive.get("id")):
            raise SystemExit(f"{name}: другой чат — id {exp.get('id')}, у архива {archive.get('id')}")
        if exp.get("name") != archive.get("name"):
            # Базовый экспорт 09-13 снят с темы «General» супергруппы, экспорты с 09-21 — с чата целиком: имя другое, id тот же
            print(f"  {name}: имя чата {exp.get('name')!r} при имени архива {archive.get('name')!r} — id совпал ({exp.get('id')}), продолжаю")
        new = changed = same = 0
        for m in exp["messages"]:
            old = by_id.get(m["id"])
            if old is None:
                new += 1
            elif old != m:
                changed += 1
            else:
                same += 1
            by_id[m["id"]] = m
        ids = [m["id"] for m in exp["messages"]]
        stats.append({"export": name, "messages": len(ids), "id_from": min(ids), "id_to": max(ids), "date_from": exp["messages"][0]["date"],
                      "date_to": exp["messages"][-1]["date"], "new": new, "overlap": changed + same, "changed": changed})
    messages = [by_id[i] for i in sorted(by_id)]
    return messages, stats


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+", help="корни экспортов (с result.json), в порядке от старого к новому")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--archive", default=ARCHIVE)
    args = ap.parse_args(argv)
    archive = json.load(open(args.archive))
    before = len(archive["messages"])
    exports = []
    for root in args.roots:
        root = os.path.expanduser(root.rstrip("/"))
        exports.append((os.path.basename(root), root, json.load(open(os.path.join(root, "result.json")))))
    messages, stats = merge(archive, [(n, e) for n, _r, e in exports])
    attachments = []
    for name, root, exp in exports:
        attachments.extend((name,) + a for a in plan_attachments(root, exp["messages"]))
    ids = [m["id"] for m in messages]
    print(f"архив: {before} сообщений, id до {max(m['id'] for m in archive['messages'])}, {archive['messages'][-1]['date']}")
    for s in stats:
        print(f"  {s['export']}: {s['messages']} сообщений, id {s['id_from']} … {s['id_to']}, {s['date_from']} … {s['date_to']}; "
              f"новых {s['new']}, пересечение {s['overlap']}, изменились {s['changed']}")
    gaps = month_gaps(messages)
    print(f"после слияния: {len(messages)} сообщений (+{len(messages) - before}), id {min(ids)} … {max(ids)}, повторов id {len(ids) - len(set(ids))}, "
          f"месяцев без сообщений: {gaps or 'нет'}")
    to_copy = [a for a in attachments if a[5]]
    reused = [a for a in attachments if not a[5]]
    print(f"вложений в экспортах: {len(attachments)}; скопировать в корень 09-13: {len(to_copy)}; уже есть в корне тем же содержимым: {len(reused)}")
    for name, m, field, rel, new_path, copy in attachments:
        print(f"  {name} id {m['id']} {field}: {rel} → {new_path}{'' if copy else '  (есть, не копирую)'}")
    if gaps or len(ids) != len(set(ids)):
        print("СТОП: проверка не пройдена, не пишу")
        return 1
    if not args.apply:
        print("без --apply ничего не пишу")
        return 0
    for name, m, field, rel, new_path, copy in attachments:
        if copy:
            root = next(r for n, r, _e in exports if n == name)
            shutil.copy2(os.path.join(root, rel), os.path.join(PHOTO_ROOT, new_path))
        m[field] = new_path
    shutil.copy2(args.archive, args.archive + ".bak")
    stamp = date.today().strftime("%Y%m%d")
    for name, _root, exp in exports:
        inc = os.path.join(os.path.dirname(args.archive), f"_increment_{stamp}_{name.replace('ChatExport_', '')}.json")
        json.dump(exp, open(inc, "w"), ensure_ascii=False, indent=1)
    archive["messages"] = messages
    json.dump(archive, open(args.archive, "w"), ensure_ascii=False, indent=1)
    check = json.load(open(args.archive))
    missing = [a for a in attachments if not os.path.isfile(os.path.join(PHOTO_ROOT, a[4]))]
    print(f"✅ записано: {len(check['messages'])} сообщений; резервная копия {args.archive}.bak; вложений не разрешилось: {len(missing)}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
