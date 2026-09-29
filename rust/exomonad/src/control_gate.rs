//! Operator gate answers delegated to the canonical Python TL writer.
//!
//! The gate name arrives percent-encoded in one path level and is decoded by the
//! router before the handler runs; `exomonad::control_gate_name` owns that
//! encoding. A run id is different: it is a real directory under
//! `.exo/tl-loop/`, so it stays a single path component.

use exomonad::control_gate_name::{self, GateNameError};
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
    InvalidGateName(GateNameError),
    MissingRun,
    MissingGate,
    CommandFailed(String),
    Io(std::io::Error),
}

impl std::fmt::Display for GateAnswerError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidIdentifier(kind) => write!(formatter, "invalid {kind} identifier"),
            Self::InvalidGateName(error) => write!(formatter, "{error}"),
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
    validate_run_id(run_id)?;
    validate_gate_name(gate_name)?;
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
            "--source",
            "control",
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

/// A run id becomes a directory under `.exo/tl-loop/`, so it must stay one
/// path component. This is the traversal guard, and it does not relax.
fn validate_run_id(value: &str) -> Result<(), GateAnswerError> {
    if value.is_empty()
        || value == "."
        || value == ".."
        || PathBuf::from(value)
            .file_name()
            .and_then(|name| name.to_str())
            != Some(value)
    {
        return Err(GateAnswerError::InvalidIdentifier("run"));
    }
    Ok(())
}

/// A gate name is an argv value and a JSON key, not a path, so the canonical
/// encoding admits the `/` a per-slice gate name inherits from its slice id.
pub fn validate_gate_name(value: &str) -> Result<(), GateAnswerError> {
    control_gate_name::validate(value).map_err(|error| match error {
        GateNameError::PathNavigation | GateNameError::Empty => {
            GateAnswerError::InvalidIdentifier("gate")
        }
        other => GateAnswerError::InvalidGateName(other),
    })
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
    fn run_ids_cannot_escape_the_project() {
        assert!(matches!(
            validate_run_id("../outside"),
            Err(GateAnswerError::InvalidIdentifier("run"))
        ));
        assert!(matches!(
            validate_run_id("nested/run"),
            Err(GateAnswerError::InvalidIdentifier("run"))
        ));
    }

    #[test]
    fn a_gate_name_may_embed_a_slice_id_containing_a_slash() {
        // The gate is addressed through an argv value and a JSON key, never a
        // path, so a per-slice gate stays answerable when the slice id has a
        // `/` in it. This is the ordinary CLI spelling.
        assert!(validate_gate_name("tl-dispatch-failed-feat/auth").is_ok());
        assert!(validate_gate_name("tl-post-merge-feat/auth").is_ok());
    }

    #[test]
    fn a_navigating_gate_name_is_still_refused() {
        assert!(matches!(
            validate_gate_name(".."),
            Err(GateAnswerError::InvalidIdentifier("gate"))
        ));
        assert!(matches!(
            validate_gate_name(""),
            Err(GateAnswerError::InvalidIdentifier("gate"))
        ));
        assert!(matches!(
            validate_gate_name("gate\nname"),
            Err(GateAnswerError::InvalidGateName(
                GateNameError::ControlCharacter
            ))
        ));
    }
}
