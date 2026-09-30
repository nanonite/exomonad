use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use crate::domain::AgentName;
use crate::services::agent_control::{
    capture_codex_trust_for_disposal, release_captured_codex_trusts, CapturedCodexTrust,
};
use crate::services::agent_resolver::AgentIdentityRecord;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use tokio::fs;

impl VerifiedCleanupService {
    pub(super) async fn execute_destructive_actions(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
    ) -> CleanupReceiptEntry {
        let mut authorization_actions = receipt.entries[index].actions.clone();
        if candidate.allow_no_pr && candidate.pull_request.is_none() {
            authorization_actions.push("allow_no_pr_override".to_string());
        }
        if candidate.discard_dirty && candidate.dirty == Some(true) {
            authorization_actions.push("record_dirty_evidence".to_string());
        }
        if candidate.preserve_unique_commits {
            authorization_actions.push("preserve_unique_commits".to_string());
        }
        if candidate.delete_remote_branch {
            authorization_actions.push("delete_remote_branch_override".to_string());
        }
        // Claimed before anything is removed: the generated `.codex/config.toml`
        // that proves which `[hooks.state]` keys are ExoMonad's lives inside the
        // worktree or agent directory this cleanup is about to destroy. Recording
        // it here makes the very first receipt of the run carry the claim, so the
        // release can be retried from evidence after the config that justified it
        // is gone.
        claim_codex_trust_for_candidate(candidate, &mut authorization_actions, receipt, index);
        authorization_actions.sort();
        authorization_actions.dedup();
        if let Some(entry) = self
            .persist_action_or_failure(candidate, receipt, index, &authorization_actions)
            .await
        {
            return entry;
        }
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
        // The worktree and the agent directory are both provably gone, so this is
        // a permanent disposal and the hook trust they justified must go too. It
        // runs before deregistration on purpose: a release that fails has to
        // leave the entry `Failed` with a retryable claim, not a deregistered
        // entry that a later run would reconcile into a clean success.
        if let Some(entry) = self
            .release_codex_trust(candidate, receipt, index, &mut actions)
            .await
        {
            return entry;
        }
        if let Some(entry) = self
            .cleanup_ephemeral_registrations(candidate, receipt, index, &mut actions)
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
        let codex_trust = receipt.entries[index].codex_trust.clone();
        let mut entry = receipt_entry(candidate, CleanupReceiptStatus::Cleaned, actions, None);
        entry.branch = branch;
        entry.codex_trust = codex_trust;
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
            return refused_with_progress(
                candidate,
                receipt,
                index,
                "resolver-only cleanup lacks a matching in-progress receipt",
            );
        }
        if path_exists(&candidate.agent_dir).await
            || candidate
                .worktree_path
                .as_ref()
                .is_some_and(|path| path_exists_sync(path))
        {
            return refused_with_progress(
                candidate,
                receipt,
                index,
                "managed resource reappeared since planning",
            );
        }
        if self.resolver.get(&expected.agent_name).await.as_ref() != Some(expected) {
            return refused_with_progress(
                candidate,
                receipt,
                index,
                "resolver identity changed since planning",
            );
        }
        if receipt.entries[index].identity_snapshot.as_ref() != Some(expected)
            || !receipt_entry_identity_is_coherent(&receipt.entries[index], expected)
        {
            return refused_with_progress(
                candidate,
                receipt,
                index,
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
        // The resources are already proven absent here, so this is the verified
        // disposal point. The claim comes from the resumed receipt: the generated
        // config died with the directory on the interrupted attempt.
        if let Some(entry) = self
            .cleanup_ephemeral_registrations(candidate, receipt, index, &mut actions)
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
            .release_codex_trust(candidate, receipt, index, &mut actions)
            .await
        {
            return entry;
        }
        let codex_trust = receipt.entries[index].codex_trust.clone();
        let mut entry = receipt_entry(candidate, CleanupReceiptStatus::Cleaned, actions, None);
        entry.codex_trust = codex_trust;
        entry
    }

    /// Releases the ExoMonad Codex hook trust claimed for this candidate.
    ///
    /// Called only from the point where the worktree and the agent directory are
    /// both provably gone. A release that cannot finish fails the entry instead
    /// of reporting plain success, and the claim stays in the receipt so the next
    /// attempt can retry it — the trust keys ExoMonad owns are already proven, so
    /// there is nothing left to re-derive once the generated config is gone.
    ///
    /// The action is recorded on the caller's list rather than persisted here:
    /// the caller writes the next progress record, and the claim that makes a
    /// retry possible was persisted before the first removal.
    async fn release_codex_trust(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
        actions: &mut Vec<String>,
    ) -> Option<CleanupReceiptEntry> {
        let Some(captured) = receipt.entries[index]
            .codex_trust
            .clone()
            .filter(|claims| !claims.is_empty())
        else {
            actions.push(CODEX_TRUST_ABSENT.to_string());
            return None;
        };
        let batch = release_trust_claims(captured).await;
        for release in &batch.released {
            tracing::info!(
                candidate = %candidate.agent_name,
                config = %release.hook_trust,
                "Released ExoMonad Codex trust for a permanently disposed agent"
            );
        }
        if batch.is_complete() {
            actions.push(RELEASE_CODEX_TRUST.to_string());
            return None;
        }
        actions.push(CODEX_TRUST_RELEASE_FAILED.to_string());
        // The failure entry has to name the action that failed and keep the
        // claim that makes the retry possible.
        receipt.entries[index].actions = actions.clone();
        Some(failed(
            candidate,
            receipt,
            index,
            format!(
                "release ExoMonad Codex trust: {}",
                batch.failures.join("; ")
            ),
        ))
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
        let worktree_present = path_exists(worktree).await;
        if worktree_present {
            if candidate.recovered_provenance.is_some() {
                if let Err(error) = self.revalidate_recovered_residual(candidate).await {
                    return Some(failed(
                        candidate,
                        receipt,
                        index,
                        format!("revalidate recovered residual: {error}"),
                    ));
                }
                match fs::remove_dir_all(worktree).await {
                    Ok(()) => actions.push("remove_recovered_residual".to_string()),
                    Err(error) => {
                        return Some(failed(
                            candidate,
                            receipt,
                            index,
                            format!("remove recovered residual: {error}"),
                        ));
                    }
                }
            } else {
                let git_worktree = self.git_worktree.clone();
                let path = worktree.clone();
                match tokio::task::spawn_blocking(move || git_worktree.remove_workspace(&path))
                    .await
                {
                    Ok(Ok(())) => {
                        actions.push("remove_worktree".to_string());
                        if candidate.discard_dirty && candidate.dirty == Some(true) {
                            actions.push("discard_dirty_changes".to_string());
                        }
                        if candidate.delete_remote_branch {
                            actions.push("delete_remote_branch_override".to_string());
                        }
                    }
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

    async fn cleanup_ephemeral_registrations(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
        actions: &mut Vec<String>,
    ) -> Option<CleanupReceiptEntry> {
        let identity = candidate.identity.as_ref()?;
        let keys = [
            identity.agent_name.as_str(),
            identity.slug.as_str(),
            identity.birth_branch.as_str(),
            identity.parent_branch.as_str(),
        ];
        if let Err(reason) = self.remove_synthetic_members(&keys, identity).await {
            return Some(failed(candidate, receipt, index, reason));
        }
        self.deregister_ephemeral_registrations(&keys, identity)
            .await;
        actions.push("remove_ephemeral_registrations".to_string());
        self.persist_action_or_failure(candidate, receipt, index, actions)
            .await
    }

    async fn remove_synthetic_members(
        &self,
        keys: &[&str; 4],
        identity: &AgentIdentityRecord,
    ) -> Result<(), String> {
        let mut teams = BTreeSet::new();
        for key in keys {
            if let Some(info) = self.team_registry.get(key).await {
                teams.insert(info.team_name);
            }
        }
        for team_name in teams {
            let team_name = crate::domain::TeamName::try_from_str(&team_name)
                .map_err(|error| format!("remove synthetic member: invalid team name: {error}"))?;
            crate::services::synthetic_members::remove_synthetic_member(
                &team_name,
                &identity.agent_name,
            )
            .map_err(|error| format!("remove synthetic member: {error}"))?;
        }
        Ok(())
    }

    async fn deregister_ephemeral_registrations(
        &self,
        keys: &[&str; 4],
        identity: &AgentIdentityRecord,
    ) {
        for key in keys {
            self.team_registry.deregister(key).await;
            self.claude_session_registry.deregister(key).await;
        }
        self.supervisor_registry
            .deregister(&[identity.birth_branch.to_string()])
            .await;
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

/// Records the ExoMonad Codex hook trust this cleanup owns, before the first
/// removal, onto the caller's action list and the receipt entry.
///
/// A claim that cannot be proven is recorded as such and the disposal continues:
/// a hand-written `.codex/config.toml` must not be able to block a legitimate
/// cleanup, and the Codex user config is never edited on a guess.
fn claim_codex_trust_for_candidate(
    candidate: &CleanupCandidate,
    actions: &mut Vec<String>,
    receipt: &mut CleanupReceipt,
    index: usize,
) {
    match capture_trust_claims(codex_configured_dirs(candidate)) {
        Ok(claims) if claims.is_empty() => actions.push(CODEX_TRUST_ABSENT.to_string()),
        Ok(claims) => {
            actions.push(CAPTURE_CODEX_TRUST.to_string());
            receipt.entries[index].codex_trust = Some(claims);
        }
        Err(error) => {
            tracing::warn!(
                candidate = %candidate.agent_name,
                %error,
                "Could not claim the ExoMonad Codex hook trust for this candidate; disposal \
                 continues and the Codex user config is left untouched"
            );
            actions.push(CODEX_TRUST_CAPTURE_FAILED.to_string());
        }
    }
}

/// Claims the ExoMonad Codex hook trust for the directories that hold a
/// generated config.
///
/// Synchronous on purpose: it runs on the single-threaded critical section that
/// records this cleanup's authorization, and it reads at most one small
/// generated config per candidate directory.
fn capture_trust_claims(dirs: Vec<PathBuf>) -> std::io::Result<Vec<CapturedCodexTrust>> {
    if dirs.is_empty() {
        return Ok(Vec::new());
    }
    let refs: Vec<&Path> = dirs.iter().map(PathBuf::as_path).collect();
    capture_codex_trust_for_disposal(&refs)
}

/// Releases captured Codex trust off the async runtime, because the release
/// takes an exclusive lock on the Codex user config.
async fn release_trust_claims(
    captured: Vec<CapturedCodexTrust>,
) -> crate::services::agent_control::CodexTrustReleaseBatch {
    tokio::task::spawn_blocking(move || release_captured_codex_trusts(&captured))
        .await
        .unwrap_or_else(
            |error| crate::services::agent_control::CodexTrustReleaseBatch {
                released: Vec::new(),
                failures: vec![format!("Codex trust release task failed: {error}")],
            },
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
    // The captured claim is the retry evidence for a failed release, so a
    // failure must never drop it.
    entry.codex_trust = receipt.entries[index].codex_trust.clone();
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
            recovered_provenance: None,
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
            dirty_evidence: None,
            protected: false,
            identity_drift: false,
            identity_error: None,
            head_matches_pull_request: None,
            remote_head_matches_pull_request: None,
            identity: None,
            branch: None,
            delete_remote_branch: false,
            allow_no_pr: false,
            discard_dirty: false,
            preserve_unique_commits: false,
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
            operator_reason: None,
            preserve_unique_commits: false,
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
