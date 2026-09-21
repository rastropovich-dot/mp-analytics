#!/usr/bin/env python3
"""Сверка ручного отчёта владельца (лист «Ozon - <месяц>») с нашими данными по дням. Только чтение.

Их колонки и формулы (разобраны в восьмой задаче, 2026-09-14): оборот —
выкупы; комиссия с НДС; выручка = (оборот − комиссия) / 1,22; себестоимость;
маржа = выручка − СС; логистика; реклама; эквайринг; прочее;
фин. рез. = маржа − логистика − реклама − эквайринг − прочее.

Наши источники: marketplace_buyouts (оборот, комиссия, позиции),
marketplace_expenses (логистика, прочее, подписка, реклама Performance),
article_unit_costs × (sku → article из marketplace_orders) — себестоимость
по позициям; сырьё by-day (data/accrual_history/) — суммы по type_id
(эквайринг = тип 1, реклама accrual = 41 + 54); accrual/postings
(data/accrual_postings/) — штуки → себестоимость по штукам и логистика
по дате реализации отправления.

    venv/bin/python3 scripts/reconcile_manual_report_ozon.py --manual-json <строки листа> \
        --date-from 2026-09-01 --date-to 2026-09-15 --postings data/accrual_postings/2026-08-31_2026-09-15.json

Лист читается либо напрямую (--xlsx data/manual_report_september.xlsx --sheet "Ozon - сентябрь",
колонки B…S листа: оборот, комиссия, %, выручка, СС, маржа, %, логистика, %, реклама, %ДРР,
эквайринг, %, прочее, фин. рез., %, EBITDA, %), либо из JSON (--manual-json), снятого тем же
чтением: {"YYYY-MM-DD": {"turnover": "…", "commission": "…", …}} — значения ячеек как repr float.
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from loaders import ozon_finance_accrual as accrual  # noqa: E402
from check_accrual_postings_quantity import D, load_byday, match_day, sale_index  # noqa: E402

VAT = Decimal("1.22")
C = Decimal("0.01")
SNAP = "2026-05-20"
LOG_TYPES = {t for t, a in accrual.TYPE_TO_EXPENSE.items() if a == "logistics"}
ACQUIRING_TYPE = 1


def q(v):
    return Decimal(v).quantize(C)


def fetch(sb, table, select, filters, order):
    out, page = [], 0
    while True:
        qb = sb.table(table).select(select)
        for f in filters:
            qb = getattr(qb, f[0])(*f[1:])
        res = qb.order(order).range(page * 1000, page * 1000 + 999).execute()
        out.extend(res.data)
        if len(res.data) < 1000:
            break
        page += 1
    return out


def type_sums(accruals):
    """type_id -> расход (положителен при списании) по строкам услуг одного дня."""
    out = defaultdict(Decimal)
    for a in accruals:
        for type_id, _sku, amount in accrual._service_lines(a):
            out[type_id] += -D(amount)
    return out


MANUAL_COLUMNS = ["turnover", "commission", "commission_pct", "revenue", "cogs", "margin", "margin_pct",
                  "logistics", "logistics_pct", "ads", "drr_pct", "acquiring", "acquiring_pct", "other",
                  "fin_result", "fin_result_pct", "ebitda", "ebitda_pct"]


def read_manual_sheet(path, sheet):
    """Строки листа по датам: колонка A — дата реализации, B…S — значения. Файл большой, читаем read_only."""
    import datetime
    import warnings
    import openpyxl
    warnings.simplefilter("ignore")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = {}
    for row in wb[sheet].iter_rows(min_row=4, max_row=60, values_only=True):
        d = row[0]
        if isinstance(d, datetime.datetime) and row[1] is not None:
            out[d.date().isoformat()] = {c: (repr(v) if v is not None else None) for c, v in zip(MANUAL_COLUMNS, row[1:19])}
    return out


def table(title, days, ours, theirs, note=""):
    print(f"\n=== {title}" + (f"  — {note}" if note else ""))
    print(f"{'день':12}{'у нас':>16}{'у них':>16}{'разница':>14}")
    t_o = t_t = Decimal(0)
    for d in days:
        o, t = ours.get(d), theirs.get(d)
        if o is None or t is None:
            print(f"{d:12}{'—' if o is None else f'{o:,.2f}':>16}{'—' if t is None else f'{t:,.2f}':>16}{'':>14}")
            continue
        t_o += o; t_t += t
        print(f"{d:12}{o:>16,.2f}{t:>16,.2f}{o - t:>14,.2f}")
    print(f"{'итого':12}{t_o:>16,.2f}{t_t:>16,.2f}{t_o - t_t:>14,.2f}")
    return t_o, t_t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manual-json", help="строки листа, снятые ранее (см. docstring)")
    ap.add_argument("--xlsx", help="файл ручного отчёта; читается лист --sheet, колонки B…S")
    ap.add_argument("--sheet", default="Ozon - сентябрь")
    ap.add_argument("--date-from", required=True)
    ap.add_argument("--date-to", required=True)
    ap.add_argument("--postings", help="файл accrual/postings, покрывающий окно (штуки, дата реализации)")
    ap.add_argument("--snapshot", default=SNAP)
    args = ap.parse_args()
    if args.xlsx:
        manual_raw = read_manual_sheet(args.xlsx, args.sheet)
    elif args.manual_json:
        manual_raw = json.load(open(args.manual_json))
    else:
        raise SystemExit("нужен --xlsx или --manual-json")
    manual = {d: {k: (q(v) if v is not None else None) for k, v in row.items()} for d, row in manual_raw.items() if d[:1] == "2"}
    days = []
    d, d_to = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
    while d <= d_to:
        days.append(d.isoformat()); d += timedelta(days=1)

    import loaders.ozon_fbo_orders_loader as fbo
    sb = fbo.supabase
    buy = fetch(sb, "marketplace_buyouts", "buyout_date,marketplace_sku,buyouts_qty,buyouts_amount_seller,commission_amount",
                [("eq", "marketplace_code", "ozon"), ("gte", "buyout_date", args.date_from), ("lte", "buyout_date", args.date_to)], "id")
    exp = fetch(sb, "marketplace_expenses", "expense_date,expense_type,expense_amount",
                [("eq", "marketplace_code", "ozon"), ("gte", "expense_date", args.date_from), ("lte", "expense_date", args.date_to)], "id")
    orders = fetch(sb, "marketplace_orders", "marketplace_sku,article,orders_qty",
                   [("eq", "marketplace_code", "ozon"), ("gte", "order_date", "2026-03-28")], "id")
    sku_art = defaultdict(lambda: defaultdict(Decimal))
    for r in orders:
        sku_art[str(r["marketplace_sku"])][str(r["article"] or "")] += D(r["orders_qty"] or 0)
    sku2art = {s: max(a.items(), key=lambda kv: kv[1])[0] for s, a in sku_art.items()}
    norms = sorted({a.lower() for a in sku2art.values() if a})
    cost = {}
    for i in range(0, len(norms), 500):
        for r in sb.table("article_unit_costs").select("offer_id_norm,unit_cost").eq("snapshot_date", args.snapshot).in_("offer_id_norm", norms[i:i + 500]).execute().data:
            cost[r["offer_id_norm"]] = D(r["unit_cost"])

    def unit_cost(sku):
        art = sku2art.get(str(sku))
        return cost.get(art.lower()) if art else None

    ours = defaultdict(lambda: defaultdict(Decimal))
    nocost = defaultdict(int)
    for r in buy:
        dd = r["buyout_date"]; qty = D(r["buyouts_qty"] or 0)
        ours[dd]["positions"] += qty
        ours[dd]["turnover"] += D(r["buyouts_amount_seller"] or 0)
        ours[dd]["commission"] += D(r["commission_amount"] or 0)
        uc = unit_cost(r["marketplace_sku"])
        if uc is None:
            nocost[dd] += int(qty)
        else:
            ours[dd]["cogs_pos"] += qty * uc
    for r in exp:
        dd, t, v = r["expense_date"], r["expense_type"], D(r["expense_amount"] or 0)
        if t.startswith("advertising"):
            ours[dd]["ads"] += v
        elif t == "commission":
            ours[dd]["commission_expenses"] += v  # та же sale_commission, что и в выкупах; не смешивать с ключом выкупов
        else:
            ours[dd][t] += v

    # by-day raw: суммы по type_id
    raw_days = set()
    for dd in days:
        path = os.path.join("data", "accrual_history", f"{dd}.json")
        if not os.path.exists(path):
            continue
        raw_days.add(dd)
        ts = type_sums(load_byday(dd))
        ours[dd]["acquiring"] = ts.get(ACQUIRING_TYPE, Decimal(0))
        ours[dd]["ads_accrual"] = sum((ts.get(t, Decimal(0)) for t in accrual.AD_TYPE_IDS), Decimal(0))
        ours[dd]["logistics_raw"] = sum((v for t, v in ts.items() if t in LOG_TYPES), Decimal(0))
        ours[dd]["type_sums"] = ts

    # accrual/postings: штуки и логистика по дате реализации
    post_days = set()
    if args.postings:
        postings = json.load(open(args.postings))
        idx = sale_index(postings)
        log_by_pn = {}
        for p in postings:
            log_by_pn[p["posting_number"]] = -sum((D(a["accrued"]["amount"]) for a in p.get("accruals") or [] if a.get("type_id") in LOG_TYPES), Decimal(0))
        last_day = {}
        for dd in days:
            if dd not in raw_days:
                continue
            post_days.add(dd)
            c, units, _pos, _bad = match_day(load_byday(dd), idx, dd)
            ours[dd]["units"] = Decimal(c["units"])
            ours[dd]["unmatched"] = Decimal(c["unmatched"])
            for sku, u in units.items():
                uc = unit_cost(sku)
                if uc is not None:
                    ours[dd]["cogs_units"] += u * uc
            for r in load_byday(dd):
                if r.get("accrued_category") == "POSTING" and r.get("posting") and any(pr.get("commission") for pr in r["posting"].get("products") or []):
                    last_day[r.get("unit_number")] = dd
        for pn, dd in last_day.items():
            ours[dd]["logistics_realization"] += log_by_pn.get(pn, Decimal(0))

    young = [dd for dd in days if (date.today() - date.fromisoformat(dd)).days < 2]
    print(f"окно {args.date_from} … {args.date_to}: выкупов строк {len(buy)}, расходов строк {len(exp)}, "
          f"sku→article {len(sku2art)}, себестоимость найдена {len(cost)} из {len(norms)}; сырьё by-day за {len(raw_days)} дн., accrual/postings за {len(post_days)} дн.")
    if young:
        print(f"МОЛОЖЕ ДВУХ СУТОК (начисления ещё доезжают): {', '.join(young)}")
    if nocost:
        print("позиций без себестоимости по дням:", dict(nocost))

    g = lambda k: {dd: ours[dd][k] for dd in days if k in ours[dd]}  # noqa: E731
    m = lambda k: {dd: manual[dd][k] for dd in days if dd in manual and manual[dd].get(k) is not None}  # noqa: E731
    table("Оборот — выкупы", days, g("turnover"), m("turnover"), "у нас buyouts_amount_seller, у них «Оборот - выкупы»")
    table("Комиссия с НДС", days, g("commission"), m("commission"), "у нас commission_amount = sale_commission accrual")
    rev = {dd: ((ours[dd]["turnover"] - ours[dd]["commission"]) / VAT).quantize(C) for dd in days if "turnover" in ours[dd]}
    table("Выручка = (оборот − комиссия) / 1,22", days, rev, m("revenue"))
    table("Себестоимость: у нас позиции × снимок 1С " + args.snapshot, days, {dd: v.quantize(C) for dd, v in g("cogs_pos").items()}, m("cogs"))
    if post_days:
        table("Себестоимость: у нас ШТУКИ (accrual/postings) × снимок 1С " + args.snapshot, days, {dd: v.quantize(C) for dd, v in g("cogs_units").items()}, m("cogs"))
        table("Штуки против позиций", days, g("units"), g("positions"), "«у них» здесь = наши позиции")
    margin = {dd: rev[dd] - ours[dd]["cogs_pos"].quantize(C) for dd in rev}
    table("Маржа = выручка − СС (у нас по позициям)", days, margin, m("margin"))
    table("Логистика: у нас по дате начисления (marketplace_expenses)", days, g("logistics"), m("logistics"))
    table("Логистика: их число × 1,22 против нашей по дате начисления", days, g("logistics"), {dd: (v * VAT).quantize(C) for dd, v in m("logistics").items()})
    if post_days:
        table("Логистика: у нас по дате реализации отправления (accrual/postings)", days, {dd: v.quantize(C) for dd, v in g("logistics_realization").items()}, m("logistics"),
              "для свежих дат неполна: начисления по отправлению доезжают позже")
        # Их «Логистика» и «Прочее» — две строки одной группы: сумма сходится с нашей (логистика + прочее без эквайринга и типа 96) × 1,22,
        # граница между строками у них проведена иначе (проверено 1–9 июля и 1–15 сентября 2026).
        lo_theirs = {dd: ((manual[dd]["logistics"] + manual[dd]["other"]) * VAT).quantize(C) for dd in days if dd in manual and manual[dd].get("logistics") is not None}
        lo_ours = {dd: (ours[dd]["logistics"] + ours[dd]["other"] - ours[dd]["acquiring"] - ours[dd]["type_sums"].get(96, Decimal(0))).quantize(C) for dd in raw_days if "logistics" in ours[dd]}
        table("Логистика + прочее: их сумма × 1,22 против нашей (логистика + прочее − эквайринг − тип 96)", days, lo_ours, lo_theirs,
              "тип 25 ItemCompensation у нас вне расходов, у них внутри прочего")
    table("Реклама: у нас Performance API (клики + CPO + Selected CPO)", days, g("ads"), m("ads"))
    if raw_days:
        table("Реклама: у нас accrual типы 41 + 54", days, {dd: v.quantize(C) for dd, v in g("ads_accrual").items()}, m("ads"))
        table("Реклама: их число × 1,22 против accrual 41 + 54", days, {dd: v.quantize(C) for dd, v in g("ads_accrual").items()}, {dd: (v * VAT).quantize(C) for dd, v in m("ads").items()})
        ads4 = {dd: sum((ours[dd]["type_sums"].get(t, Decimal(0)) for t in (41, 54, 51, 96)), Decimal(0)).quantize(C) for dd in raw_days}
        table("Реклама: их число × 1,22 против accrual 41 + 54 + 51 + 96", days, ads4, {dd: (v * VAT).quantize(C) for dd, v in m("ads").items()},
              "клики, продвижение, подписка Premium, ускоренный сбор отзывов — их группа «Продвижение и реклама», без НДС")
        table("Эквайринг: у нас тип 1 / 1,22", days, {dd: (v / VAT).quantize(C) for dd, v in g("acquiring").items()}, m("acquiring"))
        other_wo = {dd: (ours[dd]["other"] - ours[dd]["acquiring"]).quantize(C) for dd in raw_days if "other" in ours[dd]}
        table("Прочее: у нас «прочее» без эквайринга", days, other_wo, m("other"))
        table("Прочее: у нас («прочее» без эквайринга) / 1,22", days, {dd: (v / VAT).quantize(C) for dd, v in other_wo.items()}, m("other"))
        fin = {dd: (margin[dd] - ours[dd]["logistics"] - ours[dd]["ads"] - (ours[dd]["acquiring"] / VAT) - (ours[dd]["other"] - ours[dd]["acquiring"]) / VAT).quantize(C)
               for dd in raw_days if dd in margin}
        table("Фин. рез. по их формуле на наших числах (СС по позициям, прочее и эквайринг / 1,22)", days, fin, m("fin_result"))
    table("Подписка (тип 51 и др.) — у них строки нет", days, g("subscription"), {}, "только у нас")
    print("db_writes = 0")


if __name__ == "__main__":
    main()
