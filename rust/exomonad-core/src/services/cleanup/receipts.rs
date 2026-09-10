use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use crate::domain::AgentName;
use crate::services::agent_resolver::AgentIdentityRecord;
use anyhow::{Context, Result};
use std::collections::HashSet;
use std::io::Write;
#[cfg(test)]
use std::sync::atomic::Ordering;
use tokio::fs;

const RESUMED_CLEANUP_ACTION: &str = "resumed_cleanup";

impl VerifiedCleanupService {
    pub(super) async fn has_prior_remote_deletion_proof(
        &self,
        candidate: &CleanupCandidate,
    ) -> Result<bool> {
        let Some(branch) = candidate.branch.as_ref() else {
            return Ok(false);
        };
        let Some(remote_name) = branch
            .remote_name
            .as_deref()
            .filter(|value| !value.is_empty())
        else {
            return Ok(false);
        };
        let Some(remote_branch) = branch
            .branch
            .as_deref()
            .or(candidate.local_branch.as_deref())
            .filter(|value| !value.is_empty())
        else {
            return Ok(false);
        };
        Ok(self
            .read_receipts()
            .await?
            .iter()
            .filter(|receipt| !receipt.dry_run)
            .any(|receipt| {
                receipt.entries.iter().any(|entry| {
                    receipt_entry_proves_remote_deletion(
                        entry,
                        candidate,
                        remote_name,
                        remote_branch,
                    )
                })
            }))
    }

    pub(super) async fn in_progress_identity_snapshots(&self) -> Result<Vec<AgentIdentityRecord>> {
        let receipts = self.read_receipts().await?;
        Ok(receipts
            .into_iter()
            .filter(|receipt| !receipt.dry_run)
            .flat_map(|receipt| receipt.entries)
            .filter(|entry| {
                matches!(
                    &entry.status,
                    CleanupReceiptStatus::InProgress | CleanupReceiptStatus::Failed
                )
            })
            .filter(receipt_snapshot_is_coherent)
            .filter_map(|entry| entry.identity_snapshot)
            .collect())
    }

    pub(super) async fn recoverable_receipt_entries(&self) -> Result<Vec<CleanupReceiptEntry>> {
        let receipts = self.read_receipts().await?;
        Ok(receipts
            .into_iter()
            .filter(|receipt| !receipt.dry_run)
            .flat_map(|receipt| receipt.entries)
            .filter(|entry| {
                matches!(
                    &entry.status,
                    CleanupReceiptStatus::InProgress | CleanupReceiptStatus::Failed
                )
            })
            .filter(receipt_snapshot_is_coherent)
            .filter(|entry| entry.recovered_provenance.is_some())
            .collect())
    }

    pub(super) async fn resume_or_create_receipt(
        &self,
        plan: &CleanupPlan,
        target: Option<&str>,
        started_at: u64,
    ) -> Result<CleanupReceipt> {
        let Some(previous) = self.load_in_progress_receipt(plan, target).await? else {
            return Ok(in_progress_receipt(plan, started_at));
        };
        let mut receipt = resume_receipt(plan, previous);
        self.reconcile_deregistered_entries(&mut receipt).await?;
        Ok(receipt)
    }

    async fn load_in_progress_receipt(
        &self,
        plan: &CleanupPlan,
        target: Option<&str>,
    ) -> Result<Option<CleanupReceipt>> {
        let mut receipts = self
            .read_receipts()
            .await?
            .into_iter()
            .filter(|receipt| {
                !receipt.dry_run
                    && receipt.entries.iter().any(|entry| {
                        matches!(
                            &entry.status,
                            CleanupReceiptStatus::InProgress | CleanupReceiptStatus::Failed
                        ) && receipt_entry_matches(entry, plan, target)
                    })
            })
            .collect::<Vec<_>>();
        receipts.sort_by(|left, right| {
            left.started_at
                .cmp(&right.started_at)
                .then_with(|| left.operation_id.cmp(&right.operation_id))
        });
        Ok(receipts.pop())
    }

    pub(super) async fn target_was_cleaned(&self, target: &str) -> Result<bool> {
        Ok(self.read_receipts().await?.iter().any(|receipt| {
            receipt.entries.iter().any(|entry| {
                matches!(entry.status, CleanupReceiptStatus::Cleaned)
                    && (entry.agent_name == target || entry.agent_slug == target)
            })
        }))
    }

    async fn read_receipts(&self) -> Result<Vec<CleanupReceipt>> {
        let mut directory = match fs::read_dir(self.receipt_dir()).await {
            Ok(directory) => directory,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(error) => return Err(error).context("read cleanup receipts"),
        };
        let mut receipts = Vec::new();
        while let Some(entry) = directory.next_entry().await? {
            if !is_receipt_file(&entry).await? {
                continue;
            }
            let path = entry.path();
            let contents = fs::read_to_string(&path)
                .await
                .with_context(|| format!("read cleanup receipt {}", path.display()))?;
            receipts.push(
                serde_json::from_str::<CleanupReceipt>(&contents)
                    .with_context(|| format!("parse cleanup receipt {}", path.display()))?,
            );
        }
        Ok(receipts)
    }

    async fn reconcile_deregistered_entries(&self, receipt: &mut CleanupReceipt) -> Result<()> {
        for entry in &mut receipt.entries {
            if !matches!(
                &entry.status,
                CleanupReceiptStatus::InProgress | CleanupReceiptStatus::Failed
            ) || !has_deregister_intent(entry)
            {
                continue;
            }
            let Some(name) = receipt_entry_agent_name(entry) else {
                continue;
            };
            if self.resolver.get(&name).await.is_some() {
                continue;
            }
            entry.actions.retain(|action| action != DEREGISTER_PENDING);
            if !entry
                .actions
                .iter()
                .any(|action| action == "deregister_identity")
            {
                entry.actions.push("deregister_identity".to_string());
            }
            entry.status = CleanupReceiptStatus::Cleaned;
            entry.reason = None;
        }
        Ok(())
    }

    pub(super) async fn persist_receipt(&self, receipt: &CleanupReceipt) -> Result<()> {
        #[cfg(test)]
        {
            let call = self.receipt_persist_calls.fetch_add(1, Ordering::SeqCst) + 1;
            if self.fail_receipt_persist_on.load(Ordering::SeqCst) == call {
                anyhow::bail!("injected cleanup receipt persistence failure");
            }
        }
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

async fn is_receipt_file(entry: &fs::DirEntry) -> Result<bool> {
    let file_type = entry.file_type().await?;
    Ok(file_type.is_file()
        && entry.path().extension().and_then(|value| value.to_str()) == Some("json"))
}

fn receipt_entry_matches(
    entry: &CleanupReceiptEntry,
    plan: &CleanupPlan,
    target: Option<&str>,
) -> bool {
    target.is_none_or(|target| {
        entry.agent_name == target
            || entry.agent_slug == target
            || plan
                .candidates
                .iter()
                .any(|candidate| receipt_entry_matches_candidate(entry, candidate))
    }) && receipt_snapshot_is_coherent(entry)
}

fn has_deregister_intent(entry: &CleanupReceiptEntry) -> bool {
    entry
        .actions
        .iter()
        .any(|action| action == DEREGISTER_PENDING || action == "deregister_identity")
}

fn receipt_entry_agent_name(entry: &CleanupReceiptEntry) -> Option<AgentName> {
    let name = if let Some(identity) = &entry.identity_snapshot {
        identity.agent_name.as_str()
    } else if entry.agent_name.is_empty() {
        entry.candidate_id.as_str()
    } else {
        entry.agent_name.as_str()
    };
    AgentName::try_from_str(name).ok()
}

fn resume_receipt(plan: &CleanupPlan, previous: CleanupReceipt) -> CleanupReceipt {
    let CleanupReceipt {
        operation_id,
        plan_id,
        started_at,
        operator_reason: previous_reason,
        entries: previous_entries,
        ..
    } = previous;
    let mut matched = HashSet::new();
    let mut entries = plan
        .candidates
        .iter()
        .map(|candidate| {
            let previous_entry = previous_entries
                .iter()
                .enumerate()
                .find(|(_, entry)| {
                    receipt_entry_matches_candidate(entry, candidate)
                        && matches!(
                            &entry.status,
                            CleanupReceiptStatus::InProgress
                                | CleanupReceiptStatus::Failed
                                | CleanupReceiptStatus::Cleaned
                        )
                })
                .map(|(index, entry)| {
                    matched.insert(index);
                    entry
                });
            let was_resumed = previous_entry.is_some_and(|entry| {
                matches!(
                    &entry.status,
                    CleanupReceiptStatus::InProgress | CleanupReceiptStatus::Failed
                )
            });
            let mut entry = previous_entry.cloned().unwrap_or_else(|| {
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
            });
            if was_resumed
                && !entry
                    .actions
                    .iter()
                    .any(|action| action == RESUMED_CLEANUP_ACTION)
            {
                entry.actions.push(RESUMED_CLEANUP_ACTION.to_string());
            }
            entry
        })
        .collect::<Vec<_>>();
    entries.extend(
        previous_entries
            .into_iter()
            .enumerate()
            .filter_map(|(index, entry)| {
                (!matched.contains(&index)
                    && matches!(
                        &entry.status,
                        CleanupReceiptStatus::InProgress
                            | CleanupReceiptStatus::Failed
                            | CleanupReceiptStatus::Cleaned
                    ))
                .then_some(entry)
            }),
    );
    CleanupReceipt {
        schema_version: CLEANUP_RECEIPT_SCHEMA_VERSION,
        operation_id,
        plan_id,
        started_at,
        finished_at: 0,
        dry_run: false,
        operator_reason: plan.operator_reason.clone().or(previous_reason),
        preserve_unique_commits: plan.preserve_unique_commits,
        entries,
    }
}

fn receipt_entry_matches_candidate(
    entry: &CleanupReceiptEntry,
    candidate: &CleanupCandidate,
) -> bool {
    let same_identity = entry
        .identity_snapshot
        .as_ref()
        .is_some_and(|snapshot| candidate.identity.as_ref() == Some(snapshot))
        || (entry.identity_snapshot.is_none()
            && (entry.candidate_id == candidate.id
                || (!entry.agent_name.is_empty() && entry.agent_name == candidate.agent_name)
                || (!entry.agent_slug.is_empty()
                    && candidate
                        .identity
                        .as_ref()
                        .is_some_and(|identity| entry.agent_slug == identity.slug.as_str()))));
    let names_agree = entry.agent_name.is_empty() || entry.agent_name == candidate.agent_name;
    let slugs_agree = entry.agent_slug.is_empty()
        || candidate
            .identity
            .as_ref()
            .is_some_and(|identity| entry.agent_slug == identity.slug.as_str());
    same_identity && names_agree && slugs_agree && receipt_snapshot_is_coherent(entry)
}

fn receipt_snapshot_is_coherent(entry: &CleanupReceiptEntry) -> bool {
    entry.identity_snapshot.as_ref().is_none_or(|snapshot| {
        entry.agent_name == snapshot.agent_name.as_str()
            && entry.agent_slug == snapshot.slug.as_str()
    })
}

fn receipt_entry_proves_remote_deletion(
    entry: &CleanupReceiptEntry,
    candidate: &CleanupCandidate,
    remote_name: &str,
    remote_branch: &str,
) -> bool {
    let Some(evidence) = entry.branch.as_ref() else {
        return false;
    };
    let evidence_branch = evidence
        .remote_branch
        .as_deref()
        .or(evidence.branch.as_deref());
    matches!(evidence.remote.status, CleanupBranchActionStatus::Deleted)
        && entry
            .actions
            .iter()
            .any(|action| action == "delete_remote_branch")
        && receipt_entry_matches_candidate(entry, candidate)
        && evidence.remote_name.as_deref() == Some(remote_name)
        && evidence_branch == Some(remote_branch)
        && evidence
            .remote_head_sha
            .as_deref()
            .is_some_and(|sha| !sha.is_empty())
        && candidate
            .pull_request
            .as_ref()
            .and_then(|pull_request| pull_request.head_sha.as_deref())
            .is_none_or(|head| evidence.remote_head_sha.as_deref() == Some(head))
}
