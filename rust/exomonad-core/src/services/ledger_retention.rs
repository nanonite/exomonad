//! Whole-segment retention for the local immutable ledger.
//!
//! Closed segments are removed as units. The current segment is never a
//! retention target, and every non-dry-run removal is recorded in the
//! surviving ledger before the file is dropped.

use crate::services::EventLog;
use anyhow::{Context, Result};
use nix::fcntl::{Flock, FlockArg};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::fs::{self, OpenOptions};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct SegmentDrop {
    pub path: PathBuf,
    pub bytes: u64,
    pub sha256: String,
    pub reason: String,
    pub dropped: bool,
}

/// Drop closed segments older than older_than, or report the same targets in
/// dry-run mode. A zero duration means eligible as of now.
pub fn drop_expired_segments(
    project_dir: impl AsRef<Path>,
    older_than: Duration,
    dry_run: bool,
) -> Result<Vec<SegmentDrop>> {
    let project_dir = project_dir.as_ref();
    let segments_dir = project_dir.join(".exo/ledger/segments");
    fs::create_dir_all(&segments_dir)
        .with_context(|| format!("create ledger directory {}", segments_dir.display()))?;
    let mut segments = fs::read_dir(&segments_dir)
        .with_context(|| format!("read ledger directory {}", segments_dir.display()))?
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| path.extension().and_then(|ext| ext.to_str()) == Some("jsonl"))
        .collect::<Vec<_>>();
    segments.sort();
    let current = segments.last().cloned();
    let cutoff = SystemTime::now()
        .checked_sub(older_than)
        .unwrap_or(SystemTime::UNIX_EPOCH);
    let candidates = segments
        .into_iter()
        .filter(|path| Some(path) != current.as_ref())
        .filter(|path| {
            fs::metadata(path)
                .and_then(|metadata| metadata.modified())
                .map(|modified| modified <= cutoff)
                .unwrap_or(false)
        })
        .map(|path| fingerprint(&path))
        .collect::<Result<Vec<_>>>()?;
    if dry_run || candidates.is_empty() {
        return Ok(candidates);
    }

    let lock_path = segments_dir.join(".retention.lock");
    let lock_file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .open(&lock_path)
        .with_context(|| format!("open retention lock {}", lock_path.display()))?;
    let _lock = Flock::lock(lock_file, FlockArg::LockExclusive)
        .map_err(|(_, error)| anyhow::anyhow!("lock ledger retention: {error}"))?;
    let event_log = EventLog::open(project_dir.join(".exo/logs"))
        .context("open event log for retention audit")?;
    let mut dropped = Vec::with_capacity(candidates.len());
    for candidate in candidates {
        if !candidate.path.exists() {
            continue;
        }
        event_log.append(
            "ledger.segment.dropped",
            "retention",
            &serde_json::json!({
                "segment_path": candidate.path,
                "segment_sha256": candidate.sha256,
                "bytes": candidate.bytes,
                "reason": candidate.reason,
                "crypto_shredded": false,
            }),
        )?;
        fs::remove_file(&candidate.path)
            .with_context(|| format!("drop ledger segment {}", candidate.path.display()))?;
        dropped.push(SegmentDrop {
            dropped: true,
            ..candidate
        });
    }
    Ok(dropped)
}

fn fingerprint(path: &Path) -> Result<SegmentDrop> {
    let bytes = fs::read(path).with_context(|| format!("read segment {}", path.display()))?;
    let mut digest = Sha256::new();
    digest.update(&bytes);
    Ok(SegmentDrop {
        path: path.to_path_buf(),
        bytes: bytes.len() as u64,
        sha256: format!("{:x}", digest.finalize()),
        reason: "retention policy".to_string(),
        dropped: false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::{LedgerEvent, LedgerWriter};
    use tempfile::TempDir;

    #[test]
    fn dry_run_does_not_remove_closed_segments() -> Result<()> {
        let temp = TempDir::new()?;
        let writer =
            LedgerWriter::with_segment_max_bytes(temp.path().join(".exo/ledger/segments"), 128)?;
        for _ in 0..4 {
            writer.append(&LedgerEvent::new(
                "custom.retention",
                None,
                serde_json::json!({
                    "payload": "x".repeat(100),
                }),
            ))?;
        }
        let segments = writer.segments()?;
        let result = drop_expired_segments(temp.path(), Duration::ZERO, true)?;
        assert_eq!(result.len(), segments.len().saturating_sub(1));
        assert_eq!(writer.segments()?.len(), segments.len());
        Ok(())
    }

    #[test]
    fn dropped_segments_are_audited_in_surviving_segment() -> Result<()> {
        let temp = TempDir::new()?;
        let writer =
            LedgerWriter::with_segment_max_bytes(temp.path().join(".exo/ledger/segments"), 128)?;
        for _ in 0..4 {
            writer.append(&LedgerEvent::new(
                "custom.retention",
                None,
                serde_json::json!({
                    "payload": "x".repeat(100),
                }),
            ))?;
        }
        let before = writer.segments()?;
        let result = drop_expired_segments(temp.path(), Duration::ZERO, false)?;
        assert!(!result.is_empty());
        let remaining = writer.segments()?;
        assert_eq!(remaining.len(), 1);
        assert!(writer
            .read_events()?
            .iter()
            .any(|record| record.event.event_type == "ledger.segment.dropped"));
        let current = remaining.last().context("surviving segment")?;
        assert!(before
            .iter()
            .filter(|path| path.exists())
            .all(|path| path == current));
        Ok(())
    }
}
