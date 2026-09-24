#!/usr/bin/env python3
"""Книга «Фин рез» в форме второго кабинета — Ozon-часть (тридцать седьмая §2, §4).

    venv/bin/python3 scripts/report_finrez.py --month-from 2026-04 --month-to 2026-09 [--date-to 2026-09-23] [--out …]
    venv/bin/python3 scripts/report_finrez.py … --check [--book data/reports/ozon_2026-09_to_2026-09-23.xlsx]

Образец — книги владельца data/owner_finrez_ozon_buyouts.xlsx («Вывод данных») и data/owner_finrez_orders.xlsx («Свод»,
«Коэффициенты»): это другой кабинет, числа не наши; приёмка — форма один в один и тождества с нашими листами. Листы:

  «Данные Ozon выкупы»   строка на (дата, SKU): начисления с НДС, знак как в сводной владельца (списания < 0, товарооборот > 0),
                         Ст-ть продаж в себестоимости (штуки × СС снимка 1С × индекс СС), Соинвест (bonus + coinvestment), Штуки.
                         Источники: marketplace_buyouts; marketplace_expenses по SKU; реклама — Performance по SKU (ad_spend витрины,
                         решение 5); леджер начислений без SKU — строкой «(без SKU)» на день: реклама 41 + 54 минус Performance по SKU,
                         эквайринг целиком, остатки логистики / прочего против типов — чтобы Σ листа = «Ozon - месяц» по каждой колонке.
  «Выкупы Ozon»          строки — месяцы, дни последнего месяца, «Общий итог»; колонки ровно как у владельца: по статьям
                         (Комиссия, Логистика, Прочее, Реклама, Товарооборот, Эквайринг) «Начисления» и «Ст-ть продаж в себ-ти», справа
                         расчёт без НДС — формулы как на «Ozon - месяц» (НДС по дате). «Прочее» здесь включает подписку — у владельца
                         отдельной статьи подписки нет. Справочно правее: Соинвест, Штуки.
  «Выкупы Ozon × бренд», «× категория»   те же колонки блоками; бренд — KARATOV / Топаз по букве артикула, категория — карточка Ozon
                         (ozon_products или снимок data/ozon_products/catalog_latest.json; нет ни того, ни другого — по названию, с пометкой).
  «Заказы»               форма «Свод» владельца: месяцы и дни последнего месяца × блок Ozon (WB — пусто с пометкой, модуль WB-сессии):
                         Оборот (созданные, с НДС) · шт · цена продавца · комиссия % · выручка · СС · маржа · м-ть · реклама · ДРР · соинвест % ·
                         гр. фин. рез · %. Коэффициенты — НАШИ измеренные (лист «Коэффициенты»), образец владельца рядом справочно.
  «Данные заказы»        строка на (дата, SKU) из marketplace_orders: создано ₽ и шт, подтверждено ₽, по цене покупателя ₽, реклама по SKU,
                         СС созданного (шт × СС × индекс), бренд, категория, наименование; «(без SKU)» на день — остаток рекламы леджера.

--check: Σ листа выкупов за --check-month (по умолчанию последний) против build_daily генератора месяца на тех же днях по каждой
колонке; Σ брендов = Σ категорий = общий; число строк «Данные» = ключей выкупов + строк без SKU; при --book — заказы против листа
«Заказы» утренней книги (создано ₽ и шт, реклама, соинвест Standard). Только чтение; в API не ходит; db_writes = 0.
"""
import argparse
import calendar
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
import report_ozon_month as rep  # noqa: E402
import ozon_product_catalog as catalog_rules  # noqa: E402

Z, D, q, vat_for = rep.Z, rep.D, rep.q, rep.vat_for
MONTH_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
ARTICLES = ("Комиссия", "Логистика", "Прочее", "Реклама", "Товарооборот", "Эквайринг")       # порядок колонок владельца
NO_SKU = "(без SKU)"
PLATFORM = "Ozon"
OWNER_COEF = {"buyout": (("Ozon", Decimal("0.84")), ("WB", Decimal("0.86"))),
              "commission": (("Ozon (<300)", Decimal("0.23")), ("Ozon (>300)", Decimal("0.52")), ("WB", Decimal("0.43")))}
MATURE_AGE = 21
CATALOG_FILE = os.path.join(ROOT, "data", "ozon_products", "catalog_latest.json")
OUT_DIR = os.path.join(ROOT, "data", "reports")


def month_label(day):
    return MONTH_SHORT[int(day[5:7]) - 1]


def day_label(day):
    return f"{day[8:10]}.{month_label(day)}"


def days_between(month_from, month_to, date_to=None):
    y, m = int(month_to[:4]), int(month_to[5:7])
    last = date(y, m, calendar.monthrange(y, m)[1])
    if date_to:
        last = min(last, date.fromisoformat(date_to))
    d, out = date(int(month_from[:4]), int(month_from[5:7]), 1), []
    while d <= last:
        out.append(d.isoformat()); d += timedelta(days=1)
    return out


def cogs_index(day):
    return rep.cost_index_for(day) or Decimal(1)


# ---------- справочники ----------

def load_catalog(sb):
    """{sku: {category, brand, metal, name, offer_id}} и источник: table / file / none."""
    try:
        rows = rep.fetch(sb, "ozon_products", "sku,category,brand,metal,name,offer_id", [], ["sku"])
        return {str(r["sku"]): r for r in rows}, "table"
    except Exception as exc:
        if "ozon_products" not in str(exc) and "PGRST205" not in str(exc) and "42P01" not in str(exc):
            raise
    if os.path.exists(CATALOG_FILE):
        snap = json.load(open(CATALOG_FILE))
        return {str(r["sku"]): r for r in snap["rows"]}, f"file {os.path.relpath(CATALOG_FILE, ROOT)} ({snap.get('observed_at', '')[:19]})"
    return {}, "none"


def describe(sku, sku2art, catalog, names):
    """(бренд, категория, артикул, наименование) для SKU; без SKU — пустые."""
    if not sku or sku == NO_SKU:
        return "", "", NO_SKU, ""
    c = catalog.get(sku) or {}
    article = c.get("offer_id") or sku2art.get(sku) or ""
    name = c.get("name") or names.get(sku) or ""
    brand = c.get("brand") or catalog_rules.brand_of(article) or ""
    category = c.get("category") or catalog_rules.category_by_name(name)
    return brand, category, article, name


# ---------- эквайринг по SKU — из сырья by-day ----------

RAW_DIR = os.path.join(ROOT, "data", "accrual_history")
ACQUIRING_TYPE = rep.ACQUIRING_TYPE


def acquiring_by_sku(days):
    """{(день, sku): эквайринг с НДС, положительный} из файлов сырья: услуга type_id 1 лежит у строки товара, в marketplace_expenses
    она свёрнута в «other» по SKU — иначе в срезах по бренду/категории эквайринг сидел бы внутри «Прочего». Дни без файла — список."""
    from loaders import ozon_finance_accrual as accrual
    out, missing = defaultdict(Decimal), []
    for d in days:
        path = os.path.join(RAW_DIR, f"{d}.json")
        if not os.path.exists(path):
            missing.append(d); continue
        data = json.load(open(path))
        for a in (data["accruals"] if isinstance(data, dict) else data):
            if str(a.get("date") or "")[:10] != d:
                continue
            for type_id, sku, amount in accrual._service_lines(a):
                if type_id == ACQUIRING_TYPE and sku:
                    out[(d, sku)] += -D(amount)                      # знак Ozon: списание < 0 → расход > 0
    return out, missing


# ---------- выкупы: строки данных ----------

def build_buyout_rows(days, buyouts, expenses, kpi_rows, daily_rows, unit_cost, sku2art, catalog, names, acq_by_sku=None):
    """Строки «Данные Ozon выкупы» + остаток без SKU на день + счётчики. Начисления с НДС, списания отрицательные.

    acq_by_sku — эквайринг по (день, sku) из сырья (acquiring_by_sku): переносится из «Прочего» строки SKU в «Эквайринг»;
    без него эквайринг по SKU остаётся внутри «Прочего», а колонка «Эквайринг» живёт только в строке «(без SKU)»."""
    dayset = set(days)
    acc = defaultdict(lambda: defaultdict(Decimal))
    stats = defaultdict(int)
    for r in buyouts:
        d, sku = r["buyout_date"], str(r["marketplace_sku"] or "")
        if d not in dayset:
            continue
        a = acc[(d, sku or NO_SKU)]
        a["turnover"] += D(r["buyouts_amount_seller"]); a["commission"] -= D(r["commission_amount"])
        units = D(r["buyouts_units"]) if r.get("buyouts_units") is not None else D(r["buyouts_qty"])
        a["units"] += units
        uc = unit_cost(sku)
        if uc is None:
            stats["positions_without_cost"] += int(D(r["buyouts_qty"]))
        else:
            a["cogs"] += units * uc * cogs_index(d)
        if r.get("bonus_amount") is None or r.get("coinvestment_amount") is None:
            stats["rows_without_coinvest"] += 1
        else:
            a["coinvest"] += D(r["bonus_amount"]) + D(r["coinvestment_amount"])
    for r in expenses:
        d, t = r["expense_date"], str(r["expense_type"] or "")
        if d not in dayset or t == "commission" or t.startswith("advertising"):
            continue
        sku = str(r.get("marketplace_sku") or "") or NO_SKU
        a = acc[(d, sku)]
        if t == "logistics":
            a["logistics"] -= D(r["expense_amount"])
        else:
            a["other"] -= D(r["expense_amount"])           # other + external_promo + subscription — у владельца одна статья «Прочее»
            if t not in rep.KNOWN_ARTICLES:
                stats[f"unknown_article:{t}"] += 1
    for (d, sku), amount in (acq_by_sku or {}).items():
        if d in dayset and (d, sku) in acc:
            acc[(d, sku)]["other"] += amount; acc[(d, sku)]["acquiring"] -= amount
            stats["acquiring_moved_rows"] += 1
    for r in kpi_rows:
        d = r["kpi_date"]
        if d in dayset and D(r.get("ad_spend")):
            acc[(d, str(r.get("marketplace_sku") or "") or NO_SKU)]["ads"] -= D(r.get("ad_spend"))
    # остаток без SKU на день: чтобы Σ листа по каждой статье равнялась «Ozon - месяц» (типы начислений), а не только Σ по SKU
    daily = {r["date"]: r for r in daily_rows}
    for d in days:
        row = daily.get(d)
        if row is None:
            stats["days_without_daily_row"] += 1
            continue
        vat = row["vat"]
        by_sku = defaultdict(Decimal)
        for (dd, sku), a in acc.items():
            if dd == d and sku != NO_SKU:
                for k in ("ads", "logistics", "other", "acquiring"):
                    by_sku[k] += a[k]
        res = acc[(d, NO_SKU)]
        if row.get("ads") is None:
            stats["days_without_raw_ads"] += 1
        else:
            res["ads"] = -(row["ads"] * vat) - by_sku["ads"]      # реклама леджера (41 + 54) минус Performance по SKU (строки витрины без SKU входят сюда же)
        if row.get("acquiring") is not None:
            res["acquiring"] = -(row["acquiring"] * vat) - by_sku["acquiring"]
        res["logistics"] = -(row["logistics"] * vat) - by_sku["logistics"]
        other_total = (row["other"] + (row.get("subscription") or Z)) * vat
        res["other"] = -other_total - by_sku["other"]
        for k in list(res):
            res[k] = q(res[k])                 # остаток — расчётный: до копейки, без хвостов Decimal вроде −5,2e-12
        if not any(res.values()):
            del acc[(d, NO_SKU)]
    rows = []
    for (d, sku), a in sorted(acc.items()):
        if not any(q(v) for v in a.values()):
            continue
        brand, category, article, name = describe(sku, sku2art, catalog, names)
        rows.append({"date": d, "month": month_label(d), "platform": PLATFORM, "brand": brand, "category": category, "article": article,
                     "sku": sku, "name": name, "turnover": a["turnover"], "commission": a["commission"], "logistics": a["logistics"],
                     "ads": a["ads"], "acquiring": a["acquiring"], "other": a["other"], "cogs": a["cogs"], "coinvest": a["coinvest"],
                     "units": a["units"], "vat": vat_for(d)})
    return rows, dict(stats)


# ---------- сводные ----------

def labels_for(days):
    """Метки строк сводной: месяцы, дни последнего месяца, «Общий итог»; day → [метки]."""
    months = []
    for d in days:
        if month_label(d) not in months:
            months.append(month_label(d))
    last_month = days[-1][:7]
    day_labels = [day_label(d) for d in days if d.startswith(last_month)]
    order = months + day_labels + ["Общий итог"]

    def of(d):
        out = [month_label(d), "Общий итог"]
        if d.startswith(last_month):
            out.insert(1, day_label(d))
        return out
    return order, of


BUYOUT_KEYS = ("turnover", "commission", "logistics", "ads", "acquiring", "other", "cogs", "coinvest", "units",
               "revenue_net", "logistics_net", "ads_net", "acquiring_net", "other_net", "turnover_net")


def pivot_buyouts(rows, days, key_fn=lambda r: "Ozon"):
    """{ключ: {метка: суммы}} — начисления и нетто-суммы по датам (НДС по дате), метрики — от сумм."""
    order, of = labels_for(days)
    out = defaultdict(lambda: {lab: defaultdict(Decimal) for lab in order})
    for r in rows:
        vat = r["vat"]
        for lab in of(r["date"]):
            a = out[key_fn(r)][lab]
            for k in ("turnover", "commission", "logistics", "ads", "acquiring", "other", "cogs", "coinvest", "units"):
                a[k] += r[k]
            a["revenue_net"] += (r["turnover"] + r["commission"]) / vat            # комиссия в строках отрицательна
            a["turnover_net"] += r["turnover"] / vat
            for k in ("logistics", "ads", "acquiring", "other"):
                a[k + "_net"] += -r[k] / vat
    return {k: {lab: metrics(a) for lab, a in labs.items()} for k, labs in out.items()}, order


def metrics(a):
    m = dict(a)
    m["commission_pct"] = rep.ratio(-a["commission"], a["turnover"])
    m["margin"] = a["revenue_net"] - a["cogs"]
    m["margin_pct"] = rep.ratio(m["margin"], a["revenue_net"])
    m["logistics_pct"] = rep.ratio(a["logistics_net"], a["revenue_net"])
    m["drr_pct"] = rep.ratio(a["ads_net"], a["turnover_net"])
    m["acquiring_pct"] = rep.ratio(a["acquiring_net"], a["revenue_net"])
    m["fin_result"] = m["margin"] - a["logistics_net"] - a["ads_net"] - a["acquiring_net"] - a["other_net"]
    m["fin_result_pct"] = rep.ratio(m["fin_result"], a["revenue_net"])
    m["coinvest_pct"] = rep.ratio(a["coinvest"], a["turnover"])
    return m


# ---------- заказы ----------

def build_order_rows(days, order_rows, kpi_rows, daily_rows, unit_cost, sku2art, catalog, names, today):
    """Строки «Данные заказы» на (дата, SKU) + остаток рекламы леджера без SKU + счётчики."""
    dayset = set(days)
    acc = defaultdict(lambda: defaultdict(Decimal))
    unknown_buyer = defaultdict(bool)
    stats = defaultdict(int)
    for r in order_rows:
        d, sku = r["order_date"], str(r["marketplace_sku"] or "")
        if d not in dayset:
            continue
        a = acc[(d, sku or NO_SKU)]
        conf_q, canc_q = D(r["orders_qty"]), D(r.get("cancelled_orders_qty"))
        conf_a, canc_a = D(r["orders_amount_seller"]), D(r.get("cancelled_orders_amount_seller"))
        a["created_a"] += conf_a + canc_a; a["created_q"] += conf_q + canc_q
        a["confirmed_a"] += conf_a; a["confirmed_q"] += conf_q
        bc, bcc = r.get("orders_amount_buyer"), r.get("cancelled_orders_amount_buyer")
        if bc is None or (bcc is None and canc_q):
            unknown_buyer[(d, sku or NO_SKU)] = True
        else:
            a["buyer_a"] += D(bc) + D(bcc)
            if D(bc) + D(bcc) == conf_a + canc_a and conf_a + canc_a:
                a["dup_rows"] += 1
            a["buyer_rows"] += 1
        uc = unit_cost(sku)
        if uc is None:
            stats["qty_without_cost"] += int(conf_q + canc_q)
        else:
            a["cogs_created"] += (conf_q + canc_q) * uc * cogs_index(d)
    for r in kpi_rows:
        d = r["kpi_date"]
        if d in dayset and D(r.get("ad_spend")):
            acc[(d, str(r.get("marketplace_sku") or "") or NO_SKU)]["ads"] += D(r.get("ad_spend"))
    daily = {r["date"]: r for r in daily_rows}
    for d in days:
        row = daily.get(d)
        if row is None or row.get("ads") is None:
            stats["days_without_raw_ads"] += 1
            continue
        by_sku = sum((a["ads"] for (dd, sku), a in acc.items() if dd == d and sku != NO_SKU), Z)
        acc[(d, NO_SKU)]["ads"] = q(row["ads"] * row["vat"] - by_sku)
    rows = []
    for (d, sku), a in sorted(acc.items()):
        if not any(q(v) for v in a.values()):
            continue
        brand, category, article, name = describe(sku, sku2art, catalog, names)
        buyer = None if unknown_buyer[(d, sku)] or (a["buyer_rows"] and a["buyer_rows"] == a["dup_rows"]) else a["buyer_a"]
        rows.append({"date": d, "month": month_label(d), "platform": PLATFORM, "brand": brand, "category": category, "article": article,
                     "sku": sku, "name": name, "created_a": a["created_a"], "created_q": a["created_q"], "confirmed_a": a["confirmed_a"],
                     "buyer_a": buyer, "ads": a["ads"], "cogs_created": a["cogs_created"], "vat": vat_for(d),
                     "age": (date.fromisoformat(today) - date.fromisoformat(d)).days,
                     "platform_name": rep.platform_of(article) if sku != NO_SKU else rep.NO_PLATFORM})
    return rows, dict(stats)


def coefficients(order_rows_data, buyout_rows, days):
    """Наши коэффициенты: выкуп (подтверждённые / созданные ₽ по дозревшим дням, по площадке и все) и комиссия по месяцам (из выкупов)."""
    conf, created = defaultdict(Decimal), defaultdict(Decimal)
    for r in order_rows_data:
        if r["age"] >= MATURE_AGE and r["sku"] != NO_SKU:
            for key in (r["platform_name"], "все"):
                conf[key] += r["confirmed_a"]; created[key] += r["created_a"]
    buyout = {k: rep.ratio(conf[k], created[k]) for k in created}
    comm_t, comm_c = defaultdict(Decimal), defaultdict(Decimal)
    for r in buyout_rows:
        for key in (r["month"], "все"):
            comm_t[key] += r["turnover"]; comm_c[key] += -r["commission"]
    commission = {k: rep.ratio(comm_c[k], comm_t[k]) for k in comm_t}
    return {"buyout": buyout, "buyout_base": {k: (conf[k], created[k]) for k in created}, "commission": commission}


def pivot_orders(rows, days, coef, daily_rows):
    """Лист «Заказы» (блок Ozon): формулы владельца на наших коэффициентах."""
    order, of = labels_for(days)
    acc = {lab: defaultdict(Decimal) for lab in order}
    ads_by_day = {r["date"]: r.get("ads") for r in daily_rows}
    for r in rows:
        for lab in of(r["date"]):
            a = acc[lab]
            a["created_a"] += r["created_a"]; a["created_q"] += r["created_q"]; a["cogs_created"] += r["cogs_created"]
            a["turnover_net"] += r["created_a"] / r["vat"]
            if r["sku"] != NO_SKU and r["buyer_a"] is not None:
                a["buyer_a"] += r["buyer_a"]; a["buyer_base"] += r["created_a"]      # доля — по измеренным строкам, покрытие — рядом
    for d in days:
        ads = ads_by_day.get(d)
        if ads is not None:
            for lab in of(d):
                acc[lab]["ads_net"] += ads
    buyout_all = coef["buyout"].get("все")
    out = {}
    for lab in order:
        a = acc[lab]
        month_key = lab if lab in coef["commission"] else (lab.split(".")[-1] if "." in lab else "все")
        comm = coef["commission"].get(month_key) or coef["commission"].get("все")
        m = {"created_a": a["created_a"], "created_q": a["created_q"], "price": rep.ratio(a["created_a"], a["created_q"]),
             "commission_pct": comm, "buyout": buyout_all}
        if buyout_all is None or comm is None or not a["created_a"]:
            m.update({k: None for k in ("revenue", "cogs", "margin", "margin_pct", "gross_fin", "gross_fin_pct")})
        else:
            m["revenue"] = a["turnover_net"] * buyout_all * (1 - comm)          # оборот без НДС по дате × выкуп × (1 − комиссия)
            m["cogs"] = a["cogs_created"] * buyout_all
            m["margin"] = m["revenue"] - m["cogs"]
            m["margin_pct"] = rep.ratio(m["margin"], m["revenue"])
        m["ads_net"] = a["ads_net"]
        m["drr_pct"] = rep.ratio(a["ads_net"], a["turnover_net"])
        m["coinvest_pct"] = rep.ratio(a["buyer_base"] - a["buyer_a"], a["buyer_base"]) if a["buyer_base"] else None
        m["coinvest_cover"] = rep.ratio(a["buyer_base"], a["created_a"])                 # какая доля созданного оборота измерена по цене покупателя
        if m.get("margin") is not None:
            m["gross_fin"] = m["margin"] - a["ads_net"]
            m["gross_fin_pct"] = rep.ratio(m["gross_fin"], m["revenue"])
        out[lab] = m
    return out, order


# ---------- запись книги ----------

BUYOUT_DATA_COLS = [("Дата", "date", "date"), ("Месяц", "month", None), ("Площадка", "platform", None), ("Бренд", "brand", None), ("Категория", "category", None),
                    ("Артикул", "article", None), ("SKU", "sku", None), ("Наименование", "name", None), ("Товарооборот", "turnover", "money"),
                    ("Комиссия", "commission", "money"), ("Логистика", "logistics", "money"), ("Реклама", "ads", "money"), ("Эквайринг", "acquiring", "money"),
                    ("Прочее", "other", "money"), ("Ст-ть продаж в себестоимости", "cogs", "money"), ("Соинвест (баллы + зелёные цены)", "coinvest", "money"),
                    ("Штуки", "units", "int")]
ORDER_DATA_COLS = [("Дата", "date", "date"), ("Месяц", "month", None), ("Площадка", "platform", None), ("Бренд", "brand", None), ("Категория", "category", None),
                   ("Артикул", "article", None), ("SKU", "sku", None), ("Наименование", "name", None), ("Заказы, руб. (создано, с НДС)", "created_a", "money"),
                   ("Заказы, шт", "created_q", "int"), ("Подтверждено сейчас, руб.", "confirmed_a", "money"), ("По цене покупателя, руб.", "buyer_a", "money"),
                   ("Реклама (Performance по SKU; без SKU — остаток 41 + 54), руб.", "ads", "money"), ("СС созданного (шт × СС × индекс), руб.", "cogs_created", "money")]
PIVOT_ARTICLE_KEYS = {"Комиссия": "commission", "Логистика": "logistics", "Прочее": "other", "Реклама": "ads", "Товарооборот": "turnover", "Эквайринг": "acquiring"}
PIVOT_CALC = [("Оборот (с НДС)", "turnover", "money"), ("Комиссия (с НДС), руб.", "commission_abs", "money"), ("Комиссия, %", "commission_pct", "pct"),
              ("Выручка, руб. (без НДС)", "revenue_net", "money"), ("Себестоимость, руб.", "cogs", "money"), ("Маржа, руб.", "margin", "money"), ("Мар-ть, %", "margin_pct", "pct"),
              ("Логистика, руб. (без НДС)", "logistics_net", "money"), ("% Логистики", "logistics_pct", "pct"), ("Реклама, руб. (без НДС)", "ads_net", "money"), ("% ДРР", "drr_pct", "pct"),
              ("Эквайринг, руб. (без НДС)", "acquiring_net", "money"), ("% Эквайринга", "acquiring_pct", "pct"), ("Прочее, руб. (без НДС)", "other_net", "money"),
              ("Фин. рез., руб.", "fin_result", "money"), ("% Фин. рез.", "fin_result_pct", "pct")]
PIVOT_EXTRA = [("справочно: Соинвест (баллы + зелёные цены), руб.", "coinvest", "money"), ("Соинвест, % от оборота", "coinvest_pct", "pct"), ("Штуки", "units", "int")]
ORDER_COLS = [("Оборот (с НДС)", "created_a", "money"), ("Заказы, шт", "created_q", "int"), ("Цена продавца", "price", "money"), ("Комиссия %", "commission_pct", "pct"),
              ("Выручка", "revenue", "money"), ("Себестоимость", "cogs", "money"), ("Маржа", "margin", "money"), ("М-ть, %", "margin_pct", "pct"), ("Реклама", "ads_net", "money"),
              ("ДДР, %", "drr_pct", "pct"), ("Соинвест, %", "coinvest_pct", "pct"), ("Гр.фин.рез", "gross_fin", "money"), ("Гр.фин.рез, %", "gross_fin_pct", "pct")]


def _styles():
    from openpyxl.styles import Font, PatternFill
    return {"money": "#,##0.00", "pct": "0.0%", "int": "#,##0", "date": "DD.MM.YYYY"}, Font(bold=True), PatternFill("solid", fgColor="D9E1F2")


def _cell(ws, r, c, v, fmt=None, fmts=None):
    if isinstance(v, Decimal):
        v = float(v)
    cell = ws.cell(row=r, column=c, value=v)
    if fmt and fmts and v is not None and v != "":
        cell.number_format = fmts[fmt]
    return cell


def write_data_sheet(wb, title, cols, rows, notes):
    from openpyxl.utils import get_column_letter
    fmts, bold, fill = _styles()
    ws = wb.create_sheet(title)
    for j, (h, _k, _f) in enumerate(cols, 1):
        c = ws.cell(row=1, column=j, value=h); c.font = bold; c.fill = fill
    for i, r in enumerate(rows, 2):
        for j, (_h, k, f) in enumerate(cols, 1):
            v = r.get(k)
            if f == "date" and v:
                v = date.fromisoformat(v)
            _cell(ws, i, j, v, f, fmts)
    base = len(rows) + 3
    for n, line in enumerate(notes):
        ws.cell(row=base + n, column=1, value=line)
    ws.freeze_panes = "A2"
    for j in range(1, len(cols) + 1):
        ws.column_dimensions[get_column_letter(j)].width = 14
    ws.column_dimensions["H"].width = 48
    return ws


def write_pivot_block(ws, r0, title, pivot, order, fmts, bold, fill):
    """Блок сводной владельца с r0: строка заголовка статей, строка подзаголовков, строки меток. Возвращает следующую свободную строку."""
    from openpyxl.styles import Alignment
    ws.cell(row=r0, column=1, value=title).font = bold
    ws.cell(row=r0 + 1, column=2, value="Названия столбцов").font = bold
    c = 2
    for art in ARTICLES:
        ws.cell(row=r0 + 2, column=c, value=art).font = bold
        ws.cell(row=r0 + 3, column=c, value=" Начисления"); ws.cell(row=r0 + 3, column=c + 1, value=" Ст-ть продаж в себ-ти")
        c += 2
    ws.cell(row=r0 + 3, column=1, value="Названия строк").font = bold
    calc0 = c + 2
    for j, (h, _k, _f) in enumerate(PIVOT_CALC + PIVOT_EXTRA):
        cell = ws.cell(row=r0 + 3, column=calc0 + j, value=h); cell.font = bold; cell.fill = fill
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for i, lab in enumerate(order):
        r = r0 + 4 + i
        m = pivot.get(lab) or {}
        lab_cell = ws.cell(row=r, column=1, value=lab)
        if lab == "Общий итог":
            lab_cell.font = bold
        c = 2
        for art in ARTICLES:
            k = PIVOT_ARTICLE_KEYS[art]
            _cell(ws, r, c, m.get(k, Z), "money", fmts)
            _cell(ws, r, c + 1, m.get("cogs", Z) if art == "Товарооборот" else Z, "money", fmts)
            c += 2
        vals = dict(m); vals["commission_abs"] = -m.get("commission", Z) if m else None
        for j, (_h, k, f) in enumerate(PIVOT_CALC + PIVOT_EXTRA):
            _cell(ws, r, calc0 + j, vals.get(k), f, fmts)
    ws.row_dimensions[r0 + 3].height = 45
    return r0 + 4 + len(order) + 2


def write_book(path, days, buyout_rows, pivots, order_rows_data, orders_pivot, coef, notes, catalog_source, today):
    import openpyxl
    from openpyxl.utils import get_column_letter
    fmts, bold, fill = _styles()
    wb = openpyxl.Workbook()
    ws = wb.active; ws.title = "Выкупы Ozon"
    ws["A1"] = "Выкупы Ozon — форма «Вывод данных» владельца на наших данных"; ws["A1"].font = bold
    nxt = write_pivot_block(ws, 3, "Все бренды и категории", pivots["all"][0]["Ozon"], pivots["all"][1], fmts, bold, fill)
    for n, line in enumerate(notes["buyouts"]):
        ws.cell(row=nxt + n, column=1, value=line)
    for j in range(1, 40):
        ws.column_dimensions[get_column_letter(j)].width = 15
    ws.freeze_panes = "B7"
    for title, key in (("Выкупы Ozon × бренд", "brand"), ("Выкупы Ozon × категория", "category")):
        wsb = wb.create_sheet(title)
        wsb["A1"] = title; wsb["A1"].font = bold
        r0 = 3
        piv, order = pivots[key]
        for name in sorted(piv, key=lambda n: (n == "", n)):
            r0 = write_pivot_block(wsb, r0, name or "(без признака)", piv[name], order, fmts, bold, fill)
        for j in range(1, 40):
            wsb.column_dimensions[get_column_letter(j)].width = 15
    write_data_sheet(wb, "Данные Ozon выкупы", BUYOUT_DATA_COLS, buyout_rows, notes["buyout_data"])
    # заказы — форма «Свод»
    wso = wb.create_sheet("Заказы")
    wso["A1"] = "Заказы — форма «Свод» владельца; блок Ozon на наших коэффициентах (лист «Коэффициенты»), блок WB — модуль WB-сессии"; wso["A1"].font = bold
    wso.cell(row=3, column=2, value="Названия столбцов").font = bold
    wso.cell(row=4, column=2, value="Ozon").font = bold
    wso.cell(row=4, column=2 + len(ORDER_COLS), value="WB").font = bold
    wso.cell(row=5, column=1, value="Названия строк").font = bold
    for blk in (0, 1):
        for j, (h, _k, _f) in enumerate(ORDER_COLS):
            c = wso.cell(row=5, column=2 + blk * len(ORDER_COLS) + j, value=h); c.font = bold; c.fill = fill
    piv, order = orders_pivot
    extra_col = 2 + 2 * len(ORDER_COLS) + 1
    c = wso.cell(row=5, column=extra_col, value="справочно: соинвест Ozon — доля созданного оборота с измеренной ценой покупателя"); c.font = bold; c.fill = fill
    for i, lab in enumerate(order):
        r = 6 + i
        lab_cell = wso.cell(row=r, column=1, value=lab)
        if lab == "Общий итог":
            lab_cell.font = bold
        for j, (_h, k, f) in enumerate(ORDER_COLS):
            _cell(wso, r, 2 + j, piv[lab].get(k), f, fmts)
        _cell(wso, r, extra_col, piv[lab].get("coinvest_cover"), "pct", fmts)
    base = 6 + len(order) + 2
    for n, line in enumerate(notes["orders"]):
        wso.cell(row=base + n, column=1, value=line)
    for j in range(1, 30):
        wso.column_dimensions[get_column_letter(j)].width = 14
    wso.freeze_panes = "B6"
    # коэффициенты
    wsk = wb.create_sheet("Коэффициенты")
    heads = ["МП / площадка", "% выкупа — наш (подтверждённые / созданные ₽, дни от 21 суток)", "база: подтверждено ₽", "база: создано ₽", "образец владельца"]
    for j, h in enumerate(heads, 1):
        c = wsk.cell(row=1, column=j, value=h); c.font = bold; c.fill = fill
    r = 2
    owner_b = dict(OWNER_COEF["buyout"])
    for key in ["все"] + [n for _p, n in rep.PLATFORMS] + [rep.NO_PLATFORM]:
        if key not in coef["buyout"]:
            continue
        conf, created = coef["buyout_base"][key]
        wsk.cell(row=r, column=1, value=f"Ozon — {key}"); _cell(wsk, r, 2, coef["buyout"][key], "pct", fmts)
        _cell(wsk, r, 3, conf, "money", fmts); _cell(wsk, r, 4, created, "money", fmts)
        _cell(wsk, r, 5, owner_b["Ozon"] if key == "все" else None, "pct", fmts)
        r += 1
    wsk.cell(row=r, column=1, value="WB"); _cell(wsk, r, 5, owner_b["WB"], "pct", fmts); wsk.cell(row=r, column=2, value="— (модуль WB-сессии)"); r += 2
    heads2 = ["МП / месяц", "Комиссия — наша фактическая (Σ комиссии / Σ оборота выкупов, с НДС)", "", "", "образец владельца"]
    for j, h in enumerate(heads2, 1):
        c = wsk.cell(row=r, column=j, value=h); c.font = bold; c.fill = fill
    r += 1
    for key in [k for k in coef["commission"] if k != "все"] + ["все"]:
        wsk.cell(row=r, column=1, value=f"Ozon — {key}"); _cell(wsk, r, 2, coef["commission"][key], "pct", fmts); r += 1
    for name, v in OWNER_COEF["commission"]:
        wsk.cell(row=r, column=1, value=name + " — образец владельца"); _cell(wsk, r, 5, v, "pct", fmts); r += 1
    for n, line in enumerate(notes["coef"], 1):
        wsk.cell(row=r + n, column=1, value=line)
    for j in range(1, 6):
        wsk.column_dimensions[get_column_letter(j)].width = 30
    write_data_sheet(wb, "Данные заказы", ORDER_DATA_COLS, order_rows_data, notes["order_data"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wb.save(path)


# ---------- приёмка ----------

def check_against_month(buyout_rows, daily_rows, days):
    """Σ листа выкупов за дни против build_daily генератора месяца: [(колонка, лист, месяц, разница, пояснение)]."""
    dayset = set(days)
    s = defaultdict(Decimal)
    for r in buyout_rows:
        if r["date"] in dayset:
            vat = r["vat"]
            s["turnover"] += r["turnover"]; s["commission"] += -r["commission"]; s["revenue"] += (r["turnover"] + r["commission"]) / vat
            s["cogs"] += r["cogs"]; s["logistics"] += -r["logistics"] / vat; s["ads"] += -r["ads"] / vat
            s["acquiring"] += -r["acquiring"] / vat; s["other"] += -r["other"] / vat
    m = defaultdict(Decimal)
    missing_ads = 0
    for r in daily_rows:
        if r["date"] in dayset:
            for k in ("turnover", "commission", "revenue", "cogs", "logistics"):
                m[k] += r[k]
            m["cogs_index"] += r["cogs_index"] if r.get("cogs_index") is not None else r["cogs"]
            if r.get("ads") is None:
                missing_ads += 1
            else:
                m["ads"] += r["ads"]; m["acquiring"] += r["acquiring"]; m["other"] += r["other"] + (r.get("subscription") or Z)
    s["fin_result"] = s["revenue"] - s["cogs"] - s["logistics"] - s["ads"] - s["acquiring"] - s["other"]
    m["fin_result_index"] = m["revenue"] - m["cogs_index"] - m["logistics"] - m["ads"] - m["acquiring"] - m["other"]
    out = [("Оборот", s["turnover"], m["turnover"], "те же строки выкупов"), ("Комиссия", s["commission"], m["commission"], "те же строки выкупов"),
           ("Выручка без НДС", s["revenue"], m["revenue"], "те же строки"),
           ("Себестоимость (лист — × индекс)", s["cogs"], m["cogs_index"], "лист месяца: «СС по индексу»; без индекса " + f"{m['cogs']:,.2f}"),
           ("Логистика без НДС", s["logistics"], m["logistics"], "SKU из расходов + остаток без SKU против типов"),
           ("Реклама без НДС", s["ads"], m["ads"], "Performance по SKU + остаток до 41 + 54" + (f"; дней без сырья {missing_ads}" if missing_ads else "")),
           ("Эквайринг без НДС", s["acquiring"], m["acquiring"], "только строка без SKU (леджер)"),
           ("Прочее без НДС (с подпиской)", s["other"], m["other"], "прочее + подписка листа месяца"),
           ("Фин. рез. (лист — по индексу СС)", s["fin_result"], m["fin_result_index"], "лист месяца: «Фин. рез. по индексу»")]
    return [(t, a, b, q(a - b), why) for t, a, b, why in out]


def read_book_orders_total(path):
    import warnings
    import openpyxl
    warnings.simplefilter("ignore")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["Заказы"]
    rows = list(ws.iter_rows(min_row=4, max_row=60, values_only=True))
    heads = rows[0]
    tot = next(r for r in rows[1:] if r and r[0] == "Итого")
    return {h: v for h, v in zip(heads, tot) if h}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--month-from", required=True); ap.add_argument("--month-to", required=True)
    ap.add_argument("--date-to", help="последний день; по умолчанию вчера")
    ap.add_argument("--out"); ap.add_argument("--snapshot", default=rep.SNAP)
    ap.add_argument("--check", action="store_true"); ap.add_argument("--check-month", help="месяц приёмки YYYY-MM (по умолчанию --month-to)")
    ap.add_argument("--check-days", type=int, default=21, help="дней месяца приёмки (1 … N)")
    ap.add_argument("--book", help="утренняя книга Ozon (xlsx) — сверка заказов с её листом «Заказы»")
    args = ap.parse_args(argv)
    today = date.today().isoformat()
    date_to = args.date_to or (date.today() - timedelta(days=1)).isoformat()
    days = days_between(args.month_from, args.month_to, date_to)
    d1, d2 = days[0], days[-1]
    from loaders.ozon_fbo_orders_loader import supabase as sb
    flt = lambda col: [("eq", "marketplace_code", "ozon"), ("gte", col, d1), ("lte", col, d2)]  # noqa: E731
    buyout_cols = "id,buyout_date,marketplace_sku,article,product_name,buyouts_qty,buyouts_amount_seller,commission_amount"
    optional = ["buyouts_units", "bonus_amount", "coinvestment_amount"]
    while True:
        try:
            buyouts = rep.fetch(sb, "marketplace_buyouts", ",".join([buyout_cols] + optional), flt("buyout_date"), ["buyout_date", "marketplace_code", "marketplace_sku"])
            break
        except Exception as exc:
            missing = [c for c in optional if c in str(exc)]
            if not missing:
                raise
            optional = [c for c in optional if c not in missing]
    for r in buyouts:
        for c in ("buyouts_units", "bonus_amount", "coinvestment_amount"):
            r.setdefault(c, None)
    expenses = rep.fetch(sb, "marketplace_expenses", "id,expense_date,marketplace_sku,expense_type,expense_amount", flt("expense_date"),
                         ["expense_date", "marketplace_code", "marketplace_sku", "expense_type"])
    kpi = rep.fetch(sb, "daily_sku_kpi", "id,kpi_date,marketplace_sku,product_name,ad_spend", flt("kpi_date"), ["kpi_date", "marketplace_code", "marketplace_sku"])
    sku2art, unit_cost, cost_found, cost_asked, order_rows = rep.load_costs(sb, args.snapshot)
    catalog, catalog_source = load_catalog(sb)
    names = {}
    for r in buyouts + kpi:
        if r.get("product_name") and str(r["marketplace_sku"]) not in names:
            names[str(r["marketplace_sku"])] = r["product_name"]
    ledger = rep.load_types_from_ledger(sb, d1, d2)
    daily_rows, unknown = rep.build_daily(days, buyouts, expenses, ledger, unit_cost, today)
    for r in daily_rows:
        idx = rep.cost_index_for(r["date"])
        r["cogs_index"] = r["cogs"] * idx if idx is not None else r["cogs"]
    print(f"окно {d1} … {d2}: дней {len(days)}, выкупов {len(buyouts)}, расходов {len(expenses)}, строк витрины {len(kpi)}, заказов {len(order_rows)}, "
          f"дней с типами в леджере {len([d for d in days if d in ledger])}; каталог: {catalog_source} ({len(catalog)} SKU); СС: артикулов {cost_found} из {cost_asked}")
    acq_by_sku, acq_missing = acquiring_by_sku(days)
    buyout_rows, bstats = build_buyout_rows(days, buyouts, expenses, kpi, daily_rows, unit_cost, sku2art, catalog, names, acq_by_sku)
    young_days = [d for d in days if (date.today() - date.fromisoformat(d)).days < rep.YOUNG_DAYS]
    pivots = {"all": pivot_buyouts(buyout_rows, days), "brand": pivot_buyouts(buyout_rows, days, lambda r: r["brand"]),
              "category": pivot_buyouts(buyout_rows, days, lambda r: r["category"])}
    order_data, ostats = build_order_rows(days, order_rows, kpi, daily_rows, unit_cost, sku2art, catalog, names, today)
    coef = coefficients(order_data, buyout_rows, days)
    orders_pivot = pivot_orders(order_data, days, coef, daily_rows)
    idx_note = "; ".join(f"с {vf} × {v}" for vf, v in rep.COST_INDEX)
    notes = {
        "buyouts": [f"Источники: marketplace_buyouts, marketplace_expenses по SKU, реклама — Performance по SKU (ad_spend витрины) плюс остаток до начислений 41 + 54, эквайринг и остатки статей — леджер начислений (строка «(без SKU)»). НДС по дате ({vat_for(d2)} на {d2}).",
                    f"Себестоимость = штуки × СС снимка 1С {args.snapshot} × индекс СС ({idx_note}; до — без индекса); позиций без СС: {bstats.get('positions_without_cost', 0)}.",
                    "«Прочее» включает подписку (у владельца отдельной статьи нет). «Ст-ть продаж в себ-ти» — только под Товарооборотом, как в образце.",
                    f"Эквайринг по SKU — из сырья by-day (услуга типа 1 у строки товара; в marketplace_expenses она свёрнута в «прочее»): перенесён из «Прочего» в «Эквайринг» у {bstats.get('acquiring_moved_rows', 0)} строк"
                    + (f"; дней без файла сырья {len(acq_missing)} — там эквайринг остался в «Прочем» строк SKU" if acq_missing else "") + ".",
                    ("Дни моложе двух суток (" + ", ".join(young_days) + "): реклама 41 + 54 в леджере появляется следующей ночью — в этих строках реклама занижена; утренняя книга берёт их живым ответом."
                     if young_days else "Дней моложе двух суток в книге нет."),
                    f"Категория — карточка Ozon ({catalog_source}); бренд — по первой букве артикула (T — Топаз, иначе KARATOV). Строк выкупов без соинвеста (записаны до колонок): {bstats.get('rows_without_coinvest', 0)}."
                    + (f" Дней без сырья рекламы: {bstats['days_without_raw_ads']}." if bstats.get("days_without_raw_ads") else "")],
        "buyout_data": ["Знак как в сводной владельца: списания отрицательные, товарооборот и себестоимость положительные; суммы с НДС.",
                        "Строка «(без SKU)» на день — леджер начислений: реклама 41 + 54 минус Performance по SKU, эквайринг целиком, остаток логистики и прочего против типов. Без неё Σ листа ≠ «Ozon - месяц»."],
        "orders": ["Оборот — созданные заказы (подтверждённые + отменённые) по цене продавца, с НДС; Заказы, шт — созданные штуки; Цена продавца = оборот / шт.",
                   "Комиссия % — наша фактическая за месяц (Σ комиссии / Σ оборота выкупов); % выкупа — наш измеренный (лист «Коэффициенты»). Выручка = Оборот × % выкупа × (1 − комиссия) / НДС; Себестоимость = СС созданного × % выкупа; Маржа = Выручка − Себестоимость.",
                   "Реклама — начисления 41 + 54 по дню заказа без НДС; ДДР = Реклама / (Оборот / НДС); Соинвест % = (создано − оплачено покупателем) / создано по строкам с измеренной ценой покупателя — доля измеренного оборота в справочной колонке справа (история до 09-01 и ключи, не добранные бэкфиллом, не измерены).",
                   "Гр. фин. рез = Маржа − Реклама (по образцу владельца через коэффициенты). Блок WB — пусто: строки WB даёт модуль WB-сессии (scripts/report_finrez_wb.py)."],
        "coef": ["% выкупа — Σ подтверждённых ₽ / Σ созданных ₽ по дням возраста ≥ 21 суток в окне книги (дозревшие); образец владельца 0,84 / 0,86 — из его книги, числа другого кабинета.",
                 "Комиссия — Σ комиссии выкупов / Σ оборота выкупов по месяцу (с НДС); образец владельца: Ozon до 300 ₽ 23 %, выше 52 %, WB 43 %."],
        "order_data": ["Строка на (дата, SKU): создано = подтверждено сейчас + отменено; по цене покупателя — пусто, где цена не измерена. «(без SKU)» — остаток рекламы 41 + 54 над Performance по SKU.",
                       f"Штук без СС: {ostats.get('qty_without_cost', 0)}." + (f" Дней без сырья рекламы: {ostats['days_without_raw_ads']}." if ostats.get("days_without_raw_ads") else "")],
    }
    out = args.out or os.path.join(OUT_DIR, f"finrez_{args.month_from}_{args.month_to}.xlsx")
    write_book(out, days, buyout_rows, pivots, order_data, orders_pivot, coef, notes, catalog_source, today)
    tot = pivots["all"][0]["Ozon"]["Общий итог"]
    print(f"книга → {out}: строк «Данные Ozon выкупы» {len(buyout_rows)} (без SKU {sum(1 for r in buyout_rows if r['sku'] == NO_SKU)}), "
          f"«Данные заказы» {len(order_data)}; итого оборот {tot['turnover']:,.2f}, комиссия {-tot['commission']:,.2f}, СС {tot['cogs']:,.2f}, фин. рез. {tot['fin_result']:,.2f}")
    print("коэффициенты: выкуп " + ", ".join(f"{k} {v * 100:.2f} %" for k, v in coef["buyout"].items() if v is not None)
          + "; комиссия " + ", ".join(f"{k} {v * 100:.2f} %" for k, v in coef["commission"].items() if v is not None))
    if unknown:
        print(f"незнакомые статьи / типы в build_daily: {unknown}")
    code = 0
    if args.check:
        cm = args.check_month or args.month_to
        cdays = [d for d in days if d.startswith(cm)][:args.check_days]
        print(f"\nприёмка «Выкупы Ozon» за {cdays[0]} … {cdays[-1]} против build_daily генератора месяца (те же входы):")
        print(f"{'колонка':40}{'лист Фин рез':>18}{'Ozon - месяц':>18}{'разница':>14}")
        bad = 0
        for t, a, b, diff, why in check_against_month(buyout_rows, daily_rows, cdays):
            print(f"{t:40}{a:>18,.2f}{b:>18,.2f}{diff:>14,.2f}   {why}")
            bad += 1 if diff != 0 else 0
        ok_sum = all(q(sum((pivots[k][0][n]["Общий итог"][f] for n in pivots[k][0]), Z) - tot[f]) == 0
                     for k in ("brand", "category") for f in ("turnover", "commission", "logistics", "ads", "acquiring", "other", "cogs"))
        print(f"Σ брендов = Σ категорий = общий по всем статьям: {'да' if ok_sum else 'НЕТ'}; строк данных {len(buyout_rows)}")
        print(f"колонок с разницей: {bad}")
        code = 1 if bad or not ok_sum else 0
        if args.book:
            book = read_book_orders_total(args.book)
            b_days = set(d for d in days if d.startswith(cm))
            s = defaultdict(Decimal)
            std = defaultdict(Decimal)
            for r in order_data:
                if r["date"] in b_days:
                    s["created_a"] += r["created_a"]; s["created_q"] += r["created_q"]; s["ads"] += r["ads"] / r["vat"]
                    if r["platform_name"] == rep.PLATFORMS[0][1] and r["buyer_a"] is not None:
                        std["buyer"] += r["buyer_a"]; std["created"] += r["created_a"]
            print(f"\nзаказы за {cm} против «Заказы» книги {os.path.basename(args.book)} (её дни):")
            for title, ours, theirs in (("Создано, руб.", s["created_a"], book.get("Создано, руб.")), ("Создано, шт", s["created_q"], book.get("Создано, шт")),
                                        ("Реклама (41 + 54), руб.", s["ads"], book.get("Реклама (41 + 54), руб."))):
                print(f"  {title:28}{ours:>18,.2f}{D(theirs):>18,.2f}{q(ours - D(theirs)):>14,.2f}")
            g_key = next((k for k in book if str(k).startswith("Соинвест, % (Standard")), None)
            std_share = rep.ratio(std["created"] - std["buyer"], std["created"])
            print(f"  {'Соинвест Standard':28}{(std_share or 0) * 100:>17.2f}%{(D(book.get(g_key)) * 100 if g_key and book.get(g_key) is not None else 0):>17.2f}%")
    print("db_writes = 0")
    return code


if __name__ == "__main__":
    sys.exit(main())
