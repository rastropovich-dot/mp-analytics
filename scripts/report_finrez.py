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
    """Метки строк формы: каждый месяц, под ним его дни (day_label), в конце «Общий итог» — дни у всех месяцев (сороковая §3; на листах они
    свёрнуты под месяцем плюсом); of(d) → [месяц, день, «Общий итог»]."""
    order, seen = [], set()
    for d in days:
        m = d[:7]
        if m not in seen:
            seen.add(m)
            order.append(month_label(d))
            order.extend(day_label(x) for x in days if x[:7] == m)
    order.append("Общий итог")

    def of(d):
        return [month_label(d), day_label(d), "Общий итог"]
    return order, of


def label_kind(lab):
    return "total" if lab == "Общий итог" else ("day" if "." in lab else "month")


def check_days_sum(piv, order, keys):
    """§3: Σ дней каждого месяца = строке месяца по каждому ключу. Возвращает расхождения [(месяц, ключ, Σ дней, месяц)] и число сравнений."""
    bad, n, month, days_of = [], 0, None, defaultdict(list)
    for lab in order:
        kind = label_kind(lab)
        if kind == "month":
            month = lab
        elif kind == "day":
            days_of[month].append(lab)
    for m, dl in days_of.items():
        for key in keys:
            vals = [(piv.get(d) or {}).get(key) for d in dl]
            if all(v is None for v in vals):
                continue
            s = sum((D(v) for v in vals if v is not None), Z)
            mv = (piv.get(m) or {}).get(key)
            n += 1
            if q(s - D(mv or 0)) != 0:
                bad.append((m, key, s, mv))
    return bad, n


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


def svod_from_data(data_rows, days, mp):
    """Лист «Заказы» («Свод») для площадки — на СУММАХ строк «Данных заказы» по формулам вычисляемых полей сводной владельца (сороковая §2):
    Выручка = Σ Выручка (по всем созданным, без коэффициента выкупа); Себестоимость = Σ Себ-ть Реал; Маржа = Выручка − Себестоимость; М-ть = Маржа /
    Выручка; Реклама = Σ Реклама без НДС; ДДР = Реклама / Σ Выкуплено (рубли); Соинвест = (Σ без НДС − Σ с СПП) / Σ без НДС по измеренным строкам;
    Комиссия % = Σ Комиссия руб / Σ ₽; Гр.фин.рез = Σ Выкуплено − Σ Себ-ть × (Σ Выкуплено / Σ Выручка) − Реклама; Гр.фин.рез % = Гр.фин.рез / Σ Выкуплено;
    Цена = Σ ₽ / Σ шт. Коэффициент выкупа входит только через рубли «Выкуплено»."""
    order, of = labels_for(days)
    acc = {lab: defaultdict(Decimal) for lab in order}
    for r in data_rows:
        if r["mp"] != mp:
            continue
        for lab in of(r["date"]):
            a = acc[lab]
            a["created_a"] += r["created_a"]; a["created_q"] += r["created_q"]; a["net"] += r["created_net"]; a["ads_net"] += r["ads_net"]
            a["cogs"] += r.get("cogs_real") or Z
            if r.get("revenue") is not None:
                a["revenue"] += r["revenue"]; a["comm_rub"] += r["commission_rub"]; a["priced"] += 1
            if r.get("buyout_rub") is not None:
                a["buyout_rub"] += r["buyout_rub"]
            if r.get("with_spp") is not None:
                a["spp_base_a"] += r["created_a"]; a["spp_buyer_a"] += (r.get("buyer_a") if r.get("buyer_a") is not None else r["with_spp"] * r["vat"])
    out = {}
    for lab in order:
        a, priced = acc[lab], bool(acc[lab]["priced"])
        m = {"created_a": a["created_a"], "created_q": a["created_q"], "price": rep.ratio(a["created_a"], a["created_q"]),
             "commission_pct": rep.ratio(a["comm_rub"], a["created_a"]) if priced else None, "ads_net": a["ads_net"],
             "coinvest_pct": rep.ratio(a["spp_base_a"] - a["spp_buyer_a"], a["spp_base_a"]) if a["spp_base_a"] else None,   # (без НДС − с СПП) / без НДС = (₽ − покупатель) / ₽
             "coinvest_cover": rep.ratio(a["spp_base_a"], a["created_a"]), "buyout_rub": a["buyout_rub"] if priced else None,
             "buyout": rep.ratio(a["buyout_rub"], a["revenue"]) if priced and a["revenue"] else None}
        if not priced:
            m.update({k: None for k in ("revenue", "cogs", "margin", "margin_pct", "drr_pct", "gross_fin", "gross_fin_pct")})
        else:
            m["revenue"], m["cogs"] = a["revenue"], a["cogs"]
            m["margin"] = a["revenue"] - a["cogs"]
            m["margin_pct"] = rep.ratio(m["margin"], a["revenue"])
            m["drr_pct"] = rep.ratio(a["ads_net"], a["buyout_rub"]) if a["buyout_rub"] else None
            m["gross_fin"] = a["buyout_rub"] - a["cogs"] * (a["buyout_rub"] / a["revenue"] if a["revenue"] else Z) - a["ads_net"]
            m["gross_fin_pct"] = rep.ratio(m["gross_fin"], a["buyout_rub"]) if a["buyout_rub"] else None
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
        return {"rows": rows, "month": month, "split_brand": split_brand, "split_category": split_cat, "orders": orders,
                "ads_by_day": ads_by_day, "ads_note": ads_note, "buyout": buyout, "buyout_details": details, "buyout_note": buyout_note, "timings": timings}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _removed_pivot_orders_wb():
    return None


def wb_pivot(rows, days):
    """{метка: строка формы K … Z} для листа «Выкупы WB» из ДНЕВНЫХ строк модуля (build_rows): месяцы, дни последнего месяца, «Общий итог» —
    каждая метка — «Итого» модульного build_month_sheet по её строкам, поэтому строки месяцев и общий итог равны build_month_sheet модуля до копейки
    (тот же код, те же строки), а Σ дней последнего месяца = строке месяца по сложению. Модуль не меняется."""
    order, of = labels_for(days)
    dayset = set(days)
    by_label = defaultdict(list)
    for r in rows:
        d = str(r["date"])
        if d in dayset:
            for lab in of(d):
                by_label[lab].append(r)
    out = {}
    for lab in order:
        rs = by_label.get(lab)
        if rs:
            row = dict(wbfin.build_month_sheet(rs)[-1]); row["label"] = lab
        else:
            row = {"label": lab}
        out[lab] = row
    return out, order


def wb_pivots_by_group(rows, days, by):
    """{группа: ({метка: строка}, порядок)} — блоки «по брендам» / «по категориям»; группа — brand / category строки модуля («—» если пусто)."""
    groups = defaultdict(list)
    for r in rows:
        groups[r.get(by) or "—"].append(r)
    return {g: wb_pivot(rs, days) for g, rs in groups.items()}


def check_wb_pivot_against_module(piv, order, month_rows, days):
    """§2.3: строки месяцев и «Общий итог» формы против build_month_sheet модуля по всем колонкам SHEET_COLS; Σ дней последнего месяца = строке месяца.
    Возвращает список расхождений (пусто — сошлось) и число сравнённых ячеек."""
    keys = [k for _h, k, _f in wbfin.SHEET_COLS if k and k != "label"]
    module = {r["label"]: r for r in month_rows}
    bad, n = [], 0
    for lab in order:
        if "." in lab:
            continue
        m = module.get("Итого" if lab == "Общий итог" else lab)
        if m is None:
            if piv.get(lab, {}).get("sales") is None:
                continue                                   # у группы нет строк в этом месяце — модуль строки не даёт, у формы метка пустая
            bad.append((lab, "нет строки в build_month_sheet", None)); continue
        for k in keys:
            a, b = piv.get(lab, {}).get(k), m.get(k)
            n += 1
            if (a is None) != (b is None) or (a is not None and q(D(a) - D(b)) != 0):
                bad.append((lab, k, (a, b)))
    bad_days, n_days = check_days_sum(piv, order, ("sales", "commission", "cogs", "ads", "storage", "logistics", "other", "acquiring"))
    bad += [(f"Σ дней {m}", k, (s, mv)) for m, k, s, mv in bad_days]
    return bad, n + n_days


def write_wb_pivot_block(g, r0, title, piv, order):
    """Блок формы второго кабинета для WB, компактно: [заголовок группы] → шапка («Названия строк» в A, семь денег формы, правее K … Z) →
    строки меток: месяц, его дни (outline 1, свёрнуты), «Общий итог». Возвращает следующую свободную строку."""
    money = [(h, k, f) for h, k, f in wbfin.SHEET_COLS[1:8]]
    calc = [(h, k, f) for h, k, f in wbfin.SHEET_COLS[8:] if k]
    r = r0
    if title:
        g.set(r, 1, title, bold=True); r += 1
    g.set(r, 1, "Названия строк", bold=True)
    for j, (h, _k, _f) in enumerate(money):
        g.set(r, 2 + j, h, bold=True)
    calc0 = 2 + len(money)
    for j, (h, _k, _f) in enumerate(calc):
        g.set(r, calc0 + j, h, bold=True, fill=True, wrap=True)
    g.heights[r] = 45
    r += 1
    for lab in order:
        kind, m = label_kind(lab), (piv.get(lab) or {})
        g.set(r, 1, lab, bold=(kind != "day"))
        if kind == "day":
            g.outline[r] = 1
        for j, (_h, k, f) in enumerate(money):
            g.set(r, 2 + j, m.get(k), f)
        for j, (_h, k, f) in enumerate(calc):
            g.set(r, calc0 + j, m.get(k), f)
        r += 1
    return r


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
                   ("Выручка, руб без НДС с учетом комиссии", "revenue", "money"), ("Комиссия, руб", "commission_rub", "money"), ("Выкуплено", "buyout_rub", "money"),
                   ("Неделя года", "week", "int"), ("День", "day_num", "int"), ("Маржа, руб без НДС", "margin", "money"),
                   ("Маржинальность, %", "margin_pct", "pct"), ("ДДР, %", "drr_pct", "pct"), ("Соинвест, %", "coinvest_pct", "pct"), ("Комиссия сред", "commission_avg", "pct"),
                   ("Фин.рез", "fin", "money"), ("Цена", "price", "money"), ("Фин.рез %", "fin_pct", "pct"),
                   ("Месяцы", "month_num", "int"),        # у владельца — поле ГРУППИРОВКИ сводной (fieldGroup по «Дата»), не колонка источника: справочно, после его 29
                   ("SKU/nmId", "sku", None), ("Подтверждено сейчас, руб.", "confirmed_a", "money"), ("Площадка Ozon", "platform_name", None)]   # 29 владельца + «Месяцы» + 3 наших
ORDER_CHECK_COLS = [("Оборот (с НДС)", "created_a"), ("Заказы, шт", "created_q"), ("Цена продавца", "price"), ("Комиссия %", "commission_pct"), ("Выручка", "revenue"),
                    ("Себестоимость", "cogs"), ("Маржа", "margin"), ("М-ть, %", "margin_pct"), ("Реклама", "ads_net"), ("ДДР, %", "drr_pct"),
                    ("Соинвест, %", "coinvest_pct"), ("Гр.фин.рез", "gross_fin"), ("Гр.фин.рез, %", "gross_fin_pct")]


def order_data_row(r, comm, comm_avg, buyout, mp, shop):
    """Строка «Данные заказы» в семантике источника сводной владельца (сороковая §2): Заказы без НДС = ₽ / НДС; Выручка = Заказы без НДС ×
    (1 − Комиссия) — по всем созданным, без коэффициента выкупа; Комиссия, руб = Заказы ₽ × Комиссия (на базе с НДС — так его «Комиссия сред» =
    Комиссия руб / Заказы ₽ показывает коэффициент); Выкуплено = Выручка × коэффициент выкупа площадки — РУБЛИ; Себ-ть Реал = СС × шт × индекс по
    всем заказам; Заказы, руб c СПП — цена покупателя без НДС (как и «без НДС» рядом, иначе его Соинвест % делит суммы с разным НДС). Наши восемь
    колонок 22 … 29 — ровно по формулам вычисляемых полей его сводной построчно: Маржа = Выручка − Себ-ть; М-ть = Маржа / Выручка; ДДР = Реклама
    без НДС / Выкуплено; Соинвест = (без НДС − с СПП) / без НДС; Комиссия сред = Комиссия руб / Заказы ₽; Фин.рез = Выкуплено − Себ-ть × (Выкуплено /
    Выручка) − Реклама без НДС; Цена = ₽ / шт; Фин.рез % = Фин.рез / Выкуплено. comm_avg — не используется (оставлен ради подписи), buyout — коэффициент."""
    del comm_avg
    vat, created, qty, ads = r["vat"], r["created_a"], r["created_q"], (r.get("ads") or Z)
    net, ads_net, buyer, cogs_real = created / vat, ads / vat, r.get("buyer_a"), r.get("cogs_created")
    spp = (1 - buyer / created) if (buyer is not None and created) else None
    with_spp = (buyer / vat) if buyer is not None else None
    cogs_v = cogs_real if cogs_real is not None else Z
    revenue = net * (1 - comm) if comm is not None else None
    commission_rub = created * comm if comm is not None else None
    buyout_rub = revenue * buyout if (revenue is not None and buyout is not None) else None
    margin = (revenue - cogs_v) if revenue is not None else None
    fin = (buyout_rub - cogs_v * (buyout_rub / revenue if revenue else Z) - ads_net) if buyout_rub is not None else None
    d = r["date"]
    return {"date": d, "mp": mp, "shop": shop, "article": r["article"], "created_a": created, "created_q": qty, "ads": ads, "cogs_real": cogs_real,
            "name": r["name"], "brand": r["brand"], "category": r["category"], "created_net": net, "ads_net": ads_net, "spp": spp, "with_spp": with_spp,
            "commission": comm, "revenue": revenue, "commission_rub": commission_rub, "buyout_rub": buyout_rub, "buyout_rate": buyout,
            "week": date.fromisoformat(d).isocalendar()[1], "day_num": int(d[8:10]), "month_num": int(d[5:7]), "margin": margin,
            "margin_pct": rep.ratio(margin, revenue) if revenue else None, "drr_pct": rep.ratio(ads_net, buyout_rub) if buyout_rub else None,
            "coinvest_pct": spp,          # = (без НДС − с СПП) / без НДС, считано на суммах с НДС — без пыли Decimal от деления на НДС
            "buyer_a": buyer,
            "commission_avg": rep.ratio(commission_rub, created) if (commission_rub is not None and created) else comm,
            "fin": fin, "price": rep.ratio(created, qty), "fin_pct": rep.ratio(fin, buyout_rub) if buyout_rub else None, "sku": r["sku"],
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
    """Приёмка §2.3 (а): формулы вычисляемых полей сводной владельца, буквально, на простых суммах полей источника по метке — независимо от
    svod_from_data. Маржа = Выручка − Себ-ть; М-ть = Маржа / Выручка; ДДР = Реклама без НДС / Выкуплено; Соинвест = (без НДС − с СПП) / без НДС;
    Комиссия сред = Комиссия руб / Заказы ₽; Фин.рез = Выкуплено − Себ-ть × (Выкуплено / Выручка) − Реклама без НДС; Цена = ₽ / шт; Фин.рез % = Фин.рез / Выкуплено."""
    order, of = labels_for(days)
    keys = ("created_a", "created_q", "ads_net", "cogs_real", "created_net", "revenue", "commission_rub", "buyout_rub")
    s = {lab: {k: Z for k in keys} | {"spp_net": Z, "spp_with": Z, "rows": 0} for lab in order}
    for r in data_rows:
        if r["mp"] != mp:
            continue
        for lab in of(r["date"]):
            a = s[lab]; a["rows"] += 1
            for k in keys:
                a[k] += r.get(k) or Z
            if r.get("with_spp") is not None:
                a["spp_net"] += r["created_net"]; a["spp_with"] += r["with_spp"]
    out = {}
    for lab in order:
        a = s[lab]
        if not a["rows"]:
            out[lab] = {}; continue
        margin = a["revenue"] - a["cogs_real"]
        fin = a["buyout_rub"] - a["cogs_real"] * (a["buyout_rub"] / a["revenue"] if a["revenue"] else Z) - a["ads_net"]
        out[lab] = {"created_a": a["created_a"], "created_q": a["created_q"], "price": rep.ratio(a["created_a"], a["created_q"]),
                    "commission_pct": rep.ratio(a["commission_rub"], a["created_a"]), "revenue": a["revenue"], "cogs": a["cogs_real"], "margin": margin,
                    "margin_pct": rep.ratio(margin, a["revenue"]), "ads_net": a["ads_net"], "drr_pct": rep.ratio(a["ads_net"], a["buyout_rub"]),
                    "coinvest_pct": rep.ratio(a["spp_net"] - a["spp_with"], a["spp_net"]) if a["spp_net"] else None,
                    "gross_fin": fin, "gross_fin_pct": rep.ratio(fin, a["buyout_rub"])}
    return out


def check_orders_data(data_rows, days, piv_ozon, piv_wb):
    """«Свод» (svod_from_data) против формул владельца на Σ «Данных заказы» (sum_order_data) по всем меткам и площадкам: [(площадка, метка, колонка,
    свод, формулы, разница)] по 13 колонкам. Деньги до копейки, проценты до 0,01 п. п."""
    out = []
    for mp, piv in (("Ozon", piv_ozon), ("WB", piv_wb)):
        if not piv:
            continue
        sums = sum_order_data(data_rows, days, mp)
        for lab in sums:
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
    """Разреженная сетка для листов формы: ячейки по (строка, колонка), лист пишется построчно (write_only); outline — уровень группировки строки
    (дни под месяцем, свёрнуты); ширины колонок — по содержимому (без строк примечаний)."""

    def __init__(self):
        self.cells, self.max_r, self.max_c, self.heights, self.outline = {}, 0, 0, {}, {}

    def set(self, r, c, v, fmt=None, bold=False, fill=False, wrap=False, note=False):
        if isinstance(v, Decimal):
            v = float(v)
        self.cells[(r, c)] = (v, fmt, bold, fill, wrap, note)
        self.max_r, self.max_c = max(self.max_r, r), max(self.max_c, c)

    def widths(self):
        """{колонка: ширина} по самому длинному значению: числа с разделителями, проценты, текст; шапка с переносом — не шире 18."""
        w = defaultdict(lambda: 9)
        for (r, c), (v, fmt, bold, fill, wrap, note) in self.cells.items():
            if v is None or v == "" or note:
                continue
            if fmt == "money":
                n = len(f"{float(v):,.2f}") + 1
            elif fmt == "int":
                n = len(f"{float(v):,.0f}") + 1
            elif fmt == "pct":
                n = 8
            elif fmt == "date":
                n = 11
            elif wrap:
                n = min(len(str(v)), 18)
            else:
                n = min(len(str(v)) + 1, 60)
            w[c] = max(w[c], n)
        return dict(w)

    def flush(self, ws):
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Alignment
        fmts, bold_font, fill_style = _styles()
        for r in range(1, self.max_r + 1):
            if r in self.heights:
                ws.row_dimensions[r].height = self.heights[r]
            if r in self.outline:
                ws.row_dimensions[r].outlineLevel = self.outline[r]
                ws.row_dimensions[r].hidden = True
            row = []
            for c in range(1, self.max_c + 1):
                cell = self.cells.get((r, c))
                if cell is None:
                    row.append(None)
                    continue
                v, fmt, bold, fill, wrap, _note = cell
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


def write_pivot_block(g, r0, title, pivot, order):
    """Блок формы владельца (Ozon), компактно (сороковая §3): [заголовок группы] → строка статей («Названия столбцов» в A) → строка подзаголовков
    «Начисления / Ст-ть продаж» и наших расчётных («Названия строк» в A) → строки меток: месяц, под ним его дни (outline 1, свёрнуты), в конце
    «Общий итог». Пустых строк и колонок внутри блока нет. Возвращает следующую свободную строку."""
    r = r0
    if title:
        g.set(r, 1, title, bold=True); r += 1
    g.set(r, 1, "Названия столбцов", bold=True); g.set(r + 1, 1, "Названия строк", bold=True)
    c = 2
    for art in ARTICLES:
        g.set(r, c, art, bold=True)
        g.set(r + 1, c, " Начисления"); g.set(r + 1, c + 1, " Ст-ть продаж в себ-ти")
        c += 2
    calc0 = c
    for j, (h, _k, _f) in enumerate(PIVOT_CALC + PIVOT_EXTRA):
        g.set(r + 1, calc0 + j, h, bold=True, fill=True, wrap=True)
    g.heights[r + 1] = 45
    r += 2
    for lab in order:
        kind, m = label_kind(lab), (pivot.get(lab) or {})
        g.set(r, 1, lab, bold=(kind != "day"))
        if kind == "day":
            g.outline[r] = 1
        c = 2
        for art in ARTICLES:
            g.set(r, c, m.get(PIVOT_ARTICLE_KEYS[art], Z), "money")
            g.set(r, c + 1, m.get("cogs", Z) if art == "Товарооборот" else Z, "money")
            c += 2
        vals = dict(m)
        vals["commission_abs"] = -m.get("commission", Z) if m else None
        for j, (_h, k, f) in enumerate(PIVOT_CALC + PIVOT_EXTRA):
            g.set(r, calc0 + j, vals.get(k), f)
        r += 1
    return r


def _grid_sheet(wb, title, g, widths=None, ncols=None, freeze=None):
    """Лист из сетки: ширины — по содержимому (widths — {колонка: ширина} поверх), группировка строк с плюсом у строки месяца (summaryBelow=False)."""
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.properties import Outline
    ws = wb.create_sheet(title)
    auto = g.widths()
    auto.update(widths or {})
    for c, w in auto.items():
        ws.column_dimensions[get_column_letter(c)].width = w
    if g.outline:
        ws.sheet_properties.outlinePr = Outline(summaryBelow=False, summaryRight=False)
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
    articles = sorted(a for a in ({r.article for r in long_rows if r.article} | {str(r.get("article") or "") for r in (order_data_rows or [])}
                                  | ({str(r.get("article") or "") for r in (wb_parts or {}).get("rows", [])} if wb_parts and not wb_parts.get("error") else set()))
                      if a and not a.startswith("("))          # служебные «(без SKU)», «(без nmId)», «(без товара)» — не артикулы
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
    L = {k: get_column_letter(i) for i, (_h, k, _f) in enumerate(ORDER_DATA_COLS, 1)}      # буквы колонок «Данных заказы» — по ключам
    head("Заказы (площадка — B2; месяц — по номеру в строке 3)")
    for label, key, fmt in (("Заказы, ₽", "created_a", "money"), ("Заказы, шт", "created_q", "int"), ("Реклама, ₽", "ads", "money"), ("Заказы, руб c СПП", "with_spp", "money"),
                            ("Выручка, руб без НДС с учетом комиссии", "revenue", "money"), ("Маржа, руб без НДС", "margin", "money"), ("Фин.рез", "fin", "money")):
        line(label, lambda c, col=L[key]: f"=SUMIFS({O_}!${col}:${col},{O_}!${L['article']}:${L['article']},$B$1,{O_}!${L['mp']}:${L['mp']},$B$2,{O_}!${L['month_num']}:${L['month_num']},{c}$3)", fmt)
    line("Соинвест, % (по строкам с измеренной ценой покупателя)",
         lambda c: f'=IFERROR(1-SUMIFS({O_}!${L["with_spp"]}:${L["with_spp"]},{O_}!${L["article"]}:${L["article"]},$B$1,{O_}!${L["mp"]}:${L["mp"]},$B$2,{O_}!${L["month_num"]}:${L["month_num"]},{c}$3)'
                   f'/SUMIFS({O_}!${L["created_a"]}:${L["created_a"]},{O_}!${L["article"]}:${L["article"]},$B$1,{O_}!${L["mp"]}:${L["mp"]},$B$2,{O_}!${L["month_num"]}:${L["month_num"]},{c}$3,{O_}!${L["with_spp"]}:${L["with_spp"]},"<>"),"")', "pct")
    ws.append([None]); row += 1
    ws.append(["Список артикулов — лист «Списки» (скрыт); при открытии Excel пересчитывает формулы по текущим листам «Данные»."])


def write_book(path, days, long_rows, pivots, order_rows_data, orders_pivot, coef, notes, catalog_source, today, wb=None, order_data_rows=None, timings=None):
    """Книга в форме владельца. Порядок листов: Выкупы Ozon · × бренд · × категория · Данные Ozon выкупы · Выкупы WB · по брендам · по категориям ·
    Данные WB выкупы · Заказы · Коэффициенты · Данные заказы · Списки · Артикул · Примечания (+ три «Сводная …» пост-обработкой). Листы формы компактны
    (сороковая §3): подпись — строка 1, шапка — со 2-й, данные под ней; дни каждого месяца под ним, свёрнуты (outline); «Общий итог» последним.
    order_data_rows None — лист «Данные заказы» не пишется (--no-orders-data)."""
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
    g.set(1, 1, "Выкупы Ozon — форма «Вывод данных» владельца на наших данных; дни каждого месяца — под ним, раскрываются «+» (фильтры — на листе «Сводная Ozon выкупы»)", bold=True, note=True)
    nxt = write_pivot_block(g, 2, None, pivots["all"][0]["Ozon"], pivots["all"][1])
    for n, line in enumerate(notes["buyouts"]):
        g.set(nxt + 1 + n, 1, line, note=True)
    _grid_sheet(wb, "Выкупы Ozon", g, freeze="B4")
    for title, key in (("Выкупы Ozon × бренд", "brand"), ("Выкупы Ozon × категория", "category")):
        g = Grid()
        g.set(1, 1, title + " — блоки по группе; дни каждого месяца — под ним, раскрываются «+»", bold=True, note=True)
        r_cols, piv, order = 2, pivots[key][0], pivots[key][1]
        for name in sorted(piv, key=lambda n: (n == "", n)):
            r_cols = write_pivot_block(g, r_cols, name or "(без признака)", piv[name], order) + 1
        _grid_sheet(wb, title, g, freeze="B2")
    lap("сводные выкупов")
    n_long = write_data_sheet(wb, "Данные Ozon выкупы", LONG_COLS, long_rows, widths={13: 48})
    lap("Данные Ozon выкупы")
    if wb_parts and not wb_parts.get("error"):
        # форма владельца, как у Ozon (тридцать девятая §2): месяцы, дни последнего месяца, «Общий итог»; блоки по бренду / категории
        piv, order = wb_pivot(wb_parts["rows"], days)
        g = Grid()
        g.set(1, 1, "Выкупы WB — форма «Свод» второго кабинета на наших данных (строки модуля WB-сессии по дням); дни каждого месяца — под ним, «+»; K … Z — тождества второго кабинета (R включает хранение)", bold=True, note=True)
        nxt = write_wb_pivot_block(g, 2, None, piv, order)
        for n, line in enumerate(notes.get("wb") or ()):
            g.set(nxt + 1 + n, 1, line, note=True)
        _grid_sheet(wb, "Выкупы WB", g, freeze="B3")
        for title, by in (("Выкупы WB по брендам", "brand"), ("Выкупы WB по категориям", "category")):
            g = Grid()
            g.set(1, 1, title + " — блоки по группе; дни каждого месяца — под ним, «+»", bold=True, note=True)
            r_cols = 2
            for name, (p_, o_) in sorted(wb_pivots_by_group(wb_parts["rows"], days, by).items(), key=lambda kv: (kv[0] == "—", kv[0])):
                r_cols = write_wb_pivot_block(g, r_cols, name, p_, o_) + 1
            _grid_sheet(wb, title, g, freeze="B2")
        write_data_sheet(wb, "Данные WB выкупы", wbfin.DATA_COLS, wb_parts["rows"], widths={7: 48})
    else:
        g = Grid()
        g.set(1, 1, "Лист WB не собран: " + str((wb_parts or {}).get("error") or "модуля WB нет"))
        _grid_sheet(wb, "Выкупы WB", g)
    lap("листы WB")
    # заказы — форма «Свод», компактно: подпись 1, площадки 2, шапка 3, данные с 4; дни под месяцем свёрнуты
    g = Grid()
    g.set(1, 1, "Заказы — форма «Свод» владельца на суммах «Данных заказы» по формулам его вычисляемых полей; блок Ozon и блок WB — одним кодом; дни каждого месяца — под ним, «+»", bold=True, note=True)
    g.set(2, 1, "Названия столбцов", bold=True)
    g.set(2, 2, "Ozon", bold=True); g.set(2, 2 + len(ORDER_COLS), "WB", bold=True)
    g.set(3, 1, "Названия строк", bold=True)
    for blk in (0, 1):
        for j, (h, _k, _f) in enumerate(ORDER_COLS):
            g.set(3, 2 + blk * len(ORDER_COLS) + j, h, bold=True, fill=True, wrap=True)
    piv, order = orders_pivot
    extra_col = 2 + 2 * len(ORDER_COLS)
    g.set(3, extra_col, "справочно: соинвест Ozon — доля созданного оборота с измеренной ценой покупателя", bold=True, fill=True, wrap=True)
    g.heights[3] = 45
    wm_all = (wb_parts or {}).get("orders_pivot") or {}
    r = 4
    for lab in order:
        kind = label_kind(lab)
        g.set(r, 1, lab, bold=(kind != "day"))
        if kind == "day":
            g.outline[r] = 1
        for j, (_h, k, f) in enumerate(ORDER_COLS):
            g.set(r, 2 + j, (piv.get(lab) or {}).get(k), f)
        g.set(r, extra_col, (piv.get(lab) or {}).get("coinvest_cover"), "pct")
        wm = wm_all.get(lab) or {}
        for j, (_h, k, f) in enumerate(ORDER_COLS):
            g.set(r, 2 + len(ORDER_COLS) + j, wm.get(k), f)
        r += 1
    for n, line in enumerate(notes["orders"]):
        g.set(r + 1 + n, 1, line, note=True)
    _grid_sheet(wb, "Заказы", g, freeze="B4")
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
    _grid_sheet(wb, "Коэффициенты", g, widths={1: 44})
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
    _grid_sheet(wb, "Примечания", g, widths={1: 40, 2: 140})
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
    phase("заказы Ozon: строки, коэффициенты")
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
        "orders": ["Семантика источника сводной владельца (решение 25.09): Выручка = Σ Заказы без НДС × (1 − Комиссия) по ВСЕМ созданным заказам, без коэффициента выкупа; Себестоимость = Σ Себ-ть Реал "
                   "(СС × шт × индекс, все заказы); «Выкуплено» — рубли = Выручка × коэффициент выкупа площадки (Ozon измеренный, WB — модуль); коэффициент входит только в Гр.фин.рез.",
                   "Формулы его вычисляемых полей на суммах по метке: Маржа = Выручка − Себестоимость; М-ть = Маржа / Выручка; ДДР = Реклама без НДС / Выкуплено; Соинвест = (без НДС − с СПП) / без НДС "
                   "по измеренным строкам; Комиссия % = Σ Комиссия руб / Σ ₽ (Комиссия руб = Заказы ₽ × коэффициент); Гр.фин.рез = Выкуплено − Себестоимость × (Выкуплено / Выручка) − Реклама; "
                   "Гр.фин.рез % = Гр.фин.рез / Выкуплено; Цена = ₽ / шт.",
                   "Оборот — созданные заказы (подтверждённые + отменённые) по цене продавца, с НДС; Заказы, шт — созданные штуки; Цена продавца = оборот / шт.",
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
        piv_wb, order_wb = wb_pivot(wb_parts["rows"], days)
        bad_wb, n_wb = check_wb_pivot_against_module(piv_wb, order_wb, wb_parts["month"], days)
        print(f"  форма «Выкупы WB»: строки месяцев и «Общий итог» против build_month_sheet модуля — ячеек {n_wb}, расхождений {len(bad_wb)}"
              + (f" {bad_wb[:5]}" if bad_wb else "") + "; Σ дней последнего месяца = строке месяца — в том же счёте")
        for by in ("brand", "category"):
            piv_g = wb_pivots_by_group(wb_parts["rows"], days, by)
            mod = defaultdict(list)
            for r in wbfin.build_split(wb_parts["rows"], by):
                mod[r["group"]].append(r)
            bad_g, n_g = 0, 0
            for gname, (p_, o_) in piv_g.items():
                b_, k_ = check_wb_pivot_against_module(p_, o_, mod.get(gname, []), days)
                bad_g += len(b_); n_g += k_
            print(f"  форма «Выкупы WB по {'брендам' if by == 'brand' else 'категориям'}»: групп {len(piv_g)}, против build_split модуля — ячеек {n_g}, расхождений {bad_g}")
        print("  коэффициент выкупа WB (модуль): " + (", ".join(f"{k} {v * 100:.2f} %" for k, v in wb_parts["buyout"].items()) if wb_parts.get("buyout") else "нет зрелых месяцев")
              + (f"; {wb_parts['buyout_note']}" if wb_parts.get("buyout_note") else ""))
        notes["wb"] = [f"Строки и форма — модуль WB-сессии scripts/report_finrez_wb.py (договор в его docstring): продажи, комиссия, логистика, хранение, реклама и прочее с НДС; "
                       "K … Z — тождества второго кабинета (R = (логистика + хранение) / НДС). Приёмка — его --check (мост к «WB - месяц»).",
                       "Блок WB листа «Заказы»: созданные из воронки wb_funnel_products_daily; формулы как у Ozon — выручка = Заказы без НДС × выкуп × 0,58, СС модуля × выкуп, "
                       "маржа, гр. фин. рез = маржа − реклама (списания дня wb_ad_spend_daily без НДС); выкуп — итог зрелых месяцев модуля (лист «Коэффициенты»); соинвест WB не измеряется."]
    order_data_rows, dstats = build_order_data_rows(order_data, wb_parts, coef, days)       # нужны и для «Свода»: он считается на их суммах
    orders_pivot = svod_from_data(order_data_rows, days, "Ozon")
    if not wb_parts.get("error"):
        wb_parts["orders_pivot"] = svod_from_data(order_data_rows, days, "WB")[0]
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
    code = 0
    print("время записи по листам: " + ", ".join(f"{k} {v} с" for k, v in timings.items()))
    print("коэффициенты: выкуп " + ", ".join(f"{k} {v * 100:.2f} %" for k, v in coef["buyout"].items() if v is not None)
          + "; комиссия " + ", ".join(f"{k} {v * 100:.2f} %" for k, v in coef["commission"].items() if v is not None))
    skipped = {k: v for k, v in bstats.items() if k.startswith("unknown_type") or k in ("raw_foreign_date", "days_without_daily_row")}
    print("остатки дня без SKU по статьям (дней / Σ): " + "; ".join(f"{OWNER_ARTICLE[k]} {bstats.get(f'residual_{k}_days', 0)} / {bstats.get(f'residual_{k}_sum', Z):,.2f}" for k in WIDE_KEYS)
          + f"; дней из сырья {bstats.get('days_from_raw', 0)}, из таблиц {bstats.get('days_from_tables', 0)}; ключей (день, SKU) с разложенными штуками {bstats.get('units_reallocated_keys', 0)}; "
            f"строк услуг рекламы пропущено {bstats.get('raw_ads_lines_skipped', 0)}, компенсаций {bstats.get('raw_compensation_lines_skipped', 0)}")
    if unknown or skipped:
        print(f"незнакомые статьи / типы: build_daily {unknown}; сырьё {skipped}")
    bad_identity = long_identity(long_rows, pivots["all"][0]["Ozon"], pivots["all"][1])
    print(f"тождество «Данные Ozon выкупы» → «Выкупы Ozon» (Σ Начисления по (метка, статья), Σ Ст-ть продаж = Себестоимость): "
          f"{'да' if not bad_identity else 'НЕТ ' + str(bad_identity[:6])}")
    code = 1 if bad_identity else code
    if order_data_rows is not None:
        # §2.3 (в): Σ Выкуплено / Σ Выручка по площадке = коэффициенту площадки
        for mp_ in ("Ozon", "WB"):
            rs = [r for r in order_data_rows if r["mp"] == mp_ and r.get("buyout_rub") is not None]
            if rs:
                rev = sum((r["revenue"] for r in rs), Z); bo = sum((r["buyout_rub"] for r in rs), Z)
                print(f"«Данные заказы» {mp_}: Σ Выкуплено {bo:,.2f} / Σ Выручка {rev:,.2f} = {(bo / rev) if rev else Z:.6f}; коэффициент площадки "
                      f"{(coef['buyout'].get('все') if mp_ == 'Ozon' else (wb_parts.get('buyout') or {}).get('итого', Decimal(1))) or Z:.6f}")
        # §3: Σ дней каждого месяца = строке месяца на всех листах формы
        days_checks = [("Выкупы Ozon", pivots["all"][0]["Ozon"], pivots["all"][1], BUYOUT_KEYS[:9])]
        for key in ("brand", "category"):
            for name, pv in pivots[key][0].items():
                days_checks.append((f"Выкупы Ozon × {key} / {name or '(без признака)'}", pv, pivots[key][1], BUYOUT_KEYS[:9]))
        if not wb_parts.get("error"):
            wb_keys = ("sales", "commission", "cogs", "ads", "storage", "logistics", "other", "acquiring")
            pw, ow = wb_pivot(wb_parts["rows"], days)
            days_checks.append(("Выкупы WB", pw, ow, wb_keys))
            for by in ("brand", "category"):
                for name, (p_, o_) in wb_pivots_by_group(wb_parts["rows"], days, by).items():
                    days_checks.append((f"Выкупы WB по {by} / {name}", p_, o_, wb_keys))
            days_checks.append(("Заказы / WB", wb_parts["orders_pivot"], orders_pivot[1], ("created_a", "created_q", "revenue", "cogs", "ads_net", "buyout_rub")))
        days_checks.append(("Заказы / Ozon", orders_pivot[0], orders_pivot[1], ("created_a", "created_q", "revenue", "cogs", "ads_net", "buyout_rub")))
        bad_d, n_d = [], 0
        for name, pv, od, keys in days_checks:
            b_, k_ = check_days_sum(pv, od, keys)
            bad_d += [(name,) + x for x in b_]; n_d += k_
        print(f"§3 Σ дней каждого месяца = строке месяца: листов / блоков {len(days_checks)}, сравнений {n_d}, расхождений {len(bad_d)}" + (f" {bad_d[:5]}" if bad_d else ""))
        code = 1 if bad_d else code
        lab = check_labels(order_data_rows)
        print("ярлыки «Данных заказы» по площадкам: бренды " + "; ".join(f"{m}: {sorted(v)}" for m, v in sorted(lab["brands"].items()))
              + " | категории " + "; ".join(f"{m}: {sorted(v)}" for m, v in sorted(lab["categories"].items()))
              + f" | множества Ozon = WB: {'да' if lab['equal'] else 'НЕТ'}; строк Ozon без бренда по артикулу: {lab['brandless_ozon'] or 0}")
        table = check_orders_data(order_data_rows, days, orders_pivot[0], None if wb_parts.get("error") else wb_parts["orders_pivot"])
        bad = [t for t in table if t[5]]
        print(f"приёмка §2.3 (а): «Свод» = формулы владельца на Σ «Данных заказы» по 13 колонкам × {len({(t[0], t[1]) for t in table})} (площадка, метка): не сошлось {len(bad)} из {len(table)}")
        for mp, lab, title, a, b, diff in (bad if not args.check else [t for t in table if label_kind(t[1]) != "day"]):
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
