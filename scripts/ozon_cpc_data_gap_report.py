#!/usr/bin/env python3
"""Детектор дыр в CPC-данных Ozon: список дат к бэкфиллу по ФАКТИЧЕСКИМ данным.

Зачем не по статусам. run_status оказался непригоден как сигнал завершённости:
из 24 проблемных дат 12 вообще не имеют статусной строки (включая 2026-08-22,
08-23, 08-24), две помечены success при почти нулевых данных, одна застряла в
running. Проверка rows_written = 0 из 661b658 тоже не годится как основа списка:
на 2026-05-12 записана 1 строка / 1 412,30 ₽, на 2026-07-13 — 70 строк /
14 216,52 ₽, нуля нет ни там, ни там. Поэтому детектор смотрит только на данные
и о существовании daily_load_status ничего не знает.

Скрипт ничего не пишет и никуда не ходит по сети: только SELECT.

Запуск:
    python3 scripts/ozon_cpc_data_gap_report.py
    python3 scripts/ozon_cpc_data_gap_report.py --json
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, ".")

from loaders.ozon_performance_ads_loader import supabase  # noqa: E402

MARKETPLACE = "ozon"

# ВРЕМЕННЫЙ ОБХОДНОЙ ПУТЬ — снять вместе с переклассификацией advertising_other.
#
# Расход CPC считается как advertising_clicks + advertising_other. Дефект A
# (исправлен в 6967f4e на источнике) при бэкфилле старых дат ронял классификацию
# в catch-all advertising_other: архивные кампании не возвращаются
# /api/client/campaign, текст метаданных пустой. Исторически так лежит
# 1194 строки на 123 413,92 ₽ за 90 дат, и они НЕ переклассифицированы задним
# числом. Если считать только advertising_clicks, даты с сильным бэкфиллом
# покажут ложный коллапс: на 2026-07-13 это 141,28 ₽ вместо 14 357,80 ₽.
#
# Тот же обходной путь стоит ещё в трёх местах и снимать их надо разом:
#   loaders/ozon_performance_ads_loader.py  (два счётчика на CPC-пути)
#   reports_ozon_ad_diagnostic_rule.py:459
CPC_EXPENSE_TYPES = ("advertising_clicks", "advertising_other")

# Окно медианы: ±3 дня, то есть до 6 соседей.
#
# Почему ±3, а не шире и не уже. Уже (±1..2) — слишком мало точек, одна плохая
# дата рядом уводит медиану. Шире (±7) — окно начинает захватывать смену состава
# кампаний и сезонный тренд, и «нормальный» уровень размывается. ±3 держит
# полную неделю вокруг даты: недельная сезонность рекламы слабая, а состав
# кампаний за неделю меняется мало.
MEDIAN_WINDOW_DAYS = 3

# Пороги выбраны по фактическому распределению долей от медианы, а не назначены.
# В хвосте два крупных естественных разрыва:
#   ... 11,5% -> 23,7%   разрыв 12,2 пункта
#   ... 30,7% -> 45,5%   разрыв 14,8 пункта  <- крупнейший, граница collapsed
#   ... 67,6% -> 72,5%   разрыв 4,9 пункта   <- граница suspicious/normal
# Выше 72,5% лежат 120 дат сплошным массивом — это норма.
COLLAPSED_RATIO = 0.35
SUSPICIOUS_RATIO = 0.70

# Второй, независимый сигнал: работал ли кабинет вообще в этот день.
# Нужен, чтобы отличить «упал только CPC» от «весь кабинет стоял».
QUIET_CABINET_RATIO = 0.35

LEVEL_ABSENT = "absent"
LEVEL_COLLAPSED = "collapsed"
LEVEL_SUSPICIOUS = "suspicious"
LEVEL_NORMAL = "normal"

CANDIDATE_LEVELS = (LEVEL_ABSENT, LEVEL_COLLAPSED, LEVEL_SUSPICIOUS)
# Из базы сравнения исключаем только явные провалы: suspicious может оказаться
# тихим днём, и выкидывать его из медианы значит подгонять базу под гипотезу.
EXCLUDED_FROM_MEDIAN = (LEVEL_ABSENT, LEVEL_COLLAPSED)


def fetch_all(table, columns, filters=None, page_size=1000):
    """Постраничная выборка: PostgREST режет ответ на 1000 строк."""
    rows = []
    start = 0
    while True:
        query = supabase.table(table).select(columns)
        for column, value in (filters or {}).items():
            query = query.eq(column, value)
        batch = query.range(start, start + page_size - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < page_size:
            return rows
        start += page_size


def load_daily_facts():
    """Факты по каждой дате. О daily_load_status здесь ничего не известно."""
    cpc_rows = defaultdict(int)
    cpc_spend = defaultdict(float)
    cabinet_spend = defaultdict(float)

    for row in fetch_all("marketplace_expenses",
                         "expense_date,expense_type,expense_amount",
                         {"marketplace_code": MARKETPLACE}):
        day = str(row.get("expense_date") or "")
        if not day:
            continue
        amount = float(row.get("expense_amount") or 0)
        expense_type = str(row.get("expense_type") or "")
        if expense_type in CPC_EXPENSE_TYPES:
            cpc_rows[day] += 1
            cpc_spend[day] += amount
        elif expense_type in ("commission", "logistics", "other"):
            # Не реклама: показывает, что кабинет в этот день вообще работал.
            cabinet_spend[day] += amount

    campaigns = defaultdict(set)
    for row in fetch_all("ozon_daily_sku_ad_attribution",
                         "sale_date,campaign_id,ad_source", {"ad_source": "cpc"}):
        day = str(row.get("sale_date") or "")
        campaign_id = str(row.get("campaign_id") or "")
        if day and campaign_id:
            campaigns[day].add(campaign_id)

    orders = {}
    for row in fetch_all("daily_marketplace_kpi", "kpi_date,orders_qty,orders_amount_seller",
                         {"marketplace_code": MARKETPLACE}):
        day = str(row.get("kpi_date") or "")
        if day:
            orders[day] = {
                "orders_qty": float(row.get("orders_qty") or 0),
                "orders_amount": float(row.get("orders_amount_seller") or 0),
            }

    if not cpc_spend:
        return []

    days = sorted(cpc_spend)
    first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    facts = []
    cursor = first
    while cursor <= last:
        key = cursor.isoformat()
        facts.append({
            "date": key,
            "cpc_rows": cpc_rows.get(key, 0),
            "cpc_spend": round(cpc_spend.get(key, 0.0), 2),
            "cpc_campaigns": len(campaigns.get(key, ())),
            "cabinet_spend": round(cabinet_spend.get(key, 0.0), 2),
            "orders_qty": (orders.get(key) or {}).get("orders_qty", 0.0),
            "orders_amount": round((orders.get(key) or {}).get("orders_amount", 0.0), 2),
        })
        cursor += timedelta(days=1)
    return facts


def median(values):
    values = sorted(values)
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


def neighbour_median(facts, index, field, excluded_dates):
    """Медиана соседей. Исключаем саму дату и уже отобранные провалы.

    Без исключения серия подряд идущих провалов утягивает медиану вниз и прячет
    сама себя: 2026-08-22, 08-23 и 08-24 идут подряд, и каждая для другой была бы
    «нормальным соседом» с нулём.
    """
    values = []
    for offset in range(-MEDIAN_WINDOW_DAYS, MEDIAN_WINDOW_DAYS + 1):
        if offset == 0:
            continue
        position = index + offset
        if 0 <= position < len(facts):
            neighbour = facts[position]
            if neighbour["date"] in excluded_dates:
                continue
            values.append(neighbour[field])
    return median(values)


def classify(facts):
    """Итеративно: считаем медиану, отбираем провалы, пересчитываем без них."""
    excluded = set()
    levels = {}

    for _ in range(10):
        new_levels = {}
        for index, fact in enumerate(facts):
            base = neighbour_median(facts, index, "cpc_spend", excluded)
            fact["neighbour_median"] = round(base, 2) if base else None
            if fact["cpc_rows"] == 0:
                new_levels[fact["date"]] = LEVEL_ABSENT
                fact["ratio"] = 0.0
                continue
            if not base:
                new_levels[fact["date"]] = LEVEL_NORMAL
                fact["ratio"] = None
                continue
            ratio = fact["cpc_spend"] / base
            fact["ratio"] = round(ratio, 4)
            if ratio < COLLAPSED_RATIO:
                new_levels[fact["date"]] = LEVEL_COLLAPSED
            elif ratio < SUSPICIOUS_RATIO:
                new_levels[fact["date"]] = LEVEL_SUSPICIOUS
            else:
                new_levels[fact["date"]] = LEVEL_NORMAL

        new_excluded = {d for d, level in new_levels.items() if level in EXCLUDED_FROM_MEDIAN}
        levels = new_levels
        if new_excluded == excluded:
            break
        excluded = new_excluded

    for fact in facts:
        fact["level"] = levels[fact["date"]]
    return facts


def add_cabinet_signal(facts):
    """Второй сигнал: упал только CPC или стоял весь кабинет.

    Если в этот день просели и заказы, и некламный расход, низкий CPC объясняется
    тишиной в кабинете, а не потерей данных.
    """
    for index, fact in enumerate(facts):
        cabinet_base = neighbour_median(facts, index, "cabinet_spend", set())
        orders_base = neighbour_median(facts, index, "orders_qty", set())
        fact["cabinet_ratio"] = (
            round(fact["cabinet_spend"] / cabinet_base, 3) if cabinet_base else None
        )
        fact["orders_ratio"] = (
            round(fact["orders_qty"] / orders_base, 3) if orders_base else None
        )
        quiet_signals = [
            ratio for ratio in (fact["cabinet_ratio"], fact["orders_ratio"]) if ratio is not None
        ]
        fact["quiet_cabinet"] = bool(
            quiet_signals and all(ratio < QUIET_CABINET_RATIO for ratio in quiet_signals)
        )
    return facts


def build_report():
    facts = load_daily_facts()
    if not facts:
        return {"candidates": [], "facts": []}
    classify(facts)
    add_cabinet_signal(facts)
    candidates = [f for f in facts if f["level"] in CANDIDATE_LEVELS]
    return {"facts": facts, "candidates": candidates}


def print_report(report):
    facts, candidates = report["facts"], report["candidates"]
    counts = defaultdict(int)
    for fact in facts:
        counts[fact["level"]] += 1

    print(f"Дат в периоде: {len(facts)}  ({facts[0]['date']} .. {facts[-1]['date']})")
    print("Уровни: " + ", ".join(f"{level}={counts[level]}" for level in
                                 (LEVEL_ABSENT, LEVEL_COLLAPSED, LEVEL_SUSPICIOUS, LEVEL_NORMAL)))
    print(f"\nКандидатов к бэкфиллу: {len(candidates)}\n")
    header = f"{'дата':<12}{'уровень':<12}{'строк':>7}{'расход':>13}{'медиана':>13}{'доля':>8}{'кампаний':>10}  примечание"
    print(header)
    print("-" * len(header))
    for fact in candidates:
        ratio = "—" if fact["ratio"] is None else f"{100 * fact['ratio']:.1f}%"
        note = "весь кабинет тих" if fact["quiet_cabinet"] else ""
        print(f"{fact['date']:<12}{fact['level']:<12}{fact['cpc_rows']:>7}"
              f"{fact['cpc_spend']:>13,.2f}{(fact['neighbour_median'] or 0):>13,.2f}"
              f"{ratio:>8}{fact['cpc_campaigns']:>10}  {note}".replace(",", " "))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="выдать машинночитаемый JSON")
    args = parser.parse_args()

    report = build_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
