use super::receipt_support::receipt_entry;
use super::service::VerifiedCleanupService;
use super::support::PROGRESS_PERSISTENCE_FAILURE;
use super::types::*;
use anyhow::Result;

impl VerifiedCleanupService {
    pub(super) async fn set_branch_pending(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
        local: bool,
    ) -> Option<CleanupReceiptEntry> {
        let Some(branch) = receipt.entries[index].branch.as_mut() else {
            return Some(self.failed_branch(
                candidate,
                receipt,
                index,
                "branch evidence is unavailable",
            ));
        };
        branch_action_mut(branch, local).status = CleanupBranchActionStatus::DeletePending;
        branch_action_mut(branch, local).reason = None;
        receipt.entries[index]
            .actions
            .push(pending_action_name(local).to_string());
        self.persist_branch_progress(candidate, receipt, index)
            .await
    }

    pub(super) async fn finish_branch_action(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
        local: bool,
        status: CleanupBranchActionStatus,
        reason: Option<String>,
    ) -> Option<CleanupReceiptEntry> {
        let Some(branch) = receipt.entries[index].branch.as_mut() else {
            return Some(self.failed_branch(
                candidate,
                receipt,
                index,
                "branch evidence is unavailable",
            ));
        };
        let action = branch_action_mut(branch, local);
        action.status = status.clone();
        action.reason = reason;
        let Some(name) = completed_action_name(local, &status) else {
            return Some(self.failed_branch(
                candidate,
                receipt,
                index,
                "invalid branch action state",
            ));
        };
        receipt.entries[index]
            .actions
            .retain(|action| !action.ends_with("_branch_pending"));
        receipt.entries[index].actions.push(name.to_string());
        self.persist_branch_progress(candidate, receipt, index)
            .await
    }

    async fn persist_branch_progress(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
    ) -> Option<CleanupReceiptEntry> {
        let actions = receipt.entries[index].actions.clone();
        self.record_progress(receipt, index, candidate, &actions)
            .await
            .err()
            .map(|error| {
                self.failed_branch(
                    candidate,
                    receipt,
                    index,
                    format!("{PROGRESS_PERSISTENCE_FAILURE}: {error}"),
                )
            })
    }

    pub(super) async fn finish_absent_branch(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
        local: bool,
    ) -> Option<CleanupReceiptEntry> {
        let kind = if local { "local" } else { "remote" };
        self.finish_branch_action(
            candidate,
            receipt,
            index,
            local,
            CleanupBranchActionStatus::AlreadyAbsent,
            Some(format!("managed {kind} branch is already absent")),
        )
        .await
    }

    pub(super) async fn finish_delete_result(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
        local: bool,
        result: Result<()>,
    ) -> Option<CleanupReceiptEntry> {
        match result {
            Ok(()) => {
                self.finish_branch_action(
                    candidate,
                    receipt,
                    index,
                    local,
                    CleanupBranchActionStatus::Deleted,
                    None,
                )
                .await
            }
            Err(error) => {
                Some(self.refuse_branch(candidate, receipt, index, local, error.to_string()))
            }
        }
    }

    pub(super) fn refuse_branch(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
        local: bool,
        reason: impl Into<String>,
    ) -> CleanupReceiptEntry {
        let refusal_reason = reason.into();
        if let Some(branch) = receipt.entries[index].branch.as_mut() {
            let action = branch_action_mut(branch, local);
            action.status = CleanupBranchActionStatus::Refused;
            action.reason = Some(refusal_reason.clone());
        }
        let branch = receipt.entries[index].branch.clone();
        let reason = branch
            .as_ref()
            .and_then(|evidence| branch_action(evidence, local).reason.clone())
            .or(Some(refusal_reason));
        let mut entry = receipt_entry(
            candidate,
            CleanupReceiptStatus::Refused,
            receipt.entries[index].actions.clone(),
            reason,
        );
        entry.branch = branch;
        entry
    }

    pub(super) fn failed_branch(
        &self,
        candidate: &CleanupCandidate,
        receipt: &CleanupReceipt,
        index: usize,
        reason: impl Into<String>,
    ) -> CleanupReceiptEntry {
        let mut entry = receipt_entry(
            candidate,
            CleanupReceiptStatus::Failed,
            receipt.entries[index].actions.clone(),
            Some(reason.into()),
        );
        entry.branch = receipt.entries[index].branch.clone();
        entry
    }
}

pub(super) fn branch_action_is_pending_or_planned(
    status: Option<&CleanupBranchActionStatus>,
) -> bool {
    matches!(
        status,
        Some(CleanupBranchActionStatus::WouldDelete | CleanupBranchActionStatus::DeletePending)
    )
}

fn branch_action_mut(
    evidence: &mut CleanupBranchEvidence,
    local: bool,
) -> &mut CleanupBranchAction {
    if local {
        &mut evidence.local
    } else {
        &mut evidence.remote
    }
}

fn branch_action(evidence: &CleanupBranchEvidence, local: bool) -> &CleanupBranchAction {
    if local {
        &evidence.local
    } else {
        &evidence.remote
    }
}

fn pending_action_name(local: bool) -> &'static str {
    if local {
        "delete_local_branch_pending"
    } else {
        "delete_remote_branch_pending"
    }
}

fn completed_action_name(local: bool, status: &CleanupBranchActionStatus) -> Option<&'static str> {
    match (local, status) {
        (true, CleanupBranchActionStatus::Deleted) => Some("delete_local_branch"),
        (false, CleanupBranchActionStatus::Deleted) => Some("delete_remote_branch"),
        (true, CleanupBranchActionStatus::AlreadyAbsent) => Some("local_branch_already_absent"),
        (false, CleanupBranchActionStatus::AlreadyAbsent) => Some("remote_branch_already_absent"),
        _ => None,
    }
}
