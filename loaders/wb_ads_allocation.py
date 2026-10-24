"""Разнесение рекламы WB по артикулам — одно правило для книги «Фин рез» (scripts/report_finrez_wb.py), листа «WB - месяц»
(scripts/report_wb_month.py) и витрины daily_sku_kpi (reports_daily_sku_kpi.py; двадцатая WB, слово владельца 2026-10-23).

Истина по деньгам — списания adv/v1/upd (wb_ad_spend_daily, updSum с НДС), и только с оплатой «Баланс» (WB-14 §3: реклама за кэшбэк
WB — не деньги продавца). Статистика по номенклатурам adv/v3/fullstats (wb_ad_spend_nm_daily, sum с НДС) даёт только ДОЛИ:
Σ nms.sum по дню выше updSum на 0,6 … 1,8 % (измерено помесячно, WB-8 §3 и 20-я §1 б), а в марте и апреле считает траты за
кэшбэк наравне с балансом. Поэтому:
  1. ads_split_of(строки upd) — списания дня по виду оплаты и доля «Баланса» каждой кампании в день (advert_day);
  2. nm_weights(строки fullstats, advert_day, "balance") — вес номенклатуры в день = Σ по кампаниям sum × доля «Баланса» кампании;
     кампания без строки списаний в этот день считается «Балансом» целиком (так было до WB-14 и так в книге);
  3. nm_ads_by_day_and_nm(списания «Баланс» по дням, веса) — реклама (день, nmId) = списания дня × вес / Σ весов дня, копейки
     квантуются, остаток дня уходит последней по nmId номенклатуре: Σ по дню = списаниям дня до копейки.
День без статистики (есть списания, нет весов) остаётся без разнесения — решает читатель (книга: пропорционально продажам дня;
витрина: называет день и сумму вслух и не разносит). Функции перенесены из скриптов без изменения чисел; чистые, без ввода-вывода.
"""
from collections import defaultdict
from decimal import Decimal

try:
    from loaders import wb_money_rules as rules
except ImportError:  # запуск из каталога loaders/
    import wb_money_rules as rules

Z = Decimal(0)
C2 = Decimal("0.01")


def D(v):
    return Decimal(str(v)) if v not in (None, "") else Z


def q(v):
    return Decimal(v).quantize(C2)


def ads_split_of(rows):
    """Списания по видам оплаты из строк wb_ad_spend_daily: balance / other ({день: Σ updSum}), by_type {вид: Σ},
    advert_day {(день, advert_id): (Баланс, всего)} — доля «Баланса» кампании в день для разнесения по номенклатурам."""
    balance, other, by_type = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
    advert_day = defaultdict(lambda: [Z, Z])
    for r in rows:
        day, amount = str(r["upd_day"]), D(r["upd_sum"])
        pt = str(r.get("payment_type") or "").strip() or "(пусто)"
        by_type[pt] += amount
        key = (day, int(r["advert_id"]) if r.get("advert_id") not in (None, "") else None)
        advert_day[key][1] += amount
        if rules.is_balance_payment(pt):
            balance[day] += amount; advert_day[key][0] += amount
        else:
            other[day] += amount
    return {"balance": dict(balance), "other": dict(other), "by_type": dict(by_type), "advert_day": {k: tuple(v) for k, v in advert_day.items()}}


def nm_weights(nm_rows, advert_day=None, which="balance"):
    """{день: {nmId: вес}} — Σ sum fullstats по кампаниям × доля вида оплаты кампании в этот день (advert_day
    {(день, advert_id): (Баланс, всего)} из ads_split_of). which: balance | other | all. Кампания без строки
    списаний в день считается «Балансом» (как было до WB-14); advert_day нет — веса без долей (all)."""
    out = defaultdict(lambda: defaultdict(Decimal))
    for r in nm_rows or ():
        value = D(r.get("sum"))
        if value <= 0:
            continue
        day = str(r["day"])
        if which != "all" and advert_day is not None:
            adv = int(r["advert_id"]) if r.get("advert_id") not in (None, "") else None
            balance, total = advert_day.get((day, adv), (None, None))
            if total:
                frac = (balance / total) if which == "balance" else ((total - balance) / total)
            else:
                frac = Decimal(1) if which == "balance" else Z
            value = value * frac
        if value > 0:
            out[day][int(r["nm_id"])] += value
    return {d: dict(v) for d, v in out.items()}


def nm_ads_by_day_and_nm(ads_by_day, ads_nm_by_day):
    """{(день, nmId): реклама ₽} — списания дня (updSum) × доля номенклатуры из fullstats (Σ по дню = списаниям)."""
    out = {}
    for day, shares in (ads_nm_by_day or {}).items():
        total_day = (ads_by_day or {}).get(day, Z)
        weights = {nm: v for nm, v in shares.items() if v > 0}
        total = sum(weights.values(), Z)
        if not total_day or not total:
            continue
        spent = Z
        ordered = sorted(weights)
        for nm in ordered[:-1]:
            share = q(total_day * weights[nm] / total); out[(day, nm)] = share; spent += share
        out[(day, ordered[-1])] = total_day - spent
    return out


def days_without_weights(ads_by_day, ads_nm_by_day):
    """{день: списания «Баланс»} — дни со списаниями, для которых весов нет (статистика по номенклатурам не снята): их реклама
    не разнесена ни на один артикул. Читатель решает, что с этим делать, но молчать нельзя."""
    out = {}
    for day, amount in (ads_by_day or {}).items():
        if amount and not any(v > 0 for v in (ads_nm_by_day or {}).get(day, {}).values()):
            out[day] = amount
    return out
