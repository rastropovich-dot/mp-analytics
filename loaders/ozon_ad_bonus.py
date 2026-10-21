"""Реклама за счёт бонусов Ozon (пятьдесят третья §2): чтение строк атрибуции с is_bonus для утреннего листа, алерта и книги.

Бонусные кампании Ozon заводит сам (isAutocreated в списке кампаний Performance) и крутит на начисленных бонусах продвижения;
статистика Performance несёт их трату как обычную, а тип 41 начислений — нет (сверка с отчётом ЛК «Детализация расходов бонусами»
09 … 13.10: по дням и SKU до 0,04 ₽). Поэтому загрузчик в marketplace_expenses такие строки не пишет, а в ozon_daily_sku_ad_attribution
помечает is_bonus = true; читатели показывают их отдельной строкой «реклама за счёт бонусов Ozon (не расход)» и в расходы / ДРР не берут.
Чтение — страницами по ключу (sale_date, marketplace_sku, campaign_id), не range без сортировки (how-we-work).
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

TABLE = "ozon_daily_sku_ad_attribution"
PAGE = 1000


def read_bonus_rows(sb, date_from, date_to, marketplace_code="ozon", page=PAGE):
    """Строки атрибуции с is_bonus = true за окно дат: sale_date, marketplace_sku, campaign_id, ad_spend, ad_clicks, ad_orders_qty, ad_orders_revenue."""
    rows, last = [], None
    while True:
        q = (sb.table(TABLE).select("sale_date,marketplace_sku,campaign_id,ad_source,ad_spend,ad_clicks,ad_orders_qty,ad_orders_revenue")
             .eq("marketplace_code", marketplace_code).eq("is_bonus", True).gte("sale_date", str(date_from)).lte("sale_date", str(date_to))
             .order("sale_date").order("marketplace_sku").order("campaign_id").limit(page))
        if last is not None:
            d, s, c = last
            q = q.or_(f"sale_date.gt.{d},and(sale_date.eq.{d},marketplace_sku.gt.{s}),and(sale_date.eq.{d},marketplace_sku.eq.{s},campaign_id.gt.{c})")
        data = q.execute().data or []
        rows += data
        if len(data) < page:
            break
        last = (str(data[-1]["sale_date"])[:10], str(data[-1]["marketplace_sku"]), str(data[-1]["campaign_id"]))
    return rows


def spend_by_day(rows):
    """{день: Σ ad_spend} (Decimal, с НДС как в статистике Performance)."""
    out = defaultdict(Decimal)
    for r in rows:
        out[str(r["sale_date"])[:10]] += Decimal(str(r.get("ad_spend") or 0))
    return dict(out)


def spend_by_day_sku(rows):
    out = defaultdict(Decimal)
    for r in rows:
        out[(str(r["sale_date"])[:10], str(r["marketplace_sku"]))] += Decimal(str(r.get("ad_spend") or 0))
    return dict(out)


def campaigns_by_day(rows):
    out = defaultdict(lambda: defaultdict(Decimal))
    for r in rows:
        out[str(r["sale_date"])[:10]][str(r["campaign_id"])] += Decimal(str(r.get("ad_spend") or 0))
    return {d: dict(v) for d, v in out.items()}


def new_campaigns(rows, target_date, lookback_days=30):
    """Бонусные кампании, у которых первая трата в окне — именно target_date (не встречались за lookback_days до неё) — повод для
    предупреждения «новая автосозданная кампания» в алерте."""
    first = {}
    for r in rows:
        d, c = str(r["sale_date"])[:10], str(r["campaign_id"])
        if c not in first or d < first[c]:
            first[c] = d
    return sorted(c for c, d in first.items() if d == str(target_date))


def alert_lines(sb, target_date, fmt_money, lookback_days=30):
    """Строки для утреннего алерта: трата за день и новые бонусные кампании; отказ чтения — одна строка с причиной, не падение."""
    try:
        d2 = date.fromisoformat(str(target_date))
        rows = read_bonus_rows(sb, d2 - timedelta(days=lookback_days), d2)
    except Exception as exc:  # noqa: BLE001
        return [f"Реклама за счёт бонусов Ozon: не прочитано ({type(exc).__name__}: {str(exc)[:120]})."]
    day = spend_by_day(rows).get(str(target_date), Decimal(0))
    by_c = campaigns_by_day(rows).get(str(target_date), {})
    lines = [f"Реклама за счёт бонусов Ozon (не расход): {fmt_money(day)} руб." + (f" — кампаний {len(by_c)}" if by_c else "")]
    fresh = new_campaigns(rows, target_date, lookback_days)
    if fresh:
        lines.append("⚠️ Новая бонусная (автосозданная) кампания Ozon: " + ", ".join(f"{c} ({fmt_money(by_c.get(c, Decimal(0)))} руб.)" for c in fresh)
                     + " — первая трата за день; проверить в ЛК (Продвижение → Оплата за клик → Бонусы).")
    return lines
