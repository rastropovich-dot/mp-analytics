#!/usr/bin/env python3
"""WB-13 §2: сверка листов «WB - <месяц>» ручного отчёта аналитика (KARATOV) с нашими строками модуля «Выкупы WB»
(report_finrez_wb.build_rows) и с сырьём отчёта реализации — по дням, по колонкам, с кандидатами определений.

    venv/bin/python3 scripts/reconcile_manual_report_wb.py --xlsx ~/mp-analytics/data/manual_report_september_20260922_v2.xlsx \
        --months 2026-04 2026-09 --rows-json logs/finrez_wb_rows_2026-04_09-27_20260928.json --raw-json logs/wb_report_raw_2026-04_09.json \
        --out logs/wb_manual_reconcile_20260928

Его колонки (строка 3 листа): A Дата реализации · B Оборот (с НДС) · C Комиссия (с НДС) · D Комиссия % · E Выручка · F Себестоимость ·
G Маржа · I Логистика · K Реклама · M Эквайринг · O Прочее · P Фин. рез. Берутся только дни внутри месяца листа (первый блок).
Наши: строки «Данные WB выкупы» (день × nmId) → по дню; форма «Выкупы WB»: N = (B − C)/НДС, R = (логистика + хранение)/НДС,
T = реклама/НДС, V = эквайринг/НДС, X = (штрафы + удержания − доплаты)/НДС, Y = N − СС − R − T − V − X.
Сырьё: wb_sales_report_rows по дате продажи МСК (row_day), удержания разложены по bonus_type_name.
Только чтение; db_writes = 0; к WB API не ходит (реклама — из wb_ad_spend_daily через строки модуля)."""
import argparse
import csv
import importlib.util
import json
import os
import sys
from collections import defaultdict
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
_spec = importlib.util.spec_from_file_location("report_finrez_wb", os.path.join(ROOT, "scripts", "report_finrez_wb.py"))
fw = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(fw)
wbm = fw.wbm
from loaders import keyset  # noqa: E402
from loaders import wb_money_rules as rules  # noqa: E402

Z = Decimal(0)
MONTH_SHEETS = {"04": "апрель", "05": "май", "06": "июнь", "07": "июль", "08": "август", "09": "сентябрь"}
HIS_COLS = {"turnover": 1, "commission": 2, "comm_pct": 3, "revenue": 4, "cogs": 5, "margin": 6, "logistics": 8, "ads": 10, "acquiring": 12, "other": 14, "fin": 15}
RAW_SELECT = ("rrd_id,rr_date,sale_dt,seller_oper_name,bonus_type_name,retail_price_with_disc,retail_amount,for_pay,ppvz_sales_commission,"
              "commission_percent,vw,vw_nds,ppvz_reward,delivery_service,rebill_logistic_cost,paid_storage,penalty,additional_payment,deduction,"
              "paid_acceptance,acquiring_fee,acquiring_percent,quantity")
PROMO = "Оказание услуг «WB Продвижение»"
ADVANCE = ("Аванс за услугу", "Возврат неиспользованного остатка аванса")


def D(v):
    return v if isinstance(v, Decimal) else (Z if v in (None, "") else Decimal(str(v)))


def q(v):
    return D(v).quantize(Decimal("0.01"))


def month_range(m_from, m_to):
    y1, m1 = map(int, m_from.split("-")); y2, m2 = map(int, m_to.split("-"))
    out = []
    while (y1, m1) <= (y2, m2):
        out.append(f"{y1:04d}-{m1:02d}")
        m1 += 1
        if m1 == 13:
            y1, m1 = y1 + 1, 1
    return out


def read_manual(path, months):
    """{день: {колонка: Decimal}} — первый блок листа «WB - <месяц>», только дни этого месяца; D — доля (как в листе)."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out, notes = {}, []
    for m in months:
        name = f"WB - {MONTH_SHEETS[m[5:]]}"
        if name not in wb.sheetnames:
            notes.append(f"листа «{name}» нет"); continue
        rows = list(wb[name].iter_rows(min_row=4, values_only=True))
        n = 0
        for r in rows:
            a = r[0]
            day = None
            if hasattr(a, "strftime"):
                day = a.strftime("%Y-%m-%d")
            elif isinstance(a, str) and len(a) >= 10 and a[4] == "-":
                day = a[:10]
            if day is None:
                if isinstance(a, str) and a.startswith("Итого"):
                    break
                continue
            if not day.startswith(m) or day in out:
                continue
            vals = {}
            for k, i in HIS_COLS.items():
                v = r[i] if i < len(r) else None
                vals[k] = None if v in (None, "") or isinstance(v, str) else D(v)
            if vals["turnover"] is None:
                continue
            out[day] = vals; n += 1
        notes.append(f"{name}: дней {n}")
    return out, notes


def ours_by_day(rows):
    acc = defaultdict(lambda: defaultdict(Decimal))
    for r in rows:
        a = acc[str(r["date"])]
        for k in ("sales", "commission", "cogs", "logistics", "storage", "other", "acquiring", "ads", "rebill", "ppvz_reward", "coinvest"):
            a[k] += D(r.get(k))
        a["qty"] += D(r.get("qty")); a["no_cost_qty"] += D(r.get("no_cost_qty"))
    return acc


def form_of(a, day):
    """Форма «Выкупы WB» по дню (без НДС там, где у формы без НДС)."""
    vat = wbm.vat_for(day)
    n = (a["sales"] - a["commission"]) / vat
    r = (a["logistics"] + a["storage"]) / vat
    t = a["ads"] / vat
    v = a["acquiring"] / vat
    x = a["other"] / vat
    return {"B": a["sales"], "C": a["commission"], "N": n, "O": a["cogs"], "P": n - a["cogs"], "R": r, "T": t, "V": v, "X": x,
            "Y": n - a["cogs"] - r - t - v - x, "vat": vat}


def load_raw(sb, d1, d2, cache):
    if cache and os.path.exists(cache):
        return json.load(open(cache, encoding="utf-8"))
    rows = keyset.read_keyset(sb, wbm.loader.TABLE, RAW_SELECT, d1, d2, day_col="rr_date", id_col="rrd_id")
    if cache:
        json.dump(rows, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    return rows


def deduction_kind(bonus):
    b = bonus or ""
    if b.startswith(PROMO):
        return "promo"
    if b.startswith(ADVANCE):
        return "advance"
    if "Джем" in b:
        return "jam"
    if b.startswith("Витрина"):
        return "vitrina"
    if b.startswith("Списание за отзыв"):
        return "review"
    return "other"


def add_advance_net(acc, raw):
    """WB-14: нетто аванса «Баллы за отзывы» — в день возврата (rules.review_advance_net по строкам окна сырья)."""
    net, _open = rules.review_advance_net([r for r in raw if r.get("bonus_type_name")], wbm.row_day)
    for day, v in net.items():
        acc[day]["advance_net"] += v
    return acc


def raw_by_day(raw):
    """По дню продажи МСК (row_day): суммы полей, комиссия разными полями, удержания по видам; плюс оборот по rrDate и по UTC-дню."""
    acc = defaultdict(lambda: defaultdict(Decimal))
    for r in raw:
        day = wbm.row_day(r)
        op = r.get("seller_oper_name")
        a = acc[day]
        sign = 1 if op == "Продажа" else (-1 if op == "Возврат" else 0)
        price, amount, for_pay = D(r.get("retail_price_with_disc")), D(r.get("retail_amount")), D(r.get("for_pay"))
        if sign:
            a["sales"] += sign * price
            a["comm_kvv"] += sign * price * D(r.get("commission_percent")) / 100
            a["comm_ppvz"] += sign * D(r.get("ppvz_sales_commission"))
            a["comm_vw"] += sign * (D(r.get("vw")) + D(r.get("vw_nds")))
            a["withheld"] += sign * (price - for_pay)
            a["amount_minus_forpay"] += sign * (amount - for_pay)
            a["acq"] += sign * D(r.get("acquiring_fee"))
            a["acq_abs"] += D(r.get("acquiring_fee"))
            a["acq_by_pct"] += sign * price * D(r.get("acquiring_percent")) / 100
            a["acq_rows"] += 1
            if D(r.get("acquiring_fee")) == 0:
                a["acq_zero_rows"] += 1
            rr = str(r.get("rr_date"))[:10]
            acc[rr]["sales_by_rr"] += sign * price
            sd = str(r.get("sale_dt") or "")[:10]
            if sd:
                acc[sd]["sales_by_utc"] += sign * price
        a["delivery"] += D(r.get("delivery_service")); a["rebill"] += D(r.get("rebill_logistic_cost")); a["storage"] += D(r.get("paid_storage"))
        a["penalty"] += D(r.get("penalty")); a["addpay"] += D(r.get("additional_payment")); a["acceptance"] += D(r.get("paid_acceptance"))
        a["reward"] += D(r.get("ppvz_reward"))
        ded = D(r.get("deduction"))
        if ded:
            a["ded_total"] += ded
            a["ded_" + deduction_kind(r.get("bonus_type_name"))] += ded
            if rules.classify_deduction(r.get("bonus_type_name")) == rules.OTHER:
                a["ded_rules_other"] += ded                          # WB-14: вид «прочее» (в «Прочее» входит)
    return acc


def candidates(day, o, w, vat):
    """Кандидаты «как считает аналитик» по колонкам; форма «Выкупы WB» — первым."""
    f = form_of(o, day)
    ded_real = w["ded_total"] - w["ded_promo"] - w["ded_advance"]
    return {
        "turnover": {"форма B = Σ price по saleDt МСК": f["B"], "сырьё по rrDate": w["sales_by_rr"], "сырьё по UTC-дню saleDt": w["sales_by_utc"]},
        "commission": {"форма C = Σ price × кВВ": f["C"], "ppvzSalesCommission": w["comm_ppvz"], "vw + vwNds": w["comm_vw"], "price − forPay (удержано всё)": w["withheld"],
                       "B × 0,33": f["B"] * Decimal("0.33"), "B × 0,36": f["B"] * Decimal("0.36"), "B × 0,42": f["B"] * Decimal("0.42")},
        "revenue": {"форма N = (B − C)/НДС": f["N"], "(B − C)/НДС − НДС за возмещение": f["N"] - (w["reward"] + w["rebill"]) * (vat - 1) / vat},
        "cogs": {"форма O = снимок 1С × шт": f["O"]},
        "logistics": {"форма R = (доставка + хранение)/НДС": f["R"], "доставка/НДС": o["logistics"] / vat, "(доставка + rebill)/НДС": (o["logistics"] + o["rebill"]) / vat,
                      "доставка с НДС": o["logistics"], "(доставка + хранение + rebill)/НДС": (o["logistics"] + o["storage"] + o["rebill"]) / vat},
        "ads": {"форма T = списания/НДС": f["T"], "списания с НДС": o["ads"]},
        "acquiring": {"форма V = acquiringFee±/НДС": f["V"], "acquiringFee± с НДС": w["acq"], "|acquiringFee|/НДС (возврат +)": w["acq_abs"] / vat,
                      "price × acquiringPercent±/НДС": w["acq_by_pct"] / vat},
        "other": {"форма X = (штрафы + удержания − доплаты)/НДС": f["X"], "хранение/НДС + штрафы": w["storage"] / vat + w["penalty"],
                  "хранение/НДС + штрафы + удержания/НДС («WB - месяц»)": w["storage"] / vat + w["penalty"] + w["ded_total"] / vat,
                  "хранение/НДС + штрафы + (удержания − Продвижение − аванс)/НДС": w["storage"] / vat + w["penalty"] + ded_real / vat,
                  "хранение/НДС + штрафы + (удержания − Продвижение)/НДС": w["storage"] / vat + w["penalty"] + (w["ded_total"] - w["ded_promo"]) / vat,
                  "хранение/НДС + штрафы/НДС + (удержания − Продвижение − аванс)/НДС": (w["storage"] + w["penalty"] + ded_real) / vat,
                  "«WB - месяц» WB-14: хранение/НДС + штрафы + (удержания «прочее» + нетто аванса)/НДС":
                      w["storage"] / vat + w["penalty"] + (w["ded_rules_other"] + w["advance_net"]) / vat},
        "fin": {"форма Y": f["Y"]},
    }


def compare(months, his, ours, raw):
    """{колонка: {кандидат: {месяц: {his, ours, diff, days, eq, near}}}}"""
    out = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {"his": Z, "ours": Z, "days": 0, "eq": 0, "near": 0, "bad": []})))
    per_day = []
    for day in sorted(his):
        m = day[:7]
        if m not in months:
            continue
        o, w = ours.get(day) or defaultdict(Decimal), raw.get(day) or defaultdict(Decimal)
        vat = wbm.vat_for(day)
        cands = candidates(day, o, w, vat)
        rec = {"day": day}
        for col, cs in cands.items():
            h = his[day].get(col)
            rec["his_" + col] = None if h is None else q(h)
            for name, val in cs.items():
                rec[f"{col} | {name}"] = q(val)
                if h is None:
                    continue
                s = out[col][name][m]
                s["his"] += q(h); s["ours"] += q(val); s["days"] += 1
                d = q(val) - q(h)
                if abs(d) <= Decimal("0.005"):
                    s["eq"] += 1
                elif abs(d) <= 1:
                    s["near"] += 1
                else:
                    s["bad"].append((day, d))
        rec["his_comm_pct"] = his[day].get("comm_pct")
        for k in ("ded_promo", "ded_advance", "ded_jam", "ded_vitrina", "ded_review", "ded_other", "penalty", "storage", "delivery", "rebill", "reward", "acq_zero_rows", "acq_rows"):
            rec["raw_" + k] = q(w[k]) if k not in ("acq_zero_rows", "acq_rows") else int(w[k])
        per_day.append(rec)
    return out, per_day


def fmt(v):
    return f"{v:,.2f}".replace(",", " ")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", required=True); ap.add_argument("--months", nargs=2, default=["2026-04", "2026-09"])
    ap.add_argument("--rows-json", help="строки build_rows (кэш); нет — собрать живьём"); ap.add_argument("--raw-json", help="кэш сырья")
    ap.add_argument("--out", default="logs/wb_manual_reconcile", help="префикс: <out>_days.csv")
    a = ap.parse_args(argv)
    months = month_range(*a.months)
    his, notes = read_manual(os.path.expanduser(a.xlsx), months)
    print("лист аналитика: " + "; ".join(notes) + f"; всего дней {len(his)}")
    sb = fw.report_loader._client()
    if a.rows_json and os.path.exists(a.rows_json):
        rows = json.load(open(a.rows_json, encoding="utf-8"))
    else:
        rows = fw.build_rows(months[0], months[-1], None, sb)
    ours = ours_by_day(rows)
    d1, d2 = f"{months[0]}-01", wbm.window_end(fw.month_bounds(months[0], months[-1], None)[1])
    raw_rows = load_raw(sb, "2026-03-25", d2, a.raw_json)
    raw = add_advance_net(raw_by_day(raw_rows), raw_rows)
    print(f"наши строки: {len(rows)}, дней {len(ours)}; сырьё по дням: {len(raw)}")
    table, per_day = compare(months, his, ours, raw)
    # по колонкам: помесячно для формы и лучшего кандидата
    for col in ("turnover", "commission", "revenue", "cogs", "logistics", "ads", "acquiring", "other", "fin"):
        print(f"\n=== {col} ===")
        for name, by_m in table[col].items():
            tot_h = sum(s["his"] for s in by_m.values()); tot_o = sum(s["ours"] for s in by_m.values())
            eq = sum(s["eq"] for s in by_m.values()); days = sum(s["days"] for s in by_m.values()); near = sum(s["near"] for s in by_m.values())
            print(f"  {name[:62]:62} у него {fmt(tot_h):>16}  кандидат {fmt(tot_o):>16}  разница {fmt(tot_o - tot_h):>15}  дней 0,00: {eq:>3} из {days} (±1 ₽: +{near})")
            print("     по месяцам: " + "  ".join(f"{m[5:]}: {fmt(s['ours'] - s['his'])} ({s['eq']}/{s['days']})" for m, s in sorted(by_m.items())))
    # разложение «Прочего» по месяцам
    print("\n=== «Прочее»: разложение сырья по месяцам (с НДС, как в отчёте) ===")
    agg = defaultdict(lambda: defaultdict(Decimal))
    for day, w in raw.items():
        if day[:7] in months:
            for k in ("ded_promo", "ded_advance", "ded_jam", "ded_vitrina", "ded_review", "ded_other", "penalty", "storage", "addpay", "acceptance"):
                agg[day[:7]][k] += w[k]
    for m in months:
        g = agg[m]
        print(f"  {m}: удержания WB Продвижение {fmt(g['ded_promo'])}, аванс «Баллы за отзывы» {fmt(g['ded_advance'])}, Джем {fmt(g['ded_jam'])}, Витрина {fmt(g['ded_vitrina'])}, "
              f"списание за отзыв {fmt(g['ded_review'])}, прочие удержания {fmt(g['ded_other'])}; штрафы {fmt(g['penalty'])}; хранение {fmt(g['storage'])}; доплаты {fmt(g['addpay'])}; приёмка {fmt(g['acceptance'])}")
    # СС и его доля
    print("\n=== Себестоимость: доля от оборота ===")
    for m in months:
        h_t = sum(his[d]["turnover"] for d in his if d[:7] == m); h_c = sum(his[d]["cogs"] or Z for d in his if d[:7] == m)
        o_t = sum(ours[d]["sales"] for d in ours if d[:7] == m); o_c = sum(ours[d]["cogs"] for d in ours if d[:7] == m)
        print(f"  {m}: у него СС {fmt(h_c)} / оборот {fmt(h_t)} = {(h_c / h_t if h_t else Z):.4f}; у нас {fmt(o_c)} / {fmt(o_t)} = {(o_c / o_t if o_t else Z):.4f}")
    # его комиссия, %
    print("\n=== его D «Комиссия, %» по месяцам (значения и число дней) ===")
    for m in months:
        cnt = defaultdict(int)
        for d in his:
            if d[:7] == m and his[d].get("comm_pct") is not None:
                cnt[str(q(his[d]["comm_pct"] * 100))] += 1
        print(f"  {m}: " + ", ".join(f"{k} % × {v}" for k, v in sorted(cnt.items(), key=lambda kv: -kv[1])))
    # CSV по дням
    path = a.out + "_days.csv"
    keys = sorted({k for r in per_day for k in r}, key=lambda k: (k != "day", k))
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=keys); wr.writeheader()
        for r in per_day:
            wr.writerow({k: ("" if v is None else str(v)) for k, v in r.items()})
    print(f"\nпо дням: {path} ({len(per_day)} дней, {len(keys)} колонок)")


if __name__ == "__main__":
    main()
