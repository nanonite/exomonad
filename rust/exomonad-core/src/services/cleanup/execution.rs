use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use crate::services::agent_resolver::AgentIdentityRecord;
use anyhow::{Context, Result};
use std::io::Write;
use tokio::fs;

impl VerifiedCleanupService {
    pub(super) async fn execute_plan(
        &self,
        plan: &CleanupPlan,
        receipt: &mut CleanupReceipt,
    ) -> Result<()> {
        for (index, candidate) in plan.candidates.iter().enumerate() {
            if !candidate.decision.is_cleanable() {
                receipt.entries[index] = receipt_entry(
                    candidate,
                    CleanupReceiptStatus::Refused,
                    Vec::new(),
                    candidate.decision.reason().map(ToOwned::to_owned),
                );
                continue;
            }
            receipt.entries[index] = receipt_entry(
                candidate,
                CleanupReceiptStatus::InProgress,
                Vec::new(),
                None,
            );
            self.persist_receipt(receipt).await?;
            let entry = self.execute_candidate(candidate, receipt, index).await;
            receipt.entries[index] = entry;
            self.persist_receipt(receipt).await?;
        }
        Ok(())
    }

    async fn execute_candidate(
        &self,
        candidate: &CleanupCandidate,
        receipt: &mut CleanupReceipt,
        index: usize,
    ) -> CleanupReceiptEntry {
        let Ok(agent_metadata) = fs::symlink_metadata(&candidate.agent_dir).await else {
            return receipt_entry(
                candidate,
                CleanupReceiptStatus::Skipped,
                vec!["already_absent".to_string()],
                Some("agent directory is already absent".to_string()),
            );
        };
        if !agent_metadata.file_type().is_dir() || agent_metadata.file_type().is_symlink() {
            return receipt_entry(
                candidate,
                CleanupReceiptStatus::Refused,
                Vec::new(),
                Some("agent directory is not a real managed directory".to_string()),
            );
        }
        let identity_path = candidate.agent_dir.join("identity.json");
        let current_identity = match fs::read_to_string(&identity_path).await {
            Ok(contents) => match serde_json::from_str::<AgentIdentityRecord>(&contents) {
                Ok(identity) => identity,
                Err(_) => {
                    return receipt_entry(
                        candidate,
                        CleanupReceiptStatus::Refused,
                        Vec::new(),
                        Some("identity changed or is malformed".to_string()),
                    )
                }
            },
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return receipt_entry(
                    candidate,
                    CleanupReceiptStatus::Skipped,
                    vec!["already_absent".to_string()],
                    Some("agent identity is already absent".to_string()),
                )
            }
            Err(error) => {
                return receipt_entry(
                    candidate,
                    CleanupReceiptStatus::Failed,
                    Vec::new(),
                    Some(format!("read identity: {error}")),
                )
            }
        };
        let Some(expected_identity) = candidate.identity.as_ref() else {
            return receipt_entry(
                candidate,
                CleanupReceiptStatus::Refused,
                Vec::new(),
                Some("managed identity is missing or malformed".to_string()),
            );
        };
        if &current_identity != expected_identity {
            return receipt_entry(
                candidate,
                CleanupReceiptStatus::Refused,
                Vec::new(),
                Some("identity changed since planning".to_string()),
            );
        }
        if self
            .resolver
            .get(&expected_identity.agent_name)
            .await
            .as_ref()
            != Some(expected_identity)
        {
            return receipt_entry(
                candidate,
                CleanupReceiptStatus::Refused,
                Vec::new(),
                Some("resolver identity changed since planning".to_string()),
            );
        }
        if self.liveness(&candidate.agent_dir).await != CleanupLiveness::Dead {
            return receipt_entry(
                candidate,
                CleanupReceiptStatus::Refused,
                Vec::new(),
                Some("agent is no longer provably dead".to_string()),
            );
        }
        if let Some(worktree) = &candidate.worktree_path {
            if worktree.exists() {
                match workspace_dirty(worktree).await {
                    Ok(false) => {}
                    _ => {
                        return receipt_entry(
                            candidate,
                            CleanupReceiptStatus::Refused,
                            Vec::new(),
                            Some("worktree is dirty or unavailable".to_string()),
                        )
                    }
                }
                match workspace_branch(worktree).await {
                    Ok(Some(actual_branch))
                        if actual_branch == expected_identity.birth_branch.as_str() => {}
                    Ok(Some(_)) => {
                        return receipt_entry(
                            candidate,
                            CleanupReceiptStatus::Refused,
                            Vec::new(),
                            Some("worktree branch changed since planning".to_string()),
                        );
                    }
                    Ok(None) => {
                        return receipt_entry(
                            candidate,
                            CleanupReceiptStatus::Refused,
                            Vec::new(),
                            Some("worktree is detached since planning".to_string()),
                        );
                    }
                    Err(error) => {
                        return receipt_entry(
                            candidate,
                            CleanupReceiptStatus::Refused,
                            Vec::new(),
                            Some(format!("read worktree branch: {error}")),
                        );
                    }
                }
                let Ok(canonical) = fs::canonicalize(worktree).await else {
                    return receipt_entry(
                        candidate,
                        CleanupReceiptStatus::Refused,
                        Vec::new(),
                        Some("worktree path changed since planning".to_string()),
                    );
                };
                if !path_within(&self.project_dir.join(".exo/worktrees"), &canonical) {
                    return receipt_entry(
                        candidate,
                        CleanupReceiptStatus::Refused,
                        Vec::new(),
                        Some("worktree path is outside the managed root".to_string()),
                    );
                }
                let expected_path = resolve_path(&self.project_dir, &expected_identity.working_dir);
                let Ok(expected_canonical) = fs::canonicalize(expected_path).await else {
                    return receipt_entry(
                        candidate,
                        CleanupReceiptStatus::Refused,
                        Vec::new(),
                        Some("expected worktree path is unavailable".to_string()),
                    );
                };
                if canonical != expected_canonical {
                    return receipt_entry(
                        candidate,
                        CleanupReceiptStatus::Refused,
                        Vec::new(),
                        Some("worktree path changed since planning".to_string()),
                    );
                }
                match local_branch_state(&self.project_dir, expected_identity.birth_branch.as_str())
                    .await
                {
                    Ok(Some(head))
                        if candidate.local_head_sha.as_deref() == Some(head.as_str()) => {}
                    Ok(Some(_)) => {
                        return receipt_entry(
                            candidate,
                            CleanupReceiptStatus::Refused,
                            Vec::new(),
                            Some("local branch head changed since planning".to_string()),
                        );
                    }
                    Ok(None) => {
                        return receipt_entry(
                            candidate,
                            CleanupReceiptStatus::Refused,
                            Vec::new(),
                            Some("local branch disappeared since planning".to_string()),
                        );
                    }
                    Err(error) => {
                        return receipt_entry(
                            candidate,
                            CleanupReceiptStatus::Refused,
                            Vec::new(),
                            Some(format!("read local branch: {error}")),
                        );
                    }
                }
            }
        }

        let mut actions = Vec::new();
        if let Some(worktree) = &candidate.worktree_path {
            if worktree.exists() {
                let git_worktree = self.git_worktree.clone();
                let path = worktree.clone();
                match tokio::task::spawn_blocking(move || git_worktree.remove_workspace(&path))
                    .await
                {
                    Ok(Ok(())) => {
                        actions.push("remove_worktree".to_string());
                        self.record_progress(receipt, index, candidate, &actions)
                            .await;
                    }
                    Ok(Err(error)) => {
                        return receipt_entry(
                            candidate,
                            CleanupReceiptStatus::Failed,
                            actions,
                            Some(format!("remove worktree: {error}")),
                        )
                    }
                    Err(error) => {
                        return receipt_entry(
                            candidate,
                            CleanupReceiptStatus::Failed,
                            actions,
                            Some(format!("remove worktree task: {error}")),
                        )
                    }
                }
            } else {
                actions.push("worktree_already_absent".to_string());
                self.record_progress(receipt, index, candidate, &actions)
                    .await;
            }
        }
        match fs::remove_dir_all(&candidate.agent_dir).await {
            Ok(()) => {
                actions.push("remove_agent_directory".to_string());
                self.record_progress(receipt, index, candidate, &actions)
                    .await;
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                actions.push("agent_directory_already_absent".to_string());
                self.record_progress(receipt, index, candidate, &actions)
                    .await;
            }
            Err(error) => {
                return receipt_entry(
                    candidate,
                    CleanupReceiptStatus::Failed,
                    actions,
                    Some(format!("remove agent directory: {error}")),
                )
            }
        }
        if let Ok(agent_name) = crate::domain::AgentName::try_from_str(&candidate.agent_name) {
            if let Err(error) = self.resolver.deregister(&agent_name).await {
                return receipt_entry(
                    candidate,
                    CleanupReceiptStatus::Failed,
                    actions,
                    Some(format!("deregister identity: {error}")),
                );
            }
            actions.push("deregister_identity".to_string());
            self.record_progress(receipt, index, candidate, &actions)
                .await;
        } else {
            return receipt_entry(
                candidate,
                CleanupReceiptStatus::Failed,
                actions,
                Some("agent name became invalid".to_string()),
            );
        }
        receipt_entry(candidate, CleanupReceiptStatus::Cleaned, actions, None)
    }

    async fn record_progress(
        &self,
        receipt: &mut CleanupReceipt,
        index: usize,
        candidate: &CleanupCandidate,
        actions: &[String],
    ) {
        receipt.entries[index] = receipt_entry(
            candidate,
            CleanupReceiptStatus::InProgress,
            actions.to_vec(),
            None,
        );
        if let Err(error) = self.persist_receipt(receipt).await {
            tracing::error!(
                operation_id = %receipt.operation_id,
                candidate = %candidate.id,
                %error,
                "failed to persist cleanup progress receipt"
            );
        }
    }

    pub(super) async fn persist_receipt(&self, receipt: &CleanupReceipt) -> Result<()> {
        let dir = self.receipt_dir();
        fs::create_dir_all(&dir).await?;
        let path = dir.join(format!("{}.json", receipt.operation_id));
        let temporary = dir.join(format!(".{}.tmp", receipt.operation_id));
        let bytes = serde_json::to_vec_pretty(receipt)?;
        tokio::task::spawn_blocking(move || -> Result<()> {
            let mut file = std::fs::OpenOptions::new()
                .create(true)
                .truncate(true)
                .write(true)
                .open(&temporary)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            std::fs::rename(&temporary, &path)?;
            #[cfg(unix)]
            std::fs::File::open(&dir)?.sync_all()?;
            Ok(())
        })
        .await
        .context("persist cleanup receipt task")??;
        Ok(())
    }
}
