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
            receipt.entries[index] =
                receipt_entry(candidate, CleanupReceiptStatus::InProgress, actions, None);
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
        receipt.entries[index] = receipt_entry(
            candidate,
            CleanupReceiptStatus::InProgress,
            actions.to_vec(),
            None,
        );
        self.persist_receipt(receipt).await
    }
}

fn in_progress_actions(entry: &CleanupReceiptEntry) -> Vec<String> {
    if matches!(&entry.status, CleanupReceiptStatus::InProgress) {
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
