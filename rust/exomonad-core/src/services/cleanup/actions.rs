use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use crate::domain::AgentName;
use crate::services::agent_resolver::AgentIdentityRecord;
use std::path::Path;
use tokio::fs;

impl VerifiedCleanupService {
    pub(super) async fn execute_destructive_actions(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
    ) -> CleanupReceiptEntry {
        if let Some(entry) = self
            .execute_remote_branch_action(candidate, receipt, index)
            .await
        {
            return entry;
        }
        let mut actions = receipt.entries[index].actions.clone();
        if let Some(entry) = self
            .remove_worktree(candidate, receipt, index, &mut actions)
            .await
        {
            return entry;
        }
        if let Some(entry) = self
            .execute_local_branch_action(candidate, receipt, index)
            .await
        {
            return entry;
        }
        actions = receipt.entries[index].actions.clone();
        if let Some(entry) = self
            .remove_agent_directory(candidate, receipt, index, &mut actions)
            .await
        {
            return entry;
        }
        if let Some(entry) = self
            .deregister_identity(candidate, receipt, index, &mut actions)
            .await
        {
            return entry;
        }
        let branch = receipt.entries[index].branch.clone();
        let mut entry = receipt_entry(candidate, CleanupReceiptStatus::Cleaned, actions, None);
        entry.branch = branch;
        entry
    }

    pub(super) async fn execute_resolver_only(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
        expected: &AgentIdentityRecord,
    ) -> CleanupReceiptEntry {
        if !candidate.recovery_receipt {
            return refused(
                candidate,
                "resolver-only cleanup lacks a matching in-progress receipt",
            );
        }
        if path_exists(&candidate.agent_dir).await
            || candidate
                .worktree_path
                .as_ref()
                .is_some_and(|path| path_exists_sync(path))
        {
            return refused(candidate, "managed resource reappeared since planning");
        }
        if self.resolver.get(&expected.agent_name).await.as_ref() != Some(expected) {
            return refused(candidate, "resolver identity changed since planning");
        }
        if receipt.entries[index].identity_snapshot.as_ref() != Some(expected)
            || !receipt_entry_identity_is_coherent(&receipt.entries[index], expected)
        {
            return refused(
                candidate,
                "cleanup receipt identity differs from the resolver",
            );
        }
        let mut actions = receipt.entries[index].actions.clone();
        if actions.is_empty() {
            actions.push("managed_resources_already_absent".to_string());
        }
        if !actions.iter().any(|action| action == DEREGISTER_PENDING) {
            actions.push(DEREGISTER_PENDING.to_string());
        }
        if let Some(entry) = self
            .persist_action_or_failure(candidate, receipt, index, &actions)
            .await
        {
            return entry;
        }
        if let Err(error) = self.resolver.deregister(&expected.agent_name).await {
            return failed(
                candidate,
                receipt,
                index,
                format!("deregister identity: {error}"),
            );
        }
        actions.retain(|action| action != DEREGISTER_PENDING);
        actions.push("deregister_identity".to_string());
        if let Some(entry) = self
            .persist_action_or_failure(candidate, receipt, index, &actions)
            .await
        {
            return entry;
        }
        receipt_entry(candidate, CleanupReceiptStatus::Cleaned, actions, None)
    }

    async fn remove_worktree(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
        actions: &mut Vec<String>,
    ) -> Option<CleanupReceiptEntry> {
        let Some(worktree) = &candidate.worktree_path else {
            return None;
        };
        if path_exists(worktree).await {
            let git_worktree = self.git_worktree.clone();
            let path = worktree.clone();
            match tokio::task::spawn_blocking(move || git_worktree.remove_workspace(&path)).await {
                Ok(Ok(())) => actions.push("remove_worktree".to_string()),
                Ok(Err(error)) => {
                    return Some(failed(
                        candidate,
                        receipt,
                        index,
                        format!("remove worktree: {error}"),
                    ))
                }
                Err(error) => {
                    return Some(failed(
                        candidate,
                        receipt,
                        index,
                        format!("remove worktree task: {error}"),
                    ));
                }
            }
        } else {
            actions.push("worktree_already_absent".to_string());
        }
        self.persist_action_or_failure(candidate, receipt, index, actions)
            .await
    }

    async fn remove_agent_directory(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
        actions: &mut Vec<String>,
    ) -> Option<CleanupReceiptEntry> {
        match fs::remove_dir_all(&candidate.agent_dir).await {
            Ok(()) => actions.push("remove_agent_directory".to_string()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                actions.push("agent_directory_already_absent".to_string());
            }
            Err(error) => {
                return Some(failed(
                    candidate,
                    receipt,
                    index,
                    format!("remove agent directory: {error}"),
                ));
            }
        }
        self.persist_action_or_failure(candidate, receipt, index, actions)
            .await
    }

    async fn deregister_identity(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
        actions: &mut Vec<String>,
    ) -> Option<CleanupReceiptEntry> {
        let Ok(agent_name) = AgentName::try_from_str(&candidate.agent_name) else {
            return Some(failed(
                candidate,
                receipt,
                index,
                "agent name became invalid",
            ));
        };
        if !actions.iter().any(|action| action == DEREGISTER_PENDING) {
            actions.push(DEREGISTER_PENDING.to_string());
        }
        if let Some(entry) = self
            .persist_action_or_failure(candidate, receipt, index, actions)
            .await
        {
            return Some(entry);
        }
        if let Err(error) = self.resolver.deregister(&agent_name).await {
            return Some(failed(
                candidate,
                receipt,
                index,
                format!("deregister identity: {error}"),
            ));
        }
        actions.retain(|action| action != DEREGISTER_PENDING);
        actions.push("deregister_identity".to_string());
        self.persist_action_or_failure(candidate, receipt, index, actions)
            .await
    }

    async fn persist_action_or_failure(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
        actions: &[String],
    ) -> Option<CleanupReceiptEntry> {
        self.record_progress(receipt, index, candidate, actions)
            .await
            .err()
            .map(|error| {
                failed(
                    candidate,
                    receipt,
                    index,
                    format!("{PROGRESS_PERSISTENCE_FAILURE}: {error}"),
                )
            })
    }
}

async fn path_exists(path: &Path) -> bool {
    fs::symlink_metadata(path).await.is_ok()
}

fn refused(candidate: &CleanupCandidate, reason: impl Into<String>) -> CleanupReceiptEntry {
    receipt_entry(
        candidate,
        CleanupReceiptStatus::Refused,
        Vec::new(),
        Some(reason.into()),
    )
}

fn failed(
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

fn receipt_entry_identity_is_coherent(
    entry: &CleanupReceiptEntry,
    identity: &AgentIdentityRecord,
) -> bool {
    entry.agent_name == identity.agent_name.as_str() && entry.agent_slug == identity.slug.as_str()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn candidate() -> CleanupCandidate {
        CleanupCandidate {
            id: "candidate".to_string(),
            managed: true,
            resolver_only: false,
            recovery_receipt: false,
            agent_name: "stale-codex".to_string(),
            issue: None,
            agent_dir: "agent".into(),
            worktree_path: None,
            local_branch: Some("main.stale".to_string()),
            local_head_sha: Some("a".repeat(40)),
            remote_branch: None,
            remote_head_sha: None,
            pull_request: None,
            liveness: CleanupLiveness::Dead,
            dirty: Some(false),
            protected: false,
            identity_drift: false,
            identity_error: None,
            head_matches_pull_request: None,
            remote_head_matches_pull_request: None,
            identity: None,
            branch: None,
            delete_remote_branch: false,
            decision: CleanupDecision::Cleanable,
        }
    }

    #[test]
    fn failure_after_branch_action_preserves_receipt_progress() {
        let candidate = candidate();
        let mut branch = CleanupBranchEvidence {
            branch: Some("main.stale".to_string()),
            local_head_sha: candidate.local_head_sha.clone(),
            ..CleanupBranchEvidence::default()
        };
        branch.local.status = CleanupBranchActionStatus::Deleted;
        let mut receipt = CleanupReceipt {
            schema_version: CLEANUP_RECEIPT_SCHEMA_VERSION,
            operation_id: "operation".to_string(),
            plan_id: "plan".to_string(),
            started_at: 0,
            finished_at: 0,
            dry_run: false,
            entries: vec![receipt_entry(
                &candidate,
                CleanupReceiptStatus::InProgress,
                vec!["delete_local_branch".to_string()],
                None,
            )],
        };
        receipt.entries[0].branch = Some(branch.clone());

        let entry = failed(&candidate, &receipt, 0, "remove agent directory failed");

        assert_eq!(entry.actions, vec!["delete_local_branch"]);
        assert_eq!(entry.branch, Some(branch));
        assert_eq!(entry.status, CleanupReceiptStatus::Failed);
    }
}
