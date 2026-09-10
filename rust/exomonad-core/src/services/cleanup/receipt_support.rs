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
        pull_request: candidate.pull_request.clone(),
        branch: candidate.branch.clone(),
        status,
        actions,
        reason,
        dirty_evidence: candidate.dirty_evidence.clone(),
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
        operator_reason: plan.operator_reason.clone(),
        preserve_unique_commits: plan.preserve_unique_commits,
        entries: plan.candidates.iter().map(dry_run_entry).collect(),
    }
}

fn dry_run_entry(candidate: &CleanupCandidate) -> CleanupReceiptEntry {
    let actions = if candidate.decision.is_cleanable() {
        let mut actions = Vec::new();
        append_branch_preview(&mut actions, candidate.branch.as_ref());
        if candidate.preserve_unique_commits {
            actions.push("preserve_unique_commits".to_string());
        }
        if candidate.allow_no_pr && candidate.pull_request.is_none() {
            actions.push("allow_no_pr_override".to_string());
        }
        if candidate.discard_dirty && candidate.dirty == Some(true) {
            actions.push("record_dirty_evidence".to_string());
        }
        if candidate.delete_remote_branch {
            actions.push("delete_remote_branch_override".to_string());
        }
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

fn append_branch_preview(actions: &mut Vec<String>, branch: Option<&CleanupBranchEvidence>) {
    let Some(branch) = branch else {
        return;
    };
    if matches!(branch.local.status, CleanupBranchActionStatus::WouldDelete) {
        actions.push("would_delete_local_branch".to_string());
    }
    if matches!(branch.remote.status, CleanupBranchActionStatus::WouldDelete) {
        actions.push("would_delete_remote_branch".to_string());
    }
}

pub(super) fn in_progress_receipt(plan: &CleanupPlan, started_at: u64) -> CleanupReceipt {
    CleanupReceipt {
        schema_version: CLEANUP_RECEIPT_SCHEMA_VERSION,
        operation_id: Uuid::new_v4().to_string(),
        plan_id: plan.plan_id.clone(),
        started_at,
        finished_at: 0,
        dry_run: false,
        operator_reason: plan.operator_reason.clone(),
        preserve_unique_commits: plan.preserve_unique_commits,
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
