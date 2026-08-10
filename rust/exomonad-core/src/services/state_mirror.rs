//! Best-effort L3-to-L1 state mutation mirroring.

use super::event_log::EventLog;
use serde_json::Value;
use std::path::Path;
use tracing::warn;

/// Append a structured state-change event beside the mutable state source.
///
/// State writes remain authoritative for runtime behavior. The mirror is local
/// evidence and therefore fail-open for the caller, while failures are visible
/// through tracing and the EventLog sink-health fallback.
pub fn append_state_change(db_path: &Path, event_type: &str, data: Value) {
    let Some(exo_dir) = db_path.parent() else {
        return;
    };
    if exo_dir.file_name().and_then(|name| name.to_str()) != Some(".exo") {
        return;
    }
    let agent_id = std::env::var("EXOMONAD_AGENT_ID").unwrap_or_else(|_| "system".to_string());
    let event_dir = exo_dir.join("events");
    let log = match EventLog::open(event_dir) {
        Ok(log) => log,
        Err(error) => {
            if let Some(project_dir) = exo_dir.parent() {
                let _ = super::sink_health::record_failure(project_dir, &error.to_string());
            }
            warn!(%error, event_type, "Failed to open state mirror event log");
            return;
        }
    };
    if let Err(error) = log.append(event_type, &agent_id, &data) {
        warn!(%error, event_type, "Failed to mirror mutable state change");
    }
}

#[cfg(test)]
mod tests {
    use crate::services::{
        immutable_ledger::LedgerWriter, InboxStore, NewMemoryRecord, SessionMemoryService,
    };
    use tempfile::TempDir;

    #[test]
    fn memory_and_inbox_mutations_are_replayable_l1_events() -> anyhow::Result<()> {
        let temp = TempDir::new()?;
        let inbox = InboxStore::open(temp.path())?;
        let message_id =
            inbox.write_message("root", "worker", "private payload", Some("summary"))?;
        let memory = SessionMemoryService::open(temp.path())?;
        let memory_id = memory.append(NewMemoryRecord {
            run_id: "run-1".to_string(),
            agent_id: "root".to_string(),
            birth_branch: "main".to_string(),
            kind: crate::services::MemoryKind::Decision,
            importance: 80,
            summary: "decision".to_string(),
            detail: Some("private detail".to_string()),
            ..NewMemoryRecord::default()
        })?;
        let records = LedgerWriter::open_project(temp.path())?.read_events()?;
        let state_events = records
            .iter()
            .filter(|record| {
                matches!(
                    record.event.event_type.as_str(),
                    "inbox.state_changed" | "memory.state_changed"
                )
            })
            .collect::<Vec<_>>();
        assert!(state_events.iter().any(|record| {
            record.event.data["operation"] == "write_message"
                && record.event.data["message_id"] == message_id
        }));
        assert!(state_events.iter().any(|record| {
            record.event.data["operation"] == "append"
                && record.event.data["record_id"] == memory_id
        }));
        Ok(())
    }
}
