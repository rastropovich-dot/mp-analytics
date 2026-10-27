#!/usr/bin/env python3
"""Сверка справочника категорий РБХ (loaders/rbh_categories) с «Сводкой» черновика советника — только чтение.

    MP_CABINET=rbh1 venv/bin/python3 scripts/rbh_categories_check.py [--draft data/rbh_costs/rbh_categories_draft.xlsx] [--ozon-from 2026-08-23 --ozon-to 2026-10-26]

Выборка как в черновике: Ozon — все карточки ozon_products (тип карточки + название), выручка = Σ buyouts_amount_seller выкупов окна по SKU;
WB — все карточки wb_funnel_products_current (предмет + название) плюс nm_id из отчёта реализации без карточки (предмет — из отчёта, п. (5)),
выручка = Σ retail_amount «Продажа» − «Возврат» отчёта реализации по nm_id (вся таблица). Печатает: нерешённых SKU по площадкам (обязано быть 0),
SKU и ₽ по группам и категориям против листа «Источники» черновика (кабинет × площадка × категория) с разницей в той же строке.
db_writes = 0, к API площадок не ходит.
"""
import argparse
import os
import sys
from collections import defaultdict
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
import cabinet  # noqa: E402
CABINET = cabinet.assert_env()
from loaders import keyset, rbh_categories as rc  # noqa: E402

D = lambda v: Decimal(str(v or 0))  # noqa: E731
DRAFT = cabinet.data_path("../rbh_costs/rbh_categories_draft.xlsx", prof=CABINET)   # общий каталог РБХ рядом с каталогом кабинета, как прайс


def paged(sb, table, select, order, filters=()):
    out, start = [], 0
    while True:
        q = sb.table(table).select(select)
        for f in filters:
            q = getattr(q, f[0])(*f[1:])
        for c in order:
            q = q.order(c)
        data = q.range(start, start + 999).execute().data or []
        out.extend(data)
        if len(data) < 1000:
            return out
        start += 1000


def ozon_items(sb, d1, d2):
    cards = paged(sb, "ozon_products", "sku,type_name,name", ["sku"])
    rev = defaultdict(Decimal)
    for r in paged(sb, "marketplace_buyouts", "id,buyout_date,marketplace_sku,buyouts_amount_seller", ["buyout_date", "id"],
                   [("eq", "marketplace_code", "ozon"), ("gte", "buyout_date", d1), ("lte", "buyout_date", d2)]):
        rev[str(r["marketplace_sku"])] += D(r["buyouts_amount_seller"])
    return [(str(c["sku"]), c.get("type_name"), c.get("name"), rev.get(str(c["sku"]), Decimal(0))) for c in cards]


def wb_items(sb, wb_to="2100-01-01"):
    cards = {str(c["nm_id"]): c for c in paged(sb, "wb_funnel_products_current", "nm_id,subject_name,title", ["nm_id"])}   # «day» — день последнего наблюдения, не появления
    rev, subj = defaultdict(Decimal), {}
    rows = keyset.read_keyset(sb, "wb_sales_report_rows", "rr_date,rrd_id,nm_id,seller_oper_name,retail_amount,subject_name", "2000-01-01", wb_to,
                              day_col="rr_date", id_col="rrd_id", filters=[("in_", "seller_oper_name", ["Продажа", "Возврат"])])
    for r in rows:
        nm = str(r.get("nm_id") or "")
        if not nm:
            continue
        rev[nm] += (1 if r["seller_oper_name"] == "Продажа" else -1) * D(r.get("retail_amount"))
        if r.get("subject_name"):
            subj.setdefault(nm, r["subject_name"])
    items = [(nm, c.get("subject_name"), c.get("title"), rev.get(nm, Decimal(0))) for nm, c in cards.items()]
    titles = {}
    missing = sorted(nm for nm in rev if nm not in cards)
    for i in range(0, len(missing), 100):                       # название сироты — из истории воронки (карточка была, в current её нет)
        for r in sb.table("wb_funnel_products_daily").select("nm_id,title,day").in_("nm_id", [int(x) for x in missing[i:i + 100]]).order("day", desc=True).limit(1000).execute().data or []:
            titles.setdefault(str(r["nm_id"]), r.get("title") or "")
    orphans = [(nm, subj.get(nm), titles.get(nm, ""), rev[nm]) for nm in missing]                # (5): сироты — предмет из отчёта
    return items + orphans, len(orphans)


def draft_sources(path, cab):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = defaultdict(lambda: [0, Decimal(0)])
    for r in list(wb["Источники"].iter_rows(values_only=True))[1:]:
        if r[0] is None or r[2] != cab:
            continue
        x = out[(r[3], r[1])]
        x[0] += int(r[5] or 0); x[1] += D(r[6])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", default=DRAFT)
    ap.add_argument("--ozon-from", default="2026-08-23"); ap.add_argument("--ozon-to", default="2026-10-26")
    ap.add_argument("--wb-to", default="2100-01-01", help="отчёт реализации по rr_date — не позже (срез черновика)")
    args = ap.parse_args(argv)
    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    cab = os.environ.get("MP_CABINET", "")
    wb_list, orphans = wb_items(sb, args.wb_to)
    ours = defaultdict(lambda: [0, Decimal(0)])
    unresolved = defaultdict(list)
    paths = defaultdict(int)
    for mp, items in (("Ozon", ozon_items(sb, args.ozon_from, args.ozon_to)), ("WB", wb_list)):
        for sku, src, name, rev in items:
            g, c, how = rc.classify(src, name)
            paths[(mp, how)] += 1
            if c is None:
                unresolved[mp].append((sku, src, (name or "")[:60]))
                c = rc.UNRESOLVED
            x = ours[(mp, c)]
            x[0] += 1; x[1] += rev
    print(f"кабинет {cab}: WB-сирот (nm_id в отчёте без карточки) {orphans}; пути: " + ", ".join(f"{mp} {how} {n}" for (mp, how), n in sorted(paths.items())))
    for mp in ("Ozon", "WB"):
        print(f"нерешённых {mp}: {len(unresolved[mp])}" + (f" — {unresolved[mp][:10]}" if unresolved[mp] else ""))
    draft = draft_sources(args.draft, cab) if os.path.exists(args.draft) else {}
    for mp in ("Ozon", "WB"):
        o_n = sum(v[0] for (m, _c), v in ours.items() if m == mp); o_r = sum((v[1] for (m, _c), v in ours.items() if m == mp), Decimal(0))
        d_n = sum(v[0] for (m, _c), v in draft.items() if m == mp); d_r = sum((v[1] for (m, _c), v in draft.items() if m == mp), Decimal(0))
        print(f"{mp}: наши SKU {o_n} / ₽ {o_r:,.2f}; черновик {d_n} / {d_r:,.2f}; разница SKU {o_n - d_n:+}, ₽ {o_r - d_r:+,.2f}")
        diffs = []
        for c in sorted({c for (m, c) in ours if m == mp} | {c for (m, c) in draft if m == mp}):
            a, b = ours.get((mp, c), [0, Decimal(0)]), draft.get((mp, c), [0, Decimal(0)])
            if a[0] != b[0] or abs(a[1] - b[1]) >= 1:
                diffs.append((c, a[0] - b[0], a[1] - b[1]))
        print(f"  категорий с разницей (SKU ≠ или ₽ ≥ 1): {len(diffs)}" + "".join(f"\n    {c}: SKU {dn:+}, ₽ {dr:+,.2f}" for c, dn, dr in diffs[:40]))
    groups = defaultdict(lambda: [0, Decimal(0)])
    for (mp, c), v in ours.items():
        g = rc.CAT2G.get(c, rc.UNRESOLVED)
        groups[(g, mp)][0] += v[0]; groups[(g, mp)][1] += v[1]
    print("группы (SKU / ₽): " + "; ".join(f"{g} {mp} {v[0]} / {v[1]:,.0f}" for (g, mp), v in sorted(groups.items())))
    print("db_writes = 0")


if __name__ == "__main__":
    main()
