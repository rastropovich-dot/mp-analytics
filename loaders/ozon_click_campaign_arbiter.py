"""Клики Ozon по кампаниям: тип 41 начислений против атрибуции CPC, пара «день × кампания» (RBH 27.10, п. 2 (а), (б)).

У строки типа 41 в `accrual/by-day` (`NON_ITEM`, без SKU) в `unit_number` лежит номер кампании Performance. Поэтому начисление
кликов раскладывается по кампаниям бесплатно, без Performance, и сравнивается с `ozon_daily_sku_ad_attribution` (`ad_source = cpc`):
- кампания с начислением за день и без единой строки атрибуции за этот день — «без атрибуции»: её трата не попала ни в
  атрибуцию, ни в `marketplace_expenses` (найдено 27.10: по две такие кампании у РБХ-1 и РБХ-2, отбор `--mode full` их пропустил);
- пара, где обе суммы есть, но разные, — прежний класс «Performance выше начислений», только считается.

Знак: начисление Ozon отрицательное, здесь — трата (> 0). Чистые функции + одно чтение атрибуции по дням.
"""
from collections import defaultdict
from decimal import Decimal

CLICKS_TYPE_ID = 41
CENT = Decimal("0.01")


def campaign_spend_from_accruals(accruals, day=None):
    """Строки `accrual/by-day` → {(день, кампания): трата}. day — дата файла, если у строки своей нет."""
    out = defaultdict(Decimal)
    for x in accruals or []:
        fee = x.get("non_item_fee") or {}
        if fee.get("type_id") != CLICKS_TYPE_ID:
            continue
        campaign = str(x.get("unit_number") or "").strip()
        d = str(x.get("date") or day or "")[:10]
        if not campaign or not d:
            continue
        out[(d, campaign)] += -Decimal(str((fee.get("accrued") or {}).get("amount") or 0))
    return dict(out)


def attribution_campaign_spend(sb, days, marketplace_code="ozon"):
    """{(день, кампания): трата} по атрибуции CPC; пара есть, даже если строка нулевая (кампания отобрана, но не тратила)."""
    out = {}
    for d in days:
        res = (
            sb.table("ozon_daily_sku_ad_attribution")
            .select("campaign_id,ad_spend")
            .eq("marketplace_code", marketplace_code)
            .eq("ad_source", "cpc")
            .eq("sale_date", d)
            .limit(5000)
            .execute()
        )
        for r in res.data or []:
            key = (d, str(r.get("campaign_id") or ""))
            out[key] = out.get(key, Decimal(0)) + Decimal(str(r.get("ad_spend") or 0))
    return out


def compare(accrual_pairs, attribution_pairs):
    """Разбор пар: missing — начисление есть, атрибуции нет вовсе; extra — наоборот; mismatch — обе есть, разница > копейки."""
    missing, extra, mismatch, equal = {}, {}, {}, 0
    for key in set(accrual_pairs) | set(attribution_pairs):
        a = accrual_pairs.get(key, Decimal(0))
        if key not in attribution_pairs:
            if a > 0:
                missing[key] = a
            continue
        b = attribution_pairs[key]
        if abs(a - b) <= CENT:
            equal += 1
        elif key not in accrual_pairs or a == 0:
            extra[key] = b
        else:
            mismatch[key] = (a, b)
    return {"missing": missing, "extra": extra, "mismatch": mismatch, "equal": equal}


def money(v):
    return f"{v:,.2f}".replace(",", " ").replace(".", ",")


def summary_line(result, top=5):
    """Строка отчёта прогона / алерта: кампании без атрибуции — N пар, Σ ₽, номера (по убыванию суммы)."""
    missing = result["missing"]
    if not missing:
        return None
    by_campaign = defaultdict(Decimal)
    days = set()
    for (d, c), v in missing.items():
        by_campaign[c] += v
        days.add(d)
    total = sum(missing.values(), Decimal(0))
    names = "; ".join(f"{c} {money(v)}" for c, v in sorted(by_campaign.items(), key=lambda kv: -kv[1])[:top])
    more = len(by_campaign) - top
    return (
        f"клики: кампании без атрибуции — {len(by_campaign)} ({len(missing)} пар «день × кампания», {min(days)} … {max(days)}), "
        f"Σ {money(total)} ₽: {names}" + (f" и ещё {more}" if more > 0 else "")
    )
