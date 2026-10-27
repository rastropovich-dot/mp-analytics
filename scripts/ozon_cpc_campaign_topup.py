#!/usr/bin/env python3
"""Добор кликов Ozon по кампаниям, которых нет в атрибуции, — ПРИБАВЛЕНИЕМ к строкам SKU (RBH 27.10, план B / C).

    MP_CABINET=rbh1 venv/bin/python3 scripts/ozon_cpc_campaign_topup.py --date-from 2026-08-23 --date-to 2026-10-21 \\
        --campaign-id 36692482 --campaign-id 30580019                                 план по начислениям и базе, API 0
    … --fetch                                                                         снять statistics/json по пачкам (≤ 10 кампаний,
                                                                                      ≤ 62 дня), сырьё на диск, план записи — в файл
    … --write --approve-topup-write                                                   запись по сохранённому плану, по слову

Что добирается. Пара «день × кампания», у которой есть начисление типа 41 (номер кампании — `unit_number`) и нет ни одной строки
атрибуции CPC за этот день (`loaders/ozon_click_campaign_arbiter`). Без `--campaign-id` — все такие пары окна (план C: 22.10,
день пуст целиком).

Почему не `cpc-recovery --campaign-id`. Он строит строки расходов только из запрошенных кампаний и пишет upsert по
(дата, SKU, тип) — то есть ЗАМЕНЯЕТ клики SKU: у 863885383 за 08.10 пропали бы 1 164,65 кампании 42573900. Здесь строка расходов
получает «было + трата добираемых кампаний», атрибуция — строки самих кампаний (ключ с `campaign_id`, чужие не трогаются).

Идемпотентность. План (`--fetch`) пишет файл `data/<кабинет>/checks/cpc_topup/plan_<от>_<до>.json`: по каждому ключу расходов
«было» и «цель». Запись читает этот файл, а не пересчитывает: ключ, где в таблице «цель», — уже записан (пропуск); где «было» —
пишется цель; иначе — стоп (кто-то менял строку). Атрибуция — upsert абсолютных строк. Отказ в окнах ночи и утреннего алерта.
"""
import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
import cabinet  # noqa: E402
_PROFILE = cabinet.profile()
from loaders import ozon_click_campaign_arbiter as arb  # noqa: E402
from loaders import ozon_performance_ads_loader as ads  # noqa: E402
from loaders.pipeline_window import in_morning_alert_window, in_nightly_run_window  # noqa: E402

CENT = Decimal("0.01")
BATCH_SIZE = 10          # кампаний в отчёте (CLAUDE.md §4)
MAX_DAYS = 62            # дней в выгрузке
CLICKS = ads.CPC_SOURCE_EXPENSE_TYPE
D = lambda v: Decimal(str(v or 0))  # noqa: E731


def days_between(date_from, date_to):
    d0, d1 = date.fromisoformat(date_from), date.fromisoformat(date_to)
    if d1 < d0:
        raise SystemExit(f"окно {date_from} … {date_to} пусто")
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def load_accrual_pairs(days, accrual_dir):
    """Пары из файлов `accrual/by-day` (fetch_accrual_history_range.py). Нет файла — стоп: без начислений нечего сравнивать."""
    pairs, absent = {}, []
    for d in days:
        path = os.path.join(accrual_dir, f"{d}.json")
        if not os.path.exists(path):
            absent.append(d)
            continue
        doc = json.load(open(path, encoding="utf-8"))
        pairs.update(arb.campaign_spend_from_accruals(doc.get("accruals"), day=doc.get("date") or d))
    if absent:
        raise SystemExit(f"нет файлов accrual/by-day за {absent} — снять scripts/fetch_accrual_history_range.py")
    return pairs


def plan_batches(missing, batch_size=BATCH_SIZE, max_days=MAX_DAYS):
    """missing {(день, кампания): трата} → пачки [{campaigns, date_from, date_to}] по ≤ batch_size кампаний; окно пачки — от первой
    до последней даты её пар, не длиннее max_days (иначе пачка режется по кампаниям, одиночная — стоп)."""
    by_campaign = defaultdict(list)
    for d, c in missing:
        by_campaign[c].append(d)
    campaigns = sorted(by_campaign, key=lambda c: (min(by_campaign[c]), c))
    batches = []
    for i in range(0, len(campaigns), batch_size):
        part = campaigns[i:i + batch_size]
        d_from = min(min(by_campaign[c]) for c in part)
        d_to = max(max(by_campaign[c]) for c in part)
        span = (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days + 1
        if span > max_days:
            if len(part) == 1:
                raise SystemExit(f"кампания {part[0]}: пары на {span} дней > {max_days} — разбить окно")
            return plan_batches(missing, batch_size=max(1, batch_size // 2), max_days=max_days)
        batches.append({"campaigns": part, "date_from": d_from, "date_to": d_to})
    return batches


def raw_path(out_dir, batch):
    h = hashlib.sha1(",".join(batch["campaigns"]).encode()).hexdigest()[:10]
    return os.path.join(out_dir, f"stat_{batch['date_from']}_{batch['date_to']}_{len(batch['campaigns'])}c_{h}.json")


def rows_for_missing(report_data, campaigns_by_id, batch, missing):
    """Строки расходов и атрибуции только пар missing: отчёт режется по кампании, строки — по дате пары."""
    exp_rows, attr_rows = [], []
    for c in batch["campaigns"]:
        sub = {c: (report_data or {}).get(c)} if (report_data or {}).get(c) is not None else {}
        rows, _ = ads.build_rows(sub, campaigns_by_id, batch["date_from"], source_expense_type=CLICKS)
        attr, _ = ads.build_cpc_attribution_rows(sub, batch["date_from"], campaigns_by_id=campaigns_by_id)
        for r in rows:
            if (r["expense_date"], c) in missing:
                exp_rows.append({**r, "_campaign": c})
        for r in attr:
            if (r["sale_date"], c) in missing:
                attr_rows.append(r)
    return exp_rows, attr_rows


def built_pair_spend(attr_rows):
    out = defaultdict(Decimal)
    for r in attr_rows:
        out[(r["sale_date"], str(r["campaign_id"]))] += D(r["ad_spend"])
    return dict(out)


def expense_targets(exp_rows, current):
    """Ключ (день, SKU) → {before, add, target}; current — нынешние строки `advertising_clicks` {(день, SKU): (сумма, артикул)}."""
    adds, articles = defaultdict(Decimal), {}
    for r in exp_rows:
        key = (r["expense_date"], str(r["marketplace_sku"]))
        adds[key] += D(r["expense_amount"])
        articles.setdefault(key, r.get("article") or "")
    out = {}
    for key, add in sorted(adds.items()):
        before, article = current.get(key, (Decimal(0), ""))
        add = add.quantize(CENT)
        out[key] = {"before": before.quantize(CENT), "add": add, "target": (before + add).quantize(CENT), "article": article or articles[key]}
    return out


def apply_action(item, now):
    """Решение по ключу при записи: «цель» — уже записан, «было» — писать, иначе — стоп."""
    if now == item["target"]:
        return "done"
    if now == item["before"]:
        return "write"
    return "stop"


def read_clicks(sb, keys):
    """Нынешние строки `advertising_clicks` по ключам (день, SKU) → {(день, SKU): (сумма, артикул)}."""
    by_day = defaultdict(set)
    for d, sku in keys:
        by_day[d].add(sku)
    out = {}
    for d, skus in sorted(by_day.items()):
        res = (sb.table("marketplace_expenses").select("marketplace_sku,expense_amount,article").eq("marketplace_code", "ozon")
               .eq("expense_date", d).eq("expense_type", CLICKS).in_("marketplace_sku", sorted(skus)).limit(5000).execute())
        for r in res.data or []:
            out[(d, str(r["marketplace_sku"]))] = (D(r["expense_amount"]).quantize(CENT), r.get("article") or "")
    return out


def load_campaigns_by_id(client, checks_dir):
    """Метаданные кампаний (бонусные — не расход): последний сохранённый список, иначе один запрос списка (если есть клиент)."""
    saved = sorted(f for f in os.listdir(checks_dir) if f.startswith("performance_campaigns_") and f.endswith(".json"))
    if saved:
        camps = json.load(open(os.path.join(checks_dir, saved[-1]), encoding="utf-8"))
        print(f"метаданные кампаний: {saved[-1]} ({len(camps)})")
    elif client is not None:
        camps = client.list_campaigns()
        json.dump(camps, open(os.path.join(checks_dir, f"performance_campaigns_{date.today():%Y%m%d}.json"), "w", encoding="utf-8"), ensure_ascii=False)
    else:
        print("⚠️ метаданных кампаний нет — бонусные кампании не распознаются")
        camps = []
    return {str(c.get("id")): c for c in camps}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", required=True)
    ap.add_argument("--date-to", required=True)
    ap.add_argument("--campaign-id", action="append", default=[], help="только эти кампании (без флага — все пары окна без атрибуции)")
    ap.add_argument("--fetch", action="store_true", help="снять отчёты пачек, которых нет на диске (Performance), и записать план в файл")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--approve-topup-write", action="store_true")
    ap.add_argument("--max-loss-pct", type=float, default=1.0, help="стоп, если Σ построенного по парам отличается от Σ начислений больше, %")
    args = ap.parse_args(argv)
    if args.write and not args.approve_topup_write:
        raise SystemExit("--write требует --approve-topup-write (слово владельца)")
    now = datetime.now(timezone.utc)
    if (args.fetch or args.write) and (in_nightly_run_window(now) or in_morning_alert_window(now)):
        raise SystemExit("окно ночного прогона или утреннего алерта — не стартую")
    days = days_between(args.date_from, args.date_to)
    checks = cabinet.data_path("checks", prof=_PROFILE)
    out_dir = os.path.join(checks, "cpc_topup")
    os.makedirs(out_dir, exist_ok=True)
    plan_file = os.path.join(out_dir, f"plan_{args.date_from}_{args.date_to}.json")
    sb = ads.supabase

    if args.write:
        return write_plan(sb, plan_file)

    accrual = load_accrual_pairs(days, cabinet.data_path("accrual_history", prof=_PROFILE))
    attribution = arb.attribution_campaign_spend(sb, days)
    result = arb.compare(accrual, attribution)
    missing = result["missing"]
    if args.campaign_id:
        wanted = {str(c) for c in args.campaign_id}
        missing = {k: v for k, v in missing.items() if k[1] in wanted}
    print(f"окно {args.date_from} … {args.date_to}: тип 41 {arb.money(sum(accrual.values(), Decimal(0)))}, атрибуция {arb.money(sum(attribution.values(), Decimal(0)))}; "
          f"пар без атрибуции (с фильтром) {len(missing)} на {arb.money(sum(missing.values(), Decimal(0)))}; всего без атрибуции в окне {len(result['missing'])}")
    batches = plan_batches(missing)
    units = sum(len(b["campaigns"]) for b in batches)
    for i, b in enumerate(batches):
        have = os.path.exists(raw_path(out_dir, b))
        print(f"  пачка {i}: {len(b['campaigns'])} кампаний, {b['date_from']} … {b['date_to']}, сырьё {'на диске' if have else 'нет'}")
    to_fetch = [b for b in batches if not os.path.exists(raw_path(out_dir, b))]
    print(f"Performance: пачек {len(batches)}, выгрузок {units}; снимать {len(to_fetch)} пачек / {sum(len(b['campaigns']) for b in to_fetch)} выгрузок")
    if to_fetch and not args.fetch:
        print("план без --fetch: отчётов нет на диске, db_writes = 0")
        return 0

    client = ads.OzonPerformanceClient() if to_fetch else None
    campaigns_by_id = load_campaigns_by_id(client, checks)
    exp_rows, attr_rows = [], []
    for i, b in enumerate(batches):
        path = raw_path(out_dir, b)
        if os.path.exists(path):
            raw = json.load(open(path, encoding="utf-8"))
        else:
            ctx = {"mode": "cpc-topup", "target_date": b["date_to"], "load_date": ads.today_local().isoformat(), "batch_index": i,
                   "campaign_units": len(b["campaigns"]), "campaign_ids_hash": ads.payload_hash(b["campaigns"])}
            got = ads.fetch_cpc_recovery_batch_stateless(client, b["campaigns"], b["date_from"], b["date_to"], ads.DEFAULT_GROUP_BY, usage_context=ctx)
            raw = {**b, "uuid": got["uuid"], "report_data": got["report_data"], "fetched_at": datetime.now(timezone.utc).isoformat()}
            json.dump(raw, open(path, "w", encoding="utf-8"), ensure_ascii=False)
            print(f"  пачка {i}: UUID {got['uuid']} → {path}")
        e, a = rows_for_missing(raw["report_data"], campaigns_by_id, b, missing)
        exp_rows += e
        attr_rows += a

    built = built_pair_spend(attr_rows)
    pair_diff = {k: (v, built.get(k, Decimal(0))) for k, v in missing.items() if abs(v - built.get(k, Decimal(0))) > CENT}
    total_m, total_b = sum(missing.values(), Decimal(0)), sum(built.values(), Decimal(0))
    print(f"по парам: начислено {arb.money(total_m)}, в отчёте {arb.money(total_b)}, Δ {arb.money(total_b - total_m)}; пар с разницей {len(pair_diff)} из {len(missing)}")
    for k, (a, b) in sorted(pair_diff.items(), key=lambda kv: -abs(kv[1][1] - kv[1][0]))[:10]:
        print(f"    {k[0]} {k[1]}: начислено {arb.money(a)}, отчёт {arb.money(b)}")
    bonus = sum(1 for r in attr_rows if r.get("is_bonus"))
    targets = expense_targets(exp_rows, read_clicks(sb, {(r["expense_date"], str(r["marketplace_sku"])) for r in exp_rows}))
    add_total = sum((t["add"] for t in targets.values()), Decimal(0))
    print(f"запись: marketplace_expenses {len(targets)} ключей (новых {sum(1 for t in targets.values() if t['before'] == 0)}), прибавка {arb.money(add_total)}; "
          f"атрибуция {len(ads.aggregate_ad_attribution_rows(attr_rows))} строк (бонусных {bonus})")
    if total_m and abs(total_b - total_m) / total_m * 100 > Decimal(str(args.max_loss_pct)):
        raise SystemExit(f"Σ отчёта по парам расходится с начислениями больше {args.max_loss_pct} % — плана записи нет")
    plan = {
        "cabinet": _PROFILE.CODE, "date_from": args.date_from, "date_to": args.date_to, "campaign_ids": args.campaign_id,
        "made_at": datetime.now(timezone.utc).isoformat(), "pairs": len(missing), "accrued": str(total_m), "built": str(total_b),
        "expenses": [{"expense_date": k[0], "marketplace_sku": k[1], "before": str(t["before"]), "add": str(t["add"]),
                      "target": str(t["target"]), "article": t["article"]} for k, t in sorted(targets.items())],
        "attribution": ads.aggregate_ad_attribution_rows(attr_rows),
    }
    json.dump(plan, open(plan_file, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print(f"план записи → {plan_file}; db_writes = 0 (запись — --write --approve-topup-write по слову)")
    return 0


def write_plan(sb, plan_file):
    if not os.path.exists(plan_file):
        raise SystemExit(f"нет плана {plan_file} — сначала --fetch")
    plan = json.load(open(plan_file, encoding="utf-8"))
    if plan["cabinet"] != _PROFILE.CODE:
        raise SystemExit(f"план кабинета {plan['cabinet']}, а MP_CABINET — {_PROFILE.CODE}")
    items = {(e["expense_date"], e["marketplace_sku"]): {**e, "before": D(e["before"]), "target": D(e["target"])} for e in plan["expenses"]}
    now = read_clicks(sb, items)
    actions = {k: apply_action(v, now.get(k, (Decimal(0), ""))[0]) for k, v in items.items()}
    stop = [k for k, a in actions.items() if a == "stop"]
    if stop:
        raise SystemExit(f"{len(stop)} ключей изменились после плана (не «было» и не «цель»): {stop[:5]} — стоп, план заново")
    rows = [{"expense_date": k[0], "marketplace_code": "ozon", "marketplace_sku": k[1], "article": v["article"] or "",
             "expense_type": CLICKS, "expense_amount": float(v["target"])} for k, v in items.items() if actions[k] == "write"]
    print(f"расходы: писать {len(rows)}, уже записаны {sum(1 for a in actions.values() if a == 'done')}")
    for batch in ads.chunks(rows, 500):
        sb.table("marketplace_expenses").upsert(batch, on_conflict="expense_date,marketplace_code,marketplace_sku,expense_type").execute()
    for batch in ads.chunks(plan["attribution"], 500):
        sb.table("ozon_daily_sku_ad_attribution").upsert(
            batch, on_conflict="sale_date,marketplace_code,marketplace_sku,ad_source,attribution_type,campaign_id").execute()
    after = read_clicks(sb, items)
    bad = [k for k, v in items.items() if after.get(k, (Decimal(0), ""))[0] != v["target"]]
    pairs = {(r["sale_date"], str(r["campaign_id"])) for r in plan["attribution"]}
    present = arb.attribution_campaign_spend(sb, sorted({d for d, _ in pairs}))
    absent = [p for p in pairs if p not in present]
    print(f"контроль чтением: расходов не равных цели {len(bad)} {bad[:5]}; пар атрибуции не найдено {len(absent)} {absent[:5]}; "
          f"db_writes = {len(rows) + len(plan['attribution'])}")
    return 0 if not bad and not absent else 1


if __name__ == "__main__":
    sys.exit(main())
