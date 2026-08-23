//! Authenticated, narrow recovery commands for the TL control plane.

use axum::http::StatusCode;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use tokio::io::AsyncWriteExt;
use tokio::process::Command;

const PYTHON_ENV: &str = "EXOMONAD_TL_LOOP_PYTHON";

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum RecoveryAuthorization {
    Policy {
        policy_id: String,
        recovery_round: u32,
    },
    Human {
        gate_name: String,
        decision_revision: u64,
    },
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryAction {
    Inspect,
    Retry,
    Wait,
    ApproveScope,
    Abandon,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ResumeRecoveryRequest {
    pub run_id: String,
    pub slice_id: String,
    pub expected_invocation_id: String,
    pub expected_generation: u64,
    pub expected_worktree_fingerprint: String,
    pub action: RecoveryAction,
    pub authorization: RecoveryAuthorization,
    pub idempotency_key: String,
}

#[derive(Debug)]
pub enum RecoveryCommandError {
    InvalidIdentifier(&'static str),
    InvalidRequest(String),
    MissingRun,
    CommandFailed(String),
    Io(std::io::Error),
}

impl std::fmt::Display for RecoveryCommandError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidIdentifier(kind) => write!(formatter, "invalid {kind} identifier"),
            Self::InvalidRequest(message) => {
                write!(formatter, "invalid recovery command: {message}")
            }
            Self::MissingRun => formatter.write_str("run state not found"),
            Self::CommandFailed(message) => write!(formatter, "recovery command failed: {message}"),
            Self::Io(error) => write!(formatter, "could not apply recovery command: {error}"),
        }
    }
}

impl std::error::Error for RecoveryCommandError {}

pub async fn execute(
    project_dir: &Path,
    run_id: &str,
    slice_id: &str,
    mut request: ResumeRecoveryRequest,
) -> Result<Value, RecoveryCommandError> {
    validate_identifier(run_id, "run")?;
    validate_identifier(slice_id, "slice")?;
    if request.run_id != run_id {
        return Err(RecoveryCommandError::InvalidRequest(
            "request run_id does not match route".to_string(),
        ));
    }
    if request.slice_id != slice_id {
        return Err(RecoveryCommandError::InvalidRequest(
            "request slice_id does not match route".to_string(),
        ));
    }
    validate_request(&mut request)?;
    if !run_path(project_dir, run_id).is_file() {
        return Err(RecoveryCommandError::MissingRun);
    }

    let python = std::env::var(PYTHON_ENV).unwrap_or_else(|_| "python3".to_string());
    let mut child = Command::new(&python)
        .args([
            "-m",
            "tl_loop",
            "recovery-command",
            "--project-root",
            project_dir.to_string_lossy().as_ref(),
            "--run-id",
            run_id,
        ])
        .current_dir(project_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(RecoveryCommandError::Io)?;
    let body = serde_json::to_vec(&request)
        .map_err(|error| RecoveryCommandError::InvalidRequest(error.to_string()))?;
    if let Some(mut stdin) = child.stdin.take() {
        stdin
            .write_all(&body)
            .await
            .map_err(RecoveryCommandError::Io)?;
        stdin.shutdown().await.map_err(RecoveryCommandError::Io)?;
    }
    let output = child
        .wait_with_output()
        .await
        .map_err(RecoveryCommandError::Io)?;
    if !output.status.success() {
        let message = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(RecoveryCommandError::InvalidRequest(
            if message.is_empty() {
                format!("python exited with {}", output.status)
            } else {
                message
            },
        ));
    }
    serde_json::from_slice(&output.stdout)
        .map_err(|error| RecoveryCommandError::CommandFailed(error.to_string()))
}

pub fn status_code(error: &RecoveryCommandError) -> StatusCode {
    match error {
        RecoveryCommandError::InvalidIdentifier(_) | RecoveryCommandError::InvalidRequest(_) => {
            StatusCode::BAD_REQUEST
        }
        RecoveryCommandError::MissingRun => StatusCode::NOT_FOUND,
        RecoveryCommandError::CommandFailed(_) | RecoveryCommandError::Io(_) => {
            StatusCode::INTERNAL_SERVER_ERROR
        }
    }
}

fn validate_request(request: &mut ResumeRecoveryRequest) -> Result<(), RecoveryCommandError> {
    for (value, field) in [
        (&request.expected_invocation_id, "expected_invocation_id"),
        (
            &request.expected_worktree_fingerprint,
            "expected_worktree_fingerprint",
        ),
        (&request.idempotency_key, "idempotency_key"),
    ] {
        if value.trim().is_empty() {
            return Err(RecoveryCommandError::InvalidRequest(format!(
                "{field} must be non-empty"
            )));
        }
    }
    Ok(())
}

fn validate_identifier(value: &str, kind: &'static str) -> Result<(), RecoveryCommandError> {
    if value.is_empty()
        || value == "."
        || value == ".."
        || PathBuf::from(value)
            .file_name()
            .and_then(|name| name.to_str())
            != Some(value)
    {
        return Err(RecoveryCommandError::InvalidIdentifier(kind));
    }
    Ok(())
}

fn run_path(project_dir: &Path, run_id: &str) -> PathBuf {
    project_dir
        .join(".exo")
        .join("tl-loop")
        .join(run_id)
        .join("run.json")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_rejects_path_traversal() {
        assert!(matches!(
            validate_identifier("../root", "run"),
            Err(RecoveryCommandError::InvalidIdentifier("run"))
        ));
    }

    #[test]
    fn authorization_and_action_are_typed_and_closed() {
        let request: ResumeRecoveryRequest = serde_json::from_value(serde_json::json!({
            "run_id": "root",
            "slice_id": "leaf",
            "expected_invocation_id": "inv",
            "expected_generation": 2,
            "expected_worktree_fingerprint": "sha",
            "action": "retry",
            "authorization": {"kind": "policy", "policy_id": "external_dependency", "recovery_round": 1},
            "idempotency_key": "cmd-1"
        }))
        .expect("typed command");
        assert!(matches!(request.action, RecoveryAction::Retry));
        assert!(matches!(
            request.authorization,
            RecoveryAuthorization::Policy { .. }
        ));
    }
}
