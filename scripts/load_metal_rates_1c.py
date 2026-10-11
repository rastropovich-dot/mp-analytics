#!/usr/bin/env python3
"""Курс 1С из чата «LL Курсы» → таблица metal_rates_1c (сорок восьмая §1). Без --apply ничего не пишет (db_writes = 0), печатает план.

    venv/bin/python3 scripts/load_metal_rates_1c.py                 план: строк по металлам, первая / последняя установка, правки задним числом
    venv/bin/python3 scripts/load_metal_rates_1c.py --apply         запись (upsert по ключу metal, set_date) — по слову владельца
    venv/bin/python3 scripts/load_metal_rates_1c.py --check         таблица против csv: чего нет в базе, что отличается

Вход — data/ll_rates/ll_rates_1c_history.csv (scripts/ll_rates_ocr.py, сорок седьмая §1: rate, set_date, value_1c, first_seen_img) и,
если лежит рядом, data/ll_rates/ll_rates.csv (все картинки — из него seen_to, последняя картинка, где курс виден). csv нет — пересобрать:
`scripts/ll_rates_ocr.py --export "<папка ChatExport_…>"`. Грузятся gold → gold585, silver → silver925, usd → usd; kzt и scrap в таблицу не идут.
Две строки одной даты установки (правка задним числом, доллар 20.05.2026 75 → 74) — последняя по картинке, прежняя — в примечании плана.

Обновление дальше: владелец снимает экспорт чата раз в неделю → `ll_rates_ocr.py --export <папка> --since <дата>` дописывает csv →
этот скрипт --apply по слову. Таблицу создаёт sql/20261008_create_metal_rates_1c.sql (по слову через MCP).
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
import cabinet  # noqa: E402
from loaders import unit_cost_history as uch  # noqa: E402

_PROFILE = cabinet.profile()   # каталоги кабинета; guard assert_env — перед созданием клиента (make_client)
TABLE = uch.RATES_TABLE
BATCH = 200


def plan_rows(history_csv, pictures_csv=None):
    """Строки к записи (metal, set_date, rate, source, seen_from, seen_to) + replaced для примечания; csv-строк по металлам — в raw_counts."""
    rows = uch.rate_rows_from_csv(history_csv, pictures_csv if pictures_csv and os.path.exists(pictures_csv) else None)
    raw = Counter()
    import csv
    with open(history_csv, newline="") as fh:
        for r in csv.DictReader(fh):
            raw[r["rate"]] += 1
    return rows, raw


def plan_text(rows, raw, pictures_csv=None):
    by = defaultdict(list)
    for r in rows:
        by[r["metal"]].append(r)
    lines = [f"строк csv по курсам: " + ", ".join(f"{k} {v}" for k, v in sorted(raw.items())) + " (kzt, scrap не грузятся)",
             "к записи в metal_rates_1c (ключ metal, set_date):"]
    for metal in sorted(by):
        rs = by[metal]
        dup = [r for r in rs if r["replaced"]]
        lines.append(f"  {uch.METAL_TEXT.get(metal, metal):12} {metal:10} строк {len(rs):3}, установки {rs[0]['set_date']} … {rs[-1]['set_date']}, "
                     f"первый {rs[0]['rate']:,.2f}, последний {rs[-1]['rate']:,.2f} (виден {rs[-1]['seen_from']} … {rs[-1]['seen_to']}); "
                     f"дат с двумя курсами {len(dup)}")
        for r in dup:
            prev = ", ".join(f"{v:,.2f} (картинка {s})" for v, s in r["replaced"])
            same = all(v == r["rate"] for v, _s in r["replaced"])
            lines.append(f"      {r['set_date']}: пишется {r['rate']:,.2f} (картинка {r['seen_from']}), прежний {prev} — "
                         + ("тот же курс на двух картинках (дребезг даты у бота), пишется одна строка" if same else "правка задним числом, в таблицу не идёт"))
    last = max((r["seen_to"] for r in rows), default=None)
    lines.append(f"последняя картинка (seen_to действующих курсов): {last}" + ("" if pictures_csv else " — ll_rates.csv нет, seen_to = первая картинка установки"))
    return "\n".join(lines)


def to_db(rows):
    return [{"metal": r["metal"], "set_date": r["set_date"], "rate": str(r["rate"]), "source": r["source"],
             "seen_from": r["seen_from"], "seen_to": r["seen_to"]} for r in rows]


def make_client():
    from supabase import create_client
    cabinet.assert_env()  # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def read_table(sb):
    rates = uch.load_rates(sb)
    out = {}
    for metal, (ds, vs) in rates.series.items():
        for d, v in zip(ds, vs):
            out[(metal, d)] = v
    return out, rates


def compare(rows, table):
    """(нет в базе, отличаются, лишние в базе) по ключу (metal, set_date)."""
    plan = {(r["metal"], r["set_date"]): r["rate"] for r in rows}
    missing = sorted(k for k in plan if k not in table)
    differ = sorted((k, table[k], plan[k]) for k in plan if k in table and table[k] != plan[k])
    extra = sorted(k for k in table if k not in plan)
    return missing, differ, extra


def apply(sb, rows):
    payload = to_db(rows)
    written = 0
    for i in range(0, len(payload), BATCH):
        sb.table(TABLE).upsert(payload[i:i + BATCH], on_conflict="metal,set_date").execute()
        written += len(payload[i:i + BATCH])
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--history", default=cabinet.data_path("ll_rates", "ll_rates_1c_history.csv", prof=_PROFILE))
    ap.add_argument("--pictures", default=cabinet.data_path("ll_rates", "ll_rates.csv", prof=_PROFILE), help="ll_rates.csv для seen_to; нет файла — без него")
    ap.add_argument("--apply", action="store_true", help="писать в БД; без флага — только план, db_writes = 0")
    ap.add_argument("--check", action="store_true", help="сверить таблицу с csv (только чтение)")
    args = ap.parse_args(argv)
    if not os.path.exists(args.history):
        raise SystemExit(f"нет {args.history} — пересобрать scripts/ll_rates_ocr.py --export <папка ChatExport_…>")
    pictures = args.pictures if os.path.exists(args.pictures) else None
    rows, raw = plan_rows(args.history, pictures)
    print(plan_text(rows, raw, pictures))
    if not args.apply and not args.check:
        print("\nрежим плана: db_writes = 0. Для записи добавить --apply (по слову владельца).")
        return 0
    sb = make_client()
    if args.check:
        table, rates = read_table(sb)
        missing, differ, extra = compare(rows, table)
        print(f"\nтаблица: строк {len(table)}; {rates.describe()}")
        print(f"нет в базе: {len(missing)}" + (" — " + ", ".join(f"{m} {d}" for m, d in missing[:20]) if missing else ""))
        print(f"отличаются: {len(differ)}" + (" — " + ", ".join(f"{m} {d}: база {a} csv {b}" for (m, d), a, b in differ[:20]) if differ else ""))
        print(f"лишние в базе: {len(extra)}" + (" — " + ", ".join(f"{m} {d}" for m, d in extra[:20]) if extra else ""))
        print("db_writes = 0")
        return 0 if not (missing or differ or extra) else 1
    written = apply(sb, rows)
    table, rates = read_table(sb)
    missing, differ, extra = compare(rows, table)
    print(f"\nзаписано upsert: {written}; в таблице после записи {len(table)} строк; нет в базе {len(missing)}, отличаются {len(differ)}, лишних {len(extra)}")
    print(rates.describe())
    return 0 if not (missing or differ) else 1


if __name__ == "__main__":
    sys.exit(main())
