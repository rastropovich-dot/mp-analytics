import argparse
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import loaders.ozon_performance_ads_loader as loader


KNOWN_RECONSTRUCTION_EVIDENCE = {
    "2026-05-23": {
        "run_key": "2026-05-23:2026-05-23",
        "cpc_progress_key": "cpc_progress:2a090cd8790741d88d7c71127158738f6aa6bbd19dddf79bb17570c601dbfa2d",
        "batch_size": 10,
        "total_batches": 133,
        "failed_batch_index": 67,
        "failed_batch": [str(cid) for cid in range(25049153, 25049163)],
        "campaign_count": 1323,
        "ordering_source": "saved_ordered_campaign_ids",
    }
}


def latest_daily_status_for_date(db_client, account_signature, target_date):
    result = (
        db_client
        .table(loader.DAILY_LOAD_STATUS_TABLE)
        .select("*")
        .eq("marketplace_code", "ozon")
        .eq("account_signature", account_signature)
        .eq("target_date", target_date)
        .order("updated_at", desc=True)
        .limit(20)
        .execute()
    )
    rows = result.data or []
    if not rows:
        return None
    rows.sort(
        key=lambda row: (
            int(float(row.get("cpc_campaign_units_completed_total") or 0)),
            int(float(row.get("cpc_campaign_units_pending_total") or 0)),
            str(row.get("updated_at") or ""),
        ),
        reverse=True,
    )
    return rows[0]


def load_run_evidence_from_file(target_date):
    state = loader.load_file_state()
    runs = (state or {}).get("runs", {}) or {}
    candidates = []
    for run_key, summary in runs.items():
        summary = summary or {}
        if summary.get("target_date") == target_date or summary.get("date_to") == target_date:
            cpc = summary.get("cpc") or {}
            candidates.append({
                "run_key": run_key,
                "cpc_progress_key": summary.get("cpc_progress_key"),
                "batch_size": int(cpc.get("batch_size") or 0),
                "total_batches": int(cpc.get("total_batches") or 0),
                "failed_batch_index": int(cpc.get("failed_batch_index") or 0),
                "failed_batch": list(cpc.get("failed_batch") or cpc.get("failed_batch_campaign_ids") or []),
                "campaign_count": int(summary.get("campaign_count") or summary.get("cpc_campaign_units_total") or 0),
                "campaign_list_hash": summary.get("campaign_list_hash"),
                "ordering_source": summary.get("ordering_source"),
            })
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            int(item.get("failed_batch_index") or 0),
            int(item.get("campaign_count") or 0),
            int(item.get("total_batches") or 0),
        ),
        reverse=True,
    )
    return candidates[0]


def load_run_evidence(target_date):
    file_evidence = load_run_evidence_from_file(target_date)
    known_evidence = KNOWN_RECONSTRUCTION_EVIDENCE.get(str(target_date))
    candidates = [evidence for evidence in (file_evidence, known_evidence) if evidence]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            int(item.get("failed_batch_index") or 0),
            int(item.get("campaign_count") or 0),
            int(item.get("total_batches") or 0),
        ),
        reverse=True,
    )
    return candidates[0]


def load_existing_progress_row(target_date, progress_key):
    if not progress_key:
        return None
    client = loader.OzonPerformanceClient(skip_persistent_state_load=True)
    try:
        _key, progress = loader.read_exact_cpc_progress_from_db(
            client,
            progress_key,
            target_date=target_date,
            max_attempts=1,
            sleep_fn=lambda _seconds: None,
        )
    except Exception:
        return None
    return progress


def validate_status_row(status_row):
    if not status_row:
        return False, "daily_load_status_missing"
    run_status = str(status_row.get("run_status") or "").strip().lower()
    cpc_status = str(status_row.get("cpc_status") or "").strip().lower()
    completed = int(float(status_row.get("cpc_campaign_units_completed_total") or 0))
    pending = int(float(status_row.get("cpc_campaign_units_pending_total") or 0))
    stop_batch_index = status_row.get("cpc_stop_batch_index")
    if run_status != "partial_ads":
        return False, "daily_status_not_partial_ads"
    if cpc_status not in {"pending_429", "pending_backfill", "pending_quota"}:
        return False, "daily_status_not_pending_cpc"
    if completed <= 0 or pending <= 0:
        return False, "daily_status_has_no_pending_tail"
    if stop_batch_index is None:
        return False, "daily_status_has_no_stop_batch_index"
    return True, None


def reconstruct_progress_plan(target_date):
    client = loader.OzonPerformanceClient(skip_persistent_state_load=True)
    status_row = latest_daily_status_for_date(loader.supabase, client.account_signature, target_date)
    status_ok, status_reason = validate_status_row(status_row)
    if not status_ok:
        return {
            "target_date": target_date,
            "status": "reconstruction_unsafe",
            "reason": status_reason,
            "validation_result": False,
            "db_writes": 0,
        }

    run_evidence = load_run_evidence(target_date)
    if not run_evidence:
        return {
            "target_date": target_date,
            "status": "reconstruction_unsafe",
            "reason": "run_evidence_missing",
            "validation_result": False,
            "db_writes": 0,
        }

    failed_batch_ids = list(run_evidence.get("failed_batch") or [])
    if not failed_batch_ids:
        return {
            "target_date": target_date,
            "status": "reconstruction_unsafe",
            "reason": "failed_batch_ids_missing",
            "validation_result": False,
            "db_writes": 0,
            "run_evidence": run_evidence,
        }

    existing_progress = load_existing_progress_row(target_date, run_evidence.get("cpc_progress_key"))
    if existing_progress and list(existing_progress.get("ordered_campaign_ids") or []):
        ordered_campaign_ids = loader.preserve_campaign_id_order(existing_progress.get("ordered_campaign_ids") or [])
        batch_size = int(existing_progress.get("batch_size") or run_evidence.get("batch_size") or loader.DEFAULT_CAMPAIGN_BATCH_SIZE)
        source_strategy = "existing_progress_row"
    else:
        campaigns = client.list_campaigns()
        daily_selection = loader.build_daily_cpc_selection(campaigns, target_date, target_date, "complete")
        cpc_campaigns = list(daily_selection["selected_campaigns"])
        cpc_campaigns_by_id = {
            str(campaign.get("id") or campaign.get("campaignId")): campaign
            for campaign in cpc_campaigns
            if campaign.get("id") or campaign.get("campaignId")
        }
        ordered_campaign_ids = loader.deterministic_campaign_id_order(cpc_campaigns_by_id.keys())
        batch_size = int(run_evidence.get("batch_size") or loader.DEFAULT_CAMPAIGN_BATCH_SIZE)
        source_strategy = "live_campaign_list"
    cpc_batches = loader.build_cpc_batches(ordered_campaign_ids, batch_size)
    total_batches = len(cpc_batches)
    stop_batch_index = int(status_row.get("cpc_stop_batch_index"))
    completed_batch_indexes = list(range(stop_batch_index))
    pending_batch_indexes = list(range(stop_batch_index, total_batches))
    failed_429_batch_indexes = [stop_batch_index]
    reconstructed_progress_key = (
        str(run_evidence.get("cpc_progress_key") or "").strip()
        or loader.build_cpc_progress_key(target_date, target_date, batch_size, ordered_campaign_ids, loader.DEFAULT_GROUP_BY)
    )

    reconstructed_failed_batch_ids = (
        cpc_batches[stop_batch_index]
        if 0 <= stop_batch_index < len(cpc_batches)
        else []
    )
    completed_units = loader.sum_campaign_units_for_batches(cpc_batches, completed_batch_indexes)
    pending_units = loader.sum_campaign_units_for_batches(cpc_batches, pending_batch_indexes)
    campaign_count = len(ordered_campaign_ids)
    expected_campaign_count = int(float(status_row.get("cpc_campaign_count") or 0))
    expected_completed_units = int(float(status_row.get("cpc_campaign_units_completed_total") or 0))
    expected_pending_units = int(float(status_row.get("cpc_campaign_units_pending_total") or 0))

    validations = {
        "campaign_count_matches": campaign_count == expected_campaign_count == int(run_evidence.get("campaign_count") or campaign_count),
        "totals_match": completed_units == expected_completed_units and pending_units == expected_pending_units,
        "failed_batch_ids_match": list(reconstructed_failed_batch_ids) == list(failed_batch_ids),
        "total_batches_matches": total_batches == int(run_evidence.get("total_batches") or total_batches),
        "campaign_list_hash_matches": (
            not run_evidence.get("campaign_list_hash")
            or loader.compute_campaign_list_hash(ordered_campaign_ids) == run_evidence.get("campaign_list_hash")
        ),
    }
    validation_result = all(validations.values())

    progress_context = client.build_cpc_progress_context(
        target_date,
        target_date,
        batch_size,
        ordered_campaign_ids,
        loader.DEFAULT_GROUP_BY,
        selection_mode="complete",
        campaign_scope=loader.DEFAULT_CAMPAIGN_SCOPE,
    )
    progress_payload = {
        "date_from": progress_context["date_from"],
        "date_to": progress_context["date_to"],
        "account_signature": client.account_signature,
        "group_by": progress_context["group_by"],
        "batch_size": progress_context["batch_size"],
        "campaign_hash": progress_context["campaign_hash"],
        "campaign_list_hash": progress_context.get("campaign_list_hash"),
        "ordered_campaign_ids": list(progress_context.get("ordered_campaign_ids") or []),
        "total_campaigns": progress_context["total_campaigns"],
        "selection_mode": "complete",
        "campaign_scope": loader.DEFAULT_CAMPAIGN_SCOPE,
        "total_batches": total_batches,
        "completed_batch_indexes": completed_batch_indexes,
        "pending_batch_indexes": pending_batch_indexes,
        "failed_429_batch_indexes": failed_429_batch_indexes,
        "completed_batches": len(completed_batch_indexes),
        "pending_batches": len(pending_batch_indexes),
        "failed_429_batches": len(failed_429_batch_indexes),
        "next_batch_index": stop_batch_index,
        "updated_at": loader.to_iso(loader.utcnow()),
    }

    return {
        "target_date": target_date,
        "status": "validated" if validation_result else "reconstruction_unsafe",
        "validation_result": validation_result,
        "validation_checks": validations,
        "account_signature": client.account_signature,
        "run_status": status_row.get("run_status"),
        "cpc_status": status_row.get("cpc_status"),
        "cpo_status": status_row.get("cpo_status"),
        "reconstructed_progress_key": reconstructed_progress_key,
        "campaign_count": campaign_count,
        "batch_size": batch_size,
        "total_batches": total_batches,
        "completed_batches": len(completed_batch_indexes),
        "pending_batches": len(pending_batch_indexes),
        "completed_units": completed_units,
        "pending_units": pending_units,
        "first_pending_batch_index": stop_batch_index,
        "first_pending_batch_ids": reconstructed_failed_batch_ids,
        "reconstruction_source_strategy": source_strategy,
        "run_evidence": run_evidence,
        "progress_payload": progress_payload,
        "db_writes": 0,
    }


def write_reconstructed_progress(plan):
    progress_key = plan["reconstructed_progress_key"]
    row = loader.build_state_row(
        state_key=loader.build_db_state_key("cpc_progress", progress_key),
        state_type="cpc_progress",
        payload=plan["progress_payload"],
        account_signature=plan["account_signature"],
        expires_at=None,
    )
    loader.supabase.table(loader.PIPELINE_RUNTIME_STATE_TABLE).upsert(
        row,
        on_conflict="state_key",
    ).execute()
    return {
        "status": "written",
        "reconstructed_progress_key": progress_key,
        "db_writes": 1,
    }


def make_parser():
    parser = argparse.ArgumentParser(
        description="Reconstruct missing Ozon CPC progress for safe pending-only recovery."
    )
    parser.add_argument("--date", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--approve-cpc-progress-reconstruction", action="store_true")
    return parser


def main():
    args = make_parser().parse_args()
    if args.write and not args.approve_cpc_progress_reconstruction:
        raise RuntimeError(
            "Reconstruction write requires explicit --write --approve-cpc-progress-reconstruction"
        )

    plan = reconstruct_progress_plan(args.date)
    print("Ozon CPC progress reconstruction plan:")
    printable = dict(plan)
    printable.pop("progress_payload", None)
    print(json.dumps(loader.sanitize_value(printable), ensure_ascii=False, indent=2))

    if not args.write:
        return

    if not plan.get("validation_result"):
        raise RuntimeError("Reconstruction validation did not pass")

    result = write_reconstructed_progress(plan)
    print("Ozon CPC progress reconstruction result:")
    print(json.dumps(loader.sanitize_value(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
