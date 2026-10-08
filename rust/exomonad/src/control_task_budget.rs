//! Authenticated, case-specific recovery from a parked TL task budget.

use axum::http::StatusCode;
use exomonad_core::services::EventLog;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use tokio::io::AsyncWriteExt;
use tokio::process::Command;

const PYTHON_ENV: &str = "EXOMONAD_TL_LOOP_PYTHON";
const EVENT_TYPE: &str = "operator.task_budget_reset";

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ResetTaskBudgetRequest {
    pub run_id: String,
    pub child_run_id: String,
    pub slice_id: String,
    pub expected_parent_revision: u64,
    pub expected_child_revision: u64,
    pub expected_attempt: u32,
    pub expected_charge_attempt: u32,
    pub expected_task_timeout_seconds: f64,
    pub new_task_timeout_seconds: f64,
    pub expected_estimated_tokens: u64,
    pub new_estimated_tokens: u64,
    pub task_type: String,
    pub operator_reason: String,
    pub idempotency_key: String,
    #[serde(default)]
    pub observed_tokens: Option<u64>,
}

#[derive(Debug)]
pub enum TaskBudgetError {
    InvalidIdentifier(&'static str),
    InvalidRequest(String),
    MissingRun,
    CommandFailed(String),
    Io(std::io::Error),
    EventLog(String),
}

impl std::fmt::Display for TaskBudgetError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidIdentifier(kind) => write!(formatter, "invalid {kind} identifier"),
            Self::InvalidRequest(message) => {
                write!(formatter, "invalid task budget reset: {message}")
            }
            Self::MissingRun => formatter.write_str("parent or child run state not found"),
            Self::CommandFailed(message) => {
                write!(formatter, "task budget reset failed: {message}")
            }
            Self::Io(error) => write!(formatter, "could not apply task budget reset: {error}"),
            Self::EventLog(message) => {
                write!(formatter, "could not record task budget reset: {message}")
            }
        }
    }
}

impl std::error::Error for TaskBudgetError {}

pub async fn execute(
    project_dir: &Path,
    run_id: &str,
    child_run_id: &str,
    slice_id: &str,
    request: ResetTaskBudgetRequest,
    event_log: &EventLog,
) -> Result<Value, TaskBudgetError> {
    validate_identifier(run_id, "run")?;
    validate_identifier(child_run_id, "child run")?;
    validate_identifier(slice_id, "slice")?;
    if request.run_id != run_id
        || request.child_run_id != child_run_id
        || request.slice_id != slice_id
    {
        return Err(TaskBudgetError::InvalidRequest(
            "body identities must match the route".to_string(),
        ));
    }
    validate_request(&request)?;
    let parent_path = run_path(project_dir, run_id);
    let child_path = project_dir
        .join(".exo")
        .join("tl-loop")
        .join(run_id)
        .join(child_run_id)
        .join("run.json");
    if !parent_path.is_file() || !child_path.is_file() {
        return Err(TaskBudgetError::MissingRun);
    }

    let python = std::env::var(PYTHON_ENV).unwrap_or_else(|_| "python3".to_string());
    let archive = std::env::var_os("EXOMONAD_TL_LOOP_ARCHIVE")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".exo/tl_loop.pyz"))
        })
        .ok_or_else(|| {
            TaskBudgetError::CommandFailed("HOME is unavailable for the TL archive".to_string())
        })?;
    if !archive.is_file() {
        return Err(TaskBudgetError::CommandFailed(format!(
            "installed TL archive is missing at {}",
            archive.display()
        )));
    }
    let archive_arg = archive.to_string_lossy().into_owned();
    let mut child = Command::new(&python)
        .args([
            archive_arg.as_str(),
            "task-budget-reset",
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
        .map_err(TaskBudgetError::Io)?;
    let body = serde_json::to_vec(&request)
        .map_err(|error| TaskBudgetError::InvalidRequest(error.to_string()))?;
    if let Some(mut stdin) = child.stdin.take() {
        stdin.write_all(&body).await.map_err(TaskBudgetError::Io)?;
        stdin.shutdown().await.map_err(TaskBudgetError::Io)?;
    }
    let output = child
        .wait_with_output()
        .await
        .map_err(TaskBudgetError::Io)?;
    if !output.status.success() {
        let message = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(TaskBudgetError::InvalidRequest(if message.is_empty() {
            format!("python exited with {}", output.status)
        } else {
            message
        }));
    }
    let receipt: Value = serde_json::from_slice(&output.stdout)
        .map_err(|error| TaskBudgetError::CommandFailed(error.to_string()))?;
    if receipt.get("status").and_then(Value::as_str) != Some("applied")
        || receipt.get("idempotency_key").and_then(Value::as_str)
            != Some(request.idempotency_key.as_str())
    {
        return Err(TaskBudgetError::CommandFailed(
            "typed state writer returned a mismatched reset receipt".to_string(),
        ));
    }
    append_calibration_event(event_log, &request, &receipt)?;
    Ok(receipt)
}

fn append_calibration_event(
    event_log: &EventLog,
    request: &ResetTaskBudgetRequest,
    receipt: &Value,
) -> Result<(), TaskBudgetError> {
    let payload = json!({
        "run_id": request.run_id,
        "child_run_id": request.child_run_id,
        "slice_id": request.slice_id,
        "attempt": request.expected_attempt,
        "budget_charge_attempt": request.expected_charge_attempt,
        "task_type": request.task_type,
        "old_task_timeout_seconds": request.expected_task_timeout_seconds,
        "new_task_timeout_seconds": request.new_task_timeout_seconds,
        "old_estimated_tokens": request.expected_estimated_tokens,
        "new_estimated_tokens": request.new_estimated_tokens,
        "observed_tokens": request.observed_tokens,
        "operator_reason": request.operator_reason,
        "idempotency_key": request.idempotency_key,
        "budget_source": "operator",
        "parent_revision": receipt.get("parent_revision"),
        "child_revision": receipt.get("child_revision"),
        "pr_number": receipt.get("pr_number"),
        "applied": true,
        "source": "control",
        "lifecycle_state": "authoritative"
    });

    let records = event_log
        .ledger()
        .read_events()
        .map_err(|error| TaskBudgetError::EventLog(error.to_string()))?;
    for record in records {
        if record.event.event_type != EVENT_TYPE
            || record
                .event
                .data
                .get("idempotency_key")
                .and_then(Value::as_str)
                != Some(request.idempotency_key.as_str())
        {
            continue;
        }
        if record.event.data != payload {
            return Err(TaskBudgetError::InvalidRequest(
                "idempotency_key already records a different task budget reset".to_string(),
            ));
        }
        return Ok(());
    }
    event_log
        .append_with_seq(EVENT_TYPE, "operator", &payload)
        .map_err(|error| TaskBudgetError::EventLog(error.to_string()))?;
    Ok(())
}

pub fn status_code(error: &TaskBudgetError) -> StatusCode {
    match error {
        TaskBudgetError::InvalidIdentifier(_) | TaskBudgetError::InvalidRequest(_) => {
            StatusCode::BAD_REQUEST
        }
        TaskBudgetError::MissingRun => StatusCode::NOT_FOUND,
        TaskBudgetError::CommandFailed(_)
        | TaskBudgetError::Io(_)
        | TaskBudgetError::EventLog(_) => StatusCode::INTERNAL_SERVER_ERROR,
    }
}

fn validate_request(request: &ResetTaskBudgetRequest) -> Result<(), TaskBudgetError> {
    if request.task_type.len() > 64
        || request.task_type.len() < 2
        || !request.task_type.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || (index > 0 && byte == b'_')
        })
        || !request.task_type.as_bytes()[0].is_ascii_lowercase()
    {
        return Err(TaskBudgetError::InvalidRequest(
            "task_type must be a lowercase snake_case identifier".to_string(),
        ));
    }
    if request.operator_reason.trim().is_empty() || request.operator_reason.chars().count() > 512 {
        return Err(TaskBudgetError::InvalidRequest(
            "operator_reason must contain 1 to 512 characters".to_string(),
        ));
    }
    if request.idempotency_key.trim().is_empty() || request.idempotency_key.len() > 128 {
        return Err(TaskBudgetError::InvalidRequest(
            "idempotency_key must contain 1 to 128 bytes".to_string(),
        ));
    }
    if !request.expected_task_timeout_seconds.is_finite()
        || !request.new_task_timeout_seconds.is_finite()
        || request.expected_task_timeout_seconds <= 0.0
        || request.new_task_timeout_seconds > 86_400.0
    {
        return Err(TaskBudgetError::InvalidRequest(
            "task timeout must be positive and at most 86400 seconds".to_string(),
        ));
    }
    if request.expected_attempt == 0
        || request.expected_charge_attempt == 0
        || request.expected_estimated_tokens == 0
        || request.new_estimated_tokens > 10_000_000
        || request
            .observed_tokens
            .is_some_and(|tokens| tokens > 10_000_000)
    {
        return Err(TaskBudgetError::InvalidRequest(
            "attempt and token values must be positive and within the per-task maximum".to_string(),
        ));
    }
    Ok(())
}

fn validate_identifier(value: &str, kind: &'static str) -> Result<(), TaskBudgetError> {
    if value.is_empty()
        || value == "."
        || value == ".."
        || PathBuf::from(value)
            .file_name()
            .and_then(|name| name.to_str())
            != Some(value)
    {
        return Err(TaskBudgetError::InvalidIdentifier(kind));
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
