use super::types::*;
use std::time::{SystemTime, UNIX_EPOCH};
use uuid::Uuid;

pub(super) fn receipt_entry(
    candidate: &CleanupCandidate,
    status: CleanupReceiptStatus,
    actions: Vec<String>,
    reason: Option<String>,
) -> CleanupReceiptEntry {
    CleanupReceiptEntry {
        candidate_id: candidate.id.clone(),
        agent_name: candidate.agent_name.clone(),
        agent_slug: candidate
            .identity
            .as_ref()
            .map(|identity| identity.slug.to_string())
            .unwrap_or_default(),
        identity_snapshot: candidate.identity.clone(),
        status,
        actions,
        reason,
    }
}

pub(super) fn dry_run_receipt(plan: &CleanupPlan) -> CleanupReceipt {
    CleanupReceipt {
        schema_version: CLEANUP_RECEIPT_SCHEMA_VERSION,
        operation_id: Uuid::new_v4().to_string(),
        plan_id: plan.plan_id.clone(),
        started_at: plan.generated_at,
        finished_at: unix_timestamp(),
        dry_run: true,
        entries: plan.candidates.iter().map(dry_run_entry).collect(),
    }
}

fn dry_run_entry(candidate: &CleanupCandidate) -> CleanupReceiptEntry {
    let actions = if candidate.decision.is_cleanable() {
        let mut actions = Vec::new();
        if candidate.worktree_path.is_some() && !candidate.resolver_only {
            actions.push("remove_worktree".to_string());
        }
        if !candidate.resolver_only {
            actions.push("remove_agent_directory".to_string());
        }
        actions.push("deregister_identity".to_string());
        actions
    } else {
        Vec::new()
    };
    let status = if candidate.decision.is_cleanable() {
        CleanupReceiptStatus::WouldClean
    } else {
        CleanupReceiptStatus::Refused
    };
    receipt_entry(
        candidate,
        status,
        actions,
        candidate.decision.reason().map(ToOwned::to_owned),
    )
}

pub(super) fn in_progress_receipt(plan: &CleanupPlan, started_at: u64) -> CleanupReceipt {
    CleanupReceipt {
        schema_version: CLEANUP_RECEIPT_SCHEMA_VERSION,
        operation_id: Uuid::new_v4().to_string(),
        plan_id: plan.plan_id.clone(),
        started_at,
        finished_at: 0,
        dry_run: false,
        entries: plan
            .candidates
            .iter()
            .map(|candidate| {
                let status = if candidate.decision.is_cleanable() {
                    CleanupReceiptStatus::InProgress
                } else {
                    CleanupReceiptStatus::Refused
                };
                receipt_entry(
                    candidate,
                    status,
                    Vec::new(),
                    candidate.decision.reason().map(ToOwned::to_owned),
                )
            })
            .collect(),
    }
}

pub(super) fn unix_timestamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
