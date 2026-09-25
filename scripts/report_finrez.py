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
import hashlib
import json
import os
import sys
import time
from collections import defaultdict, namedtuple
from datetime import date, timedelta
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
import report_ozon_month as rep  # noqa: E402
from loaders import ozon_finance_accrual as accrual_mod  # noqa: E402
import ozon_product_catalog as catalog_rules  # noqa: E402
try:
    import report_finrez_wb as wbfin  # noqa: E402  — модуль WB-сессии (договор в его docstring): листы «Выкупы WB», «Данные WB выкупы», заказы WB
    WB_IMPORT_ERROR = None
except Exception as _exc:  # noqa: BLE001
    wbfin, WB_IMPORT_ERROR = None, _exc

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
        with open(CATALOG_FILE, encoding="utf-8") as fh:
            snap = json.load(fh)
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
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
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


# ---------- выкупы: длинный формат — строка на начисление × SKU × статья (§7.1) ----------

Long = namedtuple("Long", "accrual_id date article sku qty kind brand_cc month cogs brand category amount name day_num month_num coinvest platform key")
# key — ключ статьи для сводных (turnover / commission / logistics / ads / acquiring / other); в лист не пишется
OWNER_ARTICLE = {"turnover": "Товарооборот", "commission": "Комиссия", "logistics": "Логистика", "ads": "Реклама", "acquiring": "Эквайринг", "other": "Прочее"}
WIDE_KEYS = ("turnover", "commission", "logistics", "ads", "acquiring", "other")
SHOP = "KARATOV"


def dec_money(node):
    """Сумма начисления Decimal со знаком Ozon (списание < 0) — из узла {amount, currency} или строки; None → 0."""
    if isinstance(node, dict):
        node = node.get("amount")
    return Decimal(str(node or 0))


def article_key_of_type(type_id):
    """Статья владельца для типа начисления: 1 → Эквайринг; logistics → Логистика; other / external_promo / subscription / незнакомый → Прочее."""
    if type_id == ACQUIRING_TYPE:
        return "acquiring"
    return "logistics" if accrual_mod.TYPE_TO_EXPENSE.get(type_id) == "logistics" else "other"


def _long(aid, d, key, amount, sku, desc, qty=Z, cogs=Z, coinvest=Z):
    brand, category, article, name = desc
    return Long(aid, d, article, sku, qty, OWNER_ARTICLE[key], brand, month_label(d), cogs, brand, category, amount, name, int(d[8:10]), int(d[5:7]),
                coinvest, PLATFORM, key)


def long_rows_from_raw(day, accruals, units_by_key, unit_cost, describe_fn, stats):
    """Строки дня из сырья by-day: на каждое начисление — Товарооборот (+ количество, СС, соинвест) и Комиссия по товарной строке, услуги по
    статьям владельца (тип 1 — Эквайринг, logistics — Логистика, остальное — Прочее); строки без SKU (NON_ITEM) — с пустым SKU и своим ID.
    Реклама 41 / 54 и компенсации 25 / 10 пропускаются (реклама — Performance по SKU + остаток леджера; компенсации — не статья формы).
    Знак — как у Ozon: списание < 0, продажа > 0, возврат < 0. Количество: позиции ±1 по знаку продажи; измеренные штуки (buyouts_units по
    (день, SKU)) раскладываются по строкам — разница к первой строке продажи; СС = количество × СС снимка × индекс."""
    rows, turnover_idx = [], defaultdict(list)
    for a in accruals:
        if str(a.get("date") or "")[:10] != day:
            stats["raw_foreign_date"] += 1
            continue
        aid = a.get("accrual_id")
        for product in ((a.get("posting") or {}).get("products") or []):
            commission = product.get("commission") or {}
            if not commission:
                continue
            sale, comm = dec_money(commission.get("sale_amount")), dec_money(commission.get("sale_commission"))
            if sale == 0 and comm == 0:
                stats["raw_zero_sale"] += 1
                continue
            sku = str(product.get("sku") or "")
            desc = describe_fn(sku)
            coinvest = dec_money(commission.get("bonus")) + dec_money(commission.get("coinvestment"))
            rows.append(_long(aid, day, "turnover", sale, sku, desc, qty=Decimal(1 if sale >= 0 else -1), coinvest=coinvest))
            turnover_idx[sku].append(len(rows) - 1)
            if comm:
                rows.append(_long(aid, day, "commission", comm, sku, desc))
        for type_id, sku, amount in accrual_mod._service_lines(a):
            if type_id in accrual_mod.AD_TYPE_IDS:
                stats["raw_ads_lines_skipped"] += 1
                continue
            if type_id in accrual_mod.UNCLASSIFIED_TYPE_IDS:
                stats["raw_compensation_lines_skipped"] += 1
                continue
            amount = Decimal(str(amount))
            if amount == 0:
                continue
            if type_id not in accrual_mod.TYPE_TO_EXPENSE:
                stats[f"unknown_type:{type_id}"] += 1
            rows.append(_long(aid, day, article_key_of_type(type_id), amount, sku, describe_fn(sku)))
    idx = cogs_index(day)
    for sku, ids in turnover_idx.items():
        positions = sum((rows[i].qty for i in ids), Z)
        measured = units_by_key.get((day, sku))
        delta = (measured - positions) if measured is not None else Z
        if delta:
            first = next((i for i in ids if rows[i].qty > 0), ids[0])
            rows[first] = rows[first]._replace(qty=rows[first].qty + delta)
            stats["units_reallocated_keys"] += 1
        uc = unit_cost(sku)
        for i in ids:
            r = rows[i]
            if uc is None:
                stats["positions_without_cost"] += int(abs(r.qty))
            else:
                rows[i] = r._replace(cogs=r.qty * uc * idx)
    return rows


def explode_wide(row):
    """Широкая строка (дата, SKU) → длинные строки без ID начисления (дни без файла сырья)."""
    out, sku, d = [], "" if row["sku"] == NO_SKU else row["sku"], row["date"]
    desc = (row["brand"], row["category"], row["article"], row["name"])
    for key in WIDE_KEYS:
        amount = row[key]
        if key == "turnover":
            if amount or row["cogs"] or row["units"] or row["coinvest"]:
                out.append(_long("", d, key, amount, sku, desc, qty=row["units"], cogs=row["cogs"], coinvest=row["coinvest"]))
        elif amount:
            out.append(_long("", d, key, amount, sku, desc))
    return out


def residual_rows(day, day_rows, daily_row, stats):
    """Остаток дня без SKU и без ID по каждой статье: цель листа месяца (build_daily) минус Σ строк дня. Реклама — леджер 41 + 54 минус Performance
    по SKU (всегда есть); товарооборот и комиссия — таблица выкупов минус сырьё (0 на дозревших днях, иначе считается и называется)."""
    vat = daily_row["vat"]
    target = {"turnover": daily_row["turnover"], "commission": -daily_row["commission"], "logistics": -(daily_row["logistics"] * vat),
              "other": -((daily_row["other"] + (daily_row.get("subscription") or Z)) * vat)}
    if daily_row.get("ads") is None:
        stats["days_without_raw_ads"] += 1
    else:
        target["ads"] = -(daily_row["ads"] * vat)
    if daily_row.get("acquiring") is not None:
        target["acquiring"] = -(daily_row["acquiring"] * vat)
    have = defaultdict(Decimal)
    for r in day_rows:
        have[r.key] += r.amount
    out = []
    for key, t in target.items():
        res = q(t - have[key])
        if res:
            out.append(_long("", day, key, res, "", ("", "", NO_SKU, "")))
            stats[f"residual_{key}_days"] += 1
            stats[f"residual_{key}_sum"] += res
    return out


def wide_from_long(long_rows):
    """Широкие строки (дата, SKU) из длинных — вход сводных и приёмки (та же форма, что у build_buyout_rows)."""
    acc, desc = defaultdict(lambda: defaultdict(Decimal)), {}
    for r in long_rows:
        k = (r.date, r.sku or NO_SKU)
        a = acc[k]
        a[r.key] += r.amount
        if r.key == "turnover":
            a["cogs"] += r.cogs
            a["units"] += r.qty
            a["coinvest"] += r.coinvest
        desc.setdefault(k, (r.brand, r.category, r.article, r.name))
    rows = []
    for (d, sku), a in sorted(acc.items()):
        brand, category, article, name = desc[(d, sku)]
        rows.append({"date": d, "month": month_label(d), "platform": PLATFORM, "brand": brand, "category": category, "article": article, "sku": sku,
                     "name": name, "turnover": a["turnover"], "commission": a["commission"], "logistics": a["logistics"], "ads": a["ads"],
                     "acquiring": a["acquiring"], "other": a["other"], "cogs": a["cogs"], "coinvest": a["coinvest"], "units": a["units"], "vat": vat_for(d)})
    return rows


def build_buyout_long_rows(days, raw_dir, buyouts, expenses, kpi_rows, daily_rows, unit_cost, sku2art, catalog, names):
    """«Данные Ozon выкупы» в длинном формате + широкие строки для сводных + счётчики. День с файлом сырья — по начислениям (ID есть); без файла —
    из таблиц через build_buyout_rows (ID пусто); реклама Performance по SKU — строки без ID; остаток дня — residual_rows."""
    stats = defaultdict(int)
    daily = {r["date"]: r for r in daily_rows}
    units_by_key, buy_by_day, exp_by_day, kpi_by_day, cache = {}, defaultdict(list), defaultdict(list), defaultdict(list), {}
    for r in buyouts:
        units_by_key[(r["buyout_date"], str(r["marketplace_sku"] or ""))] = D(r["buyouts_units"]) if r.get("buyouts_units") is not None else D(r["buyouts_qty"])
        buy_by_day[r["buyout_date"]].append(r)
    for r in expenses:
        exp_by_day[r["expense_date"]].append(r)
    for r in kpi_rows:
        if D(r.get("ad_spend")):
            kpi_by_day[r["kpi_date"]].append(r)

    def describe_fn(sku):
        if sku not in cache:
            cache[sku] = describe(sku, sku2art, catalog, names)
        return cache[sku]

    long_rows = []
    for d in days:
        drow = daily.get(d)
        if drow is None:
            stats["days_without_daily_row"] += 1
            continue
        path = os.path.join(raw_dir, f"{d}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            day_rows = long_rows_from_raw(d, data["accruals"] if isinstance(data, dict) else data, units_by_key, unit_cost, describe_fn, stats)
            for r in kpi_by_day.get(d, ()):
                sku = str(r.get("marketplace_sku") or "")
                day_rows.append(_long("", d, "ads", -D(r["ad_spend"]), sku, describe_fn(sku)))
            day_rows += residual_rows(d, day_rows, drow, stats)
            stats["days_from_raw"] += 1
        else:
            wide, wstats = build_buyout_rows([d], buy_by_day.get(d, []), exp_by_day.get(d, []), kpi_by_day.get(d, []), [drow], unit_cost, sku2art, catalog, names)
            for k, v in wstats.items():
                stats[k] += v
            day_rows = [x for w in wide for x in explode_wide(w)]
            stats["days_from_tables"] += 1
        long_rows.extend(day_rows)
    return long_rows, wide_from_long(long_rows), dict(stats)


def long_identity(long_rows, pivot, order):
    """Σ «Начисления» по (метка, статья) против сводной; Σ «Ст-ть продаж в себ-ти» против «Себестоимость»: список расхождений (пусто — тождество)."""
    _order, of = labels_for([r.date for r in long_rows] or ["2026-01-01"])
    s = defaultdict(Decimal)
    for r in long_rows:
        for lab in of(r.date):
            s[(lab, r.key)] += r.amount
            if r.key == "turnover":
                s[(lab, "cogs")] += r.cogs
    bad = []
    for lab in order:
        m = pivot.get(lab) or {}
        for key in WIDE_KEYS + ("cogs",):
            diff = q(s[(lab, key)] - (m.get(key) or Z))
            if diff:
                bad.append((lab, OWNER_ARTICLE.get(key, "Себестоимость"), diff))
    return bad


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


def order_commission(coef, day):
    """Комиссия для дня — наша фактическая за его месяц, иначе «все»."""
    return coef["commission"].get(month_label(day)) or coef["commission"].get("все")


def pivot_orders(rows, days, coef, daily_rows):
    """Лист «Заказы» (блок Ozon): формулы владельца на наших коэффициентах, ПОСТРОЧНО (выручка, комиссия, СС считаются по строке и складываются —
    так «Свод» = Σ «Данных заказы» и в «Общем итоге», где месяцы с разной комиссией)."""
    order, of = labels_for(days)
    acc = {lab: defaultdict(Decimal) for lab in order}
    ads_by_day = {r["date"]: r.get("ads") for r in daily_rows}
    buyout_all = coef["buyout"].get("все")
    for r in rows:
        comm = order_commission(coef, r["date"])
        net = r["created_a"] / r["vat"]
        for lab in of(r["date"]):
            a = acc[lab]
            a["created_a"] += r["created_a"]; a["created_q"] += r["created_q"]; a["turnover_net"] += net
            if r["sku"] != NO_SKU and r["buyer_a"] is not None:
                a["buyer_a"] += r["buyer_a"]; a["buyer_base"] += r["created_a"]      # доля — по измеренным строкам, покрытие — рядом
            if buyout_all is not None and comm is not None:
                a["revenue"] += net * buyout_all * (1 - comm); a["comm_rub"] += net * buyout_all * comm; a["base"] += net * buyout_all
                a["cogs"] += r["cogs_created"] * buyout_all; a["priced"] += 1
    for d in days:
        ads = ads_by_day.get(d)
        if ads is not None:
            for lab in of(d):
                acc[lab]["ads_net"] += ads
    out = {}
    for lab in order:
        a = acc[lab]
        month_key = lab if lab in coef["commission"] else (lab.split(".")[-1] if "." in lab else "все")
        m = {"created_a": a["created_a"], "created_q": a["created_q"], "price": rep.ratio(a["created_a"], a["created_q"]), "buyout": buyout_all,
             "commission_pct": rep.ratio(a["comm_rub"], a["base"]) if a["base"] else (coef["commission"].get(month_key) or coef["commission"].get("все"))}
        if buyout_all is None or not a["priced"]:
            m.update({k: None for k in ("revenue", "cogs", "margin", "margin_pct", "gross_fin", "gross_fin_pct")})
        else:
            m["revenue"], m["cogs"] = a["revenue"], a["cogs"]
            m["margin"] = a["revenue"] - a["cogs"]
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


# ---------- WB-часть — модулем WB-сессии ----------

def build_wb_parts(month_from, month_to, date_to, d1, d2, days):
    """Листы WB по договору с report_finrez_wb: строки данных, лист выкупов, разрезы, заказы для общего листа. Ошибка — {'error': …}."""
    if wbfin is None:
        return {"error": f"модуль report_finrez_wb не импортирован: {WB_IMPORT_ERROR}"}
    t = time.time()
    timings = {}

    def lap(name):
        nonlocal t
        timings[name] = round(time.time() - t, 1)
        t = time.time()

    try:
        sb_wb = wbfin.report_loader._client()
        rows = wbfin.build_rows(month_from, month_to, date_to, sb=sb_wb)
        lap("build_rows (отчёт реализации, словарь товаров, реклама по nm)")
        month = wbfin.build_month_sheet(rows)
        split_brand, split_cat = wbfin.build_split(rows, "brand"), wbfin.build_split(rows, "category")
        lap("сводные WB")
        # воронка по товарам — по одному дню за вызов: одно окно на 177 дней (~490 тыс. строк) читается OFFSET-страницами, глубокие страницы
        # упираются в statement_timeout 8 с (2026-09-25: APIError 57014 после 1 480 с, книга без WB); по дню — страницы мелкие, тот же читатель модуля
        funnel = []
        for d in days:
            funnel += wbfin.load_funnel_products(d, d, sb=sb_wb)
        lap(f"воронка по товарам по дням ({len(days)} окон, {len(funnel)} строк)")
        costs = wbfin.wbm.load_costs(sb_wb)
        ostats = {}
        # модуль с 2026-09-25 (WB-8 §6) сам не отдаёт пустые строки и кладёт рекламу номенклатуры в строку (updSum дня × доля fullstats)
        orders = wbfin.orders_rows_for_finrez(d1, d2, sb=sb_wb, funnel_rows=funnel, costs=costs, stats=ostats)
        lap("orders_rows_for_finrez")
        if ostats:
            print(f"  заказы WB модулем: строк было {ostats.get('rows_before')}, стало {ostats.get('rows_after')}; Σ orderSum по месяцам до = после: {ostats.get('equal')}")
        try:
            ads_by_day, _undated = wbfin.wbm.load_ads_db(sb_wb, d1, d2)
        except Exception as exc:  # noqa: BLE001 — реклама не обязана ронять блок заказов
            ads_by_day, ads_note = None, f"реклама WB не прочитана: {exc}"
        else:
            ads_note = ""
        lap("реклама WB по дням")
        # коэффициент выкупа WB — модуль (WB-9): когорта месяца заказа по ₽, только зрелые месяцы (конец месяца + 25 дней ≤ сегодня)
        details, buyout_note = {}, ""
        try:
            buyout = wbfin.buyout_rate_for_finrez(month_from, month_to, sb=sb_wb, details=details)
        except Exception as exc:  # noqa: BLE001 — без коэффициента блок WB считается по созданным (выкуп 100 %) и говорит об этом
            buyout, buyout_note = {}, f"коэффициент выкупа WB не прочитан: {exc}"
        lap("коэффициент выкупа WB (модуль)")
        print("  время WB-модуля по вызовам: " + ", ".join(f"{k} {v} с" for k, v in timings.items()))
        rate = buyout.get("итого")
        return {"rows": rows, "month": month, "split_brand": split_brand, "split_category": split_cat, "orders": orders,
                "ads_by_day": ads_by_day, "ads_note": ads_note, "buyout": buyout, "buyout_details": details, "buyout_note": buyout_note,
                "orders_pivot": pivot_orders_wb(orders, days, ads_by_day, rate), "orders_pivot_no_rate": pivot_orders_wb(orders, days, ads_by_day, None),
                "timings": timings}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def pivot_orders_wb(orders, days, ads_by_day, buyout=None):
    """Блок WB листа «Заказы» по меткам — теми же формулами, что Ozon (решение владельца 2026-09-25): Выручка = Заказы без НДС × Выкуплено ×
    (1 − Комиссия), Комиссия = 1 − 0,58 модуля, Себестоимость = СС модуля × Выкуплено, Маржа = Выручка − Себестоимость, Гр. фин. рез = Маржа − Реклама;
    Выкуплено — итог зрелых месяцев buyout_rate_for_finrez модуля (одно число на площадку); buyout None — 100 % (по созданным, как до 09-25).
    Реклама — списания дня wb_ad_spend_daily без НДС; построчно — так «Свод» = Σ «Данных заказы»."""
    order, of = labels_for(days)
    acc = {lab: defaultdict(Decimal) for lab in order}
    dayset = set(days)
    comm = (1 - wbfin.wbm.OWNER_ORDERS_AFTER_COMMISSION) if wbfin else Decimal("0.42")
    b = buyout if buyout is not None else Decimal(1)
    for r in orders:
        if r["date"] not in dayset:
            continue
        net = D(r["orders_sum"]) / vat_for(r["date"])
        for lab in of(r["date"]):
            a = acc[lab]
            a["created_a"] += D(r["orders_sum"]); a["created_q"] += D(r["orders_qty"]); a["turnover_net"] += net
            a["revenue"] += net * b * (1 - comm); a["comm_rub"] += net * b * comm; a["base"] += net * b
            if r.get("cogs") is not None:
                a["cogs"] += D(r["cogs"]) * b
            else:
                a["no_cost_q"] += D(r["orders_qty"])
    if ads_by_day:
        for d in days:
            ads = ads_by_day.get(d)
            if ads is not None:
                for lab in of(d):
                    acc[lab]["ads_net"] += D(ads) / vat_for(d)
    out = {}
    for lab in order:
        a = acc[lab]
        m = {"created_a": a["created_a"], "created_q": a["created_q"], "price": rep.ratio(a["created_a"], a["created_q"]),
             "commission_pct": comm, "buyout": b, "revenue": a["revenue"], "cogs": a["cogs"],
             "margin": a["revenue"] - a["cogs"], "ads_net": a["ads_net"] if ads_by_day else None, "coinvest_pct": None, "no_cost_q": a["no_cost_q"]}
        m["margin_pct"] = rep.ratio(m["margin"], a["revenue"])
        m["drr_pct"] = rep.ratio(m["ads_net"], a["turnover_net"]) if m["ads_net"] is not None else None
        m["gross_fin"] = (m["margin"] - m["ads_net"]) if m["ads_net"] is not None else None
        m["gross_fin_pct"] = rep.ratio(m["gross_fin"], a["revenue"]) if m["gross_fin"] is not None else None
        out[lab] = m
    return out


def wb_orders_as_data_rows(orders):
    """Строки заказов WB в колонках «Данные заказы» (ORDER_DATA_COLS): подтверждено — выкупы воронки по дню заказа, цена покупателя — нет."""
    out = []
    for r in orders:
        out.append({"date": r["date"], "month": r["month"], "platform": r["platform"], "brand": r.get("brand"), "category": r.get("category"),
                    "article": r.get("article"), "sku": str(r["nm_id"]), "name": r.get("title"), "created_a": D(r["orders_sum"]), "created_q": D(r["orders_qty"]),
                    "confirmed_a": D(r.get("funnel_buyouts_sum")), "buyer_a": None, "ads": D(r.get("ads")), "cogs_created": r.get("cogs"), "vat": vat_for(r["date"])})
    return out


# ---------- «Данные заказы» — поля кэша владельца (§7.2) ----------

LONG_COLS = [("ID начисления", "accrual_id", None), ("Дата начисления", "date", "date"), ("Артикул", "article", None), ("SKU", "sku", None),
             ("Количество", "qty", "int"), ("Статьи Озон.Вид затрат", "kind", None), ("Бренд (cc)", "brand_cc", None), ("Месяц", "month", None),
             ("Ст-ть продаж в себ-ти", "cogs", "money"), ("Бренд", "brand", None), ("Статус", "category", None), ("Начисления", "amount", "money"),
             ("Наименование", "name", None), ("Дни (Дата начисления)", "day_num", "int"), ("Месяцы (Дата начисления)", "month_num", "int"),
             ("Соинвест (баллы + зелёные цены)", "coinvest", "money"), ("Площадка", "platform", None)]          # 15 полей владельца + 2 наших справа
ORDER_DATA_COLS = [("Дата", "date", "date"), ("МП", "mp", None), ("Магазин", "shop", None), ("Артикул поставщика", "article", None),
                   ("Заказы, ₽", "created_a", "money"), ("Заказы, шт", "created_q", "int"), ("Реклама, ₽", "ads", "money"), ("Себ-ть Реал", "cogs_real", "money"),
                   ("Наименование", "name", None), ("Бренд", "brand", None), ("Статус", "category", None), ("Заказы, руб без НДС", "created_net", "money"),
                   ("Реклама, руб без НДС", "ads_net", "money"), ("СПП,%", "spp", "pct"), ("Заказы, руб c СПП", "with_spp", "money"), ("Комиссия", "commission", "pct"),
                   ("Выручка, руб без НДС с учетом комиссии", "revenue", "money"), ("Комиссия, руб", "commission_rub", "money"), ("Выкуплено", "buyout", "pct"),
                   ("Неделя года", "week", "int"), ("День", "day_num", "int"), ("Месяцы", "month_num", "int"), ("Маржа, руб без НДС", "margin", "money"),
                   ("Маржинальность, %", "margin_pct", "pct"), ("ДДР, %", "drr_pct", "pct"), ("Соинвест, %", "coinvest_pct", "pct"), ("Комиссия сред", "commission_avg", "pct"),
                   ("Фин.рез", "fin", "money"), ("Цена", "price", "money"), ("Фин.рез %", "fin_pct", "pct"),
                   ("SKU/nmId", "sku", None), ("Подтверждено сейчас, руб.", "confirmed_a", "money"), ("Площадка Ozon", "platform_name", None)]   # 30 + 3 наших
ORDER_CHECK_COLS = [("Оборот (с НДС)", "created_a"), ("Заказы, шт", "created_q"), ("Цена продавца", "price"), ("Комиссия %", "commission_pct"), ("Выручка", "revenue"),
                    ("Себестоимость", "cogs"), ("Маржа", "margin"), ("М-ть, %", "margin_pct"), ("Реклама", "ads_net"), ("ДДР, %", "drr_pct"),
                    ("Соинвест, %", "coinvest_pct"), ("Гр.фин.рез", "gross_fin"), ("Гр.фин.рез, %", "gross_fin_pct")]


def order_data_row(r, comm, comm_avg, buyout, mp, shop):
    """Строка «Данные заказы» по формулам «Свод»: Выручка = Заказы без НДС × Выкуплено × (1 − Комиссия); Комиссия, руб = Заказы без НДС × Выкуплено ×
    Комиссия; Маржа = Выручка − Себ-ть Реал × Выкуплено; Фин.рез = Маржа − Реклама без НДС; СПП = 1 − цена покупателя / цена продавца, где измерено;
    Выкуплено и Комиссия — коэффициенты площадки (лист «Коэффициенты»); проценты строки — от её же чисел."""
    vat, created, qty, ads = r["vat"], r["created_a"], r["created_q"], (r.get("ads") or Z)
    net, ads_net, buyer, cogs_real = created / vat, ads / vat, r.get("buyer_a"), r.get("cogs_created")
    spp = (1 - buyer / created) if (buyer is not None and created) else None
    priced = buyout is not None and comm is not None
    revenue = net * buyout * (1 - comm) if priced else None
    margin = revenue - (cogs_real or Z) * buyout if priced else None
    fin = margin - ads_net if priced else None
    d = r["date"]
    return {"date": d, "mp": mp, "shop": shop, "article": r["article"], "created_a": created, "created_q": qty, "ads": ads, "cogs_real": cogs_real,
            "name": r["name"], "brand": r["brand"], "category": r["category"], "created_net": net, "ads_net": ads_net, "spp": spp, "with_spp": buyer,
            "commission": comm, "revenue": revenue, "commission_rub": net * buyout * comm if priced else None, "buyout": buyout,
            "week": date.fromisoformat(d).isocalendar()[1], "day_num": int(d[8:10]), "month_num": int(d[5:7]), "margin": margin,
            "margin_pct": rep.ratio(margin, revenue) if priced else None, "drr_pct": rep.ratio(ads_net, net), "coinvest_pct": spp, "commission_avg": comm_avg,
            "fin": fin, "price": rep.ratio(created, qty), "fin_pct": rep.ratio(fin, revenue) if priced else None, "sku": r["sku"],
            "confirmed_a": r.get("confirmed_a"), "platform_name": r.get("platform_name"), "month": r["month"], "vat": vat}


def build_order_data_rows(order_rows, wb_parts, coef, days):
    """Строки «Данные заказы»: Ozon из широких строк, WB — строки модуля WB-сессии плюс строка рекламы WB на день без nmId (реклама у модуля — по дням).
    Нулевые строки (Заказы ₽ = 0, шт = 0, Реклама ₽ = 0) выбрасываются и считаются — для сводной они пусты."""
    buyout_all, comm_all = coef["buyout"].get("все"), coef["commission"].get("все")
    out, stats = [], defaultdict(int)

    def keep(row, mp):
        if not row["created_a"] and not row["created_q"] and not row["ads"]:
            stats[f"dropped_zero_{mp}"] += 1
            if row.get("confirmed_a"):
                stats[f"dropped_confirmed_{mp}"] += row["confirmed_a"]
            return
        out.append(row)

    for r in order_rows:
        keep(order_data_row(r, order_commission(coef, r["date"]), comm_all, buyout_all, "Ozon", SHOP), "Ozon")
    if wb_parts and not wb_parts.get("error"):
        wb_comm = 1 - wbfin.wbm.OWNER_ORDERS_AFTER_COMMISSION
        wb_buyout = (wb_parts.get("buyout") or {}).get("итого")
        wb_buyout = wb_buyout if wb_buyout is not None else Decimal(1)      # нет зрелых месяцев — 100 %, как в блоке WB «Свода»
        ads_in_rows = defaultdict(Decimal)
        for r in wb_orders_as_data_rows(wb_parts["orders"]):
            if r["cogs_created"] is None:
                stats["wb_rows_without_cost"] += 1
            ads_in_rows[r["date"]] += r["ads"] or Z
            keep(order_data_row(r, wb_comm, wb_comm, wb_buyout, "WB", wbfin.SHOP), "WB")
        for d in days:
            # реклама дня сверх строк номенклатур (модуль разносит updSum дня по nmId долями fullstats; день без долей — целиком сюда)
            ads = (wb_parts.get("ads_by_day") or {}).get(d)
            ads = None if ads is None else D(ads) - ads_in_rows[d]
            if ads is not None and ads:
                r = {"date": d, "month": month_label(d), "brand": "", "category": "", "article": "(без nmId)", "sku": "", "name": "", "created_a": Z,
                     "created_q": Z, "confirmed_a": None, "buyer_a": None, "ads": D(ads), "cogs_created": Z, "vat": vat_for(d), "platform_name": None}
                keep(order_data_row(r, wb_comm, wb_comm, wb_buyout, "WB", wbfin.SHOP), "WB")
    return out, dict(stats)


def check_labels(data_rows):
    """Ярлыки «Данных заказы» по площадкам (§5): множества «Бренд» и «Статус» у Ozon и WB, строки Ozon без бренда по артикулу."""
    brands, cats, brandless = defaultdict(set), defaultdict(set), defaultdict(int)
    for r in data_rows:
        if r.get("brand"):
            brands[r["mp"]].add(r["brand"])
        elif r["mp"] == "Ozon":
            brandless[r.get("article") or ""] += 1
        if r.get("category"):
            cats[r["mp"]].add(r["category"])
    mps = sorted(brands) or ["Ozon"]
    equal = all(brands[m] == brands[mps[0]] and cats[m] == cats[mps[0]] for m in mps)
    return {"brands": {m: set(v) for m, v in brands.items()}, "categories": {m: set(v) for m, v in cats.items()}, "brandless_ozon": dict(brandless), "equal": equal}


def sum_order_data(data_rows, days, mp):
    """Σ «Данных заказы» по меткам сводной для площадки: деньги суммой, проценты — от сумм (как в «Свод»)."""
    order, of = labels_for(days)
    acc = {lab: defaultdict(Decimal) for lab in order}
    for r in data_rows:
        if r["mp"] != mp:
            continue
        for lab in of(r["date"]):
            a = acc[lab]
            a["created_a"] += r["created_a"]; a["created_q"] += r["created_q"]; a["net"] += r["created_net"]; a["ads_net"] += r["ads_net"]
            if r["revenue"] is not None:
                a["revenue"] += r["revenue"]; a["comm_rub"] += r["commission_rub"]; a["base"] += r["created_net"] * r["buyout"]
                a["cogs"] += (r["cogs_real"] or Z) * r["buyout"]; a["margin"] += r["margin"]; a["fin"] += r["fin"]; a["priced"] += 1
            if r["spp"] is not None:
                a["spp_base"] += r["created_a"]; a["spp_buyer"] += r["with_spp"]
    out = {}
    for lab in order:
        a, priced = acc[lab], bool(acc[lab]["priced"])
        out[lab] = {"created_a": a["created_a"], "created_q": a["created_q"], "price": rep.ratio(a["created_a"], a["created_q"]),
                    "commission_pct": rep.ratio(a["comm_rub"], a["base"]) if a["base"] else None,
                    "revenue": a["revenue"] if priced else None, "cogs": a["cogs"] if priced else None, "margin": a["margin"] if priced else None,
                    "margin_pct": rep.ratio(a["margin"], a["revenue"]) if priced else None, "ads_net": a["ads_net"], "drr_pct": rep.ratio(a["ads_net"], a["net"]),
                    "coinvest_pct": rep.ratio(a["spp_base"] - a["spp_buyer"], a["spp_base"]) if a["spp_base"] else None,
                    "gross_fin": a["fin"] if priced else None, "gross_fin_pct": rep.ratio(a["fin"], a["revenue"]) if priced else None}
    return out


def check_orders_data(data_rows, days, piv_ozon, piv_wb):
    """«Свод» против Σ «Данных заказы» по месяцам, итогу и площадкам: [(площадка, метка, колонка, свод, данные, разница)] по 13 колонкам.
    Деньги сравниваются до копейки, проценты — до 0,01 п. п."""
    out = []
    for mp, piv in (("Ozon", piv_ozon), ("WB", piv_wb)):
        if not piv:
            continue
        sums = sum_order_data(data_rows, days, mp)
        for lab in sums:
            if "." in lab:
                continue
            for title, k in ORDER_CHECK_COLS:
                a, b = (piv.get(lab) or {}).get(k), sums[lab].get(k)
                pct = k.endswith("_pct")
                diff = (D(a) if a is not None else Z) - (D(b) if b is not None else Z)
                diff = diff.quantize(Decimal("0.0001")) if pct else q(diff)
                out.append((mp, lab, title, a, b, diff))
    return out


# ---------- запись книги (write_only: строки только вперёд) ----------

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


class Grid:
    """Разреженная сетка для сводных листов: ячейки ставятся по (строка, колонка), лист пишется построчно (write_only)."""

    def __init__(self):
        self.cells, self.max_r, self.max_c, self.heights = {}, 0, 0, {}

    def set(self, r, c, v, fmt=None, bold=False, fill=False, wrap=False):
        if isinstance(v, Decimal):
            v = float(v)
        self.cells[(r, c)] = (v, fmt, bold, fill, wrap)
        self.max_r, self.max_c = max(self.max_r, r), max(self.max_c, c)

    def flush(self, ws):
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Alignment
        fmts, bold_font, fill_style = _styles()
        for r in range(1, self.max_r + 1):
            if r in self.heights:
                ws.row_dimensions[r].height = self.heights[r]
            row = []
            for c in range(1, self.max_c + 1):
                cell = self.cells.get((r, c))
                if cell is None:
                    row.append(None)
                    continue
                v, fmt, bold, fill, wrap = cell
                if not (fmt or bold or fill or wrap):
                    row.append(v)
                    continue
                wc = WriteOnlyCell(ws, value=v)
                if fmt and v is not None and v != "":
                    wc.number_format = fmts[fmt]
                if bold:
                    wc.font = bold_font
                if fill:
                    wc.fill = fill_style
                if wrap:
                    wc.alignment = Alignment(wrap_text=True, vertical="center")
                row.append(wc)
            ws.append(row)


def write_data_sheet(wb, title, cols, rows, widths=None, freeze="A2"):
    """Плоский лист: заголовки в строке 1, данные со строки 2, ничего под таблицей (примечания — лист «Примечания»). Возвращает число строк."""
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.utils import get_column_letter
    fmts, bold, fill = _styles()
    ws = wb.create_sheet(title)
    for j in range(1, len(cols) + 1):
        ws.column_dimensions[get_column_letter(j)].width = (widths or {}).get(j, 14)
    ws.freeze_panes = freeze
    head = []
    for h, _k, _f in cols:
        c = WriteOnlyCell(ws, value=h)
        c.font, c.fill = bold, fill
        head.append(c)
    ws.append(head)
    getters = [(k, f) for _h, k, f in cols]
    n = 0
    for r in rows:
        get = r.get if isinstance(r, dict) else (lambda k, _r=r: getattr(_r, k, None))
        out = []
        for k, f in getters:
            v = get(k)
            if v is None or v == "":
                out.append(None)
                continue
            if isinstance(v, Decimal):
                v = float(v)
            if f == "date" and isinstance(v, str):
                v = date.fromisoformat(v)
            if f:
                c = WriteOnlyCell(ws, value=v)
                c.number_format = fmts[f]
                out.append(c)
            else:
                out.append(v)
        ws.append(out)
        n += 1
    return n


def write_pivot_block(g, r_cols, title, pivot, order):
    """Блок сводной владельца: заголовок блока на две строки выше, «Названия столбцов» в строке r_cols (как у него — 9-я на «Вывод данных»),
    статьи r_cols + 1, «Названия строк» и подзаголовки r_cols + 2, строки меток с r_cols + 3. Возвращает следующую свободную строку."""
    g.set(r_cols - 2, 1, title, bold=True)
    g.set(r_cols, 2, "Названия столбцов", bold=True)
    c = 2
    for art in ARTICLES:
        g.set(r_cols + 1, c, art, bold=True)
        g.set(r_cols + 2, c, " Начисления"); g.set(r_cols + 2, c + 1, " Ст-ть продаж в себ-ти")
        c += 2
    g.set(r_cols + 2, 1, "Названия строк", bold=True)
    calc0 = c + 2
    for j, (h, _k, _f) in enumerate(PIVOT_CALC + PIVOT_EXTRA):
        g.set(r_cols + 2, calc0 + j, h, bold=True, fill=True, wrap=True)
    g.heights[r_cols + 2] = 45
    for i, lab in enumerate(order):
        r = r_cols + 3 + i
        m = pivot.get(lab) or {}
        g.set(r, 1, lab, bold=(lab == "Общий итог"))
        c = 2
        for art in ARTICLES:
            g.set(r, c, m.get(PIVOT_ARTICLE_KEYS[art], Z), "money")
            g.set(r, c + 1, m.get("cogs", Z) if art == "Товарооборот" else Z, "money")
            c += 2
        vals = dict(m)
        vals["commission_abs"] = -m.get("commission", Z) if m else None
        for j, (_h, k, f) in enumerate(PIVOT_CALC + PIVOT_EXTRA):
            g.set(r, calc0 + j, vals.get(k), f)
    return r_cols + 3 + len(order) + 2


def _grid_sheet(wb, title, g, widths=15, ncols=40, freeze=None):
    from openpyxl.utils import get_column_letter
    ws = wb.create_sheet(title)
    for j in range(1, ncols + 1):
        ws.column_dimensions[get_column_letter(j)].width = widths
    if freeze:
        ws.freeze_panes = freeze
    g.flush(ws)
    return ws


ARTICLE_BUYOUT_KINDS = ("Товарооборот", "Комиссия", "Логистика", "Реклама", "Эквайринг", "Прочее")


def write_article_sheet(wb, days, long_rows, order_data_rows, wb_parts):
    """Запасной лист «Артикул» (§4.3): выпадающий список артикулов (лист «Списки», скрытый), площадка заказов, формулы SUMIFS по «Данным» —
    экономика артикула по месяцам: выкупы Ozon по статьям, СС, соинвест, штуки, фин. рез.; выкупы WB по статьям; заказы по площадке.
    Считает Excel при открытии; в openpyxl формулы не вычисляются."""
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.worksheet.datavalidation import DataValidation
    fmts, bold, fill = _styles()
    months = []
    for d in days:
        if month_label(d) not in months:
            months.append(month_label(d))
    month_no = {month_label(d): int(d[5:7]) for d in days}
    articles = sorted({r.article for r in long_rows if r.article and r.article != NO_SKU} | {r["article"] for r in (order_data_rows or []) if r.get("article") and not str(r["article"]).startswith("(")}
                      | ({str(r.get("article") or "") for r in (wb_parts or {}).get("rows", []) if r.get("article")} if wb_parts and not wb_parts.get("error") else set()))
    ls = wb.create_sheet("Списки")
    ls.sheet_state = "hidden"
    ls.append(["Артикул", "МП"])
    for i, a in enumerate(articles):
        ls.append([a, ("Ozon", "WB")[i] if i < 2 else None])
    if len(articles) < 2:
        for i in range(len(articles), 2):
            ls.append([None, ("Ozon", "WB")[i]])
    ws = wb.create_sheet("Артикул")
    from openpyxl.utils import get_column_letter
    ws.column_dimensions["A"].width = 44
    for j in range(2, 12):
        ws.column_dimensions[get_column_letter(j)].width = 16
    ws.freeze_panes = "B5"
    dv_art = DataValidation(type="list", formula1=f"=Списки!$A$2:$A${max(2, len(articles) + 1)}", allow_blank=False)
    dv_mp = DataValidation(type="list", formula1="=Списки!$B$2:$B$3", allow_blank=False)
    ws.data_validations.append(dv_art); ws.data_validations.append(dv_mp)
    dv_art.add("B1"); dv_mp.add("B2")

    def cell(v, fmt=None, b=False, f=False):
        c = WriteOnlyCell(ws, value=v)
        if fmt:
            c.number_format = fmts[fmt]
        if b:
            c.font = bold
        if f:
            c.fill = fill
        return c

    ws.append([cell("Артикул (выберите из списка ↓)", b=True), articles[0] if articles else "", "экономика артикула по месяцам — формулы SUMIFS по листам «Данные»; считает Excel"])
    ws.append([cell("Площадка заказов (МП)", b=True), "Ozon", "для блока «Заказы»: Ozon / WB"])
    ws.append([cell("№ месяца", b=True)] + [month_no[m] for m in months] + [None])
    ws.append([cell("Показатель", b=True, f=True)] + [cell(m, b=True, f=True) for m in months] + [cell("Итого", b=True, f=True)])
    row = 5
    cols = [get_column_letter(j) for j in range(2, 2 + len(months))]
    total_col = get_column_letter(2 + len(months))

    def line(label, formula_of, fmt="money"):
        nonlocal row
        cells = [cell(label)] + [cell(formula_of(c), fmt) for c in cols] + [cell(f"=SUM({cols[0]}{row}:{cols[-1]}{row})", fmt)]
        ws.append(cells); row += 1

    def head(label):
        nonlocal row
        ws.append([cell(label, b=True, f=True)] + [cell(None, f=True)] * (len(months) + 1)); row += 1

    D_ = "'Данные Ozon выкупы'"
    head("Выкупы Ozon (с НДС, знак сводной владельца: списания < 0)")
    first = row
    for kind in ARTICLE_BUYOUT_KINDS:
        line(kind, lambda c, k=kind: f'=SUMIFS({D_}!$L:$L,{D_}!$C:$C,$B$1,{D_}!$H:$H,{c}$4,{D_}!$F:$F,"{k}")')
    last = row - 1
    line("Ст-ть продаж в себ-ти", lambda c: f"=SUMIFS({D_}!$I:$I,{D_}!$C:$C,$B$1,{D_}!$H:$H,{c}$4)")
    cogs_row = row - 1
    line("Соинвест (баллы + зелёные цены)", lambda c: f"=SUMIFS({D_}!$P:$P,{D_}!$C:$C,$B$1,{D_}!$H:$H,{c}$4)")
    line("Штуки", lambda c: f"=SUMIFS({D_}!$E:$E,{D_}!$C:$C,$B$1,{D_}!$H:$H,{c}$4)", "int")
    line("Фин. рез. с НДС (Σ статей − Ст-ть продаж)", lambda c: f"=SUM({c}{first}:{c}{last})-{c}{cogs_row}")
    ws.append([None]); row += 1
    W_ = "'Данные WB выкупы'"
    head("Выкупы WB (с НДС, модуль WB-сессии: продажи +, статьи как в его листе)")
    for label, col in (("Продажи, ₽", "E"), ("Реклама, ₽", "F"), ("Комиссия, ₽", "G"), ("Логистика, ₽", "H"), ("Себес-ть, ₽", "I"), ("Хранение, ₽", "J"), ("Ост.расходы и компенсации МП, ₽", "K")):
        line(label, lambda c, col=col: f"=SUMIFS({W_}!${col}:${col},{W_}!$D:$D,$B$1,{W_}!$B:$B,{c}$4)")
    ws.append([None]); row += 1
    O_ = "'Данные заказы'"
    head("Заказы (площадка — B2; месяц — по номеру в строке 3)")
    for label, col, fmt in (("Заказы, ₽", "E", "money"), ("Заказы, шт", "F", "int"), ("Реклама, ₽", "G", "money"), ("Заказы, руб c СПП", "O", "money"),
                            ("Выручка, руб без НДС с учетом комиссии", "Q", "money"), ("Маржа, руб без НДС", "W", "money"), ("Фин.рез", "AB", "money")):
        line(label, lambda c, col=col: f"=SUMIFS({O_}!${col}:${col},{O_}!$D:$D,$B$1,{O_}!$B:$B,$B$2,{O_}!$V:$V,{c}$3)", fmt)
    line("Соинвест, % (по строкам с измеренной ценой покупателя)",
         lambda c: f'=IFERROR(1-SUMIFS({O_}!$O:$O,{O_}!$D:$D,$B$1,{O_}!$B:$B,$B$2,{O_}!$V:$V,{c}$3)/SUMIFS({O_}!$E:$E,{O_}!$D:$D,$B$1,{O_}!$B:$B,$B$2,{O_}!$V:$V,{c}$3,{O_}!$O:$O,"<>"),"")', "pct")
    ws.append([None]); row += 1
    ws.append(["Список артикулов — лист «Списки» (скрыт); при открытии Excel пересчитывает формулы по текущим листам «Данные»."])


def write_book(path, days, long_rows, pivots, order_rows_data, orders_pivot, coef, notes, catalog_source, today, wb=None, order_data_rows=None, timings=None):
    """Книга в форме владельца. Порядок листов: Выкупы Ozon · × бренд · × категория · Данные Ozon выкупы · Выкупы WB · по брендам · по категориям ·
    Данные WB выкупы · Заказы · Коэффициенты · Данные заказы · Примечания. Выравнивание с образцом: «Названия столбцов» — строка 9 на листах выкупов,
    строка 7 на «Заказы»; служебные подписи — выше. order_data_rows None — лист «Данные заказы» не пишется (--no-orders-data)."""
    import openpyxl
    timings = timings if timings is not None else {}
    t = time.time()

    def lap(name):
        nonlocal t
        timings[name] = round(time.time() - t, 1)
        t = time.time()

    wb_parts = wb
    wb = openpyxl.Workbook(write_only=True)
    g = Grid()
    g.set(1, 1, "Выкупы Ozon — форма «Вывод данных» владельца на наших данных", bold=True)
    g.set(2, 1, "строки 3 … 8 — место блока фильтров сводной владельца; «Названия столбцов» — строка 9, «Названия строк» — 11, как в образце")
    nxt = write_pivot_block(g, 9, "Все бренды и категории", pivots["all"][0]["Ozon"], pivots["all"][1])
    for n, line in enumerate(notes["buyouts"]):
        g.set(nxt + n, 1, line)
    _grid_sheet(wb, "Выкупы Ozon", g, freeze="B12")
    for title, key in (("Выкупы Ozon × бренд", "brand"), ("Выкупы Ozon × категория", "category")):
        g = Grid()
        g.set(1, 1, title, bold=True)
        g.set(2, 1, "блоки — по группе; в каждом «Названия столбцов» на две строки ниже заголовка блока")
        r_cols, piv, order = 9, pivots[key][0], pivots[key][1]
        for name in sorted(piv, key=lambda n: (n == "", n)):
            r_cols = write_pivot_block(g, r_cols, name or "(без признака)", piv[name], order) + 2
        _grid_sheet(wb, title, g)
    lap("сводные выкупов")
    n_long = write_data_sheet(wb, "Данные Ozon выкупы", LONG_COLS, long_rows, widths={13: 48})
    lap("Данные Ozon выкупы")
    if wb_parts and not wb_parts.get("error"):
        write_data_sheet(wb, "Выкупы WB", wbfin.SHEET_COLS, wb_parts["month"])
        write_data_sheet(wb, "Выкупы WB по брендам", wbfin.SPLIT_COLS, wb_parts["split_brand"])
        write_data_sheet(wb, "Выкупы WB по категориям", wbfin.SPLIT_COLS, wb_parts["split_category"])
        write_data_sheet(wb, "Данные WB выкупы", wbfin.DATA_COLS, wb_parts["rows"], widths={7: 48})
    else:
        g = Grid()
        g.set(1, 1, "Лист WB не собран: " + str((wb_parts or {}).get("error") or "модуля WB нет"))
        _grid_sheet(wb, "Выкупы WB", g)
    lap("листы WB")
    # заказы — форма «Свод»: «Названия столбцов» строка 7, площадки 8, «Названия строк» 9, данные с 10
    g = Grid()
    g.set(1, 1, "Заказы — форма «Свод» владельца; блок Ozon на наших коэффициентах (лист «Коэффициенты»), блок WB — модуль WB-сессии", bold=True)
    g.set(2, 1, "строки 2 … 6 — место блока фильтров сводной владельца; «Названия столбцов» — строка 7, как в образце")
    g.set(7, 2, "Названия столбцов", bold=True)
    g.set(8, 2, "Ozon", bold=True); g.set(8, 2 + len(ORDER_COLS), "WB", bold=True)
    g.set(9, 1, "Названия строк", bold=True)
    for blk in (0, 1):
        for j, (h, _k, _f) in enumerate(ORDER_COLS):
            g.set(9, 2 + blk * len(ORDER_COLS) + j, h, bold=True, fill=True)
    piv, order = orders_pivot
    extra_col = 2 + 2 * len(ORDER_COLS) + 1
    g.set(9, extra_col, "справочно: соинвест Ozon — доля созданного оборота с измеренной ценой покупателя", bold=True, fill=True)
    for i, lab in enumerate(order):
        r = 10 + i
        g.set(r, 1, lab, bold=(lab == "Общий итог"))
        for j, (_h, k, f) in enumerate(ORDER_COLS):
            g.set(r, 2 + j, piv[lab].get(k), f)
        g.set(r, extra_col, piv[lab].get("coinvest_cover"), "pct")
        if wb_parts and not wb_parts.get("error"):
            wm = wb_parts["orders_pivot"].get(lab) or {}
            for j, (_h, k, f) in enumerate(ORDER_COLS):
                g.set(r, 2 + len(ORDER_COLS) + j, wm.get(k), f)
    base = 10 + len(order) + 2
    for n, line in enumerate(notes["orders"]):
        g.set(base + n, 1, line)
    _grid_sheet(wb, "Заказы", g, widths=14, ncols=30, freeze="B10")
    # коэффициенты: сверху две таблички владельца с нашими числами, ниже — развёрнутая
    g = Grid()
    for j, h in enumerate(["МП", "% выкупа", None, "МП", "Комиссия"], 1):
        if h:
            g.set(1, j, h, bold=True, fill=True)
    wb_comm = (1 - wbfin.wbm.OWNER_ORDERS_AFTER_COMMISSION) if wbfin is not None else None
    wb_rate = (wb_parts or {}).get("buyout") or {}
    wb_details = (wb_parts or {}).get("buyout_details") or {}
    g.set(2, 1, "Ozon"); g.set(2, 2, coef["buyout"].get("все"), "pct")
    g.set(3, 1, "WB"); g.set(3, 2, wb_rate.get("итого"), "pct")
    g.set(2, 4, "Ozon"); g.set(2, 5, coef["commission"].get("все"), "pct")
    g.set(3, 4, "WB"); g.set(3, 5, wb_comm, "pct")
    g.set(5, 1, "Две таблички — форма владельца с НАШИМИ числами (решения владельца 25.09): % выкупа Ozon — измеренный (подтверждённые / созданные ₽, дни от "
                "21 суток); % выкупа WB — модуль WB-сессии buyout_rate_for_finrez: продажи отчёта реализации по месяцу заказа / Σ orderSum воронки, только "
                "зрелые месяцы (конец месяца + 25 дней ≤ сегодня), итог = Σ числителей / Σ знаменателей; комиссия Ozon — одна фактическая за окно "
                "(деление до / выше 300 ₽ снято), WB — 1 − 0,58 модуля. Образец владельца: 0,843 / 0,865 и 0,23 / 0,52 / 0,43.")
    r = 7
    heads = ["МП / площадка", "% выкупа — наш (подтверждённые / созданные ₽, дни от 21 суток)", "база: подтверждено ₽", "база: создано ₽", "образец владельца"]
    for j, h in enumerate(heads, 1):
        g.set(r, j, h, bold=True, fill=True)
    r += 1
    owner_b = dict(OWNER_COEF["buyout"])
    for key in ["все"] + [n for _p, n in rep.PLATFORMS] + [rep.NO_PLATFORM]:
        if key not in coef["buyout"]:
            continue
        conf, created = coef["buyout_base"][key]
        g.set(r, 1, f"Ozon — {key}"); g.set(r, 2, coef["buyout"][key], "pct"); g.set(r, 3, conf, "money"); g.set(r, 4, created, "money")
        g.set(r, 5, owner_b["Ozon"] if key == "все" else None, "pct")
        r += 1
    g.set(r, 1, "WB — итого (зрелые месяцы, когорта месяца заказа, ₽)"); g.set(r, 2, wb_rate.get("итого"), "pct"); g.set(r, 5, owner_b["WB"], "pct")
    if wb_rate.get("итого") is None:
        g.set(r, 3, (wb_parts or {}).get("buyout_note") or "зрелых месяцев нет")
    r += 1
    for mo in sorted(wb_details):
        d = wb_details[mo]
        g.set(r, 1, f"WB — {month_label(mo + '-01')} ({mo})")
        if d.get("mature"):
            g.set(r, 2, d.get("rate"), "pct"); g.set(r, 3, d.get("numerator"), "money"); g.set(r, 4, d.get("denominator"), "money")
        else:
            g.set(r, 2, f"незрелый — нет (зрелость с {d.get('mature_from')})"); g.set(r, 4, d.get("denominator"), "money")
        r += 1
    r += 1
    for j, h in enumerate(["МП / месяц", "Комиссия — наша фактическая (Σ комиссии / Σ оборота выкупов, с НДС)", "", "", "образец владельца"], 1):
        g.set(r, j, h, bold=True, fill=True)
    r += 1
    for key in [k for k in coef["commission"] if k != "все"] + ["все"]:
        g.set(r, 1, f"Ozon — {key}"); g.set(r, 2, coef["commission"][key], "pct"); r += 1
    for name, v in OWNER_COEF["commission"]:
        g.set(r, 1, name + " — образец владельца"); g.set(r, 5, v, "pct"); r += 1
    for n, line in enumerate(notes["coef"], 1):
        g.set(r + n, 1, line)
    _grid_sheet(wb, "Коэффициенты", g, widths=30, ncols=6)
    lap("Заказы и Коэффициенты")
    n_orders = None
    if order_data_rows is not None:
        n_orders = write_data_sheet(wb, "Данные заказы", ORDER_DATA_COLS, order_data_rows, widths={9: 48})
        lap("Данные заказы")
    write_article_sheet(wb, days, long_rows, order_data_rows, wb_parts)
    lap("Артикул")
    g = Grid()
    g.set(1, 1, "Лист", bold=True, fill=True); g.set(1, 2, "Примечание", bold=True, fill=True)
    r = 2
    for sheet, key in (("Данные Ozon выкупы", "buyout_data"), ("Данные заказы", "order_data"), ("Выкупы WB / Данные WB выкупы", "wb"), ("Заказы", "orders"), ("Коэффициенты", "coef")):
        for line in notes.get(key) or ():
            g.set(r, 1, sheet); g.set(r, 2, line); r += 1
    _grid_sheet(wb, "Примечания", g, widths=40, ncols=2)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wb.save(path)
    lap("save")
    return {"long_rows": n_long, "order_rows": n_orders}


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
    ap.add_argument("--no-orders-data", action="store_true", help="не писать лист «Данные заказы» (запасной флаг; по умолчанию пишется)")
    ap.add_argument("--no-wb", action="store_true", help="без WB-части (модуль WB-сессии не зовётся)")
    ap.add_argument("--no-pivots", action="store_true", help="не пересаживать сводные владельца (scripts/finrez_pivots.py) в книгу")
    args = ap.parse_args(argv)
    t_start, t_last = time.time(), time.time()

    def phase(name):
        nonlocal t_last
        now = time.time()
        print(f"  [{now - t_start:6.1f} с] {name}: {now - t_last:.1f} с")
        t_last = now

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
    phase("чтение базы Ozon")
    daily_rows, unknown = rep.build_daily(days, buyouts, expenses, ledger, unit_cost, today)
    for r in daily_rows:
        idx = rep.cost_index_for(r["date"])
        r["cogs_index"] = r["cogs"] * idx if idx is not None else r["cogs"]
    print(f"окно {d1} … {d2}: дней {len(days)}, выкупов {len(buyouts)}, расходов {len(expenses)}, строк витрины {len(kpi)}, заказов {len(order_rows)}, "
          f"дней с типами в леджере {len([d for d in days if d in ledger])}; каталог: {catalog_source} ({len(catalog)} SKU); СС: артикулов {cost_found} из {cost_asked}")
    long_rows, buyout_rows, bstats = build_buyout_long_rows(days, RAW_DIR, buyouts, expenses, kpi, daily_rows, unit_cost, sku2art, catalog, names)
    young_days = [d for d in days if (date.today() - date.fromisoformat(d)).days < rep.YOUNG_DAYS]
    pivots = {"all": pivot_buyouts(buyout_rows, days), "brand": pivot_buyouts(buyout_rows, days, lambda r: r["brand"]),
              "category": pivot_buyouts(buyout_rows, days, lambda r: r["category"])}
    phase("выкупы: длинные строки из сырья и сводные")
    order_data, ostats = build_order_rows(days, order_rows, kpi, daily_rows, unit_cost, sku2art, catalog, names, today)
    coef = coefficients(order_data, buyout_rows, days)
    orders_pivot = pivot_orders(order_data, days, coef, daily_rows)
    phase("заказы Ozon: строки, коэффициенты, свод")
    idx_note = "; ".join(f"с {vf} × {v}" for vf, v in rep.COST_INDEX)
    residual_note = "; ".join(f"{OWNER_ARTICLE[k]} — дней {bstats.get(f'residual_{k}_days', 0)}, Σ {bstats.get(f'residual_{k}_sum', Z):,.2f}"
                              for k in WIDE_KEYS if bstats.get(f"residual_{k}_days"))
    notes = {
        "buyouts": [f"Источники: сырьё начислений by-day (data/accrual_history, дней {bstats.get('days_from_raw', 0)}; без файла — таблицы, дней {bstats.get('days_from_tables', 0)}), "
                    f"реклама — Performance по SKU (ad_spend витрины) плюс остаток до начислений 41 + 54 (леджер). НДС по дате ({vat_for(d2)} на {d2}).",
                    f"Себестоимость = количество × СС снимка 1С {args.snapshot} × индекс СС ({idx_note}; до — без индекса); позиций без СС: {bstats.get('positions_without_cost', 0)}.",
                    "«Прочее» включает подписку (у владельца отдельной статьи нет); эквайринг — тип 1 по строке товара; компенсации 25 / 10 — не статья формы, в лист не входят. "
                    "«Ст-ть продаж в себ-ти» — только под Товарооборотом, как в образце.",
                    ("Дни моложе двух суток (" + ", ".join(young_days) + "): реклама 41 + 54 в леджере появляется следующей ночью — в этих строках реклама занижена; утренняя книга берёт их живым ответом."
                     if young_days else "Дней моложе двух суток в книге нет."),
                    f"Категория — карточка Ozon ({catalog_source}); бренд — по первой букве артикула (T — Топаз, иначе KARATOV)."
                    + (f" Дней без сырья рекламы: {bstats['days_without_raw_ads']}." if bstats.get("days_without_raw_ads") else "")],
        "buyout_data": ["Длинный формат кэша сводной владельца: строка на начисление × SKU × статья; 15 его полей по порядку, справа наши «Соинвест» и «Площадка». "
                        "Знак как в его сводной: списания отрицательные, товарооборот и себестоимость положительные; суммы с НДС.",
                        "ID начисления — accrual_id ответа /v1/finance/accrual/by-day; пусто у строк рекламы Performance по SKU и у строк остатка дня «(без SKU)» "
                        "(леджер 41 + 54 минус Performance по SKU; для прочих статей — цель листа месяца минус Σ строк дня"
                        + (f": {residual_note}" if residual_note else ": по всем статьям, кроме рекламы, остаток 0,00 на всех днях") + ").",
                        "Количество — только у строк Товарооборот: позиции ±1 по знаку продажи, измеренные штуки (buyouts_units) разложены по строкам (день, SKU) — "
                        f"разница к первой строке продажи, ключей с разницей {bstats.get('units_reallocated_keys', 0)}; Ст-ть продаж = количество × СС × индекс."],
        "orders": ["Оборот — созданные заказы (подтверждённые + отменённые) по цене продавца, с НДС; Заказы, шт — созданные штуки; Цена продавца = оборот / шт.",
                   "Комиссия % — наша фактическая за месяц дня (Σ комиссии / Σ оборота выкупов); % выкупа — наш измеренный (лист «Коэффициенты»). Построчно: "
                   "Выручка = Заказы без НДС × % выкупа × (1 − комиссия); Себестоимость = СС созданного × % выкупа; Маржа = Выручка − Себестоимость; свод — Σ строк.",
                   "Реклама — начисления 41 + 54 по дню заказа без НДС; ДДР = Реклама / (Оборот / НДС); Соинвест % = (создано − оплачено покупателем) / создано по строкам "
                   "с измеренной ценой покупателя — доля измеренного оборота в справочной колонке справа (история до 09-01 и ключи, не добранные бэкфиллом, не измерены).",
                   "Гр. фин. рез = Маржа − Реклама (по образцу владельца через коэффициенты). Блок WB — теми же формулами: комиссия 1 − 0,58 модуля, % выкупа — "
                   "итог зрелых месяцев buyout_rate_for_finrez модуля WB-сессии (лист «Коэффициенты»), СС и созданные — модуль (scripts/report_finrez_wb.py)."],
        "coef": ["% выкупа Ozon — Σ подтверждённых ₽ / Σ созданных ₽ по дням возраста ≥ 21 суток в окне книги (дозревшие); образец владельца 0,84 / 0,86 — из его книги, числа другого кабинета.",
                 "% выкупа WB — модуль WB-сессии (report_finrez_wb.buyout_rate_for_finrez): когорта месяца заказа по ₽ — продажи отчёта реализации (Продажа − Возврат по retailPriceWithDisc, "
                 "orderDt в МСК) / Σ orderSum воронки за месяц; только зрелые месяцы (конец месяца + 25 дней ≤ сегодня), незрелые в словаре отсутствуют; "
                 "«итого» = Σ числителей / Σ знаменателей по зрелым — одно число на площадку, как у Ozon.",
                 "Комиссия Ozon — одна фактическая за окно (Σ комиссии выкупов / Σ оборота выкупов, с НДС; помесячные — ниже); WB — 1 − 0,58 модуля; "
                 "образец владельца: Ozon до 300 ₽ 23 %, выше 52 %, WB 43 % — деление по цене снято решением владельца 25.09."],
        "order_data": ["30 полей кэша владельца по порядку, справа наши SKU/nmId, «Подтверждено сейчас» и «Площадка Ozon». Строка на (дата, площадка, SKU / nmId); "
                       "нулевые строки (Заказы ₽ = 0, шт = 0, Реклама ₽ = 0) не пишутся. «(без SKU)» / «(без nmId)» — реклама дня сверх строк товаров "
                       "(Ozon: 41 + 54 над Performance по SKU; WB: списания дня целиком — модуль даёт рекламу по дням).",
                       "Формулы строки = формулам «Свод»: Заказы без НДС = ₽ / НДС; Выручка = Заказы без НДС × Выкуплено × (1 − Комиссия); Комиссия, руб = Заказы без НДС × "
                       "Выкуплено × Комиссия; Маржа = Выручка − Себ-ть Реал × Выкуплено; Фин.рез = Маржа − Реклама без НДС; СПП = Соинвест % = 1 − цена покупателя / "
                       "цена продавца, где измерено (иначе пусто); Комиссия — месяца площадки, Комиссия сред — за окно; Выкуплено — коэффициент площадки "
                       "(Ozon — измеренный, WB — итог зрелых месяцев модуля; см. «Коэффициенты»); Себ-ть Реал — СС × шт × индекс (WB — правило модуля, без индекса).",
                       f"Штук Ozon без СС: {ostats.get('qty_without_cost', 0)}." + (f" Дней без сырья рекламы: {ostats['days_without_raw_ads']}." if ostats.get("days_without_raw_ads") else "")],
    }
    if args.no_wb:
        wb_parts = {"error": "WB-часть выключена флагом --no-wb"}
    else:
        wb_parts = build_wb_parts(args.month_from, args.month_to, date_to, d1, d2, days)
    phase("WB-часть (модуль WB-сессии)")
    if wb_parts.get("error"):
        print(f"WB-часть не собрана: {wb_parts['error']}")
        notes["wb"] = []
    else:
        wt = wb_parts["month"][-1]
        print(f"WB (report_finrez_wb): строк «Данные WB выкупы» {len(wb_parts['rows'])}, заказов WB {len(wb_parts['orders'])}; итого продажи {wt['sales']:,.2f}, "
              f"комиссия {wt['commission']:,.2f}, логистика + хранение без НДС {wt['r_logistics']:,.2f}, фин. рез. {wt['y_fin']:,.2f}"
              + (f"; {wb_parts['ads_note']}" if wb_parts["ads_note"] else ""))
        rate = (wb_parts.get("buyout") or {}).get("итого")
        print("  коэффициент выкупа WB (модуль): " + (", ".join(f"{k} {v * 100:.2f} %" for k, v in wb_parts["buyout"].items()) if wb_parts.get("buyout") else "нет зрелых месяцев")
              + (f"; {wb_parts['buyout_note']}" if wb_parts.get("buyout_note") else ""))
        if rate is not None:
            print("  блок WB «Заказов» до (выкуп 100 %) → после (выкуп итог) по месяцам: выручка, себестоимость, маржа, гр. фин. рез.; отношение = коэффициенту")
            p0, p1 = wb_parts["orders_pivot_no_rate"], wb_parts["orders_pivot"]
            for lab in [l for l in p1 if "." not in l]:
                a, b = p0[lab], p1[lab]
                if not a.get("revenue"):
                    continue
                print(f"    {lab:11} выручка {a['revenue']:>16,.2f} → {b['revenue']:>16,.2f} ({b['revenue'] / a['revenue']:.6f}); СС {a['cogs']:>16,.2f} → {b['cogs']:>16,.2f}; "
                      f"маржа {a['margin']:>16,.2f} → {b['margin']:>16,.2f}; гр. фин. рез. {(a.get('gross_fin') or Z):>16,.2f} → {(b.get('gross_fin') or Z):>16,.2f}")
        notes["wb"] = [f"Строки и форма — модуль WB-сессии scripts/report_finrez_wb.py (договор в его docstring): продажи, комиссия, логистика, хранение, реклама и прочее с НДС; "
                       "K … Z — тождества второго кабинета (R = (логистика + хранение) / НДС). Приёмка — его --check (мост к «WB - месяц»).",
                       "Блок WB листа «Заказы»: созданные из воронки wb_funnel_products_daily; формулы как у Ozon — выручка = Заказы без НДС × выкуп × 0,58, СС модуля × выкуп, "
                       "маржа, гр. фин. рез = маржа − реклама (списания дня wb_ad_spend_daily без НДС); выкуп — итог зрелых месяцев модуля (лист «Коэффициенты»); соинвест WB не измеряется."]
    order_data_rows, dstats = (None, {}) if args.no_orders_data else build_order_data_rows(order_data, wb_parts, coef, days)
    if order_data_rows is not None:
        total_before = len(order_data) + (len(wb_parts["orders"]) if not wb_parts.get("error") else 0)
        print(f"«Данные заказы»: строк {len(order_data_rows)} (Ozon {sum(1 for r in order_data_rows if r['mp'] == 'Ozon')}, WB {sum(1 for r in order_data_rows if r['mp'] == 'WB')}); "
              f"было до выброса нулевых {total_before}, выброшено Ozon {dstats.get('dropped_zero_Ozon', 0)}, WB {dstats.get('dropped_zero_WB', 0)}"
              + (f" (у выброшенных WB подтверждено по воронке {dstats['dropped_confirmed_WB']:,.2f} — в сводной не участвует)" if dstats.get("dropped_confirmed_WB") else "")
              + (f"; строк WB без СС {dstats['wb_rows_without_cost']}" if dstats.get("wb_rows_without_cost") else ""))
    phase("«Данные заказы»")
    out = args.out or os.path.join(OUT_DIR, f"finrez_{args.month_from}_{args.month_to}.xlsx")
    timings = {}
    counts = write_book(out, days, long_rows, pivots, order_data, orders_pivot, coef, notes, catalog_source, today, wb=wb_parts, order_data_rows=order_data_rows, timings=timings)
    phase("запись книги")
    if not args.no_pivots:
        import finrez_pivots
        missing = [s["owner"] for s in finrez_pivots.SPECS if not os.path.exists(os.path.join(ROOT, s["owner"]))]
        if missing:
            print(f"сводные владельца не пересажены: нет файлов {missing}")
        else:
            counts_by_sheet = {"Данные Ozon выкупы": counts["long_rows"] + 1, "Данные WB выкупы": (len(wb_parts["rows"]) + 1) if not wb_parts.get("error") else None,
                               "Данные заказы": (counts["order_rows"] or 0) + 1}
            specs = [s for s in finrez_pivots.SPECS if counts_by_sheet.get(s["source_sheet"])]
            rep_ = finrez_pivots.transplant(out, out, specs=specs, row_counts={k: v for k, v in counts_by_sheet.items() if v})
            for r_ in rep_:
                print(f"сводная «{r_['sheet']}» ← «{r_['source']}» {r_['ref']}: cacheId {r_['cache_id']}, sheetId {r_['sheet_id']}, полей {r_['fields']}, срезов {r_['slicers']}")
            errs, info_ = finrez_pivots.check(out)
            print("проверка сводных: " + ("сошлось" if not errs else "ошибок " + str(len(errs))) + "; " + "; ".join(info_[-1:]))
            for e in errs:
                print("  ✗ " + e)
            phase("сводные владельца (пересадка + проверка)")
    size = os.path.getsize(out)
    sha = hashlib.sha256(open(out, "rb").read()).hexdigest()
    tot = pivots["all"][0]["Ozon"]["Общий итог"]
    print(f"книга → {out}: {size:,} байт, sha256 {sha}; строк «Данные Ozon выкупы» {counts['long_rows']} (с ID начисления {sum(1 for r in long_rows if r.accrual_id)}, "
          f"без ID {sum(1 for r in long_rows if not r.accrual_id)}: реклама Performance по SKU {sum(1 for r in long_rows if not r.accrual_id and r.sku and r.key == 'ads')}, "
          f"остатки дня {sum(1 for r in long_rows if not r.accrual_id and not r.sku)}, дни без сырья {sum(1 for r in long_rows if not r.accrual_id and r.sku and r.key != 'ads')}); "
          f"«Данные заказы» {counts['order_rows']}; итого оборот {tot['turnover']:,.2f}, комиссия {-tot['commission']:,.2f}, СС {tot['cogs']:,.2f}, фин. рез. {tot['fin_result']:,.2f}")
    print("время записи по листам: " + ", ".join(f"{k} {v} с" for k, v in timings.items()))
    print("коэффициенты: выкуп " + ", ".join(f"{k} {v * 100:.2f} %" for k, v in coef["buyout"].items() if v is not None)
          + "; комиссия " + ", ".join(f"{k} {v * 100:.2f} %" for k, v in coef["commission"].items() if v is not None))
    skipped = {k: v for k, v in bstats.items() if k.startswith("unknown_type") or k in ("raw_foreign_date", "days_without_daily_row")}
    print("остатки дня без SKU по статьям (дней / Σ): " + "; ".join(f"{OWNER_ARTICLE[k]} {bstats.get(f'residual_{k}_days', 0)} / {bstats.get(f'residual_{k}_sum', Z):,.2f}" for k in WIDE_KEYS)
          + f"; дней из сырья {bstats.get('days_from_raw', 0)}, из таблиц {bstats.get('days_from_tables', 0)}; ключей (день, SKU) с разложенными штуками {bstats.get('units_reallocated_keys', 0)}; "
            f"строк услуг рекламы пропущено {bstats.get('raw_ads_lines_skipped', 0)}, компенсаций {bstats.get('raw_compensation_lines_skipped', 0)}")
    if unknown or skipped:
        print(f"незнакомые статьи / типы: build_daily {unknown}; сырьё {skipped}")
    code = 0
    bad_identity = long_identity(long_rows, pivots["all"][0]["Ozon"], pivots["all"][1])
    print(f"тождество «Данные Ozon выкупы» → «Выкупы Ozon» (Σ Начисления по (метка, статья), Σ Ст-ть продаж = Себестоимость): "
          f"{'да' if not bad_identity else 'НЕТ ' + str(bad_identity[:6])}")
    code = 1 if bad_identity else code
    if order_data_rows is not None:
        lab = check_labels(order_data_rows)
        print("ярлыки «Данных заказы» по площадкам: бренды " + "; ".join(f"{m}: {sorted(v)}" for m, v in sorted(lab["brands"].items()))
              + " | категории " + "; ".join(f"{m}: {sorted(v)}" for m, v in sorted(lab["categories"].items()))
              + f" | множества Ozon = WB: {'да' if lab['equal'] else 'НЕТ'}; строк Ozon без бренда по артикулу: {lab['brandless_ozon'] or 0}")
        table = check_orders_data(order_data_rows, days, orders_pivot[0], None if wb_parts.get("error") else wb_parts["orders_pivot"])
        bad = [t for t in table if t[5]]
        print(f"приёмка «Свод» = Σ «Данных заказы» по 13 колонкам × {len({(t[0], t[1]) for t in table})} (площадка, метка): не сошлось {len(bad)} из {len(table)}")
        for mp, lab, title, a, b, diff in (bad if not args.check else table):
            if diff or args.check:
                print(f"   {mp:5} {lab:12} {title:16} свод {('' if a is None else f'{D(a):,.4f}'):>20} данные {('' if b is None else f'{D(b):,.4f}'):>20} разница {diff:,.4f}")
        code = 1 if bad else code
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
        print(f"Σ брендов = Σ категорий = общий по всем статьям: {'да' if ok_sum else 'НЕТ'}; строк данных {len(long_rows)}")
        print(f"колонок с разницей: {bad}")
        code = 1 if bad or not ok_sum else code
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
    if args.check and wbfin is not None and not wb_parts.get("error"):
        import subprocess
        cm = args.check_month or args.month_to
        cm_days = [d for d in days if d.startswith(cm)]
        periods = [(cm, cm, cm_days[min(args.check_days, len(cm_days)) - 1] if cm_days else None)]
        prev = f"{int(cm[:4]) - (1 if cm[5:7] == '01' else 0)}-{(int(cm[5:7]) - 2) % 12 + 1:02d}"
        periods.append((prev, prev, None))
        for mf, mt, dt in periods:
            cmd = [sys.executable, os.path.join(ROOT, "scripts", "report_finrez_wb.py"), "--month-from", mf, "--month-to", mt, "--check"] + (["--date-to", dt] if dt else [])
            print(f"\nприёмка WB-листов модулем WB-сессии: {' '.join(os.path.basename(c) if c.endswith('.py') else c for c in cmd[1:])}")
            res = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
            lines = [ln for ln in res.stdout.splitlines() if "Мост" in ln or "остаток" in ln.lower() or ln.startswith(("колонка", "Оборот", "Комиссия", "Себестоимость", "Эквайринг", "Выручка", "Логистика", "Реклама", "Прочее", "Фин. рез.")) or ln.startswith("Итого")]
            print("\n".join(lines) if lines else res.stdout[-1500:])
            if res.returncode:
                print(f"  WB-приёмка вернула код {res.returncode}: {res.stderr[-500:]}")
                code = 1
        phase("приёмка WB")
    print(f"всего {time.time() - t_start:.1f} с; db_writes = 0")
    return code


if __name__ == "__main__":
    sys.exit(main())
