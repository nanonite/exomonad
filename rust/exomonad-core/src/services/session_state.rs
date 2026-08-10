//! Authoritative locked session-boundary state.

use super::sink_health;
use super::state_mirror::append_state_change;
use anyhow::{Context, Result};
use chrono::Utc;
use nix::fcntl::{Flock, FlockArg};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::fs::{self, OpenOptions};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionTransition {
    Fresh,
    Attach,
    Recreate,
}

impl SessionTransition {
    fn as_str(self) -> &'static str {
        match self {
            Self::Fresh => "fresh",
            Self::Attach => "attach",
            Self::Recreate => "recreate",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SessionState {
    pub session_id: String,
    pub created_at: String,
    pub started_by: String,
    pub init_mode: String,
    pub attach_mode: String,
    pub recreate_generation: u64,
    pub last_server_started_at: Option<String>,
    pub completeness_status: String,
}

impl SessionState {
    pub fn read(project_dir: &Path) -> Result<Option<Self>> {
        let path = state_path(project_dir);
        match fs::read_to_string(&path) {
            Ok(contents) => Ok(Some(
                serde_json::from_str(&contents)
                    .with_context(|| format!("parse {}", path.display()))?,
            )),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error).with_context(|| format!("read {}", path.display())),
        }
    }
}

pub fn state_path(project_dir: &Path) -> PathBuf {
    project_dir.join(".exo/session.json")
}

pub fn transition(
    project_dir: &Path,
    transition: SessionTransition,
    started_by: &str,
) -> Result<SessionState> {
    let exo_dir = project_dir.join(".exo");
    fs::create_dir_all(&exo_dir)
        .with_context(|| format!("create session directory {}", exo_dir.display()))?;
    let lock_path = exo_dir.join("session.lock");
    let lock_file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .open(&lock_path)
        .with_context(|| format!("open {}", lock_path.display()))?;
    let _lock = Flock::lock(lock_file, FlockArg::LockExclusive)
        .map_err(|(_, error)| anyhow::anyhow!("lock session state: {error}"))?;
    let previous = SessionState::read(project_dir)?;
    let now = Utc::now().to_rfc3339();
    let state = match (transition, previous.as_ref()) {
        (SessionTransition::Attach, Some(previous)) => SessionState {
            session_id: previous.session_id.clone(),
            created_at: previous.created_at.clone(),
            started_by: previous.started_by.clone(),
            init_mode: "attach".to_string(),
            attach_mode: "existing".to_string(),
            recreate_generation: previous.recreate_generation,
            last_server_started_at: previous.last_server_started_at.clone(),
            completeness_status: previous.completeness_status.clone(),
        },
        (SessionTransition::Recreate, previous) => SessionState {
            session_id: uuid::Uuid::new_v4().to_string(),
            created_at: now.clone(),
            started_by: started_by.to_string(),
            init_mode: "recreate".to_string(),
            attach_mode: "new".to_string(),
            recreate_generation: previous
                .map(|state| state.recreate_generation.saturating_add(1))
                .unwrap_or(1),
            last_server_started_at: None,
            completeness_status: "unknown".to_string(),
        },
        (SessionTransition::Fresh, previous) => SessionState {
            session_id: uuid::Uuid::new_v4().to_string(),
            created_at: now.clone(),
            started_by: started_by.to_string(),
            init_mode: "fresh".to_string(),
            attach_mode: "new".to_string(),
            recreate_generation: previous.map_or(0, |state| state.recreate_generation),
            last_server_started_at: None,
            completeness_status: "unknown".to_string(),
        },
        (SessionTransition::Attach, None) => SessionState {
            session_id: uuid::Uuid::new_v4().to_string(),
            created_at: now.clone(),
            started_by: started_by.to_string(),
            init_mode: "attach".to_string(),
            attach_mode: "new".to_string(),
            recreate_generation: 0,
            last_server_started_at: None,
            completeness_status: "unknown".to_string(),
        },
    };
    write_atomic(project_dir, &state)?;
    append_state_change(
        &state_path(project_dir),
        "session.state_changed",
        json!({
            "transition": transition.as_str(),
            "session_id": state.session_id,
            "init_mode": state.init_mode,
            "attach_mode": state.attach_mode,
            "recreate_generation": state.recreate_generation
        }),
    );
    Ok(state)
}

pub fn mark_server_started(project_dir: &Path, started_by: &str) -> Result<SessionState> {
    let state = transition_if_missing(project_dir, started_by)?;
    let exo_dir = project_dir.join(".exo");
    let lock_path = exo_dir.join("session.lock");
    let lock_file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .open(&lock_path)?;
    let _lock = Flock::lock(lock_file, FlockArg::LockExclusive)
        .map_err(|(_, error)| anyhow::anyhow!("lock session state: {error}"))?;
    let mut state = SessionState::read(project_dir)?.unwrap_or(state);
    state.last_server_started_at = Some(Utc::now().to_rfc3339());
    state.completeness_status = sink_health::startup_status(project_dir);
    write_atomic(project_dir, &state)?;
    append_state_change(
        &state_path(project_dir),
        "session.state_changed",
        json!({
            "transition": "server_start",
            "session_id": state.session_id,
            "completeness_status": state.completeness_status,
            "started_by": started_by
        }),
    );
    Ok(state)
}

fn transition_if_missing(project_dir: &Path, started_by: &str) -> Result<SessionState> {
    match SessionState::read(project_dir)? {
        Some(state) => Ok(state),
        None => transition(project_dir, SessionTransition::Fresh, started_by),
    }
}

fn write_atomic(project_dir: &Path, state: &SessionState) -> Result<()> {
    let exo_dir = project_dir.join(".exo");
    let path = state_path(project_dir);
    let temporary = exo_dir.join(format!(".session-{}.tmp", uuid::Uuid::new_v4()));
    fs::write(&temporary, serde_json::to_vec_pretty(state)?)
        .with_context(|| format!("write {}", temporary.display()))?;
    if let Err(error) = fs::rename(&temporary, &path) {
        let _ = fs::remove_file(&temporary);
        return Err(error).with_context(|| format!("replace {}", path.display()));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn fresh_attach_and_recreate_preserve_boundary_rules() -> Result<()> {
        let temp = TempDir::new()?;
        let fresh = transition(temp.path(), SessionTransition::Fresh, "test")?;
        assert_eq!(fresh.init_mode, "fresh");
        assert_eq!(fresh.attach_mode, "new");
        let attached = transition(temp.path(), SessionTransition::Attach, "test")?;
        assert_eq!(attached.session_id, fresh.session_id);
        assert_eq!(attached.attach_mode, "existing");
        let recreated = transition(temp.path(), SessionTransition::Recreate, "test")?;
        assert_ne!(recreated.session_id, fresh.session_id);
        assert_eq!(recreated.recreate_generation, 1);
        assert_eq!(SessionState::read(temp.path())?, Some(recreated));
        Ok(())
    }

    #[test]
    fn startup_without_health_is_unknown_and_success_is_complete() -> Result<()> {
        let temp = TempDir::new()?;
        assert_eq!(sink_health::startup_status(temp.path()), "unknown");
        transition(temp.path(), SessionTransition::Fresh, "test")?;
        let state = mark_server_started(temp.path(), "test")?;
        assert_eq!(state.completeness_status, "complete");
        Ok(())
    }
}
