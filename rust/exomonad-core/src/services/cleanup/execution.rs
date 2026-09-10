use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use anyhow::Result;

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
            if matches!(
                &receipt.entries[index].status,
                CleanupReceiptStatus::Cleaned
            ) {
                continue;
            }
            let actions = in_progress_actions(&receipt.entries[index]);
            let historical_identity = receipt.entries[index].identity_snapshot.clone();
            let mut in_progress =
                receipt_entry(candidate, CleanupReceiptStatus::InProgress, actions, None);
            let historical_branch = receipt.entries[index].branch.clone();
            in_progress.identity_snapshot =
                historical_identity.or_else(|| candidate.identity.clone());
            in_progress.branch = historical_branch.or_else(|| candidate.branch.clone());
            normalize_remote_opt_in(&mut in_progress, candidate);
            receipt.entries[index] = in_progress;
            self.persist_receipt(receipt).await?;
            let entry = self.execute_candidate(candidate, receipt, index).await;
            let progress_failed = is_progress_persistence_failure(&entry);
            receipt.entries[index] = entry;
            if progress_failed {
                anyhow::bail!(
                    "{}",
                    receipt.entries[index]
                        .reason
                        .as_deref()
                        .unwrap_or(PROGRESS_PERSISTENCE_FAILURE)
                );
            }
            self.persist_receipt(receipt).await?;
        }
        Ok(())
    }

    pub(super) async fn record_progress(
        &self,
        receipt: &mut CleanupReceipt,
        index: usize,
        candidate: &CleanupCandidate,
        actions: &[String],
    ) -> Result<()> {
        let branch = receipt.entries[index].branch.clone();
        receipt.entries[index] = receipt_entry(
            candidate,
            CleanupReceiptStatus::InProgress,
            actions.to_vec(),
            None,
        );
        receipt.entries[index].branch = branch.or_else(|| candidate.branch.clone());
        self.persist_receipt(receipt).await
    }
}

fn in_progress_actions(entry: &CleanupReceiptEntry) -> Vec<String> {
    if matches!(
        &entry.status,
        CleanupReceiptStatus::InProgress | CleanupReceiptStatus::Failed
    ) {
        entry.actions.clone()
    } else {
        Vec::new()
    }
}

fn is_progress_persistence_failure(entry: &CleanupReceiptEntry) -> bool {
    matches!(&entry.status, CleanupReceiptStatus::Failed)
        && entry
            .reason
            .as_deref()
            .is_some_and(|reason| reason.starts_with(PROGRESS_PERSISTENCE_FAILURE))
}

fn normalize_remote_opt_in(entry: &mut CleanupReceiptEntry, candidate: &CleanupCandidate) {
    let Some(branch) = entry.branch.as_mut() else {
        return;
    };
    if candidate.delete_remote_branch {
        if matches!(
            branch.remote.status,
            CleanupBranchActionStatus::NotRequested | CleanupBranchActionStatus::Skipped
        ) {
            if let Some(planned) = candidate.branch.as_ref() {
                branch.remote = planned.remote.clone();
            }
        }
    } else if matches!(
        branch.remote.status,
        CleanupBranchActionStatus::WouldDelete | CleanupBranchActionStatus::DeletePending
    ) {
        branch.remote.status = CleanupBranchActionStatus::Skipped;
        branch.remote.reason = Some("remote deletion requires explicit opt-in".to_string());
    }
}
