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
                return self
                    .finish_absent_branch(candidate, receipt, index, false)
                    .await
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
        self.complete_local_action(candidate, receipt, index, prepared)
            .await
    }

    async fn complete_local_action(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
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
        let result = delete_local_branch(&self.project_dir, &branch.branch).await;
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
    candidate
        .pull_request
        .as_ref()
        .and_then(|pull_request| pull_request.head_sha.as_deref())
        .filter(|sha| !sha.is_empty())
        .context("remote deletion requires an authoritative pull-request head SHA")
}

fn remote_action_requested(
    candidate: &CleanupCandidate,
    receipt: &CleanupReceipt,
    index: usize,
) -> bool {
    candidate.delete_remote_branch
        && branch_action_is_pending_or_planned(
            receipt.entries[index]
                .branch
                .as_ref()
                .map(|branch| &branch.remote.status),
        )
}

fn local_action_requested(receipt: &CleanupReceipt, index: usize) -> bool {
    branch_action_is_pending_or_planned(
        receipt.entries[index]
            .branch
            .as_ref()
            .map(|branch| &branch.local.status),
    )
}
