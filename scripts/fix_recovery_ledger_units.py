#!/usr/bin/env python3
"""Леджер квоты: проставить units, mode и дату строкам submit добора cpc-recovery, которые легли с campaign_units 0 и mode null (тридцать восьмая §6).

    venv/bin/python3 scripts/fix_recovery_ledger_units.py --date 2026-09-14 --from 2026-09-25T08:20:00Z --to 2026-09-25T08:45:00Z            план, db_writes = 0
    venv/bin/python3 scripts/fix_recovery_ledger_units.py --date … --from … --to … --apply --approve-ledger-update                          запись по слову

Что делает. Берёт строки ozon_performance_statistics_json_usage с request_kind = submit, mode null, campaign_units 0 в окне времени (отправки
добора), запись прогресса cpc_progress той даты (ordered_campaign_ids, batch_size, total_batches) и раскладывает пачки по строкам в порядке
времени: пачка i → units = len(пачки i). Число строк обязано равняться total_batches, Σ units — числу кампаний; иначе план не строится.
--apply: update по id (campaign_units, mode = cpc-recovery, target_date, batch_index, load_date = дата события), контроль чтением.
Снимок строк до записи — data/snapshots/ledger_units_<UTC>.json.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from loaders import ozon_performance_ads_loader as ads  # noqa: E402

TABLE = ads.STATISTICS_JSON_USAGE_TABLE
SNAP_DIR = os.path.join(ROOT, "data", "snapshots")


def ledger_rows(sb, t_from, t_to):
    res = (sb.table(TABLE).select("id,event_at,request_kind,mode,target_date,batch_index,campaign_units,load_date")
           .eq("request_kind", "submit").is_("mode", "null").eq("campaign_units", 0).gte("event_at", t_from).lte("event_at", t_to).order("event_at").limit(1000).execute())
    return res.data or []


def progress_batches(sb, target_date):
    """Пачки из записи прогресса даты: список списков campaign_id (batch_size из записи). Запись — та, где ordered_campaign_ids покрывают дату."""
    res = sb.table("pipeline_runtime_state").select("state_key,payload,updated_at").eq("state_type", "cpc_progress").order("updated_at", desc=True).limit(50).execute()
    cands = []
    for r in res.data or []:
        p = r.get("payload") or {}
        if str(p.get("date_from") or p.get("target_date") or "")[:10] == target_date or str(p.get("date_to") or "")[:10] == target_date:
            cands.append((r["state_key"], p))
    return cands


def plan(rows, payload):
    ids = [str(c) for c in payload.get("ordered_campaign_ids") or []]
    size = int(payload.get("batch_size") or 10)
    batches = [ids[i:i + size] for i in range(0, len(ids), size)]
    if len(batches) != len(rows):
        raise SystemExit(f"строк в леджере {len(rows)}, пачек по записи прогресса {len(batches)} — план не строится")
    out = []
    for i, (row, batch) in enumerate(zip(rows, batches)):
        out.append({"id": row["id"], "event_at": row["event_at"], "batch_index": i, "campaign_units": len(batch), "mode": "cpc-recovery",
                    "target_date": payload.get("date_from") or payload.get("target_date"), "load_date": str(row["event_at"])[:10]})
    return out, len(ids)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True); ap.add_argument("--from", dest="t_from", required=True); ap.add_argument("--to", dest="t_to", required=True)
    ap.add_argument("--state-key", help="ключ записи прогресса, если кандидатов несколько")
    ap.add_argument("--apply", action="store_true"); ap.add_argument("--approve-ledger-update", action="store_true")
    args = ap.parse_args(argv)
    if args.apply and not args.approve_ledger_update:
        raise SystemExit("--apply требует --approve-ledger-update (слово владельца по плану)")
    sb = ads.supabase
    rows = ledger_rows(sb, args.t_from, args.t_to)
    cands = progress_batches(sb, args.date)
    print(f"строк submit с mode null и units 0 в окне {args.t_from} … {args.t_to}: {len(rows)}; записей прогресса на {args.date}: {len(cands)}")
    for k, p in cands:
        print(f"   {k[:60]}… кампаний {len(p.get('ordered_campaign_ids') or [])}, batch_size {p.get('batch_size')}, total_batches {p.get('total_batches')}, "
              f"completed {p.get('completed_batches')}, mode {p.get('selection_mode')}")
    if args.state_key:
        cands = [(k, p) for k, p in cands if k == args.state_key]
    fit = [(k, p) for k, p in cands if int(p.get("total_batches") or 0) == len(rows)]
    if len(fit) != 1:
        raise SystemExit(f"записей прогресса с total_batches = {len(rows)}: {len(fit)} — укажите --state-key")
    key, payload = fit[0]
    upd, n_campaigns = plan(rows, payload)
    units = sum(u["campaign_units"] for u in upd)
    print(f"план по записи {key[:60]}…: строк {len(upd)}, units {units} = кампаний {n_campaigns} {'да' if units == n_campaigns else 'НЕТ'}; "
          f"пачки: {sorted(set(u['campaign_units'] for u in upd))}; mode cpc-recovery, target_date {upd[0]['target_date']}, batch_index 0 … {len(upd) - 1}")
    if not args.apply:
        print("db_writes = 0")
        return 0
    os.makedirs(SNAP_DIR, exist_ok=True)
    snap = os.path.join(SNAP_DIR, f"ledger_units_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
    with open(snap, "w", encoding="utf-8") as fh:
        json.dump({"before": rows, "plan": upd}, fh, ensure_ascii=False, default=str, indent=1)
    print(f"снимок → {snap}")
    for u in upd:
        sb.table(TABLE).update({k: u[k] for k in ("campaign_units", "mode", "target_date", "batch_index", "load_date")}).eq("id", u["id"]).execute()
    after = sb.table(TABLE).select("id,campaign_units,mode,target_date,batch_index").in_("id", [u["id"] for u in upd]).order("id").execute().data or []
    ok = {a["id"]: (a["campaign_units"], a["mode"], str(a["target_date"]), a["batch_index"]) for a in after} == \
         {u["id"]: (u["campaign_units"], u["mode"], str(u["target_date"]), u["batch_index"]) for u in upd}
    print(f"контроль чтением: {len(after)} строк, совпали с планом — {'да' if ok else 'НЕТ'}; Σ units в таблице {sum(a['campaign_units'] for a in after)}; db_writes = {len(upd)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
