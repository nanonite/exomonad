//! Generation-scoped process-attempt metadata for issue-owned agents.
//!
//! An invocation belongs to the existing agent identity and worktree. It is
//! replaced when a new process attempt starts, but it never owns or removes
//! the identity, worktree, branch, PR, or inbox. The mutation lock plus
//! atomic rename keep a stale process exit from overwriting a newer attempt.

use super::{AgentType, RoutingInfo};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::sync::OnceLock;
use tokio::sync::Mutex;
use tracing::{debug, info, warn};
use uuid::Uuid;

pub const INVOCATION_FILENAME: &str = "invocation.json";

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum InvocationTrigger {
    Spawn,
    ResumePr,
    Review,
}

#[derive(Debug, Clone)]
pub struct InvocationMetadata {
    pub runtime: AgentType,
    pub trigger: InvocationTrigger,
    pub pr_number: Option<u64>,
    pub head_sha: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum InvocationStatus {
    Running,
    Exited,
    Failed,
    Killed,
    TimedOut,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct InvocationRecord {
    pub invocation_id: String,
    pub runtime: AgentType,
    pub trigger: InvocationTrigger,
    pub routing: RoutingInfo,
    pub started_at: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ended_at: Option<u64>,
    pub status: InvocationStatus,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exit_code: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pr_number: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub head_sha: Option<String>,
}

impl InvocationRecord {
    pub fn is_live(&self) -> bool {
        self.ended_at.is_none() && self.status == InvocationStatus::Running
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InvocationFinishResult {
    Finished(InvocationRecord),
    IgnoredStale,
    Missing,
}

fn mutation_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

fn invocation_path(agent_dir: &Path) -> std::path::PathBuf {
    agent_dir.join(INVOCATION_FILENAME)
}

fn unix_timestamp() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

async fn read_locked(agent_dir: &Path) -> Result<Option<InvocationRecord>> {
    let path = invocation_path(agent_dir);
    match tokio::fs::read_to_string(&path).await {
        Ok(contents) => Ok(Some(serde_json::from_str(&contents).with_context(
            || format!("failed to parse invocation record {}", path.display()),
        )?)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).with_context(|| format!("failed to read {}", path.display())),
    }
}

async fn write_atomic(agent_dir: &Path, record: &InvocationRecord) -> Result<()> {
    let path = invocation_path(agent_dir);
    let temporary = agent_dir.join(format!(".invocation-{}.tmp", Uuid::new_v4()));
    let json = serde_json::to_vec_pretty(record)?;
    if let Err(error) = tokio::fs::write(&temporary, json).await {
        let _ = tokio::fs::remove_file(&temporary).await;
        return Err(error).with_context(|| format!("failed to write {}", temporary.display()));
    }
    if let Err(error) = tokio::fs::rename(&temporary, &path).await {
        let _ = tokio::fs::remove_file(&temporary).await;
        return Err(error).with_context(|| format!("failed to replace {}", path.display()));
    }
    Ok(())
}

pub async fn read_invocation(agent_dir: &Path) -> Result<Option<InvocationRecord>> {
    read_locked(agent_dir).await
}

/// Read legacy metadata without allowing a malformed optional record to make
/// an agent look dead or trigger destructive cleanup.
pub async fn read_invocation_conservatively(agent_dir: &Path) -> Option<InvocationRecord> {
    match read_invocation(agent_dir).await {
        Ok(Some(record)) => Some(record),
        Ok(None) => {
            debug!(path = %invocation_path(agent_dir).display(), "No invocation record; treating agent as legacy-owned");
            None
        }
        Err(error) => {
            warn!(path = %invocation_path(agent_dir).display(), %error, "Ignoring malformed invocation record conservatively");
            None
        }
    }
}

/// Start a new process attempt for the existing agent identity.
pub async fn start_invocation(
    agent_dir: &Path,
    runtime: AgentType,
    trigger: InvocationTrigger,
    routing: RoutingInfo,
    pr_number: Option<u64>,
    head_sha: Option<String>,
) -> Result<InvocationRecord> {
    let _guard = mutation_lock().lock().await;
    let record = InvocationRecord {
        invocation_id: Uuid::new_v4().to_string(),
        runtime,
        trigger,
        routing,
        started_at: unix_timestamp(),
        ended_at: None,
        status: InvocationStatus::Running,
        exit_code: None,
        pr_number,
        head_sha,
    };
    write_atomic(agent_dir, &record).await?;
    crate::services::lifecycle::record_invocation_started(agent_dir, &record);
    info!(
        path = %invocation_path(agent_dir).display(),
        invocation_id = %record.invocation_id,
        trigger = ?record.trigger,
        "Started agent invocation"
    );
    Ok(record)
}

async fn finish_locked(
    agent_dir: &Path,
    invocation_id: &str,
    status: InvocationStatus,
    exit_code: Option<i32>,
) -> Result<InvocationFinishResult> {
    let Some(mut record) = read_locked(agent_dir).await? else {
        return Ok(InvocationFinishResult::Missing);
    };
    if record.invocation_id != invocation_id {
        return Ok(InvocationFinishResult::IgnoredStale);
    }
    if record.ended_at.is_some() {
        return Ok(InvocationFinishResult::Finished(record));
    }
    record.ended_at = Some(unix_timestamp());
    record.status = status;
    record.exit_code = exit_code;
    write_atomic(agent_dir, &record).await?;
    crate::services::lifecycle::record_invocation_finished(agent_dir, &record);
    info!(
        path = %invocation_path(agent_dir).display(),
        invocation_id,
        status = ?record.status,
        exit_code = ?record.exit_code,
        "Finished agent invocation"
    );
    Ok(InvocationFinishResult::Finished(record))
}

/// Finish only the current invocation generation identified by its ID.
pub async fn finish_invocation(
    agent_dir: &Path,
    invocation_id: &str,
    status: InvocationStatus,
    exit_code: Option<i32>,
) -> Result<InvocationFinishResult> {
    let _guard = mutation_lock().lock().await;
    finish_locked(agent_dir, invocation_id, status, exit_code).await
}

/// Finish and tombstone a routing target only when the current invocation
/// still owns the exact routing record supplied by the exiting process.
/// Missing records retain legacy cleanup behavior; malformed records are an
/// error and therefore leave routing untouched.
pub async fn finish_invocation_and_tombstone(
    agent_dir: &Path,
    expected_routing: &RoutingInfo,
    status: InvocationStatus,
    exit_code: Option<i32>,
) -> Result<InvocationFinishResult> {
    let _guard = mutation_lock().lock().await;
    let current = read_locked(agent_dir).await?;
    if let Some(record) = current.as_ref() {
        if &record.routing != expected_routing {
            warn!(
                path = %invocation_path(agent_dir).display(),
                invocation_id = %record.invocation_id,
                "Ignoring stale invocation exit for newer routing generation"
            );
            return Ok(InvocationFinishResult::IgnoredStale);
        }
    }

    let result = match current {
        Some(record) => finish_locked(agent_dir, &record.invocation_id, status, exit_code).await?,
        None => InvocationFinishResult::Missing,
    };
    let exited_at = unix_timestamp().to_string();
    tokio::fs::write(agent_dir.join("exited_at"), exited_at).await?;
    match tokio::fs::remove_file(agent_dir.join("routing.json")).await {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error).context("failed to remove exited agent routing"),
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::tmux_ipc::WindowId;
    use tempfile::tempdir;

    fn routing() -> RoutingInfo {
        RoutingInfo::window(WindowId::parse("@42").expect("valid window id"))
    }

    #[tokio::test]
    async fn invocation_record_persists_required_fields() {
        let dir = tempdir().expect("tempdir");
        let record = start_invocation(
            dir.path(),
            AgentType::Codex,
            InvocationTrigger::ResumePr,
            routing(),
            Some(580),
            Some("abc123".to_string()),
        )
        .await
        .expect("start invocation");

        let persisted = read_invocation(dir.path())
            .await
            .expect("read invocation")
            .expect("record exists");
        assert_eq!(persisted, record);
        assert!(!persisted.invocation_id.is_empty());
        assert!(persisted.is_live());
        assert!(dir.path().join(INVOCATION_FILENAME).exists());
    }

    #[tokio::test]
    async fn newer_invocation_survives_stale_finish_with_routing_intact() {
        let dir = tempdir().expect("tempdir");
        let first_routing = routing();
        first_routing.write_to_dir(dir.path()).await.unwrap();
        let first = start_invocation(
            dir.path(),
            AgentType::Codex,
            InvocationTrigger::Spawn,
            first_routing.clone(),
            None,
            None,
        )
        .await
        .expect("first invocation");
        let second_routing = RoutingInfo::window(WindowId::parse("@43").expect("window id"));
        let second = start_invocation(
            dir.path(),
            AgentType::Codex,
            InvocationTrigger::ResumePr,
            second_routing.clone(),
            Some(580),
            Some("def456".to_string()),
        )
        .await
        .expect("second invocation");
        second_routing.write_to_dir(dir.path()).await.unwrap();

        assert_eq!(
            finish_invocation(
                dir.path(),
                &first.invocation_id,
                InvocationStatus::Exited,
                Some(0),
            )
            .await
            .expect("finish stale invocation"),
            InvocationFinishResult::IgnoredStale
        );
        assert_eq!(read_invocation(dir.path()).await.unwrap(), Some(second));
        assert_eq!(
            finish_invocation_and_tombstone(
                dir.path(),
                &first_routing,
                InvocationStatus::Exited,
                Some(0),
            )
            .await
            .expect("stale tombstone"),
            InvocationFinishResult::IgnoredStale
        );
        assert_eq!(
            RoutingInfo::read_from_dir(dir.path()).await.unwrap(),
            second_routing
        );
        assert!(!dir.path().join("exited_at").exists());
    }

    #[tokio::test]
    async fn current_invocation_completion_records_outcome_code_and_end_time() {
        let dir = tempdir().expect("tempdir");
        let started = start_invocation(
            dir.path(),
            AgentType::Codex,
            InvocationTrigger::Spawn,
            routing(),
            None,
            None,
        )
        .await
        .expect("start invocation");
        let result = finish_invocation(
            dir.path(),
            &started.invocation_id,
            InvocationStatus::Failed,
            Some(17),
        )
        .await
        .expect("finish invocation");
        let InvocationFinishResult::Finished(finished) = result else {
            panic!("expected finished invocation");
        };
        assert_eq!(finished.status, InvocationStatus::Failed);
        assert_eq!(finished.exit_code, Some(17));
        assert!(finished.ended_at.is_some());
        assert!(!finished.is_live());
    }

    #[tokio::test]
    async fn one_shot_exit_tombstones_routing_and_preserves_exit_code() {
        let dir = tempdir().expect("tempdir");
        let expected_routing = routing();
        expected_routing.write_to_dir(dir.path()).await.unwrap();
        let started = start_invocation(
            dir.path(),
            AgentType::Codex,
            InvocationTrigger::Spawn,
            expected_routing.clone(),
            None,
            None,
        )
        .await
        .expect("start invocation");

        let result = finish_invocation_and_tombstone(
            dir.path(),
            &expected_routing,
            InvocationStatus::Exited,
            Some(0),
        )
        .await
        .expect("finish one-shot invocation");
        let InvocationFinishResult::Finished(finished) = result else {
            panic!("expected finished invocation");
        };
        assert_eq!(finished.invocation_id, started.invocation_id);
        assert_eq!(finished.status, InvocationStatus::Exited);
        assert_eq!(finished.exit_code, Some(0));
        assert!(!finished.is_live());
        assert!(dir.path().join("exited_at").exists());
        assert!(!dir.path().join("routing.json").exists());
    }

    #[tokio::test]
    async fn rejected_startup_does_not_create_running_invocation_record() {
        let dir = tempdir().expect("tempdir");
        assert!(!dir.path().join(INVOCATION_FILENAME).exists());
        assert!(matches!(
            crate::services::tmux_ipc::classify_window_startup(false),
            crate::services::tmux_ipc::WindowStartupStatus::ExitedBeforeReady
        ));
        assert!(read_invocation(dir.path()).await.unwrap().is_none());
    }

    #[tokio::test]
    async fn missing_and_malformed_records_are_conservative() {
        let dir = tempdir().expect("tempdir");
        assert!(read_invocation_conservatively(dir.path()).await.is_none());
        tokio::fs::write(dir.path().join(INVOCATION_FILENAME), "not-json")
            .await
            .expect("write malformed record");
        assert!(read_invocation_conservatively(dir.path()).await.is_none());
        assert!(
            finish_invocation(dir.path(), "old", InvocationStatus::Exited, None)
                .await
                .is_err()
        );
    }
}
