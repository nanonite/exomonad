//! Strict append-only storage for local observability evidence.
//!
//! The ledger is deliberately smaller than the normalized analysis database. It
//! owns the evidence boundary: events are serialized once, appended to bounded
//! segment files, and never edited in place. Derived stores may be rebuilt from
//! these segments.

use anyhow::{Context, Result};
use chrono::Utc;
use nix::fcntl::{Flock, FlockArg};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use uuid::Uuid;

/// Default maximum size for one immutable JSONL segment.
pub const DEFAULT_SEGMENT_MAX_BYTES: u64 = 4 * 1024 * 1024;

/// Canonical event envelope stored in the L1 ledger.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LedgerEvent {
    pub schema_version: u32,
    pub event_id: String,
    /// Legacy alias. New events must keep this equal to `event_id`.
    pub id: String,
    pub event_time: String,
    pub observed_at: String,
    pub run_seq: Option<u64>,
    #[serde(rename = "type")]
    pub event_type: String,
    pub agent_id: Option<String>,
    pub run_id: Option<String>,
    pub session_id: Option<String>,
    pub invocation_id: Option<String>,
    pub generation: Option<u64>,
    pub source: String,
    pub lifecycle_state: String,
    pub data: Value,
}

impl LedgerEvent {
    /// Construct a new emitted event with compatibility-safe identity fields.
    pub fn new(event_type: impl Into<String>, agent_id: Option<String>, data: Value) -> Self {
        let event_id = Uuid::new_v4().to_string();
        let now = Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        Self {
            schema_version: 1,
            id: event_id.clone(),
            event_id,
            event_time: now.clone(),
            observed_at: now,
            run_seq: None,
            event_type: event_type.into(),
            agent_id,
            run_id: None,
            session_id: None,
            invocation_id: None,
            generation: None,
            source: "exomonad".to_string(),
            lifecycle_state: "emitted".to_string(),
            data,
        }
    }
}

/// A parsed event with the segment and line that supplied it.
#[derive(Debug, Clone, PartialEq)]
pub struct LedgerRecord {
    pub segment: PathBuf,
    pub line_number: usize,
    pub event: LedgerEvent,
}

/// One process-local writer for immutable ledger segments.
///
/// A later sequencing work package will put a single canonical sequence
/// allocator in front of this writer. This type intentionally accepts, but does
/// not invent, `run_seq` values.
pub struct LedgerWriter {
    segments_dir: PathBuf,
    segment_max_bytes: u64,
    lock: Mutex<()>,
}

impl LedgerWriter {
    /// Open the project ledger at `.exo/ledger/segments`.
    pub fn open_project(project_dir: impl AsRef<Path>) -> Result<Self> {
        Self::open(project_dir.as_ref().join(".exo/ledger/segments"))
    }

    /// Open or create a ledger segment directory.
    pub fn open(segments_dir: impl Into<PathBuf>) -> Result<Self> {
        Self::with_segment_max_bytes(segments_dir, DEFAULT_SEGMENT_MAX_BYTES)
    }

    /// Open a ledger with a bounded segment size. Primarily useful for rotation
    /// tests and deployments with a tighter local retention budget.
    pub fn with_segment_max_bytes(
        segments_dir: impl Into<PathBuf>,
        segment_max_bytes: u64,
    ) -> Result<Self> {
        let segments_dir = segments_dir.into();
        fs::create_dir_all(&segments_dir)
            .with_context(|| format!("create ledger directory {}", segments_dir.display()))?;
        Ok(Self {
            segments_dir,
            segment_max_bytes: segment_max_bytes.max(1),
            lock: Mutex::new(()),
        })
    }

    /// Append one complete JSON object followed by a newline.
    pub fn append(&self, event: &LedgerEvent) -> Result<()> {
        let mut line = serde_json::to_vec(event).context("serialize ledger event")?;
        line.push(b'\n');
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow::anyhow!("ledger lock poisoned"))?;
        let lock_path = self.segments_dir.join(".append.lock");
        let lock_file = OpenOptions::new()
            .create(true)
            .write(true)
            .open(&lock_path)
            .with_context(|| format!("open ledger lock {}", lock_path.display()))?;
        let _file_lock = Flock::lock(lock_file, FlockArg::LockExclusive)
            .map_err(|(_, error)| anyhow::anyhow!("lock ledger writer: {error}"))?;

        let path = self.current_segment_path()?;
        let current_size = fs::metadata(&path)
            .map(|metadata| metadata.len())
            .unwrap_or(0);
        let target =
            if current_size > 0 && current_size + line.len() as u64 > self.segment_max_bytes {
                self.next_segment_path()?
            } else {
                path
            };

        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&target)
            .with_context(|| format!("open ledger segment {}", target.display()))?;
        file.write_all(&line)
            .with_context(|| format!("append ledger segment {}", target.display()))?;
        file.sync_data()
            .with_context(|| format!("sync ledger segment {}", target.display()))?;
        Ok(())
    }

    /// Return immutable segment files in lexical sequence order.
    pub fn segments(&self) -> Result<Vec<PathBuf>> {
        let mut paths = fs::read_dir(&self.segments_dir)
            .with_context(|| format!("read ledger directory {}", self.segments_dir.display()))?
            .filter_map(|entry| entry.ok().map(|item| item.path()))
            .filter(|path| path.extension().and_then(|ext| ext.to_str()) == Some("jsonl"))
            .collect::<Vec<_>>();
        paths.sort();
        Ok(paths)
    }

    /// Replay every valid event, preserving segment and line provenance.
    pub fn read_events(&self) -> Result<Vec<LedgerRecord>> {
        let mut records = Vec::new();
        for segment in self.segments()? {
            let file = File::open(&segment)
                .with_context(|| format!("open ledger segment {}", segment.display()))?;
            for (index, line) in BufReader::new(file).lines().enumerate() {
                let line_number = index + 1;
                let line = line
                    .with_context(|| format!("read {} line {}", segment.display(), line_number))?;
                if line.trim().is_empty() {
                    continue;
                }
                let event = serde_json::from_str(&line)
                    .with_context(|| format!("parse {} line {}", segment.display(), line_number))?;
                records.push(LedgerRecord {
                    segment: segment.clone(),
                    line_number,
                    event,
                });
            }
        }
        Ok(records)
    }

    fn current_segment_path(&self) -> Result<PathBuf> {
        Ok(self
            .segments()?
            .into_iter()
            .last()
            .unwrap_or_else(|| self.segments_dir.join("segment-000000000000.jsonl")))
    }

    fn next_segment_path(&self) -> Result<PathBuf> {
        let next_index = self
            .segments()?
            .iter()
            .filter_map(|path| {
                path.file_stem()
                    .and_then(|stem| stem.to_str())
                    .and_then(|stem| stem.strip_prefix("segment-"))
                    .and_then(|index| index.parse::<u64>().ok())
            })
            .max()
            .map_or(1, |index| index.saturating_add(1));
        Ok(self
            .segments_dir
            .join(format!("segment-{next_index:012}.jsonl")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn append_is_immutable_and_replayable() -> Result<()> {
        let temp = TempDir::new()?;
        let writer = LedgerWriter::open(temp.path().join("segments"))?;
        let event = LedgerEvent::new(
            "agent.spawned",
            Some("root".to_string()),
            serde_json::json!({
                "large": "x".repeat(8_192),
            }),
        );
        writer.append(&event)?;
        let before = fs::read(writer.segments()?.first().context("segment")?)?;
        writer.append(&LedgerEvent::new(
            "agent.invocation.started",
            None,
            Value::Null,
        ))?;
        let after = fs::read(writer.segments()?.first().context("segment")?)?;
        assert!(after.starts_with(&before));
        assert_eq!(writer.read_events()?.len(), 2);
        assert_eq!(event.id, event.event_id);
        Ok(())
    }

    #[test]
    fn concurrent_append_keeps_large_rows_parseable() -> Result<()> {
        use std::sync::Arc;
        use std::thread;

        let temp = TempDir::new()?;
        let writer = Arc::new(LedgerWriter::with_segment_max_bytes(
            temp.path().join("segments"),
            32 * 1024,
        )?);
        let mut handles = Vec::new();
        for worker in 0..4 {
            let writer = Arc::clone(&writer);
            handles.push(thread::spawn(move || {
                for index in 0..8 {
                    let event = LedgerEvent::new(
                        "custom.concurrent",
                        Some(format!("worker-{worker}")),
                        serde_json::json!({"payload": "x".repeat(8_192), "index": index}),
                    );
                    writer.append(&event).expect("append concurrent event");
                }
            }));
        }
        for handle in handles {
            handle.join().expect("join writer");
        }
        assert_eq!(writer.read_events()?.len(), 32);
        Ok(())
    }

    #[test]
    fn rotates_without_rewriting_closed_segments() -> Result<()> {
        let temp = TempDir::new()?;
        let writer = LedgerWriter::with_segment_max_bytes(temp.path().join("segments"), 128)?;
        for _ in 0..5 {
            writer.append(&LedgerEvent::new(
                "custom.test",
                None,
                serde_json::json!({
                    "payload": "x".repeat(100),
                }),
            ))?;
        }
        let segments = writer.segments()?;
        assert!(segments.len() > 1);
        let snapshots = segments
            .iter()
            .map(fs::read)
            .collect::<std::io::Result<Vec<_>>>()?;
        writer.append(&LedgerEvent::new("custom.test", None, Value::Null))?;
        for (path, snapshot) in segments.iter().zip(snapshots) {
            assert!(fs::read(path)?.starts_with(&snapshot));
        }
        Ok(())
    }
}
