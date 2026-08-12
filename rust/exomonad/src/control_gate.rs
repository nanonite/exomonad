//! Operator gate answers delegated to the canonical Python TL writer.

use serde::Deserialize;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use tokio::process::Command;

#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum GateDecision {
    Approve,
    Reject,
}

impl GateDecision {
    fn cli_flag(self) -> &'static str {
        match self {
            Self::Approve => "--approve",
            Self::Reject => "--reject",
        }
    }

    fn status(self) -> &'static str {
        match self {
            Self::Approve => "approved",
            Self::Reject => "rejected",
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct GateAnswerRequest {
    pub decision: GateDecision,
}

#[derive(Debug)]
pub enum GateAnswerError {
    InvalidIdentifier(&'static str),
    MissingRun,
    MissingGate,
    CommandFailed(String),
    Io(std::io::Error),
}

impl std::fmt::Display for GateAnswerError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidIdentifier(kind) => write!(formatter, "invalid {kind} identifier"),
            Self::MissingRun => formatter.write_str("run state not found"),
            Self::MissingGate => formatter.write_str("named gate does not exist"),
            Self::CommandFailed(message) => write!(formatter, "gate answer failed: {message}"),
            Self::Io(error) => write!(formatter, "could not answer gate: {error}"),
        }
    }
}

impl std::error::Error for GateAnswerError {}

pub async fn answer_gate(
    project_dir: &Path,
    run_id: &str,
    gate_name: &str,
    request: GateAnswerRequest,
) -> Result<Value, GateAnswerError> {
    validate_identifier(run_id, "run")?;
    validate_identifier(gate_name, "gate")?;
    let state_path = project_dir
        .join(".exo")
        .join("tl-loop")
        .join(run_id)
        .join("run.json");
    if !state_path.is_file() {
        return Err(GateAnswerError::MissingRun);
    }

    let python = std::env::var("EXOMONAD_TL_LOOP_PYTHON").unwrap_or_else(|_| "python3".to_string());
    let output = Command::new(&python)
        .current_dir(project_dir)
        .args([
            "-m",
            "tl_loop",
            "gate",
            "--project-root",
            project_dir.to_string_lossy().as_ref(),
            "--run-id",
            run_id,
            "--name",
            gate_name,
            request.decision.cli_flag(),
        ])
        .output()
        .await
        .map_err(GateAnswerError::Io)?;

    if !output.status.success() {
        let message = String::from_utf8_lossy(&output.stderr).trim().to_string();
        if message.contains("does not exist") {
            return Err(GateAnswerError::MissingGate);
        }
        return Err(GateAnswerError::CommandFailed(if message.is_empty() {
            format!("python exited with {}", output.status)
        } else {
            message
        }));
    }

    Ok(json!({
        "run_id": run_id,
        "gate": gate_name,
        "status": request.decision.status(),
    }))
}

fn validate_identifier(value: &str, kind: &'static str) -> Result<(), GateAnswerError> {
    if value.is_empty()
        || value == "."
        || value == ".."
        || PathBuf::from(value)
            .file_name()
            .and_then(|name| name.to_str())
            != Some(value)
    {
        return Err(GateAnswerError::InvalidIdentifier(kind));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gate_decisions_use_closed_cli_flags_and_statuses() {
        assert_eq!(GateDecision::Approve.cli_flag(), "--approve");
        assert_eq!(GateDecision::Approve.status(), "approved");
        assert_eq!(GateDecision::Reject.cli_flag(), "--reject");
        assert_eq!(GateDecision::Reject.status(), "rejected");
    }

    #[test]
    fn gate_identifiers_cannot_escape_the_project() {
        assert!(matches!(
            validate_identifier("../outside", "gate"),
            Err(GateAnswerError::InvalidIdentifier("gate"))
        ));
    }
}
