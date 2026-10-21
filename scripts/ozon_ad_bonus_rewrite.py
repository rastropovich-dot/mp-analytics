#!/usr/bin/env python3
"""Пересчёт окна под бонусные кампании Ozon (пятьдесят третья §2): флаг is_bonus в атрибуции и расходы без бонусной траты.

    venv/bin/python3 scripts/ozon_ad_bonus_rewrite.py --from 2026-10-09                              план (db_writes 0)
    venv/bin/python3 scripts/ozon_ad_bonus_rewrite.py --from 2026-10-09 --apply                      запись — по слову владельца

Бонусные кампании берутся из сырого списка кампаний Performance (data/ozon_performance_campaigns/campaigns_<дата>.json, последний файл;
`isAutocreated == true`) — Performance API не зовётся; --campaign-id … задаёт их руками. Для окна [--from, --to] (--to по умолчанию сегодня):
  1. ozon_daily_sku_ad_attribution: строки ad_source = cpc этих кампаний → is_bonus = true (upsert только is_bonus по ключу таблицы);
  2. marketplace_expenses, expense_type = advertising_clicks: из строки (день, SKU) вычитается Σ ad_spend бонусных кампаний по тому же (день, SKU)
     из атрибуции; результат < 0,005 → строка удаляется (только бонусная трата), иначе expense_amount уменьшается. Атрибуция и расходы строятся
     из одних строк статистики, поэтому вычитание точное; расхождение (бонус больше строки расходов) — стоп с перечнем.
Снимок затронутых строк — data/snapshots/ad_bonus_rewrite_<UTC>.json до записи. daily_sku_kpi не трогается: ночной пересчёт KPI
переписывает окно 31 дня сам (ad_spend уменьшится на те же суммы по SKU); даты старше окна KPI — перечисляются отдельно.
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
import cabinet  # noqa: E402
from loaders.ozon_performance_ads_loader import is_bonus_campaign  # noqa: E402

_PROFILE = cabinet.profile()
ATTR = "ozon_daily_sku_ad_attribution"
EXP = "marketplace_expenses"
PAGE = 1000
EPS = Decimal("0.005")
KPI_WINDOW_DAYS = 31


def latest_campaign_list(prof=None):
    files = sorted(glob.glob(cabinet.data_path("ozon_performance_campaigns", "campaigns_*.json", prof=prof or _PROFILE)))
    return files[-1] if files else None


def bonus_campaign_ids(path):
    with open(path, encoding="utf-8") as fh:
        camps = json.load(fh)
    return sorted(str(c.get("id")) for c in camps if is_bonus_campaign(c)), len(camps)


def read_attribution(sb, campaign_ids, d1, d2, page=PAGE, with_flag=True):
    """with_flag=False — колонка is_bonus ещё не применена: читаем без неё (все строки «без флага»), план это называет."""
    rows, last = [], None
    cols = "sale_date,marketplace_code,marketplace_sku,ad_source,attribution_type,campaign_id,ad_spend" + (",is_bonus" if with_flag else "")
    for cid in campaign_ids:
        last = None
        while True:
            q = (sb.table(ATTR).select(cols)
                 .eq("marketplace_code", "ozon").eq("ad_source", "cpc").eq("campaign_id", cid).gte("sale_date", d1).lte("sale_date", d2)
                 .order("sale_date").order("marketplace_sku").limit(page))
            if last is not None:
                q = q.or_(f"sale_date.gt.{last[0]},and(sale_date.eq.{last[0]},marketplace_sku.gt.{last[1]})")
            data = q.execute().data or []
            rows += data
            if len(data) < page:
                break
            last = (str(data[-1]["sale_date"])[:10], str(data[-1]["marketplace_sku"]))
    return rows


def read_expenses(sb, d1, d2, page=PAGE):
    rows, last = [], None
    while True:
        q = (sb.table(EXP).select("id,expense_date,marketplace_sku,expense_type,expense_amount").eq("marketplace_code", "ozon")
             .eq("expense_type", "advertising_clicks").gte("expense_date", d1).lte("expense_date", d2).order("id").limit(page))
        if last is not None:
            q = q.gt("id", last)
        data = q.execute().data or []
        rows += data
        if len(data) < page:
            break
        last = data[-1]["id"]
    return rows


def plan(attr_rows, exp_rows):
    """→ (к флагу, обновить расходы [(id, было, станет)], удалить [(id, было)], ошибки, по дням)."""
    bonus = defaultdict(Decimal)
    to_flag = [r for r in attr_rows if not r.get("is_bonus")]
    for r in attr_rows:
        bonus[(str(r["sale_date"])[:10], str(r["marketplace_sku"]))] += Decimal(str(r.get("ad_spend") or 0))
    exp = {(str(r["expense_date"])[:10], str(r["marketplace_sku"])): r for r in exp_rows}
    update, delete, errors = [], [], []
    by_day = defaultdict(lambda: {"bonus": Decimal(0), "update": 0, "delete": 0, "flag": 0})
    for key, b in sorted(bonus.items()):
        by_day[key[0]]["bonus"] += b
        r = exp.get(key)
        if r is None:
            if b > 0:
                errors.append(f"{key}: бонус {b} есть в атрибуции, строки advertising_clicks нет")
            continue
        was = Decimal(str(r["expense_amount"]))
        new = (was - b).quantize(Decimal("0.01"))
        if new < -EPS:
            errors.append(f"{key}: бонус {b} больше строки расходов {was}")
        elif abs(new) <= EPS:
            delete.append((r["id"], key, was)); by_day[key[0]]["delete"] += 1
        else:
            update.append((r["id"], key, was, new)); by_day[key[0]]["update"] += 1
    for r in to_flag:
        by_day[str(r["sale_date"])[:10]]["flag"] += 1
    return to_flag, update, delete, errors, dict(by_day)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", default=date.today().isoformat())
    ap.add_argument("--campaign-id", action="append", help="бонусные кампании руками (иначе — из последнего списка кампаний)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    if args.campaign_id:
        ids, n_list, src = sorted(args.campaign_id), None, "--campaign-id"
    else:
        src = latest_campaign_list()
        if not src:
            raise SystemExit("нет data/ozon_performance_campaigns/campaigns_*.json — снять список кампаний (loader.list_campaigns) или задать --campaign-id")
        ids, n_list = bonus_campaign_ids(src)
    print(f"бонусные кампании (isAutocreated): {len(ids)} — {', '.join(ids)}; источник {src}" + (f" ({n_list} кампаний в списке)" if n_list else ""))
    from supabase import create_client
    cabinet.assert_env()
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    try:
        attr = read_attribution(sb, ids, args.date_from, args.date_to)
        flag_column = True
    except Exception as exc:  # noqa: BLE001
        if "is_bonus" not in str(exc):
            raise
        print("⚠️ колонки is_bonus в ozon_daily_sku_ad_attribution ещё нет (миграция sql/20261021_add_ad_attribution_is_bonus.sql через db_migrate) — "
              "план без флага; --apply до миграции невозможен")
        attr = read_attribution(sb, ids, args.date_from, args.date_to, with_flag=False)
        flag_column = False
    exp = read_expenses(sb, args.date_from, args.date_to)
    to_flag, update, delete, errors, by_day = plan(attr, exp)
    print(f"окно {args.date_from} … {args.date_to}: строк атрибуции бонусных кампаний {len(attr)} (без флага {len(to_flag)}), строк advertising_clicks {len(exp)}")
    print("по дням: день | бонус Σ | строк атрибуции к флагу | расходов уменьшить | расходов удалить")
    for d in sorted(by_day):
        x = by_day[d]
        print(f"  {d} | {x['bonus']:>12,.2f} | {x['flag']:4} | {x['update']:4} | {x['delete']:4}")
    total_bonus = sum((x["bonus"] for x in by_day.values()), Decimal(0))
    print(f"итого: бонус {total_bonus:,.2f}; к флагу {len(to_flag)}; расходов уменьшить {len(update)} на {sum((w - n for _i, _k, w, n in update), Decimal(0)):,.2f}, "
          f"удалить {len(delete)} на {sum((w for _i, _k, w in delete), Decimal(0)):,.2f}")
    kpi_edge = (date.today().toordinal() - KPI_WINDOW_DAYS)
    old_days = sorted(d for d in by_day if date.fromisoformat(d).toordinal() < kpi_edge)
    if old_days:
        print(f"⚠️ дни старше окна ночного KPI ({KPI_WINDOW_DAYS} дн.): {', '.join(old_days)} — витрину за них пересчитать отдельно")
    if errors:
        print("ОШИБКИ (стоп, ничего не пишу):"); [print("  " + e) for e in errors]
        return 2
    if not args.apply:
        print("план: db_writes = 0. Для записи добавить --apply (по слову владельца).")
        return 0
    if not flag_column:
        print("стоп: сначала миграция is_bonus"); return 2
    snap_dir = cabinet.data_path("snapshots", prof=_PROFILE); os.makedirs(snap_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = os.path.join(snap_dir, f"ad_bonus_rewrite_{stamp}.json")
    with open(snap, "w", encoding="utf-8") as fh:
        json.dump({"campaign_ids": ids, "window": [args.date_from, args.date_to], "attribution": attr, "expenses_update": [(i, k, str(w), str(n)) for i, k, w, n in update],
                   "expenses_delete": [(i, k, str(w)) for i, k, w in delete]}, fh, ensure_ascii=False, indent=1)
    print(f"снимок: {snap}")
    writes = 0
    for i in range(0, len(to_flag), 200):
        chunk = [{"sale_date": r["sale_date"], "marketplace_code": r["marketplace_code"], "marketplace_sku": r["marketplace_sku"], "ad_source": r["ad_source"],
                  "attribution_type": r["attribution_type"], "campaign_id": r["campaign_id"], "is_bonus": True} for r in to_flag[i:i + 200]]
        sb.table(ATTR).upsert(chunk, on_conflict="sale_date,marketplace_code,marketplace_sku,ad_source,attribution_type,campaign_id").execute(); writes += 1
    for rid, _k, _w, new in update:
        sb.table(EXP).update({"expense_amount": float(new)}).eq("id", rid).execute(); writes += 1
    for rid, _k, _w in delete:
        sb.table(EXP).delete().eq("id", rid).execute(); writes += 1
    attr2 = read_attribution(sb, ids, args.date_from, args.date_to)
    exp2 = read_expenses(sb, args.date_from, args.date_to)
    f2, u2, d2, e2, _ = plan(attr2, exp2)
    print(f"записано: запросов {writes}; контроль чтением: без флага {len(f2)}, к уменьшению {len(u2)}, к удалению {len(d2)}, ошибок {len(e2)} — ожидание 0 / 0 / 0 / 0")
    return 0 if not (f2 or u2 or d2 or e2) else 1


if __name__ == "__main__":
    sys.exit(main())
