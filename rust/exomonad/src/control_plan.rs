//! Read-only validation adapter for operator plan proposals.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use tokio::io::AsyncWriteExt;
use tokio::process::Command;

const PYTHON_ENV: &str = "EXOMONAD_TL_LOOP_PYTHON";

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PlanProposalRequest {
    pub plan: Value,
}

#[derive(Debug)]
pub enum PlanProposalError {
    InvalidIdentifier,
    MissingRun,
    InvalidProposal(String),
    CommandFailed(String),
    Io(std::io::Error),
}

impl std::fmt::Display for PlanProposalError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidIdentifier => formatter.write_str("invalid run identifier"),
            Self::MissingRun => formatter.write_str("run state not found"),
            Self::InvalidProposal(message) => write!(formatter, "invalid plan proposal: {message}"),
            Self::CommandFailed(message) => write!(formatter, "plan validator failed: {message}"),
            Self::Io(error) => write!(formatter, "could not validate plan proposal: {error}"),
        }
    }
}

impl std::error::Error for PlanProposalError {}

impl From<std::io::Error> for PlanProposalError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

pub async fn propose_plan(
    project_dir: &Path,
    run_id: &str,
    request: PlanProposalRequest,
) -> Result<Value, PlanProposalError> {
    let python = std::env::var(PYTHON_ENV).unwrap_or_else(|_| "python3".to_string());
    propose_plan_with_python(project_dir, run_id, request, &python).await
}

async fn propose_plan_with_python(
    project_dir: &Path,
    run_id: &str,
    request: PlanProposalRequest,
    python: &str,
) -> Result<Value, PlanProposalError> {
    validate_identifier(run_id)?;
    if !run_path(project_dir, run_id).is_file() {
        return Err(PlanProposalError::MissingRun);
    }

    let mut child = Command::new(python)
        .args([
            "-m",
            "tl_loop",
            "plan-proposal",
            "--project-root",
            project_dir.to_string_lossy().as_ref(),
            "--run-id",
            run_id,
        ])
        .current_dir(project_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let body = serde_json::to_vec(&request)
        .map_err(|error| PlanProposalError::InvalidProposal(error.to_string()))?;
    if let Some(mut stdin) = child.stdin.take() {
        stdin.write_all(&body).await?;
        stdin.shutdown().await?;
    }
    let output = child.wait_with_output().await?;
    if !output.status.success() {
        let message = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(PlanProposalError::InvalidProposal(if message.is_empty() {
            "proposal did not pass the plan validator".to_string()
        } else {
            message
        }));
    }
    let value: Value = serde_json::from_slice(&output.stdout)
        .map_err(|error| PlanProposalError::CommandFailed(error.to_string()))?;
    if value.get("inert") != Some(&Value::Bool(true))
        || value.get("status") != Some(&Value::String("proposed".to_string()))
    {
        return Err(PlanProposalError::CommandFailed(
            "validator returned a non-proposal response".to_string(),
        ));
    }
    Ok(value)
}

fn run_path(project_dir: &Path, run_id: &str) -> PathBuf {
    project_dir
        .join(".exo")
        .join("tl-loop")
        .join(run_id)
        .join("run.json")
}

fn validate_identifier(value: &str) -> Result<(), PlanProposalError> {
    if value.is_empty()
        || value == "."
        || value == ".."
        || value.contains('/')
        || value.contains('\\')
    {
        return Err(PlanProposalError::InvalidIdentifier);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    #[test]
    fn proposal_body_is_closed() {
        let valid = serde_json::json!({"plan": {"leaves": []}});
        assert!(serde_json::from_value::<PlanProposalRequest>(valid).is_ok());

        let unknown = serde_json::json!({"plan": {"leaves": []}, "confirm": true});
        assert!(serde_json::from_value::<PlanProposalRequest>(unknown).is_err());
    }

    #[test]
    fn run_identifier_cannot_escape_state_root() {
        assert!(validate_identifier("root").is_ok());
        assert!(validate_identifier("../other").is_err());
        assert!(validate_identifier("nested/run").is_err());
    }

    #[tokio::test]
    async fn proposal_validation_does_not_write_run_state() {
        let project = tempdir().unwrap();
        let run_path = project.path().join(".exo/tl-loop/root/run.json");
        fs::create_dir_all(run_path.parent().unwrap()).unwrap();
        let original = br#"{"sentinel":"unchanged"}"#;
        fs::write(&run_path, original).unwrap();
        let validator = project.path().join("validator");
        fs::write(
            &validator,
            b"#!/bin/sh\ncat >/dev/null\nprintf '%s' '{\"run_id\":\"root\",\"plan\":{\"leaves\":[]},\"inert\":true,\"status\":\"proposed\"}'\n",
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut permissions = fs::metadata(&validator).unwrap().permissions();
            permissions.set_mode(0o755);
            fs::set_permissions(&validator, permissions).unwrap();
        }

        let result = propose_plan_with_python(
            project.path(),
            "root",
            PlanProposalRequest {
                plan: serde_json::json!({"leaves": []}),
            },
            validator.to_str().unwrap(),
        )
        .await
        .unwrap();

        assert_eq!(result["status"], "proposed");
        assert_eq!(result["inert"], true);
        assert_eq!(fs::read(&run_path).unwrap(), original);
    }
}
