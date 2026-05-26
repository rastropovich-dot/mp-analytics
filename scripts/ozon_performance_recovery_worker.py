import argparse
import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import loaders.ozon_performance_ads_loader as loader


CONTROLLED_FINAL_STATUSES = {
    "complete",
    "no_partial_candidates",
    "skipped",
    "skipped_cooldown_active",
    "skipped_daily_budget_guard",
    "skipped_no_recovery_budget",
    "deadline_already_passed",
    "deadline_before_cooldown",
    "deadline_after_429",
    "max_attempts_exhausted",
    "partial_remaining",
    "pending_429",
    "pending_quota",
    "skipped_daily_quota_exhausted",
    "runtime_state_unavailable",
}


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


def is_complete_status_row(row):
    row = row or {}
    run_status = str(row.get("run_status") or "").strip().lower()
    cpc_status = str(row.get("cpc_status") or "").strip().lower()
    pending_campaigns = int(float(row.get("cpc_pending_campaigns") or 0))
    pending_units = int(float(row.get("cpc_campaign_units_pending_total") or 0))
    return (
        run_status == "success"
        and cpc_status == "success"
        and pending_campaigns <= 0
        and pending_units <= 0
    )


def build_budget_guard(daily_budget_used_today, phase="pre"):
    daily_limit = loader.STATS_DAILY_CAMPAIGN_LIMIT
    daily_reserve = loader.STATS_DAILY_CAMPAIGN_RESERVE
    used = int(daily_budget_used_today or 0)
    remaining_daily_budget = max(0, daily_limit - used)

    if phase == "post":
        recovery_budget_available = remaining_daily_budget - daily_reserve
    else:
        recovery_budget_available = min(200, remaining_daily_budget - daily_reserve)

    result = {
        "phase": phase,
        "daily_budget_used_today": used,
        "daily_limit": daily_limit,
        "daily_reserve": daily_reserve,
        "remaining_daily_budget": remaining_daily_budget,
        "recovery_budget_available": max(0, int(recovery_budget_available)),
        "will_run": True,
        "budget_skip_reason": None,
    }

    if phase == "pre" and used > 1500:
        result["will_run"] = False
        result["budget_skip_reason"] = "skipped_daily_budget_guard"
        return result

    if recovery_budget_available <= 0:
        result["will_run"] = False
        result["budget_skip_reason"] = "skipped_no_recovery_budget"
        return result

    return result


def get_recent_daily_quota_event(client, target_date=None, load_date=None):
    try:
        events = client.get_statistics_json_usage_events()
    except Exception:
        return None

    candidates = []
    for event in events or []:
        if str(event.get("response_kind") or "") != "daily_quota_exhausted":
            continue
        if target_date and str(event.get("target_date") or "") != str(target_date):
            continue
        if load_date and str(event.get("load_date") or "") != str(load_date):
            continue
        candidates.append(event)

    if not candidates:
        return None

    candidates.sort(key=lambda item: str(item.get("event_at") or ""), reverse=True)
    return candidates[0]


def parse_wait_deadline(wait_until, timezone):
    if not wait_until:
        return None

    hour_text, minute_text = str(wait_until).split(":", 1)
    tz = ZoneInfo(timezone)
    now_local = loader.utcnow().astimezone(tz)
    return now_local.replace(
        hour=int(hour_text),
        minute=int(minute_text),
        second=0,
        microsecond=0,
    ).astimezone(ZoneInfo("UTC"))


def parse_relative_wait_deadline(wait_for_minutes, now_utc=None):
    if wait_for_minutes is None:
        return None
    now_utc = now_utc or loader.utcnow()
    return now_utc + timedelta(minutes=int(wait_for_minutes))


def format_time_pair(dt, timezone):
    if not dt:
        return None, None
    tz = ZoneInfo(timezone)
    return dt.astimezone(ZoneInfo("UTC")).isoformat(), dt.astimezone(tz).isoformat()


def build_wait_metadata(cooldown_until, deadline_utc, sleep_padding_seconds, now_utc=None, timezone="Europe/Moscow"):
    now_utc = now_utc or loader.utcnow()
    cooldown_dt = loader.from_iso(cooldown_until) if cooldown_until else None
    current_utc, current_local = format_time_pair(now_utc, timezone)
    deadline_utc_text, deadline_local_text = format_time_pair(deadline_utc, timezone)
    if not cooldown_dt:
        return {
            "current_time_utc": current_utc,
            "current_time_local": current_local,
            "cooldown_until": None,
            "cooldown_until_utc": None,
            "cooldown_until_local": None,
            "deadline": deadline_utc_text,
            "deadline_utc": deadline_utc_text,
            "deadline_local": deadline_local_text,
            "wait_seconds": 0,
            "will_wait": False,
            "estimated_completion_possible": True,
            "ready_at": None,
            "deadline_already_passed": bool(deadline_utc and now_utc >= deadline_utc),
        }

    ready_at = cooldown_dt + timedelta(seconds=int(sleep_padding_seconds or 0))
    wait_seconds = max(0, int((ready_at - now_utc).total_seconds() + 0.999999))
    estimated_completion_possible = deadline_utc is None or ready_at <= deadline_utc
    will_wait = wait_seconds > 0 and estimated_completion_possible
    cooldown_utc_text, cooldown_local_text = format_time_pair(cooldown_dt, timezone)

    return {
        "current_time_utc": current_utc,
        "current_time_local": current_local,
        "cooldown_until": cooldown_utc_text,
        "cooldown_until_utc": cooldown_utc_text,
        "cooldown_until_local": cooldown_local_text,
        "deadline": deadline_utc_text,
        "deadline_utc": deadline_utc_text,
        "deadline_local": deadline_local_text,
        "wait_seconds": wait_seconds,
        "will_wait": will_wait,
        "estimated_completion_possible": estimated_completion_possible,
        "ready_at": loader.to_iso(ready_at),
        "deadline_already_passed": bool(deadline_utc and now_utc >= deadline_utc),
    }


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
    rows = query.execute().data or []
    latest_by_target = {}
    for row in rows:
        key = row.get("target_date")
        if key and key not in latest_by_target:
            latest_by_target[key] = row
    return list(latest_by_target.values())


def get_latest_status_row(db_client, account_signature, target_date=None):
    rows = fetch_latest_status_rows(db_client, account_signature, target_date=target_date, limit=10)
    if target_date:
        for row in rows:
            if row.get("target_date") == target_date:
                return row
        return None
    return rows[0] if rows else None


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


def resolve_worker_progress_candidate(db_client, client, target_date):
    progress_key, progress, source_progress_kind = loader.resolve_cpc_backfill_progress(client, target_date)
    if progress:
        return progress_key, progress, source_progress_kind

    try:
        result = (
            db_client
            .table(loader.PIPELINE_RUNTIME_STATE_TABLE)
            .select("state_key,payload,updated_at")
            .eq("account_signature", client.account_signature)
            .eq("state_type", "cpc_progress")
            .order("updated_at", desc=True)
            .execute()
        )
    except Exception:
        return None, None, None

    candidates = []
    for row in result.data or []:
        payload = row.get("payload") or {}
        if payload.get("date_from") != target_date or payload.get("date_to") != target_date:
            continue
        if str(payload.get("account_signature") or client.account_signature) != str(client.account_signature):
            continue
        if str(payload.get("selection_mode") or "").strip().lower() != "complete":
            continue
        pending_batch_indexes = loader.normalize_batch_indexes(payload.get("pending_batch_indexes"))
        pending_batches = int(payload.get("pending_batches") or len(pending_batch_indexes) or 0)
        if not pending_batch_indexes and pending_batches <= 0:
            continue
        logical_key = loader.parse_db_state_key("cpc_progress", row.get("state_key"))
        if not logical_key:
            continue
        progress = dict(payload)
        progress["updated_at"] = progress.get("updated_at") or row.get("updated_at")
        progress["pending_batch_indexes"] = pending_batch_indexes
        progress["pending_batches"] = pending_batches
        candidates.append((logical_key, progress))

    if not candidates:
        return None, None, None

    candidates.sort(
        key=lambda item: (
            int(item[1].get("total_campaigns") or 0),
            int(item[1].get("batch_size") or 0),
            int(item[1].get("pending_batches") or 0),
            str(item[1].get("updated_at") or ""),
        ),
        reverse=True,
    )
    progress_key, progress = candidates[0]
    return progress_key, progress, "daily_yesterday_pending_disambiguated"


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


def build_loader_command(
    target_date,
    max_batches_per_run,
    write=False,
    progress_key=None,
    write_runtime_only=False,
    inter_batch_pause=None,
):
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
        "--allow-recovery-worker-before-backfill-window",
    ]
    if progress_key:
        command.extend(["--progress-key", str(progress_key)])
    if inter_batch_pause is not None:
        command.extend(["--inter-batch-pause", str(int(inter_batch_pause))])
    if write:
        command.extend(["--write", "--approve-cpc-recovery-write"])
        if write_runtime_only:
            command.append("--write-runtime-only")
    else:
        command.extend(["--dry-run", "--no-write"])
    return command


def build_candidate_plan(db_client, client, status_row, budget_guard, max_batches_per_run):
    target_date = status_row.get("target_date")
    progress_key, progress, source_progress_kind = resolve_worker_progress_candidate(
        db_client,
        client,
        target_date,
    )
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
        progress_key=progress_key,
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


def build_recovery_plan(
    target_date=None,
    max_batches_per_run=1,
    db_client=None,
    client=None,
    phase="pre",
    wait_until=None,
    wait_for_minutes=None,
    timezone="Europe/Moscow",
    max_attempts=10,
    sleep_padding_seconds=10,
):
    db_client = db_client or loader.supabase
    client = client or loader.OzonPerformanceClient()
    load_date = loader.today_local().isoformat()
    now_utc = loader.utcnow()
    if hasattr(client, "get_statistics_json_usage_events"):
        budget_diagnostics = loader.get_statistics_json_budget_diagnostics(
            load_date,
            client.account_signature,
            client=client,
        )
    else:
        budget_diagnostics = {
            "daily_budget_used_today": loader.read_attempted_campaign_units_for_load_date(
                load_date,
                client.account_signature,
            ),
            "budget_source": "status_snapshot",
            "budget_confidence": "low",
            "usage_event_count": 0,
        }
    daily_budget_used_today = int(budget_diagnostics.get("daily_budget_used_today") or 0)
    budget_guard = build_budget_guard(daily_budget_used_today, phase=phase)
    cooldown = get_statistics_cooldown(client)
    deadline_utc = (
        parse_relative_wait_deadline(wait_for_minutes, now_utc=now_utc)
        if wait_for_minutes is not None
        else parse_wait_deadline(wait_until, timezone)
    )
    wait_meta = build_wait_metadata(
        cooldown["cooldown_until"],
        deadline_utc,
        sleep_padding_seconds,
        now_utc=now_utc,
        timezone=timezone,
    )
    candidates = get_partial_candidates(db_client, client.account_signature, target_date=target_date)
    latest_status_row = get_latest_status_row(db_client, client.account_signature, target_date=target_date)
    latest_quota_event = get_recent_daily_quota_event(client, target_date=target_date, load_date=load_date)

    plan = {
        "load_date": load_date,
        "requested_target_date": target_date,
        "phase": phase,
        "current_time_utc": wait_meta["current_time_utc"],
        "current_time_local": wait_meta["current_time_local"],
        "daily_budget_used_today": budget_guard["daily_budget_used_today"],
        "budget_source": budget_diagnostics.get("budget_source"),
        "budget_confidence": budget_diagnostics.get("budget_confidence"),
        "usage_event_count": budget_diagnostics.get("usage_event_count"),
        "daily_limit": budget_guard["daily_limit"],
        "daily_reserve": budget_guard["daily_reserve"],
        "recovery_budget_available": budget_guard["recovery_budget_available"],
        "will_run": False,
        "budget_skip_reason": budget_guard["budget_skip_reason"],
        "cooldown_active": cooldown["cooldown_active"],
        "cooldown_until": wait_meta["cooldown_until"],
        "cooldown_until_utc": wait_meta["cooldown_until_utc"],
        "cooldown_until_local": wait_meta["cooldown_until_local"],
        "deadline": wait_meta["deadline"],
        "deadline_utc": wait_meta["deadline_utc"],
        "deadline_local": wait_meta["deadline_local"],
        "deadline_already_passed": wait_meta["deadline_already_passed"],
        "wait_seconds": wait_meta["wait_seconds"],
        "will_wait": wait_meta["will_wait"],
        "sleep_padding_seconds": int(sleep_padding_seconds or 0),
        "planned_attempts": max(1, int(max_attempts or 1)),
        "estimated_completion_possible": wait_meta["estimated_completion_possible"],
        "candidates": [],
        "latest_status_row": latest_status_row,
        "latest_quota_event": latest_quota_event,
    }

    if wait_meta["deadline_already_passed"]:
        plan["status"] = "deadline_already_passed"
        plan["planned_recovery_units"] = 0
        return plan

    if not candidates:
        if latest_status_row and is_complete_status_row(latest_status_row):
            plan["status"] = "complete"
        else:
            plan["status"] = "no_partial_candidates"
        plan["planned_recovery_units"] = 0
        return plan

    for row in candidates:
        candidate_quota_event = get_recent_daily_quota_event(
            client,
            target_date=row.get("target_date"),
            load_date=load_date,
        )
        candidate_plan = {
            "target_date": row.get("target_date"),
            "run_status": row.get("run_status"),
            "cpc_status": row.get("cpc_status"),
            "cpo_status": row.get("cpo_status"),
            "cpc_pending_campaigns": int(float(row.get("cpc_pending_campaigns") or 0)),
            "cpc_campaign_units_pending_total": int(float(row.get("cpc_campaign_units_pending_total") or 0)),
            "cpc_campaign_units_completed_total": int(float(row.get("cpc_campaign_units_completed_total") or 0)),
        }

        if (
            str(row.get("cpc_status") or "").strip().lower() in {"pending_quota", "daily_quota_exhausted"}
            or candidate_quota_event
        ):
            next_attempt_at = (candidate_quota_event or {}).get("next_attempt_at") or (candidate_quota_event or {}).get("cooldown_until")
            candidate_plan.update(
                {
                    "status": "skipped_daily_quota_exhausted",
                    "reason": "ozon_statistics_json_daily_quota_exhausted",
                    "next_attempt_at": next_attempt_at,
                    "will_run": False,
                }
            )
            plan["candidates"].append(candidate_plan)
            continue

        if cooldown["cooldown_active"]:
            candidate_plan.update(
                {
                    "status": "waiting_for_cooldown" if wait_meta["will_wait"] else (
                        "deadline_before_cooldown" if deadline_utc else "skipped_cooldown_active"
                    ),
                    "next_attempt_at": wait_meta["cooldown_until"],
                    "next_attempt_at_utc": wait_meta["cooldown_until_utc"],
                    "next_attempt_at_local": wait_meta["cooldown_until_local"],
                    "wait_seconds": wait_meta["wait_seconds"],
                    "will_wait": wait_meta["will_wait"],
                    "estimated_completion_possible": wait_meta["estimated_completion_possible"],
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

        candidate_plan.update(build_candidate_plan(db_client, client, row, budget_guard, max_batches_per_run))
        plan["candidates"].append(candidate_plan)

    runnable = [candidate for candidate in plan["candidates"] if candidate.get("will_run")]
    if runnable:
        plan["status"] = "planned"
        plan["will_run"] = True
        plan["planned_recovery_units"] = int(runnable[0].get("planned_recovery_units") or 0)
        plan["selected_target_date"] = runnable[0].get("target_date")
        plan["selected_command"] = runnable[0].get("recovery_command")
    else:
        candidate_statuses = {candidate.get("status") for candidate in plan["candidates"]}
        if "deadline_before_cooldown" in candidate_statuses:
            plan["status"] = "deadline_before_cooldown"
        elif "waiting_for_cooldown" in candidate_statuses:
            plan["status"] = "waiting_for_cooldown"
        elif "skipped_daily_quota_exhausted" in candidate_statuses:
            plan["status"] = "skipped_daily_quota_exhausted"
        else:
            plan["status"] = "skipped"
        plan["planned_recovery_units"] = 0

    return plan


def _extract_batch_events(stdout):
    marker = "Ozon Performance CPC batch event:"
    events = []
    for line in (stdout or "").splitlines():
        if marker not in line:
            continue
        payload = line.split(marker, 1)[1].strip()
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return events


def run_recovery_write(plan, approve_write=False, write_runtime_only=False, inter_batch_pause=None):
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
        progress_key=candidate.get("progress_key"),
        write_runtime_only=write_runtime_only,
        inter_batch_pause=inter_batch_pause,
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
        "batch_events": _extract_batch_events(stdout),
    }

    if "\"pending_429\"" in stdout or "\"status\": \"pending_429\"" in stdout:
        result["status"] = "pending_429"
    elif "\"pending_quota\"" in stdout or "\"status\": \"pending_quota\"" in stdout or "\"daily_quota_exhausted\"" in stdout:
        result["status"] = "pending_quota"
    elif "\"runtime_state_unavailable\"" in stdout or "\"status\": \"runtime_state_unavailable\"" in stdout:
        result["status"] = "runtime_state_unavailable"
    return result


def should_continue_loop(wait_until, wait_for_minutes, stop_when_complete):
    return bool(wait_until or wait_for_minutes is not None or stop_when_complete)


def execute_recovery_session(
    *,
    target_date=None,
    phase="pre",
    max_batches_per_run=1,
    wait_until=None,
    wait_for_minutes=None,
    timezone="Europe/Moscow",
    max_attempts=10,
    sleep_padding_seconds=10,
    stop_when_complete=False,
    dry_run=True,
    approve_write=False,
    write_runtime_only=False,
    inter_batch_pause=None,
    db_client=None,
    client=None,
    sleep_fn=time.sleep,
):
    attempts = 0
    history = []
    continue_loop = should_continue_loop(wait_until, wait_for_minutes, stop_when_complete)
    current_plan = None

    while True:
        plan = current_plan or build_recovery_plan(
            target_date=target_date,
            max_batches_per_run=max_batches_per_run,
            db_client=db_client,
            client=client,
            phase=phase,
            wait_until=wait_until,
            wait_for_minutes=wait_for_minutes,
            timezone=timezone,
            max_attempts=max_attempts,
            sleep_padding_seconds=sleep_padding_seconds,
        )
        current_plan = None

        if dry_run:
            plan["history"] = history
            return plan

        if plan["status"] == "complete":
            return {
                "status": "complete",
                "attempts": attempts,
                "history": history,
                "plan": plan,
            }

        if plan["cooldown_active"]:
            if continue_loop and plan["will_wait"] and attempts < int(max_attempts or 1):
                history.append(
                    {
                        "status": "waiting_for_cooldown",
                        "cooldown_until": plan.get("cooldown_until"),
                        "wait_seconds": plan.get("wait_seconds", 0),
                    }
                )
                sleep_fn(plan.get("wait_seconds", 0))
                continue

            return {
                "status": "deadline_before_cooldown" if plan.get("deadline") else "skipped_cooldown_active",
                "attempts": attempts,
                "history": history,
                "plan": plan,
            }

        if not plan.get("will_run"):
            return {
                "status": plan.get("status") or "skipped",
                "attempts": attempts,
                "history": history,
                "plan": plan,
            }

        if attempts >= int(max_attempts or 1):
            return {
                "status": "max_attempts_exhausted",
                "attempts": attempts,
                "history": history,
                "plan": plan,
            }

        result = run_recovery_write(
            plan,
            approve_write=approve_write,
            write_runtime_only=write_runtime_only,
            inter_batch_pause=inter_batch_pause,
        )
        attempts += 1
        history.append(
            {
                "attempt": attempts,
                "status": result.get("status"),
                "returncode": result.get("returncode"),
            }
        )

        if result.get("status") == "failed":
            return {
                "status": "failed",
                "attempts": attempts,
                "history": history,
                "plan": plan,
                "result": result,
            }

        if result.get("status") == "runtime_state_unavailable":
            return {
                "status": "runtime_state_unavailable",
                "attempts": attempts,
                "history": history,
                "plan": plan,
                "result": result,
            }

        if result.get("status") == "pending_quota":
            post_quota_plan = build_recovery_plan(
                target_date=target_date,
                max_batches_per_run=max_batches_per_run,
                db_client=db_client,
                client=client,
                phase=phase,
                wait_until=wait_until,
                wait_for_minutes=wait_for_minutes,
                timezone=timezone,
                max_attempts=max_attempts,
                sleep_padding_seconds=sleep_padding_seconds,
            )
            return {
                "status": "skipped_daily_quota_exhausted",
                "attempts": attempts,
                "history": history,
                "plan": post_quota_plan,
                "result": result,
            }

        if result.get("status") == "pending_429":
            post_429_plan = build_recovery_plan(
                target_date=target_date,
                max_batches_per_run=max_batches_per_run,
                db_client=db_client,
                client=client,
                phase=phase,
                wait_until=wait_until,
                wait_for_minutes=wait_for_minutes,
                timezone=timezone,
                max_attempts=max_attempts,
                sleep_padding_seconds=sleep_padding_seconds,
            )
            if continue_loop and post_429_plan["cooldown_active"] and post_429_plan["will_wait"] and attempts < int(max_attempts or 1):
                history.append(
                    {
                        "status": "waiting_after_429",
                        "cooldown_until": post_429_plan["cooldown_until"],
                        "wait_seconds": post_429_plan["wait_seconds"],
                    }
                )
                sleep_fn(post_429_plan["wait_seconds"])
                continue

            return {
                "status": "deadline_after_429" if post_429_plan.get("deadline") else "pending_429",
                "attempts": attempts,
                "history": history,
                "plan": post_429_plan,
                "result": result,
            }

        next_plan = build_recovery_plan(
            target_date=target_date,
            max_batches_per_run=max_batches_per_run,
            db_client=db_client,
            client=client,
            phase=phase,
            wait_until=wait_until,
            wait_for_minutes=wait_for_minutes,
            timezone=timezone,
            max_attempts=max_attempts,
            sleep_padding_seconds=sleep_padding_seconds,
        )
        if next_plan["status"] == "complete":
            return {
                "status": "complete",
                "attempts": attempts,
                "history": history,
                "plan": next_plan,
                "result": result,
            }

        if continue_loop and next_plan.get("will_run") and attempts < int(max_attempts or 1):
            current_plan = next_plan
            continue

        return {
            "status": "partial_remaining" if next_plan.get("candidates") else "success",
            "attempts": attempts,
            "history": history,
            "plan": next_plan,
            "result": result,
        }


def make_parser():
    parser = argparse.ArgumentParser(
        description="Safe self-healing recovery worker for Ozon Performance pending CPC tails."
    )
    parser.add_argument("--date", help="Optional target_date to inspect/recover")
    parser.add_argument("--phase", choices=("pre", "post"), default="pre")
    parser.add_argument("--max-batches-per-run", type=int, default=1)
    deadline_group = parser.add_mutually_exclusive_group()
    deadline_group.add_argument("--wait-until", help="Local wall-clock deadline in HH:MM")
    deadline_group.add_argument("--wait-for-minutes", type=int, help="Relative deadline from now, in minutes")
    parser.add_argument("--timezone", default="Europe/Moscow")
    parser.add_argument("--max-attempts", type=int, default=10)
    parser.add_argument("--sleep-padding-seconds", type=int, default=10)
    parser.add_argument("--inter-batch-pause", type=int, default=2)
    parser.add_argument("--stop-when-complete", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--write-runtime-only", action="store_true")
    parser.add_argument("--approve-recovery-worker-write", action="store_true")
    return parser


def main():
    args = make_parser().parse_args()
    if args.write and not args.approve_recovery_worker_write:
        raise RuntimeError(
            "Recovery worker write requires explicit --write --approve-recovery-worker-write"
        )

    dry_run = bool(args.dry_run or not args.write)
    payload = execute_recovery_session(
        target_date=args.date,
        phase=args.phase,
        max_batches_per_run=max(1, int(args.max_batches_per_run or 1)),
        wait_until=args.wait_until,
        wait_for_minutes=args.wait_for_minutes,
        timezone=args.timezone,
        max_attempts=max(1, int(args.max_attempts or 1)),
        sleep_padding_seconds=max(0, int(args.sleep_padding_seconds or 0)),
        inter_batch_pause=max(0, int(args.inter_batch_pause or 0)),
        stop_when_complete=bool(args.stop_when_complete),
        dry_run=dry_run,
        approve_write=bool(args.approve_recovery_worker_write),
        write_runtime_only=bool(args.write_runtime_only),
    )
    print("Ozon Performance recovery worker plan:")
    print(json.dumps(loader.sanitize_value(payload if dry_run else payload.get("plan")), ensure_ascii=False, indent=2))

    if dry_run:
        return

    print("Ozon Performance recovery worker result:")
    print(json.dumps(loader.sanitize_value(payload), ensure_ascii=False, indent=2))

    if payload.get("status") == "failed":
        raise RuntimeError("Ozon Performance recovery worker failed unexpectedly")


if __name__ == "__main__":
    main()
