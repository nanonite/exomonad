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
        if !candidate.delete_remote_branch {
            return None;
        }
        if matches!(
            receipt.entries[index]
                .branch
                .as_ref()
                .map(|branch| &branch.remote.status),
            Some(CleanupBranchActionStatus::Deleted)
        ) {
            return match self
                .verify_prior_remote_deletion(candidate, receipt, index)
                .await
            {
                Ok(()) => None,
                Err(error) => {
                    Some(self.refuse_branch(candidate, receipt, index, false, error.to_string()))
                }
            };
        }
        if !remote_action_requested(candidate, receipt, index) {
            return Some(self.refuse_branch(
                candidate,
                receipt,
                index,
                false,
                "remote deletion requested but verified remote branch evidence is unavailable",
            ));
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
        if let Err(error) = self.revalidate_liveness_and_worktree(candidate).await {
            return Some(self.refuse_branch(
                candidate,
                receipt,
                index,
                false,
                format!("state changed before remote deletion: {error}"),
            ));
        }
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

    async fn verify_prior_remote_deletion(
        &self,
        candidate: &CleanupCandidate,
        receipt: &CleanupReceipt,
        index: usize,
    ) -> Result<()> {
        let entry = &receipt.entries[index];
        let evidence = entry
            .branch
            .as_ref()
            .context("cleanup receipt is missing remote deletion evidence")?;
        if !entry
            .actions
            .iter()
            .any(|action| action == "delete_remote_branch")
        {
            anyhow::bail!("cleanup receipt is missing remote deletion intent");
        }
        let remote_name = evidence
            .remote_name
            .as_deref()
            .filter(|value| !value.is_empty())
            .context("cleanup receipt is missing the verified remote name")?;
        let remote_branch = evidence
            .remote_branch
            .as_deref()
            .or(evidence.branch.as_deref())
            .filter(|value| !value.is_empty())
            .context("cleanup receipt is missing the verified remote ref")?;
        let expected_sha = evidence
            .remote_head_sha
            .as_deref()
            .filter(|value| !value.is_empty())
            .context("cleanup receipt is missing the verified remote head SHA")?;
        let identity = candidate
            .identity
            .as_ref()
            .context("managed identity is unavailable while verifying prior deletion")?;
        if entry.identity_snapshot.as_ref() != Some(identity)
            || entry.agent_name != candidate.agent_name
            || entry.agent_slug != identity.slug.as_str()
        {
            anyhow::bail!("cleanup receipt identity differs from the current candidate");
        }
        let candidate_branch = candidate
            .branch
            .as_ref()
            .context("current candidate is missing branch evidence")?;
        if candidate_branch.remote_name.as_deref() != Some(remote_name) {
            anyhow::bail!("cleanup receipt remote differs from the current candidate");
        }
        let candidate_branch_name = candidate_branch
            .branch
            .as_deref()
            .or(candidate.local_branch.as_deref());
        if candidate_branch_name != Some(remote_branch) {
            anyhow::bail!("cleanup receipt ref differs from the current candidate");
        }
        if candidate_branch
            .remote_branch
            .as_deref()
            .is_some_and(|branch| branch != remote_branch)
        {
            anyhow::bail!("cleanup receipt remote ref differs from current branch evidence");
        }
        if candidate
            .pull_request
            .as_ref()
            .and_then(|pull_request| pull_request.head_sha.as_deref())
            .is_some_and(|head| head != expected_sha)
        {
            anyhow::bail!(
                "cleanup receipt expected SHA differs from current pull-request evidence"
            );
        }
        let current = self.revalidate_branch(candidate).await?;
        if current.repository.remote_name != remote_name || current.branch != remote_branch {
            anyhow::bail!("configured remote or ref changed since prior deletion");
        }
        if let Some(observed) =
            remote_branch_state(&self.project_dir, remote_name, remote_branch).await?
        {
            anyhow::bail!(
                "remote branch was recreated or moved after prior deletion (observed {observed})"
            );
        }
        Ok(())
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

fn local_action_requested(receipt: &CleanupReceipt, index: usize) -> bool {
    branch_action_is_pending_or_planned(
        receipt.entries[index]
            .branch
            .as_ref()
            .map(|branch| &branch.local.status),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::agent_resolver::AgentResolver;
    use crate::services::git_worktree::GitWorktreeService;
    use crate::services::mutex_registry::MutexRegistry;
    use std::path::PathBuf;
    use std::sync::Arc;

    #[tokio::test]
    async fn requested_remote_deletion_without_branch_evidence_is_refused() {
        let temp = tempfile::tempdir().unwrap();
        let service = VerifiedCleanupService::new(
            temp.path(),
            Arc::new(AgentResolver::load(temp.path().to_path_buf()).await),
            Arc::new(GitWorktreeService::new(temp.path().to_path_buf())),
            None,
            Arc::new(MutexRegistry::new()),
            None,
        );
        let candidate = CleanupCandidate {
            id: "missing-remote-evidence".to_string(),
            managed: true,
            resolver_only: false,
            recovery_receipt: false,
            recovered_provenance: None,
            agent_name: "missing-remote-evidence".to_string(),
            issue: None,
            agent_dir: temp.path().join(".exo/agents/missing-remote-evidence"),
            worktree_path: Some(temp.path().join(".exo/worktrees/missing-remote-evidence")),
            local_branch: None,
            local_head_sha: None,
            remote_branch: None,
            remote_head_sha: None,
            pull_request: None,
            liveness: CleanupLiveness::Dead,
            dirty: Some(false),
            dirty_evidence: None,
            protected: false,
            identity_drift: false,
            identity_error: None,
            head_matches_pull_request: None,
            remote_head_matches_pull_request: None,
            identity: None,
            branch: None,
            delete_remote_branch: true,
            allow_no_pr: true,
            discard_dirty: false,
            preserve_unique_commits: false,
            decision: CleanupDecision::Cleanable,
        };
        let mut receipt = CleanupReceipt {
            schema_version: CLEANUP_RECEIPT_SCHEMA_VERSION,
            operation_id: "missing-remote-evidence".to_string(),
            plan_id: "missing-remote-evidence".to_string(),
            started_at: 0,
            finished_at: 0,
            dry_run: false,
            operator_reason: None,
            preserve_unique_commits: false,
            entries: vec![receipt_entry(
                &candidate,
                CleanupReceiptStatus::InProgress,
                Vec::new(),
                None,
            )],
        };

        let entry = service
            .execute_remote_branch_action(&candidate, &mut receipt, 0)
            .await
            .expect("requested remote deletion must return a refusal");
        assert_eq!(entry.status, CleanupReceiptStatus::Refused);
        assert_eq!(
            entry.reason.as_deref(),
            Some("remote deletion requested but verified remote branch evidence is unavailable")
        );
    }

    #[test]
    fn recovered_provenance_uses_the_authoritative_pull_request_head() {
        let candidate = CleanupCandidate {
            id: "recovered".to_string(),
            managed: true,
            resolver_only: false,
            recovery_receipt: true,
            recovered_provenance: None,
            agent_name: "recovered".to_string(),
            issue: None,
            agent_dir: PathBuf::from(".exo/agents/recovered"),
            worktree_path: None,
            local_branch: Some("main.recovered".to_string()),
            local_head_sha: Some("local-sha".to_string()),
            remote_branch: Some("main.recovered".to_string()),
            remote_head_sha: None,
            pull_request: Some(CleanupPullRequest {
                number: 43,
                head_ref: "main.recovered".to_string(),
                base_ref: "main".to_string(),
                state: "closed".to_string(),
                merged: true,
                head_sha: Some("recovered-pr-head".to_string()),
                merge_commit_sha: Some("recovered-merge".to_string()),
                authoring_agent: None,
                birth_branch: None,
            }),
            liveness: CleanupLiveness::Dead,
            dirty: Some(false),
            dirty_evidence: None,
            protected: false,
            identity_drift: false,
            identity_error: None,
            head_matches_pull_request: Some(true),
            remote_head_matches_pull_request: Some(true),
            identity: None,
            branch: None,
            delete_remote_branch: true,
            allow_no_pr: false,
            discard_dirty: false,
            preserve_unique_commits: false,
            decision: CleanupDecision::Cleanable,
        };

        assert_eq!(
            remote_expected_sha(&candidate).unwrap(),
            "recovered-pr-head"
        );
    }
}
