//! Structured event logging service with a compatibility JSONL view and an
//! append-only canonical L1 ledger. The compatibility view remains per-agent
//! under the project event directories; the analysis source of truth is the
//! bounded ledger under the project ledger directory.

use super::immutable_ledger::{LedgerEvent, LedgerWriter};
use super::sink_health;
use nix::fcntl::{Flock, FlockArg};
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use tracing::warn;
use uuid::Uuid;

/// Compatibility event view plus the canonical append-only ledger.
///
/// The process mutex and OS advisory locks serialize sequence allocation and
/// segment appends, including rows larger than 4 KiB.
pub struct EventLog {
    dir: PathBuf,
    project_dir: PathBuf,
    ledger: LedgerWriter,
    sequence_path: PathBuf,
    lock: Mutex<()>,
}

impl EventLog {
    /// Open (or create) the event log directory at `dir`.
    pub fn open(dir: PathBuf) -> io::Result<Self> {
        fs::create_dir_all(&dir)?;
        let (project_dir, ledger_segments) = ledger_paths(&dir);
        let ledger = LedgerWriter::open(ledger_segments.clone())
            .map_err(|error| io::Error::other(error.to_string()))?;
        let sequence_path = ledger_segments
            .parent()
            .map(|parent| parent.join("run_seq"))
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "ledger has no parent"))?;
        Ok(Self {
            dir,
            project_dir,
            ledger,
            sequence_path,
            lock: Mutex::new(()),
        })
    }

    /// Append one compatibility event and one canonical L1 event.
    pub fn append(
        &self,
        event_type: &str,
        agent_id: &str,
        data: &serde_json::Value,
    ) -> io::Result<String> {
        let _guard = self.lock.lock().unwrap_or_else(|error| error.into_inner());
        let event_id = Uuid::new_v4().to_string();
        let event_time = data
            .get("event_time")
            .and_then(serde_json::Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(now);
        let observed_at = now();
        let run_seq = match self.allocate_sequence() {
            Ok(run_seq) => run_seq,
            Err(error) => {
                self.record_failure(&error);
                return Err(error);
            }
        };
        let ledger_event = LedgerEvent {
            schema_version: 1,
            event_id: event_id.clone(),
            id: event_id.clone(),
            event_time,
            observed_at: observed_at.clone(),
            run_seq: Some(run_seq),
            event_type: event_type.to_string(),
            agent_id: Some(agent_id.to_string()),
            run_id: self.run_id(),
            session_id: self.session_id(),
            invocation_id: string_field(data, "invocation_id"),
            generation: data.get("generation").and_then(serde_json::Value::as_u64),
            source: "rust".to_string(),
            lifecycle_state: string_field(data, "lifecycle_state")
                .unwrap_or_else(|| "emitted".to_string()),
            data: data.clone(),
        };
        if let Err(error) = self
            .ledger
            .append(&ledger_event)
            .map_err(|error| io::Error::other(error.to_string()))
        {
            self.record_failure(&error);
            return Err(error);
        }

        let legacy_event = serde_json::json!({
            "ts": observed_at,
            "id": event_id,
            "type": event_type,
            "agent_id": agent_id,
            "data": data,
        });
        let sanitized_id = agent_id.replace(['/', char::from(92), char::from(0)], "_");
        let path = self.dir.join(format!("{sanitized_id}.jsonl"));
        let mut file = match OpenOptions::new().create(true).append(true).open(&path) {
            Ok(file) => file,
            Err(error) => {
                self.record_failure(&error);
                return Err(error);
            }
        };
        let line = match serde_json::to_vec(&legacy_event) {
            Ok(mut line) => {
                line.push(b'\n');
                line
            }
            Err(error) => {
                let error = io::Error::other(error);
                self.record_failure(&error);
                return Err(error);
            }
        };
        if let Err(error) = file.write_all(&line).and_then(|_| file.sync_data()) {
            self.record_failure(&error);
            return Err(error);
        }
        if let Err(error) = sink_health::record_success(&self.project_dir, run_seq) {
            warn!(%error, "Event committed but sink-health fallback could not be updated");
        }
        Ok(event_id)
    }

    /// Path to the log directory.
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// Access the canonical ledger for replay and sequence inspection.
    pub fn ledger(&self) -> &LedgerWriter {
        &self.ledger
    }

    fn allocate_sequence(&self) -> io::Result<u64> {
        let parent = self
            .sequence_path
            .parent()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "sequence has no parent"))?;
        fs::create_dir_all(parent)?;
        let lock_path = parent.join("run_seq.lock");
        let lock_file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(false)
            .open(lock_path)?;
        let _lock = Flock::lock(lock_file, FlockArg::LockExclusive)
            .map_err(|(_, error)| io::Error::from_raw_os_error(error as i32))?;
        let current = match fs::read_to_string(&self.sequence_path) {
            Ok(value) => value.trim().parse::<u64>().map_err(|error| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("invalid run_seq: {error}"),
                )
            })?,
            Err(error) if error.kind() == io::ErrorKind::NotFound => 0,
            Err(error) => return Err(error),
        };
        let next = current
            .checked_add(1)
            .ok_or_else(|| io::Error::other("run_seq exhausted"))?;
        let temporary = parent.join(format!(".run-seq-{}.tmp", Uuid::new_v4()));
        fs::write(&temporary, next.to_string())?;
        fs::rename(temporary, &self.sequence_path)?;
        Ok(next)
    }

    fn project_state_path(&self, name: &str) -> PathBuf {
        self.project_dir.join(".exo").join(name)
    }

    fn run_id(&self) -> Option<String> {
        fs::read_to_string(self.project_state_path("run_id"))
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    }

    fn session_id(&self) -> Option<String> {
        fs::read_to_string(self.project_state_path("session.json"))
            .ok()
            .and_then(|value| serde_json::from_str::<serde_json::Value>(&value).ok())
            .and_then(|value| {
                value
                    .get("session_id")
                    .and_then(serde_json::Value::as_str)
                    .map(str::to_string)
            })
    }

    fn record_failure(&self, error: &io::Error) {
        if let Err(health_error) =
            sink_health::record_failure(&self.project_dir, &error.to_string())
        {
            warn!(%health_error, "Event sink failed and fallback health could not be written");
        }
    }
}

fn ledger_paths(event_dir: &Path) -> (PathBuf, PathBuf) {
    let is_exo_log = event_dir
        .parent()
        .and_then(Path::file_name)
        .and_then(|name| name.to_str())
        == Some(".exo");
    if is_exo_log {
        if let Some(exo_dir) = event_dir.parent() {
            let project_dir = exo_dir.parent().unwrap_or(exo_dir).to_path_buf();
            return (project_dir, exo_dir.join("ledger/segments"));
        }
    }
    (event_dir.to_path_buf(), event_dir.join(".ledger/segments"))
}

fn string_field(value: &serde_json::Value, name: &str) -> Option<String> {
    value
        .get(name)
        .and_then(serde_json::Value::as_str)
        .map(str::to_string)
}

fn now() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_append_and_read_back() {
        let dir = tempfile::tempdir().unwrap();
        let log = EventLog::open(dir.path().to_path_buf()).unwrap();

        let data = serde_json::json!({"slug": "feature-a", "agent_type": "claude"});
        let id = log.append("agent.spawned", "root", &data).unwrap();
        assert!(!id.is_empty());

        let path = dir.path().join("root.jsonl");
        let content = std::fs::read_to_string(&path).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(content.trim()).unwrap();
        assert_eq!(parsed["type"], "agent.spawned");
        assert_eq!(parsed["agent_id"], "root");
        assert_eq!(parsed["data"]["slug"], "feature-a");
        assert!(parsed["ts"].as_str().unwrap().contains("T"));
        assert_eq!(parsed["id"], id);
        let records = log.ledger().read_events().unwrap();
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].event.event_id, id);
        assert_eq!(records[0].event.id, id);
        assert_eq!(records[0].event.run_seq, Some(1));
    }

    #[test]
    fn concurrent_appends_use_one_monotonic_sequence() {
        use std::sync::Arc;
        use std::thread;

        let dir = tempfile::TempDir::new().unwrap();
        let log = Arc::new(EventLog::open(dir.path().to_path_buf()).unwrap());
        let mut handles = Vec::new();
        for worker in 0..4 {
            let log = Arc::clone(&log);
            handles.push(thread::spawn(move || {
                for index in 0..8 {
                    log.append(
                        "custom.concurrent",
                        &format!("worker-{worker}"),
                        &serde_json::json!({"index": index}),
                    )
                    .unwrap();
                }
            }));
        }
        for handle in handles {
            handle.join().unwrap();
        }
        let records = log.ledger().read_events().unwrap();
        let mut sequences = records
            .iter()
            .filter_map(|record| record.event.run_seq)
            .collect::<Vec<_>>();
        sequences.sort_unstable();
        assert_eq!(sequences, (1..=32).collect::<Vec<_>>());
    }

    #[test]
    fn test_multiple_appends() {
        let dir = tempfile::tempdir().unwrap();
        let log = EventLog::open(dir.path().to_path_buf()).unwrap();

        log.append("a", "agent-x", &serde_json::json!({})).unwrap();
        log.append("b", "agent-y", &serde_json::json!({})).unwrap();

        let path_x = dir.path().join("agent-x.jsonl");
        let path_y = dir.path().join("agent-y.jsonl");

        assert!(path_x.exists());
        assert!(path_y.exists());

        let content_x = std::fs::read_to_string(&path_x).unwrap();
        let content_y = std::fs::read_to_string(&path_y).unwrap();

        assert_eq!(content_x.trim().lines().count(), 1);
        assert_eq!(content_y.trim().lines().count(), 1);
    }

    #[test]
    fn test_agent_id_sanitization() {
        let dir = tempfile::tempdir().unwrap();
        let log = EventLog::open(dir.path().to_path_buf()).unwrap();

        log.append("a", "feature/bug", &serde_json::json!({}))
            .unwrap();
        let path = dir.path().join("feature_bug.jsonl");
        assert!(path.exists());
    }
}
