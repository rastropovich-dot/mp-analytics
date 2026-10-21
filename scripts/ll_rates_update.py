#!/usr/bin/env python3
"""Курс 1С из экспорта чата «LL Курсы» одной командой (сорок девятая, ручной путь по решению владельца 2026-10-11: от бота в группе отказались).

    venv/bin/python3 scripts/ll_rates_update.py --export "~/Downloads/Telegram Desktop/ChatExport_2026-10-13"           план: ничего не пишет
    venv/bin/python3 scripts/ll_rates_update.py --export … --apply                                                       запись — по слову владельца

Что делает. since берёт сам — дата последней картинки в data/ll_rates/ll_rates.csv (--since DATE — задать руками); из result.json экспорта берёт фото
с датой сообщения ≥ since; картинки, чей файл уже есть в csv, пропускает (повтор); новые гонит через Vision (scripts/ll_rates_ocr.py, кэш OCR
data/ll_rates/ocr/); печатает, что распозналось, и план: строк в csv (+N), новые установки курса 1С, строки таблицы metal_rates_1c к upsert (новая
установка или сдвиг seen_to / seen_from), сверку «нет / отличаются / лишние», рабочие дни без картинки. Без --apply всё это считается на
временных копиях csv и истории, таблица только читается — db_writes 0, файлы не тронуты. С --apply: строки дописываются в ll_rates.csv, история
ll_rates_1c_history.csv пересобирается из всего csv, в таблицу идёт upsert только изменившихся строк (обычно 1 … 3: seen_to действующих
установок; новая установка — +1), затем compare → 0 / 0 / 0, иначе код 1.

Провал формул на картинке: строка пишется в csv с пометкой, в историю и в seen_to не идёт, таблица по этой картинке не меняется, в лог
«ПРОВЕРИТЬ ГЛАЗАМИ <файл>», код 1. Картинки не бота (скриншоты людей, ширина < 600) перечисляются и пропускаются. Экспорт старее csv
(фото с датой ≥ since нет) — «новых картинок нет», код 0. Seller / Performance / Render — 0; запись в базу — только metal_rates_1c (upsert).
"""
import argparse
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
import cabinet  # noqa: E402
import ll_rates_ocr as ocr  # noqa: E402
import load_metal_rates_1c as rates_loader  # noqa: E402

_PROFILE = cabinet.profile()
MSK = ZoneInfo("Europe/Moscow")


def default_paths(prof=None, data_dir=None):
    d = os.path.abspath(data_dir) if data_dir else cabinet.data_path("ll_rates", prof=prof or _PROFILE)
    return {"dir": d, "csv": os.path.join(d, "ll_rates.csv"), "history": os.path.join(d, "ll_rates_1c_history.csv"), "ocr": os.path.join(d, "ocr")}


def last_picture_date(csv_path):
    return max((x["img_date"] for x in ocr.read_rows(csv_path) if x.get("img_date")), default=None)


def workdays_between(after, before):
    """Рабочие дни (пн–пт) d: after < d ≤ before, ISO-строки."""
    if not after or not before:
        return []
    a, b = date.fromisoformat(after), date.fromisoformat(before)
    out, d = [], a + timedelta(days=1)
    while d <= b:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def lost_days(pictures_dates, last_before, today):
    """Рабочие дни без картинки между последней картинкой до прогона и вчера (сегодняшняя приходит ~08:50–09:00 МСК). Праздник РФ и день,
    когда бот ЦУП молчал, от пропуска неотличимы — «без картинки (потеря или праздник)»."""
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    have = set(pictures_dates)
    return [d for d in workdays_between(last_before, yesterday) if d not in have]


def select_photos(export_dir, since):
    """(chat, все фото, фото с датой сообщения ≥ since, файлы на диске, отсутствующие)."""
    chat, msgs, photos = ocr.load_export(export_dir)
    picked = [m for m in photos if since is None or m["date"][:10] >= since]
    files, absent = [], []
    for m in picked:
        p = os.path.join(export_dir, m["photo"])
        (files if os.path.exists(p) else absent).append(p if os.path.exists(p) else m["photo"])
    return chat, msgs, photos, picked, files, absent


class SupabaseDB:
    def __init__(self):
        from supabase import create_client
        cabinet.assert_env()
        self.sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
        self.writes = 0

    def read_table_rows(self):
        return rates_loader.read_table_rows(self.sb)

    def upsert_rates(self, rows):
        n = rates_loader.apply(self.sb, rows) if rows else 0
        self.writes += 1 if rows else 0
        return n


def run(args, paths, db, log=print, today=None, recognize=None):
    """Весь прогон. db — SupabaseDB или заглушка (None — базу не трогать); recognize(files) → (results, skipped) — по умолчанию Vision."""
    today = today or datetime.now(MSK).date().isoformat()
    export = os.path.expanduser(args.export)
    mode = "apply" if args.apply else "план"
    log(cabinet.banner(_PROFILE))
    since = args.since or last_picture_date(paths["csv"])
    chat, msgs, photos, picked, files, absent = select_photos(export, since)
    log(f"ll_rates_update: {mode}; экспорт «{chat.get('name')}»: сообщений {len(msgs)}, с фото {len(photos)}, {msgs[0]['date'][:10]} … {msgs[-1]['date'][:10]}; "
        f"since {since or 'нет (csv пуст — берутся все)'} ({'задан руками' if args.since else 'последняя картинка csv'}): фото {len(picked)}")
    if absent:
        log(f"фото из result.json нет на диске: {len(absent)} — {absent[:5]}")
    have = ocr.existing_files(paths["csv"])
    new_files = [f for f in files if os.path.basename(f) not in have]
    dup = len(files) - len(new_files)
    log(f"картинок к разбору: {len(new_files)} новых, {dup} уже в csv (повтор)")
    rc, failed = 0, []
    if new_files:
        if recognize is None:
            exes = ocr.ensure_binaries(ocr.OUT_DIR)
            os.makedirs(paths["ocr"], exist_ok=True)
            recs = ocr.recognize_files(new_files, exes, paths["ocr"], reocr=args.reocr, log=log)
            results, skipped = ocr.parse_pictures(new_files, recs, exes, paths["ocr"])
        else:
            results, skipped = recognize(new_files)
        for name, why in skipped:
            log(f"  пропуск: {name} — {why}")
        for o in results:
            log("  распознано: " + ocr.row_summary(o))
            if o.get("check"):
                log(f"  ПРОВЕРИТЬ ГЛАЗАМИ {o['file']}: формулы не сошлись — {o['check']}; строка пишется с пометкой, в историю и таблицу не идёт")
                failed.append(o["file"])
                rc = 1
    else:
        results, skipped = [], []
    good = [o for o in results if not o.get("check")]

    # ---- csv + история (в плане — на временных копиях)
    last_before = last_picture_date(paths["csv"])
    all_dates = {x["img_date"] for x in ocr.read_rows(paths["csv"]) if x.get("img_date")} | {o["img_date"] for o in results if o.get("img_date")}
    last_picture = max(all_dates, default=None)
    lost = lost_days(all_dates, last_before, today)
    if args.apply:
        csv_path, hist_path = paths["csv"], paths["history"]
    else:
        tmpd = tempfile.mkdtemp(prefix="ll_rates_plan_")
        csv_path, hist_path = os.path.join(tmpd, "ll_rates.csv"), os.path.join(tmpd, "history.csv")
        for src, dst in ((paths["csv"], csv_path), (paths["history"], hist_path)):
            if os.path.exists(src):
                shutil.copyfile(src, dst)
    added, dup_csv = ocr.append_rows(csv_path, results)
    new_set, gone_set, n_hist = ocr.rebuild_history(csv_path, hist_path)
    verb = "дописано" if args.apply else "дописалось бы"
    log(f"csv: {verb} строк {added}, повторов {dup_csv}; история курса 1С: строк {n_hist}, новых установок {len(new_set)}"
        + (" — " + ", ".join(f"{r} {d} {v}" for r, d, v in new_set[:12]) if new_set else "") + (f"; пропавших {gone_set[:12]}" if gone_set else ""))
    stale = (date.fromisoformat(today) - date.fromisoformat(last_picture)).days if last_picture else None
    log(f"последняя картинка: {last_picture or 'нет'} (до прогона {last_before or 'нет'}); дней с неё: {stale if stale is not None else '—'}; "
        f"рабочих дней без картинки (потеря или праздник): {len(lost)}" + (f" — {', '.join(lost)}" if lost else ""))

    # ---- таблица
    rates_changed, cmp = 0, None
    if not os.path.exists(hist_path):
        log("истории курса нет — таблица не сверяется")
    elif db is None:
        rows, _ = rates_loader.plan_rows(hist_path, csv_path)
        log(f"таблица: базу не трогаю (--no-db); строк плана {len(rows)}")
    else:
        rows, _ = rates_loader.plan_rows(hist_path, csv_path)
        table = db.read_table_rows()
        changed = rates_loader.changed_rows(rows, table)
        log(f"таблица metal_rates_1c: строк {len(table)}, план {len(rows)}, к upsert {len(changed)}"
            + (" — " + ", ".join(f"{r['metal']} {r['set_date']} {r['rate']:,.2f} виден {r['seen_from']} … {r['seen_to']}" for r in changed) if changed else ""))
        missing, differ, extra = rates_loader.compare(rows, {k: v["rate"] for k, v in table.items()})
        if args.apply:
            if failed and not good:
                log("таблица не меняется: все новые картинки с провалом формул")
            else:
                rates_changed = db.upsert_rates(changed)
                table = db.read_table_rows()
                left = rates_loader.changed_rows(rows, table)
                missing, differ, extra = rates_loader.compare(rows, {k: v["rate"] for k, v in table.items()})
                cmp = (len(missing), len(differ), len(extra))
                log(f"upsert: {rates_changed} строк; после записи: нет в базе {cmp[0]} / отличаются {cmp[1]} / лишние {cmp[2]}; не совпали по seen_from / seen_to: {len(left)}")
                if any(cmp) or left:
                    rc = 1
        else:
            log(f"сверка по курсу (до записи): нет в базе {len(missing)} / отличаются {len(differ)} / лишние {len(extra)}")
    if args.apply:
        log(f"записано: {paths['csv']}, {paths['history']}; db_writes = {getattr(db, 'writes', 0) if db is not None else 0}; код {rc}")
    else:
        log("план: csv, история и таблица не тронуты; db_writes = 0. Для записи добавить --apply (по слову владельца)." + (f" Код {rc}." if rc else ""))
    return rc, {"since": since, "photos": len(picked), "new": len(new_files), "duplicate": dup, "added": added, "check_failed": failed,
                "new_settings": new_set, "last_picture": last_picture, "lost_days": lost, "rates_changed": rates_changed, "compare": cmp, "rc": rc}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", required=True, help="корень экспорта чата «LL Курсы» (result.json + photos/)")
    ap.add_argument("--since", help="YYYY-MM-DD вместо даты последней картинки csv")
    ap.add_argument("--apply", action="store_true", help="писать csv / историю / таблицу; без флага — план, db_writes 0")
    ap.add_argument("--no-db", action="store_true", help="не ходить в базу (план по csv)")
    ap.add_argument("--reocr", action="store_true", help="не брать кэш OCR, распознать заново")
    ap.add_argument("--data-dir", help="каталог вместо data/ll_rates кабинета (стенд)")
    ap.add_argument("--today", help="YYYY-MM-DD вместо сегодняшней даты МСК (стенд)")
    args = ap.parse_args(argv)
    paths = default_paths(data_dir=args.data_dir)
    os.makedirs(paths["dir"], exist_ok=True)
    db = None if args.no_db else SupabaseDB()
    rc, _ = run(args, paths, db, today=args.today)
    return rc


if __name__ == "__main__":
    sys.exit(main())
