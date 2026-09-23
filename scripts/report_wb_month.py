#!/usr/bin/env python3
"""Лист «WB - <месяц>» по образцу ручного листа владельца — из отчёта реализации WB.

    venv/bin/python3 scripts/report_wb_month.py --month 2026-09                     → data/reports/wb_2026-09_to_<дата>.xlsx
    venv/bin/python3 scripts/report_wb_month.py --month 2026-09 --date-to 2026-09-21 --rows-from-files data/wb_sales_report_raw
    venv/bin/python3 scripts/report_wb_month.py --month 2026-09 --date-to 2026-09-21 --check \\
        --xlsx ../mp-analytics/data/manual_report_september_20260922_v2.xlsx --sheet "WB - сентябрь"

ИСТОЧНИК — строки отчёта реализации (wb_sales_report_rows, зерно rrdId; или те же
строки из файлов сырья бэкфилла — --rows-from-files, пока таблицы нет). ДЕНЬ СТРОКИ —
saleDt в московском времени (у строки без saleDt — rrDate): так датирует владелец,
проверено 21 из 21 по обороту, комиссии, выручке, логистике и прочему за 1…21.09
(WB-5 §3). rrDate отстаёт от даты продажи у 65 строк из 32 009 (02-01…09-22), обычно
на день, поэтому строки читаются по rrDate с запасом LAG_DAYS, а строки продаж с датой
вне листа считаются и печатаются. Карта колонок — docs/wb_report_model.md.

КОЛОНКИ (по дню, суммы за день; знак: Продажа +, Возврат −):
    Оборот              Σ retailPriceWithDisc (продажа − возврат) — = marketplace_buyouts, до рубля
    Комиссия (факт)     Оборот − Σ forPay± («К перечислению продавцу»), по отчёту 44,4 %
    Комиссия по образцу Σ retailPriceWithDisc × commissionPercent / 100 (кВВ строки, знак возврата) — его C
    НДС за возмещение   (Σ ppvzReward + Σ rebillLogisticCost) × (НДС − 1) / НДС — колонка AB его «свода»
    Выручка (факт)      (Оборот − Комиссия факт) / НДС − НДС за возмещение
    Выручка по образцу  (Оборот − Комиссия по образцу) / НДС − НДС за возмещение — его E, до копейки
    Себестоимость       снимок 1С (article_unit_costs) по базовому артикулу (unit_cost_uniform), для
                        безразмерных — точная строка; --cogs-by variant — по варианту артикул-размер
    Логистика           Σ deliveryService / НДС — его I до копейки при дате saleDt МСК
    Реклама             advert-api /adv/v1/upd за месяц (фактические списания), 1 обращение; --no-fetch — пусто
    Эквайринг           Σ acquiringFee (продажа +, возврат −) / НДС — его M; 09-17 он возвраты не вычел
    Прочее              Σ paidStorage / НДС + Σ penalty (штрафы без НДС) + Σ deduction / НДС — его O
    Фин. рез.           Маржа − Логистика − Реклама − Эквайринг − Прочее
    Накладные в день    OVERHEAD_PER_DAY["wb"] с датой действия (T81 листа владельца: 74 002,00 с 09-01)
    Ebitda              Фин. рез. − Накладные

НДС — VAT_RATES из scripts/report_ozon_month.py (один параметр на обе площадки).
Дни моложе двух суток — жёлтые: строки дня появляются на D+1 и могут доезжать.
Только чтение; db_writes = 0.
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from loaders import stale_keys  # noqa: E402
import loaders.wb_sales_report_loader as loader  # noqa: E402
from reconcile_manual_report_ozon import read_manual_sheet  # noqa: E402
from report_ozon_month import MONTHS, VAT_RATES, vat_for  # noqa: E402,F401  — тот же НДС, что у Ozon

Z = Decimal(0)
C = Decimal("0.01")
SNAP = "2026-05-20"
OUT_DIR = os.path.join("data", "reports")
YOUNG_DAYS = 2
MSK = timezone(timedelta(hours=3))
LAG_DAYS = 7   # rrDate позже даты продажи: 60 строк из 32 009 на 1…3 дня, 5 — на 16…115 (те на лист своего месяца не попадут, печатаются)
# Накладные в день по площадке с датой действия — ячейка T81 листа «WB - сентябрь» владельца (22.09).
OVERHEAD_PER_DAY = {"wb": (("2026-09-01", Decimal("74002.00")),)}
# Расхождения с листом на стороне владельца (WB-5 §3): печатаем, не чиним, в отказ не считаем.
# 09-17: эквайринг двух возвратов (441,25 + 936,24 с НДС) он не вычел, на 09-03 / 09-07 / 09-14 — вычел.
# Прежние три «известных дня» (эквайринг 09-03 и 09-07, прочее 09-02) сняты 2026-09-23: это были наши
# формулы — знак возврата в эквайринге и штрафы без НДС, — а не его сторона.
KNOWN_MANUAL_DIFFS = {("acquiring", "2026-09-17")}
MONEY_FIELDS = ("retail_price_with_disc", "retail_amount", "for_pay", "commission_percent", "ppvz_reward", "rebill_logistic_cost",
                "delivery_service", "acquiring_fee", "paid_storage", "penalty", "deduction", "cashback_discount")
SELECT = ("rrd_id,rr_date,sale_dt,seller_oper_name,doc_type,vendor_code,tech_size,nm_id,quantity," + ",".join(MONEY_FIELDS))


def D(v):
    return Decimal(str(v)) if v not in (None, "") else Z


def q(v):
    return Decimal(v).quantize(C)


def ratio(a, b):
    return (Decimal(a) / Decimal(b)).quantize(Decimal("0.0001")) if b else None


def overhead_for(day, platform="wb"):
    for valid_from, value in OVERHEAD_PER_DAY.get(platform, ()):
        if day >= valid_from:
            return value
    return None


def month_days(month, date_to=None):
    import calendar
    y, m = int(month[:4]), int(month[5:7])
    last = date(y, m, calendar.monthrange(y, m)[1])
    if date_to:
        last = min(last, date.fromisoformat(date_to))
    out, d = [], date(y, m, 1)
    while d <= last:
        out.append(d.isoformat()); d += timedelta(days=1)
    return out


def row_day(r):
    """День строки на листе: saleDt в московском времени; без saleDt (или с датой-заглушкой) — rrDate.
    Правило владельца: оборот, комиссия, выручка, логистика и прочее сошлись 21 из 21 только так (WB-5 §3);
    по rrDate оборот расходился 09-09 / 09-10 (четыре продажи 09-09 16:50–18:44 UTC с rrDate 09-10)."""
    value = r.get("sale_dt")
    if value:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            ts = None
        if ts is not None and ts.year >= 2000:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts.astimezone(MSK).date().isoformat()
    return str(r["rr_date"])[:10]


def window_end(d2):
    """Последний rrDate, который читаем ради строк с датой продажи ≤ d2."""
    return (date.fromisoformat(d2) + timedelta(days=LAG_DAYS)).isoformat()


# ---------- источники ----------

def load_rows_db(sb, d1, d2):
    """Строки по rrDate d1 … d2 + LAG_DAYS; день строки на листе даёт row_day."""
    return stale_keys.read_window_rows(sb, loader.TABLE, SELECT, [("gte", "rr_date", d1), ("lte", "rr_date", window_end(d2))], ["rr_date", "rrd_id"])


def load_rows_files(raw_dir, d1, d2):
    """Те же строки из файлов сырья бэкфилла, через loader.build_row — колонки как в таблице."""
    rows, seen = [], set()
    d2x = window_end(d2)
    for path in sorted(glob.glob(os.path.join(raw_dir, "daily_*.json"))):
        name = os.path.basename(path)[len("daily_"):-len(".json")]
        f1, f2 = name.split("_")
        if f2 < d1 or f1 > d2x:
            continue
        for item in json.load(open(path, encoding="utf-8"), parse_float=Decimal):
            day = str(item["rrDate"])[:10]
            if not (d1 <= day <= d2x):
                continue
            rid = int(item["rrdId"])
            if rid in seen:
                raise RuntimeError(f"rrdId {rid} встречается дважды в файлах сырья")
            seen.add(rid)
            rows.append(loader.build_row(item, "files"))
    return rows


def load_costs(sb, snapshot=SNAP):
    """Снимок 1С целиком (offer_id_norm, unit_cost, unit_cost_uniform): точные строки и «единая» по базовому артикулу."""
    rows = stale_keys.read_window_rows(sb, "article_unit_costs", "offer_id_norm,unit_cost,unit_cost_uniform",
                                       [("eq", "snapshot_date", snapshot)], ["offer_id_norm"])
    exact, uniform = {}, {}
    for r in rows:
        exact[r["offer_id_norm"]] = D(r["unit_cost"])
        base = r["offer_id_norm"].split("-", 1)[0]
        if r.get("unit_cost_uniform") is not None and base not in uniform:
            uniform[base] = D(r["unit_cost_uniform"])
    return exact, uniform


def unit_cost_for(exact, uniform, vendor_code, tech_size, mode="base"):
    """(себестоимость, источник): variant — точный вариант артикул-размер; plain — безразмерная точная строка;
    uniform — «единая» по базовому артикулу; none — в снимке нет."""
    code = str(vendor_code or "").strip().lower()
    if not code:
        return None, "none"
    size = str(tech_size or "").strip()
    if mode == "variant" and size and size != "0":
        v = exact.get(f"{code}-{size}")
        if v is not None:
            return v, "variant"
    if code in exact:
        return exact[code], "plain"
    if code in uniform:
        return uniform[code], "uniform"
    return None, "none"


def fetch_ads(d1, d2):
    """Фактические списания за рекламу по дням: advert-api /adv/v1/upd (интервал ≤ 31 день), одно обращение."""
    import requests
    resp = requests.get("https://advert-api.wildberries.ru/adv/v1/upd", headers={"Authorization": os.getenv("WB_API_KEY")},
                        params={"from": d1, "to": d2}, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"adv/v1/upd HTTP {resp.status_code}: {resp.text[:200]}")
    by, undated = defaultdict(Decimal), Z
    for x in resp.json() or []:
        when = (x.get("updTime") or "")[:10]
        if when:
            by[when] += D(x.get("updSum"))
        else:
            undated += D(x.get("updSum"))
    return dict(by), undated


# ---------- расчёт ----------

def build_daily(rows, days, cost_fn, ads_by_day, today, ads_known=True, outside=None):
    """Строки листа по дням. cost_fn(vendor_code, tech_size) → (себестоимость, источник).
    outside (если передан словарь) получает продажи/возвраты с датой продажи вне листа: {"before"|"after": [строк, Σ оборот]}."""
    by = {d: defaultdict(Decimal) for d in days}
    cnt = {d: defaultdict(int) for d in days}
    if outside is not None:
        outside.setdefault("before", [0, Z]); outside.setdefault("after", [0, Z])
    for r in rows:
        d = row_day(r)
        op = r["seller_oper_name"]
        if d not in by:
            if outside is not None and op in ("Продажа", "Возврат") and days:
                side = outside["before" if d < days[0] else "after"]
                side[0] += 1; side[1] += (1 if op == "Продажа" else -1) * D(r["retail_price_with_disc"])
            continue
        s = by[d]
        if op in ("Продажа", "Возврат"):
            sign = 1 if op == "Продажа" else -1
            price = D(r["retail_price_with_disc"])
            s["turnover"] += sign * price
            s["for_pay"] += sign * D(r["for_pay"])
            pct = r.get("commission_percent")
            if pct in (None, ""):
                cnt[d]["no_pct_rows"] += 1
            s["commission_manual"] += sign * price * D(pct) / 100
            s["acquiring_raw"] += sign * D(r["acquiring_fee"])
            qty = int(r.get("quantity") or 1)
            cost, source = cost_fn(r.get("vendor_code"), r.get("tech_size"))
            cnt[d]["positions"] += sign * qty
            cnt[d]["cost_" + source] += qty
            if cost is None:
                cnt[d]["no_cost_positions"] += qty
                s["no_cost_turnover"] += sign * price   # доля оборота без СС — вслух (задача WB-5 §3)
            else:
                s["cogs"] += sign * cost * qty
        else:
            s["acquiring_other"] += D(r["acquiring_fee"])   # эквайринг вне продаж/возвратов — в лист не входит, печатается, если не ноль
        s["reward"] += D(r["ppvz_reward"]); s["rebill"] += D(r["rebill_logistic_cost"])
        s["delivery"] += D(r["delivery_service"])
        s["storage"] += D(r["paid_storage"]); s["penalty"] += D(r["penalty"]); s["deduction"] += D(r.get("deduction"))
        cnt[d]["rows"] += 1
    out = []
    for d in days:
        s, c, vat = by[d], cnt[d], vat_for(d)
        vat_refund = (s["reward"] + s["rebill"]) * (vat - 1) / vat
        commission = s["turnover"] - s["for_pay"]
        commission_manual = s["commission_manual"]
        revenue = (s["turnover"] - commission) / vat - vat_refund
        revenue_manual = (s["turnover"] - commission_manual) / vat - vat_refund
        cogs = s["cogs"]
        ads = ads_by_day.get(d, Z) if ads_known else None
        logistics, acquiring = s["delivery"] / vat, s["acquiring_raw"] / vat
        other = s["storage"] / vat + s["penalty"] + s["deduction"] / vat   # штрафы у владельца без НДС — 21 из 21 только так
        margin = revenue - cogs
        fin = margin - logistics - (ads or Z) - acquiring - other
        overhead = overhead_for(d)
        young = (date.fromisoformat(d) >= today - timedelta(days=YOUNG_DAYS - 1))
        row = {"date": d, "vat": vat, "rows": c["rows"], "positions": c["positions"], "no_cost_positions": c["no_cost_positions"], "no_pct_rows": c["no_pct_rows"],
               "cost_sources": {k[5:]: v for k, v in c.items() if k.startswith("cost_")},
               "turnover": s["turnover"], "commission": commission, "commission_pct": ratio(commission, s["turnover"]),
               "commission_manual": commission_manual, "vat_refund": vat_refund, "revenue": revenue, "revenue_manual": revenue_manual,
               "cogs": cogs, "margin": margin, "margin_pct": ratio(margin, revenue), "logistics": logistics, "logistics_pct": ratio(logistics, revenue),
               "ads": ads, "drr_pct": ratio(ads, s["turnover"]) if ads is not None else None, "acquiring": acquiring, "acquiring_pct": ratio(acquiring, revenue),
               "other": other, "fin_result": fin, "fin_result_pct": ratio(fin, revenue), "overhead": overhead,
               "ebitda": (fin - overhead) if overhead is not None else None, "young": young,
               "reward": s["reward"], "rebill": s["rebill"], "storage": s["storage"], "penalty": s["penalty"], "deduction": s["deduction"],
               "acquiring_other": s["acquiring_other"], "no_cost_turnover": s["no_cost_turnover"]}
        row["ebitda_pct"] = ratio(row["ebitda"], revenue) if row["ebitda"] is not None else None
        out.append(row)
    return out


def total_row(rows):
    t = {"date": "Итого", "young": False, "vat": rows[-1]["vat"] if rows else vat_for("2026-01-01")}
    for k in ("rows", "positions", "no_cost_positions", "no_pct_rows"):
        t[k] = sum(r[k] for r in rows)
    for k in ("turnover", "commission", "commission_manual", "vat_refund", "revenue", "revenue_manual", "cogs", "margin", "logistics", "acquiring", "other", "fin_result", "reward", "rebill", "storage", "penalty", "deduction", "acquiring_other", "no_cost_turnover"):
        t[k] = sum((r[k] for r in rows), Z)
    t["ads"] = sum((r["ads"] for r in rows if r["ads"] is not None), Z) if any(r["ads"] is not None for r in rows) else None
    t["overhead"] = sum((r["overhead"] for r in rows if r["overhead"] is not None), Z) if any(r["overhead"] is not None for r in rows) else None
    t["ebitda"] = (t["fin_result"] - t["overhead"]) if t["overhead"] is not None else None
    t["commission_pct"] = ratio(t["commission"], t["turnover"]); t["margin_pct"] = ratio(t["margin"], t["revenue"])
    t["logistics_pct"] = ratio(t["logistics"], t["revenue"]); t["acquiring_pct"] = ratio(t["acquiring"], t["revenue"])
    t["drr_pct"] = ratio(t["ads"], t["turnover"]) if t["ads"] is not None else None
    t["fin_result_pct"] = ratio(t["fin_result"], t["revenue"]); t["ebitda_pct"] = ratio(t["ebitda"], t["revenue"]) if t["ebitda"] is not None else None
    srcs = defaultdict(int)
    for r in rows:
        for k, v in r["cost_sources"].items():
            srcs[k] += v
    t["cost_sources"] = dict(srcs)
    return t


# ---------- книга ----------

COLS = [("Дата реализации", "date", None), ("Оборот (с НДС), руб.", "turnover", "money"), ("Комиссия факт (с НДС), руб.", "commission", "money"),
        ("Комиссия факт, %", "commission_pct", "pct"), ("справочно: Комиссия по образцу (Σ цена × кВВ строки)", "commission_manual", "money"),
        ("Выручка, руб.", "revenue", "money"), ("справочно: Выручка по образцу (с комиссией по кВВ)", "revenue_manual", "money"),
        ("Себестоимость, руб.", "cogs", "money"), ("Маржа, руб.", "margin", "money"), ("Мар-ть, %", "margin_pct", "pct"),
        ("Логистика, руб.", "logistics", "money"), ("% Логистики", "logistics_pct", "pct"), ("Реклама, руб.", "ads", "money"),
        ("% ДРР (от ТО)", "drr_pct", "pct"), ("Эквайринг, руб.", "acquiring", "money"), ("% Эквайринга", "acquiring_pct", "pct"),
        ("Прочее, руб.", "other", "money"), ("Фин. рез., руб.", "fin_result", "money"), ("% Фин. рез.", "fin_result_pct", "pct"),
        ("Накладные в день, руб.", "overhead", "money"), ("Ebitda, руб.", "ebitda", "money"), ("% Ebitda", "ebitda_pct", "pct"),
        (None, None, None), ("НДС за возмещение (вычет), руб.", "vat_refund", "money"), ("Возмещение ПВЗ (с НДС)", "reward", "money"),
        ("Возмещение перемещения (с НДС)", "rebill", "money"), ("Хранение (с НДС)", "storage", "money"), ("Штрафы (без НДС, как в отчёте)", "penalty", "money"),
        ("Удержания (с НДС)", "deduction", "money"),
        ("Позиций (продажи − возвраты)", "positions", "int"), ("позиций без себестоимости", "no_cost_positions", "int"),
        ("строк отчёта", "rows", "int"), ("Примечание", "note", None)]


def write_xlsx(path, month, rows, total, notes):
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    fmt = {"money": "#,##0.00", "pct": "0.0%", "int": "#,##0"}
    bold, young_fill, head_fill = Font(bold=True), PatternFill("solid", fgColor="FFF2CC"), PatternFill("solid", fgColor="D9E1F2")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"WB - {MONTHS[int(month[5:7]) - 1]}"
    ws["A1"] = "Выкупы WB"; ws["A1"].font = bold
    ws["E2"] = "Вычитается НДС за возмещение из выручки! (как на листе владельца)"
    for j, (head, _k, _f) in enumerate(COLS, 1):
        if head:
            c = ws.cell(row=3, column=j, value=head); c.font = bold; c.fill = head_fill
            c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    for i, r in enumerate(rows + [total], 4):
        r = dict(r)
        r["note"] = "; ".join(x for x in (
            "моложе двух суток — строки отчёта доезжают" if r.get("young") else "",
            f"без СС {r['no_cost_positions']} из {r['positions']} позиций" if r.get("no_cost_positions") else "",
            "реклама не запрашивалась" if r.get("ads") is None else "") if x)
        for j, (head, k, f) in enumerate(COLS, 1):
            if not head:
                continue
            v = r.get(k)
            if k == "date" and v != "Итого":
                v = date.fromisoformat(v)
            elif isinstance(v, Decimal):
                v = float(v)
            c = ws.cell(row=i, column=j, value=v)
            if f:
                c.number_format = fmt[f]
            if k == "date" and r["date"] != "Итого":
                c.number_format = "DD.MM.YYYY"
            if r.get("young"):
                c.fill = young_fill
            if r["date"] == "Итого":
                c.font = bold
    base = 4 + len(rows) + 2
    for n, line in enumerate(notes):
        ws.cell(row=base + n, column=1, value=line)
    ws.freeze_panes = "B4"; ws.row_dimensions[3].height = 48
    for j in range(1, len(COLS) + 1):
        ws.column_dimensions[get_column_letter(j)].width = 15 if j > 1 else 16
    ws.column_dimensions[get_column_letter(len(COLS))].width = 60
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wb.save(path)


# ---------- приёмка ----------

CHECK_LINES = (
    # (заголовок, наше поле, их колонка, обязана сходиться, чем объясняется расхождение)
    ("Оборот (с НДС)", "turnover", "turnover", True, ""),
    ("Выручка по образцу (комиссия по кВВ, минус НДС за возмещение)", "revenue_manual", "revenue", True, ""),
    ("Эквайринг (acquiringFee, возврат со знаком минус) / НДС", "acquiring", "acquiring", True,
     "09-17 владелец не вычел эквайринг двух возвратов (441,25 + 936,24 с НДС = 1 129,09 без) — на 09-03, 09-07, 09-14 вычел; его сторона"),
    ("Прочее (хранение / НДС + штрафы без НДС + удержания / НДС)", "other", "other", True, ""),
    ("Комиссия по образцу (Σ цена × кВВ строки, знак возврата)", "commission_manual", "commission", True, ""),
    ("Логистика (deliveryService / НДС, по дате продажи МСК)", "logistics", "logistics", True, ""),
    ("Комиссия факт (оборот − forPay) против его по кВВ", "commission", "commission", False, "у него кВВ строки (42 % почти везде), факт по отчёту 44,4 % — решение 4 WB-5: наш лист по факту"),
    ("Выручка факт против его выручки", "revenue", "revenue", False, "разница — комиссия факт против кВВ"),
    ("Себестоимость (снимок 1С 05-20 по базовому артикулу против его цен)", "cogs", "cogs", False, "снимок 05-20 и стыковка по базовому артикулу (WB-5 §4)"),
    ("Фин. рез. (наш факт против его)", "fin_result", "fin_result", False, "комиссия факт против кВВ + СС"),
)


def check(rows, manual, young_days=()):
    out, failures = [], 0
    for title, ours_k, theirs_k, must, why in CHECK_LINES:
        t_o = t_t = Z
        bad, missing, known = [], [], []
        for r in rows:
            if r["date"] in young_days:
                continue
            m = manual.get(r["date"])
            o = r.get(ours_k)
            t = None if not m or m.get(theirs_k) is None else Decimal(m[theirs_k])
            if o is None or t is None:
                missing.append(r["date"]); continue
            o, t = q(o), q(t)
            t_o += o; t_t += t
            if o != t:
                (known if (ours_k, r["date"]) in KNOWN_MANUAL_DIFFS else bad).append((r["date"], o - t))
        days = len([r for r in rows if r["date"] not in young_days])
        if must and (bad or missing):
            failures += 1
        out.append({"title": title, "theirs": t_t, "ours": t_o, "diff": t_o - t_t, "equal_days": days - len(bad) - len(known) - len(missing),
                    "days": days, "bad": bad, "known": known, "missing": missing, "must": must, "why": why})
    return out, failures


def print_check(table):
    print(f"\n{'колонка':66}{'у него':>17}{'у нас':>17}{'разница':>15}{'дней 0,00':>11}")
    for t in table:
        print(f"{t['title'][:65]:66}{t['theirs']:>17,.2f}{t['ours']:>17,.2f}{t['diff']:>15,.2f}{t['equal_days']:>7} из {t['days']}"
              + ("   ОБЯЗАНА СХОДИТЬСЯ" if t["must"] and (t["bad"] or t["missing"]) else ""))
        if t["known"]:
            print("      известные расхождения (его сторона, не чиним): " + ", ".join(f"{d} {v:+,.2f}" for d, v in t["known"]))
        if t["bad"] and (t["must"] or len(t["bad"]) <= 8):
            print("      расходится: " + ", ".join(f"{d} {v:+,.2f}" for d, v in t["bad"]))
        elif t["bad"]:
            print(f"      расходится на {len(t['bad'])} днях: от {min(v for _d, v in t['bad']):+,.2f} до {max(v for _d, v in t['bad']):+,.2f}")
        if t["missing"]:
            print("      нет значения: " + ", ".join(t["missing"]))
        if t["why"] and (t["bad"] or t["known"] or t["missing"]):
            print(f"      чем объясняется: {t['why']}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--date-to", help="последний день листа; по умолчанию — вчера (строки дня появляются на D+1)")
    ap.add_argument("--out")
    ap.add_argument("--rows-from-files", metavar="DIR", help="строки отчёта из файлов сырья бэкфилла, а не из таблицы")
    ap.add_argument("--snapshot", default=SNAP)
    ap.add_argument("--cogs-by", choices=("base", "variant"), default="base", help="СС по базовому артикулу (решение WB-5) или по варианту артикул-размер")
    ap.add_argument("--no-fetch", action="store_true", help="в API не ходить (реклама пустая)")
    ap.add_argument("--check", action="store_true"); ap.add_argument("--xlsx"); ap.add_argument("--sheet")
    args = ap.parse_args(argv)

    today = date.today()
    d2 = args.date_to or (today - timedelta(days=1)).isoformat()
    days = month_days(args.month, d2)
    d1 = days[0]
    sb = loader._client()
    rows = load_rows_files(args.rows_from_files, d1, days[-1]) if args.rows_from_files else load_rows_db(sb, d1, days[-1])
    print(f"строк отчёта по rrDate за {d1} … {window_end(days[-1])} (запас {LAG_DAYS} дн. под дату продажи): {len(rows)} ("
          + ("файлы " + args.rows_from_files if args.rows_from_files else loader.TABLE) + ")")
    exact, uniform = load_costs(sb, args.snapshot)
    print(f"снимок 1С {args.snapshot}: точных строк {len(exact)}, базовых артикулов с единой СС {len(uniform)}; стыковка — {args.cogs_by}")
    ads_by_day, ads_known, ads_note = {}, False, "реклама не запрашивалась (--no-fetch)"
    if not args.no_fetch:
        try:
            ads_by_day, undated = fetch_ads(d1, days[-1])
            ads_known = True
            ads_note = f"реклама: adv/v1/upd за {d1}…{days[-1]} — списаний {sum(ads_by_day.values(), Z):,.2f}" + (f", без даты {undated:,.2f}" if undated else "")
        except Exception as error:  # реклама не обязана останавливать лист
            ads_note = f"реклама не получена: {error}"
    print(ads_note)
    cost_fn = lambda code, size: unit_cost_for(exact, uniform, code, size, args.cogs_by)  # noqa: E731
    outside = {}
    daily = build_daily(rows, days, cost_fn, ads_by_day, today, ads_known, outside)
    total = total_row(daily)
    young = [r["date"] for r in daily if r["young"]]
    outside_text = (f"продаж/возвратов с датой продажи раньше листа {outside['before'][0]} (оборот {outside['before'][1]:,.2f}), "
                    f"позже {outside['after'][0]} (оборот {outside['after'][1]:,.2f})")
    print(outside_text)
    if total["acquiring_other"]:
        print(f"ВНИМАНИЕ: acquiringFee вне строк продаж/возвратов — {total['acquiring_other']:,.2f} с НДС, в лист не вошёл")
    if total["no_pct_rows"]:
        print(f"ВНИМАНИЕ: строк продаж/возвратов без commissionPercent — {total['no_pct_rows']}, в комиссии по образцу они как 0 %")
    notes = [f"Источник: отчёт реализации WB (finance-api sales-reports/detailed, period=daily); день строки — saleDt в московском времени, без saleDt — rrDate. "
             f"Строк {len(rows)}; {outside_text}.",
             f"Комиссия факт = Оборот − forPay; по образцу владельца — Σ цена × кВВ строки (commissionPercent) — в справочных колонках и в приёмке. НДС {total['vat']}.",
             f"Себестоимость: снимок 1С {args.snapshot}, стыковка {args.cogs_by}; позиций по источникам: " + ", ".join(f"{k} {v}" for k, v in sorted(total["cost_sources"].items()))
             + f"; без СС {total['no_cost_positions']} из {total['positions']} позиций — оборот {total['no_cost_turnover']:,.2f}"
             + (f" ({ratio(total['no_cost_turnover'], total['turnover']):.1%} оборота)." if total["turnover"] else "."),
             ads_note, "Логистика = deliveryService / НДС по дате продажи МСК — с листом владельца до копейки; эквайринг — возврат со знаком минус; "
             "прочее = хранение / НДС + штрафы без НДС + удержания / НДС (docs/wb_report_model.md).",
             f"Жёлтым — дни моложе {YOUNG_DAYS} суток: строки доезжают." + (f" Таких дней: {', '.join(young)}." if young else "")]
    out = args.out or os.path.join(OUT_DIR, f"wb_{args.month}_to_{days[-1]}.xlsx")
    write_xlsx(out, args.month, daily, total, notes)
    ads_text = "—" if total["ads"] is None else f"{total['ads']:,.2f}"
    print(f"итого {d1}…{days[-1]}: оборот {total['turnover']:,.2f}, комиссия факт {total['commission']:,.2f} ({total['commission_pct']:.2%}), выручка {total['revenue']:,.2f}, "
          f"СС {total['cogs']:,.2f}, маржа {total['margin']:,.2f}, логистика {total['logistics']:,.2f}, реклама {ads_text}, "
          f"эквайринг {total['acquiring']:,.2f}, прочее {total['other']:,.2f}, фин. рез. {total['fin_result']:,.2f}"
          + (f", накладные {total['overhead']:,.2f}, Ebitda {total['ebitda']:,.2f}" if total["ebitda"] is not None else ""))
    print(f"без СС: {total['no_cost_positions']} из {total['positions']} позиций, оборот {total['no_cost_turnover']:,.2f}"
          + (f" — {ratio(total['no_cost_turnover'], total['turnover']):.2%} оборота" if total["turnover"] else ""))
    print(f"по образцу (кВВ строк): комиссия {total['commission_manual']:,.2f}, выручка {total['revenue_manual']:,.2f}; НДС за возмещение {total['vat_refund']:,.2f}")
    print(f"записано: {out}")
    code = 0
    if args.check:
        if not args.xlsx or not args.sheet:
            raise SystemExit("--check требует --xlsx и --sheet")
        manual = read_manual_sheet(args.xlsx, args.sheet)
        table, failures = check(daily, manual, set(young))
        print_check(table)
        print(f"приёмка: колонок, обязанных сходиться до копейки, — {sum(1 for l in CHECK_LINES if l[3])}; не сошлось {failures}; дни моложе двух суток исключены: {young or 'нет'}")
        code = 1 if failures else 0
    print("db_writes = 0")
    return code


if __name__ == "__main__":
    sys.exit(main())
