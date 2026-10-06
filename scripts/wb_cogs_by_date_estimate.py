#!/usr/bin/env python3
"""Себестоимость WB по дате продажи — оценка по файлам снимков 1С (пятнадцатая задача WB, §3). Только чтение.

    venv/bin/python3 scripts/wb_cogs_by_date_estimate.py --month-from 2026-04 --month-to 2026-09 [--files-dir …]

Что делает:
  1. читает шесть снимков 1С (TDSheet; колонки ключа WB в файлах названы по-разному — сопоставление по имени, SNAPSHOTS);
     на каждый снимок строит карты: «единая» по идентификатору WB (первая строка побеждает — как uniform модуля),
     точная по (идентификатор WB, размер), точная без размера, и «единая» по базе (до первого «-»);
  2. читает строки отчёта реализации за окно (как build_rows: rr_date d1 … window_end(d2), день строки — row_day, упаковка
     исключена) и на строках «Продажа» / «Возврат» считает СС = знак × цена × кол-во тремя способами:
       «было»     — снимок 20.05 из базы, ключ модуля (wbm.load_costs + wbm.unit_cost_for, mode base) — обязано сойтись
                    с «СС» месяцев модуля (report_finrez_wb.py --month-from … --month-to …);
       «20.05 WB» — файл 20.05, ключ WB (вариант по размеру → без размера → единая по идентификатору → единая по базе);
       «по дате»  — снимок, действующий на день продажи (последний со snapshot_date ≤ день), ключ WB;
  3. печатает по месяцам СС по трём способам и Δ (фин. рез. меняется ровно на −Δ: в форме O = СС без деления на НДС),
     покрытие ключей WB-строк окна в каждом снимке (строк / штук / ₽ по источнику ключа), медиану отношения
     «единой» снимка к 20.05 по общим идентификаторам. db_writes = 0, к WB API — 0. Деньги — Decimal.
"""
import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")
import cabinet  # noqa: E402
_PROFILE = cabinet.profile()   # каталоги данных и логов кабинета (MP_CABINET); guard — у загрузчика / точки входа, здесь только профиль

import pandas as pd  # noqa: E402

import loaders.wb_money_rules as rules  # noqa: E402
import loaders.wb_sales_report_loader as report_loader  # noqa: E402
import scripts.report_finrez_wb as fw  # noqa: E402
import scripts.report_wb_month as wbm  # noqa: E402

FILES_DIR = "/Users/mihaileliseev/Downloads/Telegram Desktop/ChatExport_2026-09-29 (2)/files"
# snapshot_date → (файл, колонки идентификаторов WB — основной F… и дискаунтер T…, колонка размера, колонка себестоимости
# строки, колонка «единой»). Коды WB в отчёте реализации бывают обоих кабинетов (буква t — Дискаунтер, WB-6), поэтому обе колонки.
SNAPSHOTS = {
    "2026-03-30": ("СС 30.03.26.xlsx", ("Идентификатор WB", "Идентификатор WB Дискаунтер"), "Размер", "Себестоимость Озон и ЛК ВБ", "Себестоимость ВБ"),
    "2026-04-06": ("СС 06.04.26.xlsx", ("Основной ВБ", "ВБ дискаунтер"), "Размер", "Себестоимость", "единая сс"),
    "2026-04-20": ("СС 20.04.26.xlsx", ("Идентификатор WB", " WB дискаунтерр"), "Размер", "Себестоимость", "единая сс для ВБ"),
    "2026-04-29": ("СС 29.04.26.xlsx", ("Идентификатор WB", "Идентификатор WB дискаунтер"), "Размер", "Себестоимость", "единая сс"),
    "2026-05-12": ("СС 12.05.26.xlsx", ("Идентификатор WB", "Дискаунтер WB"), "Размер", "Себестоимость", "Единая сс"),
    "2026-05-20": ("СС 20.05.26.xlsx", ("Идентификатор WB", "дискаунтер WB"), "Размер", "Себестоимость", "единая"),
}
OZON_COLS_2005 = ("Идентификатор Ozon", "селект Ozon", "дискаунтер Ozon")   # чем грузили 20.05 в базу (load_article_unit_costs)
Z = Decimal(0)
SOURCES = ("variant", "plain", "uniform", "base", "none")


def money(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().replace(" ", "").replace(",", ".")
    if not text or text.lower() == "nan":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        raise RuntimeError(f"не число в колонке себестоимости: {value!r}")


def norm_size(value):
    text = str(value if value is not None else "").strip().replace(",", ".")
    if not text or text.lower() == "nan" or text == "0":
        return ""
    try:
        d = Decimal(text)
    except InvalidOperation:
        return text.lower()
    return format(d.normalize(), "f")


def norm_code(value):
    text = str(value if value is not None else "").strip().lower()
    return "" if text in ("", "nan") else text


def load_snapshot(path, id_cols, size_col, cost_col, uniform_col):
    """Карты снимка по ключу WB (обе колонки идентификаторов — F… и T… — с одной себестоимостью строки).
    variant: (id, размер) → СС строки; plain: id → СС строки, если у id одна строка или строка без размера;
    uniform: id → «единая» (первая строка); base: база id → «единая» (первая строка)."""
    df = pd.read_excel(path, sheet_name="TDSheet", dtype=str)
    for col in tuple(id_cols) + (size_col, cost_col, uniform_col):
        if col not in df.columns:
            raise RuntimeError(f"{os.path.basename(path)}: нет колонки {col!r}; есть {list(df.columns)}")
    variant, plain, uniform, base, rows_per_id = {}, {}, {}, {}, defaultdict(int)
    n_rows = 0
    for _i, row in df.iterrows():
        codes = [c for c in (norm_code(row.get(col)) for col in id_cols) if c]
        if not codes:
            continue
        cost, uni = money(row.get(cost_col)), money(row.get(uniform_col))
        if cost is None and uni is None:
            continue
        n_rows += 1
        size = norm_size(row.get(size_col))
        for code in codes:
            rows_per_id[code] += 1
            if cost is not None:
                variant.setdefault((code, size), cost)
                if size == "":
                    plain.setdefault(code, cost)
            if uni is not None:
                uniform.setdefault(code, uni)
                base.setdefault(code.split("-", 1)[0], uni)
    for code, n in rows_per_id.items():
        if n == 1 and code not in plain:
            for (c, _s), cost in variant.items():
                if c == code:
                    plain[code] = cost
                    break
    return {"variant": variant, "plain": plain, "uniform": uniform, "base": base, "rows": n_rows, "ids": len(rows_per_id)}


def cost_wb(snap, vendor_code, tech_size):
    code = norm_code(vendor_code)
    if not code:
        return None, "none"
    size = norm_size(tech_size)
    if size:
        v = snap["variant"].get((code, size))
        if v is not None:
            return v, "variant"
    v = snap["plain"].get(code)
    if v is not None:
        return v, "plain"
    v = snap["uniform"].get(code)
    if v is not None:
        return v, "uniform"
    v = snap["base"].get(code.split("-", 1)[0])
    if v is not None:
        return v, "base"
    return None, "none"


def snapshot_for(day, dates):
    chosen = None
    for d in dates:
        if d <= day:
            chosen = d
    return chosen


def fmt(v):
    return f"{v:,.2f}".replace(",", " ")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--month-from", default="2026-04"); ap.add_argument("--month-to", default="2026-09")
    ap.add_argument("--date-to")
    ap.add_argument("--files-dir", default=FILES_DIR)
    ap.add_argument("--rows-cache", help="кэш строк продаж окна (json); по умолчанию logs/wb15_sale_rows_<от>_<до>.json")
    args = ap.parse_args(argv)
    d1, d2 = fw.month_bounds(args.month_from, args.month_to, args.date_to)
    dates = sorted(SNAPSHOTS)
    rows_cache = args.rows_cache or cabinet.logs_path(f"wb15_sale_rows_{d1}_{d2}.json", prof=_PROFILE)

    snaps = {}
    for sd in dates:
        name, id_cols, size_col, cost_col, uni_col = SNAPSHOTS[sd]
        snaps[sd] = load_snapshot(os.path.join(args.files_dir, name), id_cols, size_col, cost_col, uni_col)
        s = snaps[sd]
        print(f"снимок {sd} ({name}): строк с СС {s['rows']}, идентификаторов WB {s['ids']} (колонки {id_cols!r}), вариантов {len(s['variant'])}, "
              f"без размера {len(s['plain'])}, «единая» ({uni_col!r}) {len(s['uniform'])}, баз {len(s['base'])}", flush=True)

    sb = report_loader._client()
    exact_db, uniform_db = wbm.load_costs(sb)
    print(f"снимок {wbm.SNAP} из базы (ключ модуля): точных {len(exact_db)}, единых {len(uniform_db)}", flush=True)
    # Файл 20.05 = база? — те же Ozon-колонки, что у загрузчика
    df = pd.read_excel(os.path.join(args.files_dir, SNAPSHOTS["2026-05-20"][0]), sheet_name="TDSheet", dtype=str)
    file_exact = {}
    for _i, row in df.iterrows():
        cost = money(row.get("Себестоимость"))
        for col in OZON_COLS_2005:
            code = norm_code(row.get(col))
            if code and cost is not None:
                file_exact.setdefault(code, cost.quantize(Decimal("0.01")))
    same = sum(1 for k, v in file_exact.items() if exact_db.get(k) == v)
    print(f"файл 20.05 по Ozon-колонкам: ключей {len(file_exact)}; в базе таких {sum(1 for k in file_exact if k in exact_db)}, "
          f"с той же СС {same}; ключей базы нет в файле {sum(1 for k in exact_db if k not in file_exact)}", flush=True)

    if os.path.exists(rows_cache):
        sale_rows = json.load(open(rows_cache, encoding="utf-8"))
        print(f"строки продаж из кэша {rows_cache}: {len(sale_rows)}", flush=True)
    else:
        rows = fw.load_report_rows(sb, d1, d2, select=fw.BUILD_SELECT)
        products = fw.load_product_dictionary(sb, d1, d2)
        packaging_nm = fw.packaging_nm_ids(rows, products)
        print(f"строк отчёта {len(rows)} (rr_date {d1} … {wbm.window_end(d2)}), упаковка nmId {sorted(packaging_nm)}", flush=True)
        sale_rows = []
        for r in rows:
            day = wbm.row_day(r)
            if not (d1 <= day <= d2) or fw._is_packaging_row(r, packaging_nm):
                continue
            op = r["seller_oper_name"]
            sign = 1 if op == "Продажа" else (-1 if op == "Возврат" else 0)
            if sign:
                sale_rows.append({"day": day, "code": r.get("vendor_code"), "size": r.get("tech_size"), "qty": int(r.get("quantity") or 1),
                                  "price": str(fw.D(r["retail_price_with_disc"])), "sign": sign, "nm_id": r.get("nm_id")})
        with open(rows_cache, "w", encoding="utf-8") as f:
            json.dump(sale_rows, f, ensure_ascii=False)
        print(f"строки продаж окна записаны в кэш {rows_cache}: {len(sale_rows)}", flush=True)

    months = defaultdict(lambda: defaultdict(Decimal))
    cover_all = {sd: defaultdict(lambda: defaultdict(Decimal)) for sd in dates}       # снимок → источник → rows/qty/rub (все строки окна)
    cover_own = {sd: defaultdict(lambda: defaultdict(Decimal)) for sd in dates}       # снимок → источник → … (строки его интервала)
    cover_db = defaultdict(lambda: defaultdict(Decimal))
    keys_seen = defaultdict(set)
    n_sales = 0
    none_codes = defaultdict(lambda: defaultdict(Decimal))
    for r in sale_rows:
        day, sign = r["day"], r["sign"]
        n_sales += 1
        price, qty = Decimal(r["price"]), int(r["qty"])
        code, size = r.get("code"), r.get("size")
        m = months[day[:7]]
        m["rows"] += 1; m["qty"] += sign * qty; m["rub"] += sign * price
        c_db, src_db = wbm.unit_cost_for(exact_db, uniform_db, code, size, "base")
        cover_db[src_db]["rows"] += 1; cover_db[src_db]["qty"] += qty; cover_db[src_db]["rub"] += sign * price
        if c_db is not None:
            m["cogs_db"] += sign * c_db * qty
        else:
            m["no_cost_db_qty"] += qty
        c_2005, _ = cost_wb(snaps["2026-05-20"], code, size)
        if c_2005 is not None:
            m["cogs_2005_wb"] += sign * c_2005 * qty
        else:
            m["no_cost_2005_wb_qty"] += qty
        sd = snapshot_for(day, dates)
        c_dt, src_dt = cost_wb(snaps[sd], code, size)
        if c_dt is not None:
            m["cogs_by_date"] += sign * c_dt * qty
        else:
            m["no_cost_by_date_qty"] += qty
            nc = none_codes[norm_code(code)]
            nc["qty"] += qty; nc["rub"] += sign * price; nc["db_" + src_db] += qty
        m[f"src_{src_dt}"] += qty
        for s in dates:
            _c, src = cost_wb(snaps[s], code, size)
            cover_all[s][src]["rows"] += 1; cover_all[s][src]["qty"] += qty; cover_all[s][src]["rub"] += sign * price
            if s == sd:
                cover_own[s][src]["rows"] += 1; cover_own[s][src]["qty"] += qty; cover_own[s][src]["rub"] += sign * price
        keys_seen[norm_code(code)].add(norm_size(size))

    print(f"\nстрок «Продажа»/«Возврат» в окне {d1} … {d2} (без упаковки): {n_sales}, идентификаторов WB {len(keys_seen)}")
    print("\nСС по месяцам (Σ знак × СС × кол-во), фин. рез. меняется на −Δ:")
    print("    месяц | строк | шт нетто | оборот | СС было (база 20.05, ключ модуля) | без СС шт | СС 20.05 ключ WB | без СС шт | "
          "СС по дате ключ WB | без СС шт | Δ по дате − было | Δ 20.05 WB − было | шт по источнику ключа (по дате)")
    tot = defaultdict(Decimal)
    for mo in sorted(months):
        m = months[mo]
        for k in ("rows", "qty", "rub", "cogs_db", "no_cost_db_qty", "cogs_2005_wb", "no_cost_2005_wb_qty", "cogs_by_date", "no_cost_by_date_qty"):
            tot[k] += m[k]
        srcs = ", ".join(f"{s} {int(m[f'src_{s}'])}" for s in SOURCES if m[f"src_{s}"])
        print(f"    {mo} | {int(m['rows'])} | {int(m['qty'])} | {fmt(m['rub'])} | {fmt(m['cogs_db'])} | {int(m['no_cost_db_qty'])} | "
              f"{fmt(m['cogs_2005_wb'])} | {int(m['no_cost_2005_wb_qty'])} | {fmt(m['cogs_by_date'])} | {int(m['no_cost_by_date_qty'])} | "
              f"{fmt(m['cogs_by_date'] - m['cogs_db'])} | {fmt(m['cogs_2005_wb'] - m['cogs_db'])} | {srcs}")
    print(f"    итого | {int(tot['rows'])} | {int(tot['qty'])} | {fmt(tot['rub'])} | {fmt(tot['cogs_db'])} | {int(tot['no_cost_db_qty'])} | "
          f"{fmt(tot['cogs_2005_wb'])} | {int(tot['no_cost_2005_wb_qty'])} | {fmt(tot['cogs_by_date'])} | {int(tot['no_cost_by_date_qty'])} | "
          f"{fmt(tot['cogs_by_date'] - tot['cogs_db'])} | {fmt(tot['cogs_2005_wb'] - tot['cogs_db'])}")

    print("\nпокрытие ключом модуля (база 20.05): " + ", ".join(
        f"{s}: строк {int(cover_db[s]['rows'])}, шт {int(cover_db[s]['qty'])}, ₽ {fmt(cover_db[s]['rub'])}" for s in SOURCES if cover_db[s]["rows"]))
    total_rub = sum((cover_db[s]["rub"] for s in SOURCES), Z)
    total_qty = sum((cover_db[s]["qty"] for s in SOURCES), Z)
    print("\nпокрытие ключей WB-строк окна каждым снимком (все строки окна): снимок | источник: строк / шт / ₽ | без СС: шт, ₽, доля ₽")
    for s in dates:
        parts = "; ".join(f"{src} {int(cover_all[s][src]['rows'])} / {int(cover_all[s][src]['qty'])} / {fmt(cover_all[s][src]['rub'])}"
                          for src in SOURCES if cover_all[s][src]["rows"])
        none_rub, none_qty = cover_all[s]["none"]["rub"], cover_all[s]["none"]["qty"]
        print(f"    {s} | {parts} | без СС: {int(none_qty)} шт, {fmt(none_rub)} ₽, {(none_rub / total_rub if total_rub else Z):.2%}")
    print("\nто же — только строки интервала действия снимка (по дате): снимок | интервал | строк | без СС шт / ₽ / доля ₽ интервала | источники (шт)")
    for i, s in enumerate(dates):
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        own = cover_own[s]
        rows_n = sum((own[src]["rows"] for src in SOURCES), Z)
        rub_n = sum((own[src]["rub"] for src in SOURCES), Z)
        srcs = ", ".join(f"{src} {int(own[src]['qty'])}" for src in SOURCES if own[src]["rows"])
        interval = f"{max(s, d1)} … {(date.fromisoformat(nxt) - __import__('datetime').timedelta(days=1)).isoformat() if nxt else d2}"
        print(f"    {s} | {interval} | {int(rows_n)} | {int(own['none']['qty'])} / {fmt(own['none']['rub'])} / "
              f"{(own['none']['rub'] / rub_n if rub_n else Z):.2%} | {srcs}")

    print("\nотношение «единой» снимка к 20.05 по общим идентификаторам WB (медиана; в скобках — по идентификаторам из окна продаж):")
    u2005 = snaps["2026-05-20"]["uniform"]
    for s in dates:
        u = snaps[s]["uniform"]
        common = [k for k in u if k in u2005 and u2005[k] and u[k]]
        ratios = [u[k] / u2005[k] for k in common]
        common_w = [k for k in common if k in keys_seen]
        ratios_w = [u[k] / u2005[k] for k in common_w]
        med = statistics.median(ratios) if ratios else None
        med_w = statistics.median(ratios_w) if ratios_w else None
        print(f"    {s}: общих {len(common)}, медиана {med:.4f}" + (f" (окно: {len(common_w)}, {med_w:.4f})" if med_w is not None else ""))
    top = sorted(none_codes.items(), key=lambda kv: -abs(kv[1]["rub"]))[:20]
    print("\nбез СС по дате (ключ WB) — 20 кодов по ₽: код | шт | ₽ | у ключа модуля (шт по источнику) | код или база среди идентификаторов файла 20.05 (F/T)")
    for code, v in top:
        srcs = ", ".join(f"{k[3:]} {int(v[k])}" for k in v if k.startswith("db_"))
        in_file = "код" if code in snaps["2026-05-20"]["uniform"] else ("база" if code.split("-", 1)[0] in snaps["2026-05-20"]["base"] else "нет")
        print(f"    {code} | {int(v['qty'])} | {fmt(v['rub'])} | {srcs} | {in_file}")
    print(f"\nключ модуля сейчас: vendor_code строчными без размера → точная строка снимка offer_id_norm == код, иначе «единая» по базе "
          f"(до первого «-») — суффикс WB вида «-жм» в базу не входит, поэтому такие коды находят только точную строку без размера; "
          f"кодов с суффиксом в окне {sum(1 for k in keys_seen if '-' in k)} из {len(keys_seen)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
