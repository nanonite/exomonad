//! Durable fallback health for the structured event sink.

use anyhow::{Context, Result};
use chrono::Utc;
use nix::fcntl::{Flock, FlockArg};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fs::{self, OpenOptions};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SinkHealth {
    pub accepted_event_count: u64,
    pub rejected_event_count: u64,
    pub write_failure_count: u64,
    pub last_successful_seq: Option<u64>,
    pub measurement_status: String,
    pub last_error: Option<String>,
    pub updated_at: String,
}

impl Default for SinkHealth {
    fn default() -> Self {
        Self {
            accepted_event_count: 0,
            rejected_event_count: 0,
            write_failure_count: 0,
            last_successful_seq: None,
            measurement_status: "unknown".to_string(),
            last_error: None,
            updated_at: Utc::now().to_rfc3339(),
        }
    }
}

pub fn path(project_dir: &Path) -> PathBuf {
    project_dir.join(".exo/sink-health.json")
}

pub fn read(project_dir: &Path) -> Result<Option<SinkHealth>> {
    let path = path(project_dir);
    match fs::read_to_string(&path) {
        Ok(contents) => Ok(Some(
            serde_json::from_str(&contents).with_context(|| format!("parse {}", path.display()))?,
        )),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).with_context(|| format!("read {}", path.display())),
    }
}

pub fn record_success(project_dir: &Path, run_seq: u64) -> Result<()> {
    update(project_dir, |health| {
        health.accepted_event_count = health.accepted_event_count.saturating_add(1);
        health.last_successful_seq = Some(run_seq);
        health.measurement_status = "complete".to_string();
        health.last_error = None;
    })
}

pub fn record_failure(project_dir: &Path, error: &str) -> Result<()> {
    update(project_dir, |health| {
        health.rejected_event_count = health.rejected_event_count.saturating_add(1);
        health.write_failure_count = health.write_failure_count.saturating_add(1);
        health.measurement_status = "partial".to_string();
        health.last_error = Some(hash_error(error));
    })
}

pub fn startup_status(project_dir: &Path) -> String {
    match read(project_dir) {
        Ok(Some(health)) if health.write_failure_count > 0 => "partial".to_string(),
        Ok(Some(_)) => "complete".to_string(),
        Ok(None) | Err(_) => "unknown".to_string(),
    }
}

pub fn as_event_data(health: &SinkHealth) -> Value {
    serde_json::to_value(health).unwrap_or_else(|_| {
        serde_json::json!({
            "measurement_status": "unknown"
        })
    })
}

fn update<F>(project_dir: &Path, mutate: F) -> Result<()>
where
    F: FnOnce(&mut SinkHealth),
{
    let exo_dir = project_dir.join(".exo");
    fs::create_dir_all(&exo_dir)
        .with_context(|| format!("create sink-health directory {}", exo_dir.display()))?;
    let lock_path = exo_dir.join("sink-health.lock");
    let lock_file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .open(&lock_path)
        .with_context(|| format!("open {}", lock_path.display()))?;
    let _lock = Flock::lock(lock_file, FlockArg::LockExclusive)
        .map_err(|(_, error)| anyhow::anyhow!("lock sink health: {error}"))?;
    let output_path = path(project_dir);
    let mut health = match fs::read_to_string(&output_path) {
        Ok(contents) => serde_json::from_str(&contents)
            .with_context(|| format!("parse {}", output_path.display()))?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => SinkHealth::default(),
        Err(error) => return Err(error).with_context(|| format!("read {}", output_path.display())),
    };
    mutate(&mut health);
    health.updated_at = Utc::now().to_rfc3339();
    let temporary = exo_dir.join(format!(".sink-health-{}.tmp", uuid::Uuid::new_v4()));
    fs::write(&temporary, serde_json::to_vec_pretty(&health)?)
        .with_context(|| format!("write {}", temporary.display()))?;
    if let Err(error) = fs::rename(&temporary, &output_path) {
        let _ = fs::remove_file(&temporary);
        return Err(error).with_context(|| format!("replace {}", output_path.display()));
    }
    Ok(())
}

fn hash_error(error: &str) -> String {
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(error.as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn health_counters_classify_success_and_failure() -> Result<()> {
        let temp = TempDir::new()?;
        record_success(temp.path(), 4)?;
        record_failure(temp.path(), "disk full")?;
        let health = read(temp.path())?.context("health file")?;
        assert_eq!(health.accepted_event_count, 1);
        assert_eq!(health.rejected_event_count, 1);
        assert_eq!(health.write_failure_count, 1);
        assert_eq!(health.last_successful_seq, Some(4));
        assert_eq!(health.measurement_status, "partial");
        assert_ne!(health.last_error.as_deref(), Some("disk full"));
        Ok(())
    }
}
