#!/usr/bin/env python3
"""Лист «Выкупы WB» книги «Фин рез» (форма второго кабинета) и строки «Данные WB выкупы» — из отчёта реализации.

    venv/bin/python3 scripts/report_finrez_wb.py --month-from 2026-09 --month-to 2026-09 --date-to 2026-09-21 --xlsx data/reports/finrez_wb_2026-09.xlsx --check
    venv/bin/python3 scripts/report_finrez_wb.py --month-from 2026-04 --month-to 2026-09 --xlsx data/reports/finrez_wb_2026-04_09.xlsx

ДОГОВОР С OZON-СБОРКОЙ (scripts/report_finrez.py импортирует этот модуль; сигнатуры не менять без записи в отчёт):
    build_rows(month_from, month_to, date_to=None, sb=None)  -> список строк «Данные WB выкупы» (dict по DATA_COLS)
    build_month_sheet(rows)                                   -> строки листа «Выкупы WB»: месяцы + «Итого» (dict по SHEET_COLS)
    build_split(rows, by='brand'|'category')                  -> те же колонки в разрезе бренд × месяц / категория × месяц (+ итог группы)
    SHEET_COLS, DATA_COLS                                     -> (заголовок, ключ, формат) для записи листов
    load_funnel_products(date_from, date_to, sb=None)         -> строки wb_funnel_products_daily за окно (для общего листа «Заказы»)
    orders_rows_for_finrez(date_from, date_to, sb=None)       -> строки заказов WB по (день, nmId) для общего листа «Заказы»

ФОРМА (образец — книга второго кабинета `data/owner_finrez_wb_buyouts.xlsx`, лист «Свод», сводная без формул; тождества
проверены по значениям на четырёх месяцах, WB-8 §2): B Продажи · C Комиссия · D Себес-ть · E Реклама · F Хранение ·
G Логистика · H Ост. расходы и компенсации МП (все с НДС, ₽) и K … Z:
    K = B;  L = C;  M = L / K;  N = (K − L) / НДС;  O = D;  P = N − O;  Q = P / N;
    R = (G + F) / НДС  ← «Логистика без НДС» второго кабинета ВКЛЮЧАЕТ хранение;  S = R / N;
    T = E / НДС;  U = T / N;  V = эквайринг без НДС (у второго кабинета 0 — у нас заполнен);  W = V / N;
    X = H / НДС;  Y = P − R − T − V − X;  Z = Y / N.
Строки образца — месяцы (у свежих месяцев ещё и дни) и «Общий итог»; здесь — месяцы и «Итого».

«ДАННЫЕ WB ВЫКУПЫ» — строка на (дата продажи saleDt МСК, nmId), как на листе «Данные» книги владельца (задача WB-8 §2.1):
    Продажи = Σ retailPriceWithDisc (Продажа +, Возврат −); Комиссия = Σ цена × commissionPercent / 100 (знак тот же);
    Логистика = deliveryService + rebillLogisticCost; Себес-ть = СС снимка 1С (базовый артикул, как на «WB - месяц») × штуки;
    Хранение = paidStorage; Ост. расходы и компенсации = penalty + deduction − additionalPayment; Эквайринг = acquiringFee
    (возврат минус); Соинвест = Σ retailPriceWithDisc − Σ retailAmount; Штуки со знаком; Реклама — списания дня из
    wb_ad_spend_daily, разнесённые по артикулам пропорционально продажам дня (ads_allocated = True), пока нет таблицы
    по номенклатурам (WB-8 §3). Строки отчёта без nmId (возмещение ПВЗ, хранение, удержания) — строка «(без товара)» за
    день, чтобы Σ = «WB - месяц». Справочно: Возмещение ПВЗ (ppvzReward) — в форму не входит, но нужен мосту к «WB - месяц».

ПРИЁМКА (--check): за период сравнивается с итогом report_wb_month.build_daily по тем же строкам отчёта — оборот,
комиссия, эквайринг, СС — до копейки; логистика, прочее, выручка и фин. рез. — через мост с названными слагаемыми
(хранение внутри R у второго кабинета; rebillLogisticCost внутри G; штрафы у владельца без НДС, здесь H / НДС;
НДС за возмещение вычитается из выручки только у владельца), остаток моста обязан быть 0,00.
Только чтение; db_writes = 0.
"""
import argparse
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
import loaders.wb_sales_report_loader as report_loader  # noqa: E402
import report_wb_month as wbm  # noqa: E402  — те же строки отчёта, СС, реклама и НДС, что у «WB - месяц»

Z = Decimal(0)
C2 = Decimal("0.01")
PLATFORM = "WB"
SHOP = "KARATOV"                       # кабинет; имя магазина в книге владельца — уточнить (WB-8 §2 вопрос)
DISCOUNTER_LETTER = "t"
BRAND_BY_LETTER = {DISCOUNTER_LETTER: "КОЮЗ Топаз"}
BRAND_DEFAULT = "KARATOV"
NO_PRODUCT = "(без товара)"
MONTHS_SHORT = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
# Предмет WB → категория листа владельца (список из задачи WB-8); всё остальное — «прочее», считается вслух.
CATEGORY_BY_SUBJECT = {
    "Ювелирные кольца": "Кольца", "Ювелирные серьги": "Серьги", "Ювелирные подвески": "Подвески", "Ювелирные цепочки": "Цепочки",
    "Ювелирные браслеты": "Браслеты", "Ювелирный пирсинг": "Пирсинг", "Ювелирные колье": "Колье", "Ювелирные броши": "Броши",
}
CATEGORY_OTHER = "прочее"
CATEGORY_UNKNOWN = "прочее (нет предмета)"
SELECT = wbm.SELECT + ",additional_payment"   # те же поля, что читает «WB - месяц» (мост зовёт его build_daily), плюс доплаты
FUNNEL_SELECT = "day,nm_id,vendor_code,title,brand,subject_name,order_count,order_sum,buyout_count,buyout_sum,cancel_count,cancel_sum"

DATA_COLS = [
    ("Дата", "date", "date"), ("Месяц", "month", None), ("Площадка", "platform", None), ("Магазин", "shop", None),
    ("Артикул", "article", None), ("nmId", "nm_id", "int"), ("Наименование", "title", None), ("Бренд", "brand", None), ("Категория", "category", None),
    ("Продажи, ₽", "sales", "money"), ("Реклама, ₽", "ads", "money"), ("Комиссия, ₽", "commission", "money"), ("Логистика, ₽", "logistics", "money"),
    ("Себес-ть, ₽", "cogs", "money"), ("Хранение, ₽", "storage", "money"), ("Ост. расходы и компенсации МП, ₽", "other", "money"),
    ("Эквайринг, ₽", "acquiring", "money"), ("Соинвест, ₽", "coinvest", "money"), ("Штуки", "qty", "int"),
    ("справочно: Возмещение ПВЗ, ₽ (в форму не входит)", "ppvz_reward", "money"), ("справочно: rebillLogisticCost внутри логистики, ₽", "rebill", "money"),
    ("реклама разнесена", "ads_allocated", None), ("без СС, шт", "no_cost_qty", "int"),
]
SHEET_COLS = [
    ("Названия строк", "label", None),
    ("Продажи, ₽", "sales", "money"), ("Комиссия, ₽", "commission", "money"), ("Себес-ть, ₽", "cogs", "money"), ("Реклама, ₽", "ads", "money"),
    ("Хранение, ₽", "storage", "money"), ("Логистика, ₽", "logistics", "money"), ("Ост.расходы и компенсации МП, ₽", "other", "money"),
    (None, None, None), (None, None, None),
    ("Оборот (с НДС)", "k_turnover", "money"), ("Комиссия (с НДС), руб.", "l_commission", "money"), ("Комиссия, %", "m_commission_pct", "pct"),
    ("Выручка, руб. (без НДС)", "n_revenue", "money"), ("Себестоимость, руб.", "o_cogs", "money"), ("Маржа, руб.", "p_margin", "money"), ("Мар-ть, %", "q_margin_pct", "pct"),
    ("Логистика, руб. (без НДС)", "r_logistics", "money"), ("% Логистики", "s_logistics_pct", "pct"), ("Реклама, руб. (без НДС)", "t_ads", "money"), ("% ДРР", "u_drr_pct", "pct"),
    ("Эквайринг, руб.", "v_acquiring", "money"), ("% Эквайринга", "w_acquiring_pct", "pct"), ("Прочее, руб. (без НДС)", "x_other", "money"),
    ("Фин. рез., руб.", "y_fin", "money"), ("% Фин. рез.", "z_fin_pct", "pct"),
]
SPLIT_COLS = [("Группа", "group", None)] + SHEET_COLS
MONEY_KEYS = ("sales", "ads", "commission", "logistics", "cogs", "storage", "other", "acquiring", "coinvest", "ppvz_reward", "rebill")


def D(v):
    return Decimal(str(v)) if v not in (None, "") else Z


def q(v):
    return Decimal(v).quantize(C2)


def ratio(a, b):
    return (Decimal(a) / Decimal(b)).quantize(Decimal("0.0001")) if b else None


def month_label(day):
    return MONTHS_SHORT[int(str(day)[5:7]) - 1]


def month_bounds(month_from, month_to, date_to=None):
    d1 = f"{month_from}-01"
    y, m = int(month_to[:4]), int(month_to[5:7])
    last = (date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)).isoformat()
    if date_to:
        last = min(last, date_to)
    return d1, last


def brand_of(vendor_code, funnel_brand=None):
    if funnel_brand:
        return funnel_brand
    return BRAND_BY_LETTER.get(str(vendor_code or "").strip()[:1].lower(), BRAND_DEFAULT)


def category_of(subject_name):
    if not subject_name:
        return CATEGORY_UNKNOWN
    return CATEGORY_BY_SUBJECT.get(subject_name, CATEGORY_OTHER)


# ---------- источники ----------

def _sb(sb):
    return sb or report_loader._client()


def load_report_rows(sb, d1, d2):
    """Строки отчёта по rrDate d1 … d2 + запас под дату продажи (как в report_wb_month)."""
    return stale_keys.read_window_rows(sb, report_loader.TABLE, SELECT, [("gte", "rr_date", d1), ("lte", "rr_date", wbm.window_end(d2))], ["rr_date", "rrd_id"])


def load_product_dictionary(sb):
    """nmId → {title, brand, subject} из воронки по товарам (последний день карточки); откат — product_name выкупов (там предмет)."""
    out = {}
    try:
        rows = stale_keys.read_window_rows(sb, wbm.FUNNEL_TABLE, "day,nm_id,title,brand,subject_name", [], ["nm_id", "day"])
    except Exception as error:
        print(f"словарь товаров: не прочитать {wbm.FUNNEL_TABLE} — {str(error)[:120]}; категория и название — по откату", flush=True)
        rows = []
    for r in rows:   # отсортировано по nm_id, day — последняя запись побеждает
        out[int(r["nm_id"])] = {"title": r.get("title"), "brand": r.get("brand"), "subject": r.get("subject_name")}
    try:
        buy = stale_keys.read_window_rows(sb, "marketplace_buyouts", "marketplace_sku,product_name",
                                          [("eq", "marketplace_code", "wb"), ("neq", "product_name", None)], ["marketplace_sku"])
    except Exception as error:
        print(f"словарь товаров: не прочитать marketplace_buyouts — {str(error)[:120]}", flush=True)
        buy = []
    for r in buy:
        try:
            nm = int(r["marketplace_sku"])
        except (TypeError, ValueError):
            continue
        if r.get("product_name") and not out.get(nm, {}).get("subject"):
            out.setdefault(nm, {"title": None, "brand": None, "subject": None})["subject"] = r["product_name"]
    return out


# ---------- «Данные WB выкупы» ----------

def build_rows(month_from, month_to, date_to=None, sb=None, rows=None, costs=None, ads_by_day=None, products=None):
    """Строки «Данные WB выкупы» за месяцы month_from … month_to (до date_to включительно, если задано).

    rows / costs / ads_by_day / products — для тестов и повторных сборок; по умолчанию читаются из базы."""
    sb = _sb(sb) if rows is None or costs is None or ads_by_day is None or products is None else sb
    d1, d2 = month_bounds(month_from, month_to, date_to)
    rows = load_report_rows(sb, d1, d2) if rows is None else rows
    exact, uniform = wbm.load_costs(sb) if costs is None else costs
    if ads_by_day is None:
        try:
            ads_by_day, _undated = wbm.load_ads_db(sb, d1, d2)
        except RuntimeError as error:
            print(f"реклама не прочитана: {error}; колонка «Реклама» пуста", flush=True)
            ads_by_day = None
    products = load_product_dictionary(sb) if products is None else products
    acc = {}
    for r in rows:
        day = wbm.row_day(r)
        if not (d1 <= day <= d2):
            continue
        nm = r.get("nm_id")
        nm = int(nm) if nm not in (None, "", 0, "0") else None
        key = (day, nm)
        a = acc.get(key)
        if a is None:
            a = acc[key] = {k: Z for k in MONEY_KEYS}
            a.update({"qty": 0, "no_cost_qty": 0, "vendor": str(r.get("vendor_code") or "")})
        op = r["seller_oper_name"]
        sign = 1 if op == "Продажа" else (-1 if op == "Возврат" else 0)
        if sign:
            price, amount, qty = D(r["retail_price_with_disc"]), D(r["retail_amount"]), int(r.get("quantity") or 1)
            a["sales"] += sign * price
            a["commission"] += sign * price * D(r.get("commission_percent")) / 100
            a["coinvest"] += sign * (price - amount)
            a["acquiring"] += sign * D(r.get("acquiring_fee"))
            a["qty"] += sign * qty
            cost, _source = wbm.unit_cost_for(exact, uniform, r.get("vendor_code"), r.get("tech_size"), "base")
            if cost is None:
                a["no_cost_qty"] += qty
            else:
                a["cogs"] += sign * cost * qty
        a["logistics"] += D(r.get("delivery_service")) + D(r.get("rebill_logistic_cost"))
        a["rebill"] += D(r.get("rebill_logistic_cost"))
        a["storage"] += D(r.get("paid_storage"))
        a["other"] += D(r.get("penalty")) + D(r.get("deduction")) - D(r.get("additional_payment"))
        a["ppvz_reward"] += D(r.get("ppvz_reward"))
    # реклама дня — по артикулам пропорционально положительным продажам; без продаж — на строку «(без товара)»
    by_day = defaultdict(list)
    for key in acc:
        by_day[key[0]].append(key)
    if ads_by_day is not None:                       # день со списаниями без единой строки отчёта — тоже день: реклама не теряется
        for day in ads_by_day:
            if d1 <= day <= d2 and day not in by_day and ads_by_day[day]:
                by_day[day] = []
    for day, keys in sorted(by_day.items()):
        ads = (ads_by_day or {}).get(day, Z) if ads_by_day is not None else None
        if ads is None:
            continue
        positive = {k: acc[k]["sales"] for k in keys if k[1] is not None and acc[k]["sales"] > 0}
        total = sum(positive.values(), Z)
        if ads and total:
            spent = Z
            ordered = sorted(positive)
            for k in ordered[:-1]:
                share = q(ads * positive[k] / total)
                acc[k]["ads"] += share; spent += share
            acc[ordered[-1]]["ads"] += ads - spent            # остаток копеек — последнему, Σ по дню = списания дня
        elif ads:
            a = acc.get((day, None))
            if a is None:
                a = acc[(day, None)] = {k: Z for k in MONEY_KEYS}
                a.update({"qty": 0, "no_cost_qty": 0, "vendor": ""})
            a["ads"] += ads
    out = []
    for (day, nm), a in sorted(acc.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        p = products.get(nm, {}) if nm is not None else {}
        row = {"date": day, "month": month_label(day), "platform": PLATFORM, "shop": SHOP,
               "article": (a["vendor"].upper() or None) if nm is not None else NO_PRODUCT, "nm_id": nm,
               "title": p.get("title") if nm is not None else NO_PRODUCT,
               "brand": brand_of(a["vendor"], p.get("brand")) if nm is not None else NO_PRODUCT,
               "category": category_of(p.get("subject")) if nm is not None else NO_PRODUCT,
               "ads_allocated": bool(ads_by_day is not None and a["ads"]), "qty": a["qty"], "no_cost_qty": a["no_cost_qty"]}
        for k in MONEY_KEYS:
            row[k] = a[k] if (k != "ads" or ads_by_day is not None) else None
        out.append(row)
    return out


# ---------- «Выкупы WB» ----------

def _form(label, s, vat):
    """Колонки K … Z второго кабинета из сумм с НДС (тождества образца)."""
    k, l, o = s["sales"], s["commission"], s["cogs"]
    n = (k - l) / vat
    p = n - o
    r = (s["logistics"] + s["storage"]) / vat
    t = (s["ads"] / vat) if s["ads"] is not None else None
    v = s["acquiring"] / vat
    x = s["other"] / vat
    y = p - r - (t or Z) - v - x
    return {"label": label, "sales": k, "commission": l, "cogs": o, "ads": s["ads"], "storage": s["storage"], "logistics": s["logistics"], "other": s["other"],
            "k_turnover": k, "l_commission": l, "m_commission_pct": ratio(l, k), "n_revenue": n, "o_cogs": o, "p_margin": p, "q_margin_pct": ratio(p, n),
            "r_logistics": r, "s_logistics_pct": ratio(r, n), "t_ads": t, "u_drr_pct": ratio(t, n) if t is not None else None,
            "v_acquiring": v, "w_acquiring_pct": ratio(v, n), "x_other": x, "y_fin": y, "z_fin_pct": ratio(y, n),
            "acquiring": s["acquiring"], "vat": vat, "rows": s["rows"], "qty": s["qty"], "coinvest": s["coinvest"]}


def _sum_rows(rows):
    s = {k: Z for k in ("sales", "commission", "cogs", "storage", "logistics", "other", "acquiring", "coinvest")}
    s["ads"] = Z if any(r.get("ads") is not None for r in rows) else None
    s["rows"], s["qty"] = 0, 0
    for r in rows:
        for k in ("sales", "commission", "cogs", "storage", "logistics", "other", "acquiring", "coinvest"):
            s[k] += D(r.get(k))
        if s["ads"] is not None and r.get("ads") is not None:
            s["ads"] += D(r["ads"])
        s["rows"] += 1; s["qty"] += int(r.get("qty") or 0)
    return s


def _vat_for_rows(rows):
    return wbm.vat_for(min(r["date"] for r in rows)) if rows else wbm.vat_for("2026-01-01")


def build_month_sheet(rows):
    """Строки листа «Выкупы WB»: по месяцам (в порядке дат) и «Итого»."""
    by_month = defaultdict(list)
    for r in rows:
        by_month[str(r["date"])[:7]].append(r)
    out = [_form(month_label(f"{m}-01"), _sum_rows(rs), wbm.vat_for(f"{m}-01")) for m, rs in sorted(by_month.items())]
    out.append(_form("Итого", _sum_rows(rows), _vat_for_rows(rows)))
    return out


def build_split(rows, by="brand"):
    """Разрез группа × месяц (+ «Итого» группы), группа — brand или category; колонки те же, плюс «Группа»."""
    if by not in ("brand", "category"):
        raise ValueError("by: brand | category")
    groups = defaultdict(lambda: defaultdict(list))
    for r in rows:
        groups[r.get(by) or "—"][str(r["date"])[:7]].append(r)
    out = []
    for g in sorted(groups):
        all_rows = []
        for m, rs in sorted(groups[g].items()):
            row = _form(month_label(f"{m}-01"), _sum_rows(rs), wbm.vat_for(f"{m}-01")); row["group"] = g; out.append(row); all_rows.extend(rs)
        total = _form("Итого", _sum_rows(all_rows), _vat_for_rows(all_rows)); total["group"] = g; out.append(total)
    return out


# ---------- §4: воронка по товарам для общего листа «Заказы» ----------

def load_funnel_products(date_from, date_to, sb=None):
    """Строки wb_funnel_products_daily за окно (день заказа МСК), с сортировкой по ключу."""
    return stale_keys.read_window_rows(_sb(sb), wbm.FUNNEL_TABLE, FUNNEL_SELECT, [("gte", "day", date_from), ("lte", "day", date_to)], ["day", "nm_id"])


def orders_rows_for_finrez(date_from, date_to, sb=None, funnel_rows=None, costs=None):
    """Заказы WB по (день, nmId) для общего листа «Заказы»: созданные из воронки, выручка по правилу владельца
    (orderSum × 0,58 / НДС, WB-6 §2), СС снимка по базовому артикулу × штуки; buyout_* воронки — по дню заказа."""
    sb = _sb(sb) if funnel_rows is None or costs is None else sb
    funnel_rows = load_funnel_products(date_from, date_to, sb) if funnel_rows is None else funnel_rows
    exact, uniform = wbm.load_costs(sb) if costs is None else costs
    out = []
    for r in funnel_rows:
        qty = int(r.get("order_count") or 0)
        cost, _src = wbm.unit_cost_for(exact, uniform, r.get("vendor_code"), None, "base")
        day = str(r["day"])
        vat = wbm.vat_for(day)
        order_sum = D(r.get("order_sum"))
        out.append({"date": day, "month": month_label(day), "platform": PLATFORM, "shop": SHOP, "article": (str(r.get("vendor_code") or "").upper() or None),
                    "nm_id": int(r["nm_id"]), "title": r.get("title"), "brand": brand_of(r.get("vendor_code"), r.get("brand")),
                    "category": category_of(r.get("subject_name")), "orders_qty": qty, "orders_sum": order_sum,
                    "revenue": order_sum * wbm.OWNER_ORDERS_AFTER_COMMISSION / vat, "cogs": (cost * qty) if cost is not None else None,
                    "no_cost": cost is None and qty > 0, "funnel_buyouts_qty": int(r.get("buyout_count") or 0), "funnel_buyouts_sum": D(r.get("buyout_sum")),
                    "funnel_cancel_qty": int(r.get("cancel_count") or 0), "funnel_cancel_sum": D(r.get("cancel_sum"))})
    return out


# ---------- приёмка против «WB - месяц» ----------

def bridge_to_month_sheet(rows, report_rows, d1, d2, costs, ads_by_day):
    """Мост между итогами формы и итогом «WB - месяц» (report_wb_month.build_daily по тем же строкам отчёта) за d1 … d2."""
    days = []
    d = date.fromisoformat(d1)
    while d <= date.fromisoformat(d2):
        days.append(d.isoformat()); d += timedelta(days=1)
    exact, uniform = costs
    cost_fn = lambda code, size: wbm.unit_cost_for(exact, uniform, code, size, "base")  # noqa: E731
    daily = wbm.build_daily(report_rows, days, cost_fn, ads_by_day or {}, date.today() + timedelta(days=3), ads_by_day is not None)
    m = wbm.total_row(daily)
    form = build_month_sheet(rows)[-1]
    vat = form["vat"]
    sums = _sum_rows(rows)
    ppvz = sum((D(r.get("ppvz_reward")) for r in rows), Z)
    rebill = sum((D(r.get("rebill")) for r in rows), Z)
    penalty_vat_part = m["penalty"] - m["penalty"] / vat       # владелец: штрафы без НДС; форма: H / НДС
    add_pay = sums["other"] - (m["penalty"] + m["deduction"])  # other = penalty + deduction − additionalPayment → остаток = −additionalPayment
    lines = [
        ("Оборот", form["k_turnover"], m["turnover"], []),
        ("Комиссия (Σ цена × кВВ)", form["l_commission"], m["commission"], []),
        ("Себестоимость (снимок, базовый артикул)", form["o_cogs"], m["cogs"], []),
        ("Эквайринг без НДС", form["v_acquiring"], m["acquiring"], []),
        ("Выручка без НДС", form["n_revenue"], m["revenue"], [("НДС за возмещение вычитается только у владельца", m["vat_refund"])]),
        ("Логистика без НДС", form["r_logistics"], m["logistics"], [("хранение / НДС внутри R у второго кабинета", m["storage"] / vat), ("rebillLogisticCost / НДС внутри логистики (задача)", rebill / vat)]),
        ("Реклама без НДС", form["t_ads"] if form["t_ads"] is not None else Z, m["ads"] if m["ads"] is not None else Z, []),
        ("Прочее без НДС", form["x_other"], m["other"], [("хранение / НДС — у владельца в прочем, здесь в логистике", -(m["storage"] / vat)), ("штрафы: у владельца без НДС, здесь H / НДС", -penalty_vat_part), ("additionalPayment / НДС вычтен из H", add_pay / vat)]),
        ("Фин. рез.", form["y_fin"], m["fin_result"], [("НДС за возмещение", m["vat_refund"]), ("rebillLogisticCost / НДС", -(rebill / vat)), ("штрафы без НДС у владельца", penalty_vat_part), ("additionalPayment / НДС", -(add_pay / vat))]),
    ]
    out = []
    for title, ours, theirs, terms in lines:
        explained = sum((t for _n, t in terms), Z)
        out.append({"title": title, "form": q(ours), "month": q(theirs), "diff": q(ours - theirs), "terms": [(n, q(t)) for n, t in terms],
                    "rest": q(ours - theirs - explained)})
    return out, m, ppvz


def print_bridge(table, d1, d2):
    print(f"\nМост «Выкупы WB» (форма второго кабинета) ↔ «WB - месяц» за {d1} … {d2}")
    print(f"{'колонка':44}{'форма':>16}{'WB - месяц':>16}{'разница':>14}{'остаток':>12}")
    failures = 0
    for t in table:
        print(f"{t['title'][:43]:44}{t['form']:>16,.2f}{t['month']:>16,.2f}{t['diff']:>14,.2f}{t['rest']:>12,.2f}" + ("   ОСТАТОК ≠ 0" if t["rest"] != 0 else ""))
        for name, value in t["terms"]:
            print(f"      {value:>+14,.2f}  {name}")
        if t["rest"] != 0:
            failures += 1
    print(f"остаток моста ≠ 0 в строках: {failures}")
    return failures


# ---------- книга ----------

def write_xlsx(path, data_rows, month_rows, split_brand, split_category, notes):
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    fmt = {"money": "#,##0.00", "pct": "0.0%", "int": "#,##0", "date": "DD.MM.YYYY"}
    bold, head_fill = Font(bold=True), PatternFill("solid", fgColor="D9E1F2")
    wb = openpyxl.Workbook()

    def sheet(title, cols, rows, footer=(), title_line=None):
        ws = wb.create_sheet(title)
        if title_line:
            ws["A1"] = title_line; ws["A1"].font = bold
        for j, (head, _k, _f) in enumerate(cols, 1):
            if head:
                c = ws.cell(row=3, column=j, value=head); c.font = bold; c.fill = head_fill
                c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        for i, r in enumerate(rows, 4):
            for j, (head, k, f) in enumerate(cols, 1):
                if not head:
                    continue
                v = r.get(k)
                if f == "date" and isinstance(v, str):
                    v = date.fromisoformat(v)
                elif isinstance(v, Decimal):
                    v = float(v)
                elif isinstance(v, bool):
                    v = "да" if v else ""
                c = ws.cell(row=i, column=j, value=v)
                if f:
                    c.number_format = fmt[f]
                if r.get("label") == "Итого":
                    c.font = bold
        for n, line in enumerate(footer):
            ws.cell(row=4 + len(rows) + 2 + n, column=1, value=line)
        ws.freeze_panes = "B4"; ws.row_dimensions[3].height = 48
        for j in range(1, len(cols) + 1):
            ws.column_dimensions[get_column_letter(j)].width = 14

    wb.remove(wb.active)
    sheet("Выкупы WB", SHEET_COLS, month_rows, notes, "Данные с НДС — форма второго кабинета; K … Z по его тождествам (R включает хранение)")
    sheet("Выкупы WB по брендам", SPLIT_COLS, split_brand, (), "Бренд × месяц")
    sheet("Выкупы WB по категориям", SPLIT_COLS, split_category, (), "Категория × месяц")
    sheet("Данные WB выкупы", DATA_COLS, data_rows, (), "Строка на (дата продажи saleDt МСК, nmId); «(без товара)» — операции без nmId")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    wb.save(path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--month-from", required=True); ap.add_argument("--month-to", required=True)
    ap.add_argument("--date-to", help="последний день (внутри month-to); по умолчанию — конец месяца")
    ap.add_argument("--xlsx", help="куда писать книгу")
    ap.add_argument("--check", action="store_true", help="мост к «WB - месяц» за тот же период")
    args = ap.parse_args(argv)
    sb = report_loader._client()
    d1, d2 = month_bounds(args.month_from, args.month_to, args.date_to)
    report_rows = load_report_rows(sb, d1, d2)
    costs = wbm.load_costs(sb)
    try:
        ads_by_day, _u = wbm.load_ads_db(sb, d1, d2)
    except RuntimeError as error:
        print(f"реклама не прочитана: {error}; колонка «Реклама» пуста"); ads_by_day = None
    products = load_product_dictionary(sb)
    rows = build_rows(args.month_from, args.month_to, args.date_to, sb, rows=report_rows, costs=costs, ads_by_day=ads_by_day, products=products)
    month_rows = build_month_sheet(rows)
    with_product = [r for r in rows if r["nm_id"] is not None]
    cats = defaultdict(int)
    for r in with_product:
        cats[r["category"]] += 1
    print(f"строк отчёта {len(report_rows)}; «Данные» {len(rows)} строк: с товаром {len(with_product)} (ключей день × nmId), «{NO_PRODUCT}» {len(rows) - len(with_product)}; "
          f"словарь товаров {len(products)} nmId; категории: " + ", ".join(f"{k} {v}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1]))
          + f"; без СС {sum(r['no_cost_qty'] for r in rows)} шт; реклама {'разнесена по продажам дня' if ads_by_day is not None else 'не прочитана'}")
    for r in month_rows:
        ads_text = f"{r['t_ads']:,.2f}" if r["t_ads"] is not None else "—"
        print(f"{r['label']:6} продажи {r['sales']:,.2f}, комиссия {r['commission']:,.2f} ({r['m_commission_pct'] or 0:.2%}), выручка {r['n_revenue']:,.2f}, СС {r['o_cogs']:,.2f}, "
              f"маржа {r['p_margin']:,.2f}, логистика+хранение {r['r_logistics']:,.2f}, реклама {ads_text}, эквайринг {r['v_acquiring']:,.2f}, прочее {r['x_other']:,.2f}, фин. рез. {r['y_fin']:,.2f}")
    split_brand, split_category = build_split(rows, "brand"), build_split(rows, "category")
    tot = month_rows[-1]
    sb_tot = sum((r["sales"] for r in split_brand if r["label"] == "Итого"), Z); sc_tot = sum((r["sales"] for r in split_category if r["label"] == "Итого"), Z)
    print(f"Σ брендов {sb_tot:,.2f} = Σ категорий {sc_tot:,.2f} = общий {tot['sales']:,.2f}: {'да' if sb_tot == sc_tot == tot['sales'] else 'НЕТ'}")
    code = 0
    if args.check:
        table, _m, ppvz = bridge_to_month_sheet(rows, report_rows, d1, d2, costs, ads_by_day)
        failures = print_bridge(table, d1, d2)
        print(f"справочно: возмещение ПВЗ (ppvzReward) за период {ppvz:,.2f} с НДС — в форму не входит")
        code = 1 if failures else 0
    if args.xlsx:
        notes = [f"Источник: отчёт реализации WB (wb_sales_report_rows), день — saleDt МСК; период {d1} … {d2}; строк «Данные» {len(rows)}.",
                 "Форма — книга второго кабинета (лист «Свод»): N = (K − L)/НДС, R = (Логистика + Хранение)/НДС, T = E/НДС, X = H/НДС, Y = P − R − T − V − X, доли от N.",
                 "Отличие от образца: эквайринг V заполнен (у второго кабинета 0). Логистика ₽ = deliveryService + rebillLogisticCost; Ост. расходы = penalty + deduction − additionalPayment.",
                 "Реклама — списания дня (wb_ad_spend_daily, с НДС), разнесённые по артикулам пропорционально продажам дня, пока нет разреза по номенклатурам (WB-8 §3).",
                 f"Себестоимость — снимок 1С {wbm.SNAP} по базовому артикулу; категория — предмет карточки (воронка / выкупы) по списку владельца, прочее — вслух."]
        write_xlsx(args.xlsx, rows, month_rows, split_brand, split_category, notes)
        print(f"записано: {args.xlsx}")
    print("db_writes = 0")
    return code


if __name__ == "__main__":
    sys.exit(main())
