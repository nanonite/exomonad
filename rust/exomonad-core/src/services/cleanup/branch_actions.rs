use super::branch_preparation::{LocalBranchDeletion, RemoteBranchDeletion};
use super::branch_receipts::branch_action_is_pending_or_planned;
use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use anyhow::{Context, Result};

impl VerifiedCleanupService {
    pub(super) async fn execute_remote_branch_action(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
    ) -> Option<CleanupReceiptEntry> {
        if remote_deletion_was_proven(receipt, index) {
            return None;
        }
        if matches!(
            receipt.entries[index]
                .branch
                .as_ref()
                .map(|branch| &branch.remote.status),
            Some(CleanupBranchActionStatus::Deleted)
        ) {
            return Some(self.refuse_branch(
                candidate,
                receipt,
                index,
                false,
                "cleanup receipt marks remote deletion complete without exact remote, ref, and SHA evidence",
            ));
        }
        if !remote_action_requested(candidate, receipt, index) {
            return None;
        }
        let expected = match remote_expected_sha(candidate) {
            Ok(expected) => expected,
            Err(error) => {
                return Some(self.refuse_branch(
                    candidate,
                    receipt,
                    index,
                    false,
                    error.to_string(),
                ))
            }
        };
        if matches!(
            receipt.entries[index]
                .branch
                .as_ref()
                .map(|branch| &branch.remote.status),
            Some(CleanupBranchActionStatus::AlreadyAbsent)
        ) {
            return Some(self.refuse_branch(
                candidate,
                receipt,
                index,
                false,
                "remote branch is absent and no durable receipt proves a prior deletion at the verified SHA",
            ));
        }
        if let Some(entry) = self
            .set_branch_pending(candidate, receipt, index, false)
            .await
        {
            return Some(entry);
        }
        let prepared = self
            .prepare_remote_branch_deletion(candidate, expected)
            .await;
        self.complete_remote_action(candidate, receipt, index, expected, prepared)
            .await
    }

    async fn complete_remote_action(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
        expected: &str,
        prepared: Result<RemoteBranchDeletion>,
    ) -> Option<CleanupReceiptEntry> {
        let branch = match prepared {
            Ok(RemoteBranchDeletion::Present(branch)) => branch,
            Ok(RemoteBranchDeletion::AlreadyAbsent) => {
                return Some(self.refuse_branch(
                    candidate,
                    receipt,
                    index,
                    false,
                    "remote branch disappeared before its exact lease could be exercised",
                ))
            }
            Err(error) => {
                return Some(self.refuse_branch(
                    candidate,
                    receipt,
                    index,
                    false,
                    error.to_string(),
                ))
            }
        };
        let result = delete_remote_branch_with_lease(
            &self.project_dir,
            &branch.repository.remote_name,
            &branch.branch,
            expected,
        )
        .await;
        self.finish_delete_result(candidate, receipt, index, false, result)
            .await
    }

    pub(super) async fn execute_local_branch_action(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
    ) -> Option<CleanupReceiptEntry> {
        if !local_action_requested(receipt, index) {
            return None;
        }
        if worktree_exists(candidate) {
            return Some(self.refuse_branch(
                candidate,
                receipt,
                index,
                true,
                "managed worktree must be removed before deleting its local branch",
            ));
        }
        let Some(expected) = candidate.local_head_sha.as_deref() else {
            return self
                .finish_absent_branch(candidate, receipt, index, true)
                .await;
        };
        if let Some(entry) = self
            .set_branch_pending(candidate, receipt, index, true)
            .await
        {
            return Some(entry);
        }
        let prepared = self
            .prepare_local_branch_deletion(candidate, expected)
            .await;
        self.complete_local_action(candidate, receipt, index, expected, prepared)
            .await
    }

    async fn complete_local_action(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
        expected: &str,
        prepared: Result<LocalBranchDeletion>,
    ) -> Option<CleanupReceiptEntry> {
        let branch = match prepared {
            Ok(LocalBranchDeletion::Present(branch)) => branch,
            Ok(LocalBranchDeletion::AlreadyAbsent) => {
                return self
                    .finish_absent_branch(candidate, receipt, index, true)
                    .await
            }
            Err(error) => {
                return Some(self.refuse_branch(candidate, receipt, index, true, error.to_string()))
            }
        };
        let result = delete_local_branch(&self.project_dir, &branch.branch, expected).await;
        self.finish_delete_result(candidate, receipt, index, true, result)
            .await
    }
}

fn worktree_exists(candidate: &CleanupCandidate) -> bool {
    candidate
        .worktree_path
        .as_ref()
        .is_some_and(|path| path_exists_sync(path))
}

fn remote_expected_sha(candidate: &CleanupCandidate) -> Result<&str> {
    let pull_request_head = candidate
        .pull_request
        .as_ref()
        .and_then(|pull_request| pull_request.head_sha.as_deref())
        .filter(|sha| !sha.is_empty());
    let no_pr_remote_head = (candidate.allow_no_pr && candidate.pull_request.is_none())
        .then_some(candidate.remote_head_sha.as_deref())
        .flatten()
        .filter(|sha| !sha.is_empty());
    pull_request_head
        .or(no_pr_remote_head)
        .context(
            "remote deletion requires an authoritative pull-request head SHA or verified no-PR remote head",
        )
}

fn remote_action_requested(
    candidate: &CleanupCandidate,
    receipt: &CleanupReceipt,
    index: usize,
) -> bool {
    candidate.delete_remote_branch
        && matches!(
            receipt.entries[index]
                .branch
                .as_ref()
                .map(|branch| &branch.remote.status),
            Some(
                CleanupBranchActionStatus::WouldDelete
                    | CleanupBranchActionStatus::DeletePending
                    | CleanupBranchActionStatus::AlreadyAbsent
            )
        )
}

fn remote_deletion_was_proven(receipt: &CleanupReceipt, index: usize) -> bool {
    let entry = &receipt.entries[index];
    let Some(branch) = entry.branch.as_ref() else {
        return false;
    };
    branch.remote.status == CleanupBranchActionStatus::Deleted
        && entry
            .actions
            .iter()
            .any(|action| action == "delete_remote_branch")
        && branch.remote_name.is_some()
        && branch.remote_branch.is_some()
        && branch.remote_head_sha.is_some()
}

fn local_action_requested(receipt: &CleanupReceipt, index: usize) -> bool {
    branch_action_is_pending_or_planned(
        receipt.entries[index]
            .branch
            .as_ref()
            .map(|branch| &branch.local.status),
    )
}
