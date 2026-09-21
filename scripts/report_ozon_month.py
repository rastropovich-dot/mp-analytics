#!/usr/bin/env python3
"""Лист «Ozon - <месяц>» по выкупам: то, что владелец считал руками, — из наших источников. Только чтение.

    venv/bin/python3 scripts/report_ozon_month.py --month 2026-09                       → data/reports/ozon_2026-09.xlsx
    venv/bin/python3 scripts/report_ozon_month.py --month 2026-09 --date-to 2026-09-15 \
        --check --xlsx data/manual_report_september.xlsx --sheet "Ozon - сентябрь"      приёмка против ручного листа

Лист 1 «Ozon - <месяц>» — по дням и «Итого». Что измеряет каждая колонка:

    Оборот            marketplace_buyouts.buyouts_amount_seller за день реализации, с НДС (как в ручном листе)
    Комиссия          marketplace_buyouts.commission_amount (sale_commission начислений), с НДС
    Выручка           (Оборот − Комиссия) / НДС
    Себестоимость     article_unit_costs (снимок 1С, --snapshot) × ПОЗИЦИИ выкупов; sku → article по заказам
    Логистика         статья logistics из marketplace_expenses / НДС
    Эквайринг         тип 1 из сырья accrual by-day / НДС (в базе он внутри статьи other)
    Подписка          статья subscription (типы 51, 52, 74) / НДС
    Реклама           типы 41 + 54 из сырья accrual by-day / НДС — «на круг», решение владельца 5 от 2026-09-19
    ДРР %             Реклама / (Оборот / НДС) — формула ручного листа, «от ТО»
    Прочее            (статьи other + external_promo − тип 1) / НДС; незнакомая статья — сюда же И называется вслух
    Фин. рез.         Маржа − Логистика − Эквайринг − Подписка − Реклама − Прочее
    справочно         «Реклама по образцу» = (41 + 54 + 51 + 96) / НДС — так считает ручной лист;
                      «Логистика + Прочее по образцу» = (logistics + other − тип 1 − тип 96) / НДС —
                      у них это две строки одной группы, граница между ними проведена не по type_id

Лист 2 «По SKU» — daily_sku_kpi за месяц; реклама там из Performance API (решение 5), поэтому итог
рекламы двух листов различается — разница и причина написаны внизу листа.

НДС — параметр с датой действия (VAT_RATES), не константа в формуле. Даты моложе двух суток
помечаются: начисления доезжают. Сырьё by-day: data/accrual_history/<день>.json; нет файла — один
сбор дня из API (1–3 обращения, метод листает по last_id), для дат моложе двух суток файл НЕ
сохраняется, иначе недоехавший день застыл бы на диске. В окне 00:15…03:15 UTC в API не идёт.
В БД не пишет: db_writes = 0.
"""
import argparse
import calendar
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from loaders import ozon_finance_accrual as accrual  # noqa: E402
from reconcile_manual_report_ozon import read_manual_sheet, type_sums  # noqa: E402

Z = Decimal(0)
C = Decimal("0.01")
SNAP = "2026-05-20"
OUT_DIR = os.path.join("data", "reports")
RAW_DIR = os.path.join("data", "accrual_history")
YOUNG_DAYS = 2                     # сверка берёт дату не моложе двух суток: начисления доезжают (CLAUDE.md §2)
ACQUIRING_TYPE, PREMIUM_TYPE, REVIEWS_TYPE = 1, 51, 96
KNOWN_ARTICLES = {"logistics", "other", "subscription", "external_promo", "commission"}

# НДС с датой действия: (с какой даты, коэффициент). Лист декабря 2025 считался с 1,2.
VAT_RATES = (("2026-01-01", Decimal("1.22")), ("0001-01-01", Decimal("1.20")))

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def D(v):
    return Decimal(str(v or 0))


def q(v):
    return Decimal(v).quantize(C)


def vat_for(day):
    for valid_from, rate in VAT_RATES:
        if day >= valid_from:
            return rate
    raise ValueError(f"нет ставки НДС для {day}")


def month_days(month, date_to=None):
    y, m = int(month[:4]), int(month[5:7])
    last = date(y, m, calendar.monthrange(y, m)[1])
    if date_to:
        last = min(last, date.fromisoformat(date_to))
    out, d = [], date(y, m, 1)
    while d <= last:
        out.append(d.isoformat()); d += timedelta(days=1)
    return out


def ratio(a, b):
    return (a / b) if b else None


def build_daily(days, buyouts, expenses, types_by_day, unit_cost, today):
    """Строки листа по дням. Чистая функция: на входе строки таблиц и суммы сырья по type_id.

    buyouts      [{buyout_date, marketplace_sku, buyouts_qty, buyouts_amount_seller, commission_amount}]
    expenses     [{expense_date, expense_type, expense_amount}]
    types_by_day {день: {type_id: расход, положителен при списании}}; дня нет — сырья нет
    unit_cost    sku -> Decimal | None
    """
    acc = {d: defaultdict(Decimal) for d in days}
    no_cost = defaultdict(int)
    unknown = defaultdict(Decimal)
    for r in buyouts:
        d = r["buyout_date"]
        if d not in acc:
            continue
        qty = D(r["buyouts_qty"])
        acc[d]["positions"] += qty
        acc[d]["turnover"] += D(r["buyouts_amount_seller"])
        acc[d]["commission"] += D(r["commission_amount"])
        uc = unit_cost(r["marketplace_sku"])
        if uc is None:
            no_cost[d] += int(qty)
        else:
            acc[d]["cogs"] += qty * uc
    for r in expenses:
        d, t, v = r["expense_date"], str(r["expense_type"] or ""), D(r["expense_amount"])
        if d not in acc:
            continue
        if t.startswith("advertising") or t == "commission":
            continue            # реклама Performance живёт на листе «По SKU»; комиссия уже взята из выкупов
        if t not in KNOWN_ARTICLES:
            unknown[t] += v     # незнакомая статья идёт в прочее И называется вслух
            t = "other"
        acc[d]["exp_" + t] += v

    rows = []
    for d in days:
        a, vat, ts = acc[d], vat_for(d), types_by_day.get(d)
        row = {"date": d, "vat": vat, "has_raw": ts is not None,
               "young": (date.fromisoformat(today) - date.fromisoformat(d)).days < YOUNG_DAYS,
               "positions": a["positions"], "no_cost_positions": no_cost.get(d, 0)}
        row["turnover"], row["commission"] = a["turnover"], a["commission"]
        row["revenue"] = (a["turnover"] - a["commission"]) / vat
        row["cogs"] = a["cogs"]
        row["margin"] = row["revenue"] - row["cogs"]
        row["logistics"] = a["exp_logistics"] / vat
        row["subscription"] = a["exp_subscription"] / vat
        other_gross = a["exp_other"] + a["exp_external_promo"]
        if ts is None:
            # без сырья эквайринг от прочего не отделить, рекламы из начислений нет — не ноль, а пусто
            row["acquiring"] = row["ads"] = row["ads_like_manual"] = row["log_other_like_manual"] = None
            row["other"] = other_gross / vat
        else:
            acq = ts.get(ACQUIRING_TYPE, Z)
            ads = sum((ts.get(t, Z) for t in accrual.AD_TYPE_IDS), Z)
            row["acquiring"] = acq / vat
            row["ads"] = ads / vat
            row["other"] = (other_gross - acq) / vat
            row["ads_like_manual"] = (ads + ts.get(PREMIUM_TYPE, Z) + ts.get(REVIEWS_TYPE, Z)) / vat
            row["log_other_like_manual"] = (a["exp_logistics"] + a["exp_other"] - acq - ts.get(REVIEWS_TYPE, Z)) / vat
            # Два источника на одной строке: статьи — из базы (ночь обновляет окно 30 дней), типы 1 / 41 / 54 — из файла
            # сырья, застывшего в день сбора. Начисления доезжают неделями, поэтому расхождение называется вслух:
            # эквайринг и реклама такого дня могут быть неполны, а «Прочее» — завышено на ту же сумму.
            raw_articles = defaultdict(Decimal)
            for type_id, v in ts.items():
                article = accrual.TYPE_TO_EXPENSE.get(type_id)
                if article:
                    raw_articles[article] += v
            row["db_minus_raw"] = {art: q(a["exp_" + art] - raw_articles[art]) for art in ("logistics", "other", "subscription", "external_promo")
                                   if q(a["exp_" + art]) != q(raw_articles[art])}
        row["fin_result"] = None if ts is None else (
            row["margin"] - row["logistics"] - row["acquiring"] - row["subscription"] - row["ads"] - row["other"])
        rows.append(row)
    return rows, dict(unknown)


MONEY = ["turnover", "commission", "revenue", "cogs", "margin", "logistics", "acquiring", "subscription", "ads", "other",
         "fin_result", "ads_like_manual", "log_other_like_manual", "positions"]


def add_ratios(row):
    rev = row.get("revenue") or Z
    row["commission_pct"] = ratio(row["commission"], row["turnover"])
    row["margin_pct"] = ratio(row["margin"], rev)
    row["logistics_pct"] = ratio(row["logistics"], rev)
    row["acquiring_pct"] = None if row.get("acquiring") is None else ratio(row["acquiring"], rev)
    row["drr_pct"] = None if row.get("ads") is None else ratio(row["ads"], row["turnover"] / row["vat"]) if row["turnover"] else None
    row["fin_result_pct"] = None if row.get("fin_result") is None else ratio(row["fin_result"], rev)
    return row


def total_row(rows):
    """«Итого»: деньги — суммой, проценты — от сумм. Колонка пуста хоть за один день — в итоге она неполна, и это видно."""
    t = {"date": "Итого", "vat": rows[-1]["vat"] if rows else vat_for("2026-01-01"), "young": False, "has_raw": all(r["has_raw"] for r in rows),
         "no_cost_positions": sum(r["no_cost_positions"] for r in rows)}
    for k in MONEY:
        vals = [r.get(k) for r in rows]
        t[k] = None if any(v is None for v in vals) else sum(vals, Z)
    # ДРР в итоге — по формуле листа от сумм; при смене НДС внутри месяца оборот без НДС считается по дням
    t["_turnover_net"] = sum((r["turnover"] / r["vat"] for r in rows), Z)
    add_ratios(t)
    t["drr_pct"] = None if t["ads"] is None else ratio(t["ads"], t["_turnover_net"])
    return t


def build_sku(kpi_rows, sku2art, unit_cost, vat):
    """Лист «По SKU»: daily_sku_kpi за месяц, реклама — Performance API (ad_spend)."""
    by = defaultdict(lambda: defaultdict(Decimal))
    names = {}
    for r in kpi_rows:
        sku = str(r.get("marketplace_sku") or "")
        a = by[sku]
        a["positions"] += D(r.get("buyouts_qty")); a["turnover"] += D(r.get("buyouts_amount_seller"))
        a["commission"] += D(r.get("commission_amount")); a["ads_gross"] += D(r.get("ad_spend"))
        a["logistics_gross"] += D(r.get("logistics_amount")); a["other_gross"] += D(r.get("other_expenses_amount"))
        if sku not in names and (r.get("article") or r.get("product_name")):
            names[sku] = (r.get("article") or "", r.get("product_name") or "")
    out = []
    for sku, a in by.items():
        if not any(a.values()):
            continue
        uc = unit_cost(sku) if sku else None
        row = {"sku": sku or "(без SKU)", "article": sku2art.get(sku) or names.get(sku, ("", ""))[0], "name": names.get(sku, ("", ""))[1],
               "positions": a["positions"], "turnover": a["turnover"], "commission": a["commission"],
               "revenue": (a["turnover"] - a["commission"]) / vat,
               "cogs": None if (uc is None and a["positions"]) else a["positions"] * (uc or Z),
               "ads": a["ads_gross"] / vat, "logistics": a["logistics_gross"] / vat, "other": a["other_gross"] / vat}
        row["margin"] = None if row["cogs"] is None else row["revenue"] - row["cogs"]
        row["margin_pct"] = None if row["margin"] is None else ratio(row["margin"], row["revenue"])
        row["drr_pct"] = ratio(row["ads"], row["turnover"] / vat) if row["turnover"] else None
        out.append(row)
    out.sort(key=lambda r: (-r["turnover"], r["sku"]))
    return out


# ---------- ввод-вывод ----------

def fetch(sb, table, select, filters, orders):
    """Постраничное чтение с сортировкой по ПОЛНОМУ уникальному ключу таблицы.

    Без order() PostgREST отдаёт страницы с повторами и пропусками (how-we-work, 15 сентября).
    Сортировка по id при фильтре по дате упирается в statement_timeout 8 с: планировщик идёт по
    индексу id через всю таблицу. Ключ (дата, площадка, sku[, …]) совпадает с уникальным индексом —
    он обслуживает и фильтр, и порядок.
    """
    out, page = [], 0
    while True:
        qb = sb.table(table).select(select)
        for f in filters:
            qb = getattr(qb, f[0])(*f[1:])
        for col in orders:
            qb = qb.order(col)
        res = qb.range(page * 1000, page * 1000 + 999).execute()
        out.extend(res.data)
        if len(res.data) < 1000:
            return out
        page += 1


def load_day_raw(day, today, allow_fetch, counters):
    """Начисления дня: файл → API. Возвращает (accruals | None, откуда)."""
    import json
    path = os.path.join(RAW_DIR, f"{day}.json")
    if os.path.exists(path):
        data = json.load(open(path))
        return (data["accruals"] if isinstance(data, dict) else data), "файл"
    if not allow_fetch:
        return None, "нет сырья"
    import fetch_accrual_postings_raw as raw
    if raw.in_night_window(datetime.now(timezone.utc)):
        print(f"  {day}: сырья нет, а сейчас окно ночного прогона 00:15…03:15 UTC — в API не иду")
        return None, "нет сырья (ночное окно)"
    accruals = raw.fetch_day(day, counters, 60)
    if (date.fromisoformat(today) - date.fromisoformat(day)).days < YOUNG_DAYS:
        return accruals, "API, без файла (моложе двух суток)"
    os.makedirs(RAW_DIR, exist_ok=True)
    json.dump({"date": day, "fetched_at": datetime.now(timezone.utc).isoformat(), "accruals": accruals}, open(path, "w"), ensure_ascii=False)
    return accruals, "API → файл"


def load_costs(sb, snapshot):
    orders = fetch(sb, "marketplace_orders", "id,marketplace_sku,article,orders_qty,cancelled_orders_qty",
                   [("eq", "marketplace_code", "ozon"), ("gte", "order_date", "2026-03-28")],
                   ["order_date", "marketplace_code", "marketplace_sku", "order_schema"])
    sku_art = defaultdict(lambda: defaultdict(Decimal))
    for r in orders:
        # вес — созданные (подтв. + отм.): после пересборки у полностью отменённых ключей orders_qty = 0, а артикул у них тот же
        sku_art[str(r["marketplace_sku"])][str(r["article"] or "")] += D(r["orders_qty"]) + D(r.get("cancelled_orders_qty"))
    sku2art = {s: max(sorted(a.items()), key=lambda kv: kv[1])[0] for s, a in sku_art.items()}
    norms = sorted({a.lower() for a in sku2art.values() if a})
    cost = {}
    for i in range(0, len(norms), 500):
        res = sb.table("article_unit_costs").select("offer_id_norm,unit_cost").eq("snapshot_date", snapshot).in_("offer_id_norm", norms[i:i + 500]).order("offer_id_norm").execute()
        for r in res.data:
            cost[r["offer_id_norm"]] = D(r["unit_cost"])

    def unit_cost(sku):
        art = sku2art.get(str(sku))
        return cost.get(art.lower()) if art else None
    return sku2art, unit_cost, len(cost), len(norms)


def write_xlsx(path, month, rows, total, sku_rows, notes, sku_notes):
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    money, pct = "#,##0.00", "0.0%"
    bold, young_fill, head_fill = Font(bold=True), PatternFill("solid", fgColor="FFF2CC"), PatternFill("solid", fgColor="D9E1F2")
    wb = openpyxl.Workbook()
    ws = wb.active
    title = f"Ozon - {MONTHS[int(month[5:7]) - 1]}"
    ws.title = title
    ws["A1"] = "Выкупы Ozon"; ws["A1"].font = bold
    cols = [("Дата реализации", "date", None), ("Оборот - выкупы", "turnover", money), ("Комиссия (с НДС), руб.", "commission", money), ("Комиссия, %", "commission_pct", pct),
            ("Выручка, руб.", "revenue", money), ("Себестоимость, руб.", "cogs", money), ("Маржа, руб.", "margin", money), ("Мар-ть, %", "margin_pct", pct),
            ("Логистика, руб.", "logistics", money), ("% Логистики", "logistics_pct", pct), ("Эквайринг, руб.", "acquiring", money), ("% Эквайринга", "acquiring_pct", pct),
            ("Подписка, руб.", "subscription", money), ("Реклама, руб.", "ads", money), ("% ДРР (от ТО)", "drr_pct", pct), ("Прочее, руб.", "other", money),
            ("Фин. рез., руб.", "fin_result", money), ("% Фин. рез.", "fin_result_pct", pct), (None, None, None),
            ("справочно: Реклама по образцу", "ads_like_manual", money), ("справочно: Логистика + Прочее по образцу", "log_other_like_manual", money),
            ("Позиций выкупов", "positions", "#,##0"), ("из них без себестоимости", "no_cost_positions", "#,##0"), ("Примечание", "note", None)]
    for j, (head, _k, _f) in enumerate(cols, 1):
        if head:
            c = ws.cell(row=3, column=j, value=head); c.font = bold; c.fill = head_fill
            c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    for i, r in enumerate(rows + [total], 4):
        r = dict(r)
        drift = r.get("db_minus_raw") or {}
        r["note"] = "; ".join(x for x in ("моложе двух суток — начисления доезжают" if r.get("young") else "",
                                           ("база и файл сырья расходятся (база − файл): " + ", ".join(f"{k} {v:+,.2f}" for k, v in drift.items())
                                            + " (с НДС) — эквайринг и реклама дня могут быть неполны") if drift else "",
                                           "" if r.get("has_raw") else ("нет сырья начислений — эквайринг внутри прочего, рекламы нет" if r["date"] != "Итого" else "есть дни без сырья — итог неполон")) if x)
        for j, (head, k, fmt) in enumerate(cols, 1):
            if not head:
                continue
            v = r.get(k)
            if k == "date" and v != "Итого":
                v = date.fromisoformat(v)
            elif isinstance(v, Decimal):
                v = float(v)
            c = ws.cell(row=i, column=j, value=v)
            if fmt:
                c.number_format = fmt
            if k == "date" and r["date"] != "Итого":
                c.number_format = "DD.MM.YYYY"
            if r.get("young"):
                c.fill = young_fill
            if r["date"] == "Итого":
                c.font = bold
    base = 4 + len(rows) + 2
    for n, line in enumerate(notes):
        ws.cell(row=base + n, column=1, value=line)
    ws.freeze_panes = "B4"
    ws.row_dimensions[3].height = 45
    for j in range(1, len(cols) + 1):
        ws.column_dimensions[get_column_letter(j)].width = 14 if j > 1 else 16
    ws.column_dimensions[get_column_letter(len(cols))].width = 60

    ws2 = wb.create_sheet("По SKU")
    cols2 = [("SKU", "sku", None), ("Артикул", "article", None), ("Товар", "name", None), ("Позиций", "positions", "#,##0"), ("Оборот - выкупы", "turnover", money),
             ("Комиссия (с НДС)", "commission", money), ("Выручка", "revenue", money), ("Себестоимость", "cogs", money), ("Маржа", "margin", money), ("Мар-ть, %", "margin_pct", pct),
             ("Реклама (Performance)", "ads", money), ("% ДРР (от ТО)", "drr_pct", pct), ("Логистика", "logistics", money), ("Прочее (вкл. эквайринг и подписку)", "other", money)]
    for j, (head, _k, _f) in enumerate(cols2, 1):
        c = ws2.cell(row=1, column=j, value=head); c.font = bold; c.fill = head_fill
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    tot = {"sku": "Итого", "article": "", "name": ""}
    for k in ("positions", "turnover", "commission", "revenue", "ads", "logistics", "other"):
        tot[k] = sum((r[k] for r in sku_rows), Z)
    tot["cogs"] = sum((r["cogs"] for r in sku_rows if r["cogs"] is not None), Z)
    tot["margin"] = tot["revenue"] - tot["cogs"]
    tot["margin_pct"] = ratio(tot["margin"], tot["revenue"])
    vat = rows[0]["vat"] if rows else vat_for(month + "-01")
    tot["drr_pct"] = ratio(tot["ads"], tot["turnover"] / vat) if tot["turnover"] else None
    for i, r in enumerate(sku_rows + [tot], 2):
        for j, (_head, k, fmt) in enumerate(cols2, 1):
            v = r.get(k)
            c = ws2.cell(row=i, column=j, value=float(v) if isinstance(v, Decimal) else v)
            if fmt:
                c.number_format = fmt
            if r["sku"] == "Итого":
                c.font = bold
    for n, line in enumerate(sku_notes):
        ws2.cell(row=len(sku_rows) + 4 + n, column=1, value=line)
    ws2.freeze_panes = "D2"
    ws2.row_dimensions[1].height = 45
    for j, w in enumerate([14, 18, 40] + [15] * (len(cols2) - 3), 1):
        ws2.column_dimensions[get_column_letter(j)].width = w
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wb.save(path)
    return tot


# ---------- приёмка против ручного листа ----------

EXACT = (("Оборот", "turnover", "turnover"), ("Комиссия", "commission", "commission"), ("Выручка", "revenue", "revenue"),
         ("Эквайринг", "acquiring", "acquiring"), ("Реклама по образцу = их «Реклама»", "ads_like_manual", "ads"))


def check(rows, manual):
    """Сверка с ручным листом. Возвращает (таблица строк, число нарушений в колонках, обязанных сходиться до копейки)."""
    out, failures = [], 0

    def line(title, ours_fn, theirs_fn, must_match, why=""):
        nonlocal failures
        t_o = t_t = Z
        diffs = []
        for r in rows:
            m = manual.get(r["date"])
            o = ours_fn(r)
            t = theirs_fn(m) if m else None
            if o is None or t is None:
                diffs.append((r["date"], None)); continue
            o, t = q(o), q(t)
            t_o += o; t_t += t
            if o != t:
                diffs.append((r["date"], o - t))
        bad = [(d, v) for d, v in diffs if v is not None]
        missing = [d for d, v in diffs if v is None]
        if must_match and (bad or missing):
            failures += 1
        out.append({"title": title, "theirs": t_t, "ours": t_o, "diff": t_o - t_t, "equal_days": len(rows) - len(bad) - len(missing),
                    "days": len(rows), "bad": bad, "missing": missing, "must": must_match, "why": why})

    g = lambda k: (lambda m: None if m.get(k) is None else D(m[k]))  # noqa: E731
    for title, ours_k, theirs_k in EXACT:
        line(title, lambda r, k=ours_k: r.get(k), g(theirs_k), True)
    line("Логистика + Прочее по образцу = их Логистика + Прочее", lambda r: r.get("log_other_like_manual"),
         lambda m: None if m.get("logistics") is None or m.get("other") is None else D(m["logistics"]) + D(m["other"]), False,
         "граница между их двумя строками проведена не по type_id; тип 25 ItemCompensation у них внутри прочего, у нас вне расходов")
    line("Себестоимость (наш снимок 1С против их цен)", lambda r: r.get("cogs"), g("cogs"), False,
         "решение 4 от 2026-09-19: остаёмся на снимке 05-20, расхождение принимается как известное")
    line("Логистика (наша статья против их строки)", lambda r: r.get("logistics"), g("logistics"), False, "границу не повторяем — решение 7")
    line("Прочее (наше без эквайринга против их строки)", lambda r: r.get("other"), g("other"), False, "границу не повторяем — решение 7")
    line("Реклама (41 + 54) против их «Рекламы»", lambda r: r.get("ads"), g("ads"), False, "у них в рекламе ещё подписка Premium (51) и сбор отзывов (96)")
    return out, failures


def print_check(table):
    print(f"\n{'колонка':58}{'у них':>17}{'у нас':>17}{'разница':>15}{'дней 0,00':>11}")
    for t in table:
        print(f"{t['title'][:57]:58}{t['theirs']:>17,.2f}{t['ours']:>17,.2f}{t['diff']:>15,.2f}{t['equal_days']:>7} из {t['days']}"
              + ("   ОБЯЗАНА СХОДИТЬСЯ" if t["must"] and (t["bad"] or t["missing"]) else ""))
        if t["bad"] and (t["must"] or len(t["bad"]) <= 6):
            print("      расходится: " + ", ".join(f"{d} {v:+,.2f}" for d, v in t["bad"]))
        if t["missing"]:
            print("      нет значения: " + ", ".join(t["missing"]))
        if t["why"] and (t["bad"] or t["missing"]):
            print(f"      чем объясняется: {t['why']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--date-to", help="последний день листа; по умолчанию — вчера по местному времени: сегодняшний день ночь ещё не грузила")
    ap.add_argument("--out")
    ap.add_argument("--snapshot", default=SNAP)
    ap.add_argument("--no-fetch", action="store_true", help="сырьё только из файлов, в API не ходить")
    ap.add_argument("--check", action="store_true", help="сверить с ручным листом (--xlsx, --sheet); код возврата 1, если обязанные колонки не сошлись")
    ap.add_argument("--xlsx"); ap.add_argument("--sheet")
    args = ap.parse_args()
    # «Сегодня» — по местному времени, как у загрузчиков. Сегодняшнего дня в базе нет (ночь грузит по вчера), а в
    # сырье начислений он уже есть: строка с рекламой и эквайрингом без оборота врала бы, поэтому лист кончается вчера.
    today_date = datetime.now(timezone.utc).astimezone(ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Moscow"))).date()
    today = today_date.isoformat()
    last = args.date_to or (today_date - timedelta(days=1)).isoformat()
    days = [d for d in month_days(args.month, last) if d <= today]
    if not days:
        raise SystemExit("в окне нет ни одного наступившего дня")
    d1, d2 = days[0], days[-1]

    import loaders.ozon_fbo_orders_loader as fbo
    sb = fbo.supabase
    flt = lambda col: [("eq", "marketplace_code", "ozon"), ("gte", col, d1), ("lte", col, d2)]  # noqa: E731
    buyouts = fetch(sb, "marketplace_buyouts", "id,buyout_date,marketplace_sku,buyouts_qty,buyouts_amount_seller,commission_amount", flt("buyout_date"),
                    ["buyout_date", "marketplace_code", "marketplace_sku"])
    expenses = fetch(sb, "marketplace_expenses", "id,expense_date,expense_type,expense_amount", flt("expense_date"),
                     ["expense_date", "marketplace_code", "marketplace_sku", "expense_type"])
    kpi = fetch(sb, "daily_sku_kpi", "id,kpi_date,marketplace_sku,article,product_name,buyouts_qty,buyouts_amount_seller,commission_amount,ad_spend,logistics_amount,other_expenses_amount",
                flt("kpi_date"), ["kpi_date", "marketplace_code", "marketplace_sku"])
    sku2art, unit_cost, cost_found, cost_asked = load_costs(sb, args.snapshot)

    counters, sources, types_by_day = {"requests": 0, "429": 0}, defaultdict(list), {}
    for d in days:
        accruals, where = load_day_raw(d, today, not args.no_fetch, counters)
        sources[where].append(d)
        if accruals is not None:
            types_by_day[d] = type_sums(accruals)

    rows, unknown = build_daily(days, buyouts, expenses, types_by_day, unit_cost, today)
    for r in rows:
        add_ratios(r)
    total = total_row(rows)
    vat = vat_for(d1)
    sku_rows = build_sku(kpi, sku2art, unit_cost, vat)

    print(f"окно {d1} … {d2} ({len(days)} дн.): выкупов строк {len(buyouts)}, расходов {len(expenses)}, строк витрины {len(kpi)}; "
          f"sku→article {len(sku2art)}, себестоимость найдена у {cost_found} артикулов из {cost_asked} (снимок {args.snapshot}); НДС {vat}")
    for where, ds in sources.items():
        print(f"  сырьё by-day — {where}: {len(ds)} дн." + (f" ({ds[0]} … {ds[-1]})" if len(ds) > 1 else f" ({ds[0]})"))
    print(f"  обращений к Seller API {counters['requests']}, 429 — {counters['429']}")
    if unknown:
        print("ВНИМАНИЕ: незнакомые статьи расходов, учтены в «Прочем»:", {k: f"{v:,.2f}" for k, v in unknown.items()})
    drifted = [(r["date"], r["db_minus_raw"]) for r in rows if r.get("db_minus_raw")]
    if drifted:
        print("  база и файлы сырья расходятся по статьям (статьи — из базы на момент ночи, типы — из файла на момент его сбора; начисления доезжают,\n"
              "  поэтому эквайринг и реклама этих дней могут быть неполны; «+» — в базе больше, чем в файле):")
        for d, drift in drifted:
            print(f"      {d}: " + ", ".join(f"{k} {v:+,.2f}" for k, v in drift.items()))
    young = [r["date"] for r in rows if r["young"]]
    if young:
        print(f"  моложе двух суток (начисления доезжают, в листе помечены): {', '.join(young)}")
    # статья subscription против типа 51: «Подписка» листа — вся статья; если в ней не только Premium, сказать
    sub_article = sum((D(r["expense_amount"]) for r in expenses if r["expense_type"] == "subscription"), Z)
    sub_51 = sum((ts.get(PREMIUM_TYPE, Z) for ts in types_by_day.values()), Z)
    if types_by_day and len(types_by_day) == len(days) and q(sub_article) != q(sub_51):
        print(f"  подписка: статья {sub_article:,.2f} против типа 51 в сырье {sub_51:,.2f}, разница {sub_article - sub_51:,.2f} — в статье есть типы 52 / 74 либо начисления доехали после сбора")

    ads_sheet1 = total["ads"]
    ads_sku = sum((r["ads"] for r in sku_rows), Z)
    # разница рекламы двух листов — по дням, чтобы объяснение стояло рядом с числом
    perf_by_day = defaultdict(Decimal)
    for r in kpi:
        perf_by_day[r["kpi_date"]] += D(r.get("ad_spend")) / vat_for(r["kpi_date"])
    ad_gaps = sorted(((r["date"], perf_by_day.get(r["date"], Z) - r["ads"]) for r in rows if r.get("ads") is not None),
                     key=lambda kv: -abs(kv[1]))
    ad_gaps = [(d, v) for d, v in ad_gaps if abs(v) >= 1]
    sku_notes = [f"Реклама на этом листе — Performance API (ad_spend витрины), без НДС: {ads_sku:,.2f}.",
                 (f"На листе «Ozon - …» реклама из начислений (типы 41 + 54), без НДС: {ads_sheet1:,.2f}; разница {ads_sku - ads_sheet1:+,.2f} — "
                  "Performance против начислений: день без сбора Performance даёт недобор (за 2026-09-14 в Performance пусто — известная дыра), "
                  "начисления ведут учёт по своей дате.") if ads_sheet1 is not None else "Реклама из начислений за окно неполна (есть дни без сырья) — разницу не считаю.",
                 ("Разница по дням (Performance − начисления, без НДС): " + "; ".join(f"{d} {v:+,.2f}" for d, v in ad_gaps[:8])
                  + (f"; ещё {len(ad_gaps) - 8} дн. на {sum((v for _d, v in ad_gaps[8:]), Z):+,.2f}" if len(ad_gaps) > 8 else "")) if ad_gaps else "По дням реклама двух источников сходится до рубля.",
                 "Прочее здесь — other_expenses_amount витрины: эквайринг, подписка и внешнее продвижение внутри; строка «(без SKU)» — расходы без товара."]
    notes = [f"Источники: marketplace_buyouts, marketplace_expenses, article_unit_costs (снимок {args.snapshot}, позиции выкупов), сырьё accrual by-day (типы 1, 41, 54, 51, 96). НДС {vat}.",
             "Реклама = начисления 41 + 54. «Реклама по образцу» = (41 + 54 + 51 + 96) / НДС — так считал ручной лист. «Логистика + Прочее по образцу» = (логистика + прочее − тип 1 − тип 96) / НДС.",
             "Даты, залитые жёлтым, моложе двух суток: начисления ещё доезжают, числа вырастут.",
             f"Собрано {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}; scripts/report_ozon_month.py."]
    out = args.out or os.path.join(OUT_DIR, f"ozon_{args.month}.xlsx")
    write_xlsx(out, args.month, rows, total, sku_rows, notes, sku_notes)
    print(f"\nитого: оборот {total['turnover']:,.2f}, комиссия {total['commission']:,.2f}, выручка {total['revenue']:,.2f}, СС {total['cogs']:,.2f}, маржа {total['margin']:,.2f}")
    if total["fin_result"] is not None:
        print(f"       логистика {total['logistics']:,.2f}, эквайринг {total['acquiring']:,.2f}, подписка {total['subscription']:,.2f}, реклама {total['ads']:,.2f}, "
              f"прочее {total['other']:,.2f}, фин. рез. {total['fin_result']:,.2f}")
    print(f"лист «По SKU»: строк {len(sku_rows)}, реклама Performance {ads_sku:,.2f}" + (f" против начислений {ads_sheet1:,.2f}, разница {ads_sku - ads_sheet1:+,.2f}" if ads_sheet1 is not None else ""))
    if ad_gaps:
        print("       по дням (Performance − начисления, без НДС): " + "; ".join(f"{d} {v:+,.2f}" for d, v in ad_gaps[:8])
              + (f"; ещё {len(ad_gaps) - 8} дн. на {sum((v for _d, v in ad_gaps[8:]), Z):+,.2f}" if len(ad_gaps) > 8 else ""))
    print(f"записано: {out}")

    code = 0
    if args.check:
        if not args.xlsx or not args.sheet:
            raise SystemExit("--check требует --xlsx и --sheet")
        manual = {d: {k: (None if v is None else Decimal(v)) for k, v in row.items()} for d, row in read_manual_sheet(args.xlsx, args.sheet).items()}
        table, failures = check(rows, manual)
        print_check(table)
        print(f"\nприёмка: колонок, обязанных сходиться до копейки, — {len(EXACT)}, не сошлось {failures}")
        code = 1 if failures else 0
    print("db_writes = 0")
    sys.exit(code)


if __name__ == "__main__":
    main()
