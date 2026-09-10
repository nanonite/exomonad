use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use crate::services::agent_resolver::AgentIdentityRecord;
use anyhow::{bail, Context};
use tokio::fs;

impl VerifiedCleanupService {
    pub(super) async fn execute_candidate(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
    ) -> CleanupReceiptEntry {
        let Some(expected) = candidate.identity.as_ref() else {
            return refused_with_progress(
                candidate,
                receipt,
                index,
                "managed identity is missing or malformed",
            );
        };
        if candidate.resolver_only {
            return self
                .execute_resolver_only(candidate, receipt, index, expected)
                .await;
        }
        if candidate.recovered_provenance.is_some() {
            if let Err(error) = self.revalidate_recovered_residual(candidate).await {
                return refused_with_progress(candidate, receipt, index, error.to_string());
            }
            return self
                .execute_destructive_actions(candidate, receipt, index)
                .await;
        }
        let current = match self.load_current_identity(candidate).await {
            Ok(identity) => identity,
            Err(entry) => {
                return if matches!(entry.status, CleanupReceiptStatus::Refused) {
                    refused_with_progress(
                        candidate,
                        receipt,
                        index,
                        entry
                            .reason
                            .unwrap_or_else(|| "cleanup was refused".to_string()),
                    )
                } else {
                    entry
                }
            }
        };
        if let Some(entry) = self
            .validate_identity_and_liveness(candidate, expected, &current)
            .await
        {
            return refused_with_progress(
                candidate,
                receipt,
                index,
                entry
                    .reason
                    .unwrap_or_else(|| "cleanup was refused".to_string()),
            );
        }
        if let Some(entry) = self.validate_worktree(candidate, expected).await {
            return refused_with_progress(
                candidate,
                receipt,
                index,
                entry
                    .reason
                    .unwrap_or_else(|| "cleanup was refused".to_string()),
            );
        }
        self.execute_destructive_actions(candidate, receipt, index)
            .await
    }

    async fn load_current_identity(
        &self,
        candidate: &CleanupCandidate,
    ) -> Result<AgentIdentityRecord, CleanupReceiptEntry> {
        let metadata = match fs::symlink_metadata(&candidate.agent_dir).await {
            Ok(metadata) => metadata,
            Err(_) => return Err(skipped(candidate, "agent directory is already absent")),
        };
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(refused(
                candidate,
                "agent directory is not a real managed directory",
            ));
        }
        let identity_path = candidate.agent_dir.join("identity.json");
        let contents = match fs::read_to_string(&identity_path).await {
            Ok(contents) => contents,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Err(skipped(candidate, "agent identity is already absent"));
            }
            Err(error) => return Err(failed(candidate, format!("read identity: {error}"))),
        };
        serde_json::from_str(&contents)
            .map_err(|_| refused(candidate, "identity changed or is malformed"))
    }

    pub(super) async fn revalidate_liveness_and_worktree(
        &self,
        candidate: &CleanupCandidate,
    ) -> anyhow::Result<()> {
        if candidate.recovered_provenance.is_some() {
            return self.revalidate_recovered_residual(candidate).await;
        }
        let expected = candidate
            .identity
            .as_ref()
            .context("managed identity is unavailable")?;
        let current = match self.load_current_identity(candidate).await {
            Ok(identity) => identity,
            Err(entry) => bail!(
                "{}",
                entry
                    .reason
                    .unwrap_or_else(|| "managed identity could not be revalidated".to_string())
            ),
        };
        if let Some(entry) = self
            .validate_identity_and_liveness(candidate, expected, &current)
            .await
        {
            bail!(
                "{}",
                entry
                    .reason
                    .unwrap_or_else(|| "agent liveness could not be revalidated".to_string())
            );
        }
        if let Some(entry) = self.validate_worktree(candidate, expected).await {
            bail!(
                "{}",
                entry
                    .reason
                    .unwrap_or_else(|| "worktree could not be revalidated".to_string())
            );
        }
        Ok(())
    }

    async fn validate_identity_and_liveness(
        &self,
        candidate: &CleanupCandidate,
        expected: &AgentIdentityRecord,
        current: &AgentIdentityRecord,
    ) -> Option<CleanupReceiptEntry> {
        if current != expected {
            return Some(refused(candidate, "identity changed since planning"));
        }
        if self.resolver.get(&expected.agent_name).await.as_ref() != Some(expected) {
            return Some(refused(
                candidate,
                "resolver identity changed since planning",
            ));
        }
        (self.liveness(&candidate.agent_dir).await != CleanupLiveness::Dead)
            .then(|| refused(candidate, "agent is no longer provably dead"))
    }

    async fn validate_worktree(
        &self,
        candidate: &CleanupCandidate,
        expected: &AgentIdentityRecord,
    ) -> Option<CleanupReceiptEntry> {
        let Some(worktree) = &candidate.worktree_path else {
            return None;
        };
        if !worktree.exists() {
            return None;
        }
        if let Some(entry) = self
            .validate_worktree_contents(candidate, expected, worktree)
            .await
        {
            return Some(entry);
        }
        if let Some(entry) = self
            .validate_worktree_location(candidate, expected, worktree)
            .await
        {
            return Some(entry);
        }
        self.validate_local_branch(candidate, expected).await
    }

    async fn validate_worktree_contents(
        &self,
        candidate: &CleanupCandidate,
        expected: &AgentIdentityRecord,
        worktree: &std::path::Path,
    ) -> Option<CleanupReceiptEntry> {
        let status = match workspace_status(worktree).await {
            Ok(status) => status,
            Err(_) => return Some(refused(candidate, "worktree status is unavailable")),
        };
        if !status.porcelain.is_empty() {
            if !candidate.discard_dirty {
                return Some(refused(
                    candidate,
                    "worktree is dirty; discard_dirty authorization is required",
                ));
            }
            if candidate.dirty_evidence.as_ref() != Some(&status) {
                return Some(refused(candidate, "dirty worktree changed since planning"));
            }
        }
        match workspace_branch(worktree).await {
            Ok(Some(branch)) if branch == expected.birth_branch.as_str() => None,
            Ok(Some(_)) => Some(refused(candidate, "worktree branch changed since planning")),
            Ok(None) => Some(refused(candidate, "worktree is detached since planning")),
            Err(error) => Some(refused(candidate, format!("read worktree branch: {error}"))),
        }
    }

    async fn validate_worktree_location(
        &self,
        candidate: &CleanupCandidate,
        expected: &AgentIdentityRecord,
        worktree: &std::path::Path,
    ) -> Option<CleanupReceiptEntry> {
        let canonical = match fs::canonicalize(worktree).await {
            Ok(canonical) => canonical,
            Err(_) => return Some(refused(candidate, "worktree path changed since planning")),
        };
        if !path_within(&self.project_dir.join(".exo/worktrees"), &canonical) {
            return Some(refused(
                candidate,
                "worktree path is outside the managed root",
            ));
        }
        let expected_path = resolve_path(&self.project_dir, &expected.working_dir);
        let expected_canonical = match fs::canonicalize(expected_path).await {
            Ok(path) => path,
            Err(_) => return Some(refused(candidate, "expected worktree path is unavailable")),
        };
        (canonical != expected_canonical)
            .then(|| refused(candidate, "worktree path changed since planning"))
    }

    async fn validate_local_branch(
        &self,
        candidate: &CleanupCandidate,
        expected: &AgentIdentityRecord,
    ) -> Option<CleanupReceiptEntry> {
        match local_branch_state(&self.project_dir, expected.birth_branch.as_str()).await {
            Ok(Some(head)) if candidate.local_head_sha.as_deref() == Some(head.as_str()) => None,
            Ok(Some(_)) => Some(refused(
                candidate,
                "local branch head changed since planning",
            )),
            Ok(None) => Some(refused(
                candidate,
                "local branch disappeared since planning",
            )),
            Err(error) => Some(refused(candidate, format!("read local branch: {error}"))),
        }
    }
}

fn refused(candidate: &CleanupCandidate, reason: impl Into<String>) -> CleanupReceiptEntry {
    receipt_entry(
        candidate,
        CleanupReceiptStatus::Refused,
        Vec::new(),
        Some(reason.into()),
    )
}

fn skipped(candidate: &CleanupCandidate, reason: &str) -> CleanupReceiptEntry {
    receipt_entry(
        candidate,
        CleanupReceiptStatus::Skipped,
        vec!["already_absent".to_string()],
        Some(reason.to_string()),
    )
}

fn failed(candidate: &CleanupCandidate, reason: impl Into<String>) -> CleanupReceiptEntry {
    receipt_entry(
        candidate,
        CleanupReceiptStatus::Failed,
        Vec::new(),
        Some(reason.into()),
    )
}
