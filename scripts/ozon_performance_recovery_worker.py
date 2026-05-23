import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import loaders.ozon_performance_ads_loader as loader


def is_partial_ads_candidate(row):
    row = row or {}
    run_status = str(row.get("run_status") or "").strip().lower()
    cpc_status = str(row.get("cpc_status") or "").strip().lower()
    pending_campaigns = int(float(row.get("cpc_pending_campaigns") or 0))
    pending_units = int(float(row.get("cpc_campaign_units_pending_total") or 0))

    return (
        run_status == "partial_ads"
        or cpc_status in {"pending_429", "pending_backfill", "pending_quota"}
        or pending_campaigns > 0
        or pending_units > 0
    )


def build_budget_guard(daily_budget_used_today):
    daily_limit = loader.STATS_DAILY_CAMPAIGN_LIMIT
    daily_reserve = loader.STATS_DAILY_CAMPAIGN_RESERVE
    remaining_daily_budget = max(0, daily_limit - int(daily_budget_used_today or 0))
    recovery_budget_available = min(200, remaining_daily_budget - daily_reserve)

    result = {
        "daily_budget_used_today": int(daily_budget_used_today or 0),
        "daily_limit": daily_limit,
        "daily_reserve": daily_reserve,
        "remaining_daily_budget": remaining_daily_budget,
        "recovery_budget_available": max(0, int(recovery_budget_available)),
        "will_run": True,
        "budget_skip_reason": None,
    }

    if int(daily_budget_used_today or 0) > 1500:
        result["will_run"] = False
        result["budget_skip_reason"] = "skipped_daily_budget_guard"
        return result

    if recovery_budget_available <= 0:
        result["will_run"] = False
        result["budget_skip_reason"] = "skipped_no_recovery_budget"
        return result

    return result


def fetch_latest_status_rows(db_client, account_signature, target_date=None, limit=100):
    query = (
        db_client
        .table(loader.DAILY_LOAD_STATUS_TABLE)
        .select("*")
        .eq("marketplace_code", "ozon")
        .eq("account_signature", account_signature)
        .order("updated_at", desc=True)
    )
    if target_date:
        query = query.eq("target_date", target_date)
    if hasattr(query, "limit"):
        query = query.limit(limit)
    rows = (query.execute().data or [])
    latest_by_target = {}
    for row in rows:
        key = row.get("target_date")
        if key and key not in latest_by_target:
            latest_by_target[key] = row
    return list(latest_by_target.values())


def get_partial_candidates(db_client, account_signature, target_date=None):
    rows = fetch_latest_status_rows(db_client, account_signature, target_date=target_date)
    return [row for row in rows if is_partial_ads_candidate(row)]


def get_statistics_cooldown(client):
    cooldown_key = client.scoped_state_key("statistics_json")
    cooldown_until = client.get_cooldown(cooldown_key)
    return {
        "cooldown_key": cooldown_key,
        "cooldown_until": loader.to_iso(cooldown_until) if cooldown_until else None,
        "cooldown_active": bool(cooldown_until),
    }


def build_pending_batches_plan(progress):
    ordered_campaign_ids = loader.preserve_campaign_id_order(progress.get("ordered_campaign_ids") or [])
    batch_size = int(progress.get("batch_size") or 1)
    pending_batch_indexes = list(progress.get("pending_batch_indexes") or [])
    cpc_batches = loader.build_cpc_batches(ordered_campaign_ids, batch_size)

    pending_campaign_ids = [
        campaign_id
        for batch_index in pending_batch_indexes
        if 0 <= int(batch_index) < len(cpc_batches)
        for campaign_id in cpc_batches[int(batch_index)]
    ]

    pending_campaign_units = loader.sum_campaign_units_for_batches(cpc_batches, pending_batch_indexes)
    return {
        "ordered_campaign_ids": ordered_campaign_ids,
        "batch_size": batch_size,
        "cpc_batches": cpc_batches,
        "pending_batch_indexes": pending_batch_indexes,
        "pending_campaign_ids": pending_campaign_ids,
        "pending_campaign_units": pending_campaign_units,
    }


def pick_recovery_batches(cpc_batches, pending_batch_indexes, recovery_budget_available, max_batches_per_run):
    limited_batch_indexes, limited_units = loader.build_limited_batch_indexes(
        cpc_batches,
        pending_batch_indexes,
        recovery_budget_available,
    )
    if max_batches_per_run:
        limited_batch_indexes = limited_batch_indexes[: max(1, int(max_batches_per_run))]
        limited_units = loader.sum_campaign_units_for_batches(cpc_batches, limited_batch_indexes)
    return limited_batch_indexes, limited_units


def build_loader_command(target_date, max_batches_per_run, write=False):
    loader_script = Path(loader.__file__).resolve()
    command = [
        sys.executable,
        str(loader_script),
        "--mode",
        "cpc-backfill",
        "--date",
        str(target_date),
        "--max-cpc-batches",
        str(max_batches_per_run),
        "--allow-recovery-worker-before-daily-status",
    ]
    if write:
        command.extend(["--write", "--approve-cpc-recovery-write"])
    else:
        command.extend(["--dry-run", "--no-write"])
    return command


def build_candidate_plan(client, status_row, budget_guard, max_batches_per_run):
    target_date = status_row.get("target_date")
    progress_key, progress, source_progress_kind = loader.resolve_cpc_backfill_progress(client, target_date)
    if not progress:
        return {
            "target_date": target_date,
            "status": "skipped_no_pending_progress",
            "source_progress_kind": None,
            "progress_key": None,
            "pending_batch_indexes": [],
            "pending_campaign_units": 0,
            "pending_campaign_ids": [],
            "planned_batch_indexes": [],
            "planned_recovery_units": 0,
            "will_run": False,
            "cpo_status": status_row.get("cpo_status"),
        }

    pending_plan = build_pending_batches_plan(progress)
    planned_batch_indexes, planned_recovery_units = pick_recovery_batches(
        pending_plan["cpc_batches"],
        pending_plan["pending_batch_indexes"],
        budget_guard["recovery_budget_available"],
        max_batches_per_run,
    )
    planned_campaign_ids = [
        campaign_id
        for batch_index in planned_batch_indexes
        for campaign_id in pending_plan["cpc_batches"][int(batch_index)]
    ]

    command = build_loader_command(
        target_date=target_date,
        max_batches_per_run=max(1, len(planned_batch_indexes) or int(max_batches_per_run or 1)),
        write=False,
    )

    will_run = bool(planned_batch_indexes) and budget_guard["will_run"]
    status = "planned_resume" if will_run else "skipped_budget_cap"
    if not planned_batch_indexes and budget_guard["will_run"]:
        status = "skipped_no_recovery_budget"

    return {
        "target_date": target_date,
        "status": status,
        "source_progress_kind": source_progress_kind,
        "progress_key": progress_key,
        "pending_batch_indexes": pending_plan["pending_batch_indexes"],
        "pending_campaign_units": pending_plan["pending_campaign_units"],
        "pending_campaign_ids": pending_plan["pending_campaign_ids"],
        "planned_batch_indexes": planned_batch_indexes,
        "planned_recovery_units": planned_recovery_units,
        "planned_campaign_ids": planned_campaign_ids,
        "will_run": will_run,
        "cpo_status": status_row.get("cpo_status"),
        "recovery_command": command,
    }


def build_recovery_plan(target_date=None, max_batches_per_run=1, db_client=None, client=None):
    db_client = db_client or loader.supabase
    client = client or loader.OzonPerformanceClient()
    load_date = loader.today_local().isoformat()
    daily_budget_used_today = loader.read_attempted_campaign_units_for_load_date(load_date, client.account_signature)
    budget_guard = build_budget_guard(daily_budget_used_today)
    cooldown = get_statistics_cooldown(client)
    candidates = get_partial_candidates(db_client, client.account_signature, target_date=target_date)

    plan = {
        "load_date": load_date,
        "requested_target_date": target_date,
        "daily_budget_used_today": budget_guard["daily_budget_used_today"],
        "daily_limit": budget_guard["daily_limit"],
        "daily_reserve": budget_guard["daily_reserve"],
        "recovery_budget_available": budget_guard["recovery_budget_available"],
        "will_run": False,
        "budget_skip_reason": budget_guard["budget_skip_reason"],
        "cooldown_active": cooldown["cooldown_active"],
        "cooldown_until": cooldown["cooldown_until"],
        "candidates": [],
    }

    if not candidates:
        plan["status"] = "no_partial_candidates"
        return plan

    for row in candidates:
        candidate_plan = {
            "target_date": row.get("target_date"),
            "run_status": row.get("run_status"),
            "cpc_status": row.get("cpc_status"),
            "cpo_status": row.get("cpo_status"),
            "cpc_pending_campaigns": int(float(row.get("cpc_pending_campaigns") or 0)),
            "cpc_campaign_units_pending_total": int(float(row.get("cpc_campaign_units_pending_total") or 0)),
            "cpc_campaign_units_completed_total": int(float(row.get("cpc_campaign_units_completed_total") or 0)),
        }

        if cooldown["cooldown_active"]:
            candidate_plan.update(
                {
                    "status": "skipped_cooldown_active",
                    "next_attempt_at": cooldown["cooldown_until"],
                    "will_run": False,
                }
            )
            plan["candidates"].append(candidate_plan)
            continue

        if not budget_guard["will_run"]:
            candidate_plan.update(
                {
                    "status": budget_guard["budget_skip_reason"],
                    "will_run": False,
                    "planned_recovery_units": 0,
                }
            )
            plan["candidates"].append(candidate_plan)
            continue

        candidate_plan.update(build_candidate_plan(client, row, budget_guard, max_batches_per_run))
        plan["candidates"].append(candidate_plan)

    runnable = [candidate for candidate in plan["candidates"] if candidate.get("will_run")]
    if runnable:
        plan["status"] = "planned"
        plan["will_run"] = True
        plan["planned_recovery_units"] = int(runnable[0].get("planned_recovery_units") or 0)
        plan["selected_target_date"] = runnable[0].get("target_date")
        plan["selected_command"] = runnable[0].get("recovery_command")
    else:
        plan["status"] = "skipped"
        plan["planned_recovery_units"] = 0

    return plan


def run_recovery_write(plan, approve_write=False):
    if not approve_write:
        raise RuntimeError(
            "Recovery worker write requires explicit --write --approve-recovery-worker-write"
        )

    if not plan.get("will_run"):
        return {
            "status": "skipped",
            "reason": plan.get("budget_skip_reason") or "no_runnable_candidate",
        }

    candidates = [candidate for candidate in plan.get("candidates", []) if candidate.get("will_run")]
    if not candidates:
        return {"status": "skipped", "reason": "no_runnable_candidate"}

    candidate = candidates[0]
    write_command = build_loader_command(
        target_date=candidate["target_date"],
        max_batches_per_run=max(1, len(candidate.get("planned_batch_indexes") or [])),
        write=True,
    )
    completed = subprocess.run(
        write_command,
        capture_output=True,
        text=True,
        check=False,
    )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    result = {
        "status": "success" if completed.returncode == 0 else "failed",
        "command": write_command,
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }

    if "\"pending_429\"" in stdout or "\"status\": \"pending_429\"" in stdout:
        result["status"] = "pending_429"
    return result


def make_parser():
    parser = argparse.ArgumentParser(
        description="Safe self-healing recovery worker for Ozon Performance pending CPC tails."
    )
    parser.add_argument("--date", help="Optional target_date to inspect/recover")
    parser.add_argument("--max-batches-per-run", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--approve-recovery-worker-write", action="store_true")
    return parser


def main():
    args = make_parser().parse_args()
    if args.write and not args.approve_recovery_worker_write:
        raise RuntimeError(
            "Recovery worker write requires explicit --write --approve-recovery-worker-write"
        )

    plan = build_recovery_plan(
        target_date=args.date,
        max_batches_per_run=max(1, int(args.max_batches_per_run or 1)),
    )
    print("Ozon Performance recovery worker plan:")
    print(json.dumps(loader.sanitize_value(plan), ensure_ascii=False, indent=2))

    if not args.write:
        return

    result = run_recovery_write(plan, approve_write=bool(args.approve_recovery_worker_write))
    print("Ozon Performance recovery worker result:")
    print(json.dumps(loader.sanitize_value(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
