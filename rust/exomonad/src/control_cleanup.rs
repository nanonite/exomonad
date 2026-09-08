//! Authenticated control-plane adapter for verified cleanup.

use exomonad_core::services::{CleanupReceipt, CleanupRequest, VerifiedCleanupService};
use std::fmt;

pub const MAX_REQUEST_BYTES: usize = 16 * 1024;

const LOCK_ERROR_PREFIX: &str = "cleanup is already in progress:";

#[derive(Debug, PartialEq, Eq)]
pub enum CleanupControlError {
    InvalidRequest(String),
    Busy(String),
    Service(String),
}

impl fmt::Display for CleanupControlError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidRequest(message) => {
                write!(formatter, "invalid cleanup request: {message}")
            }
            Self::Busy(message) => write!(formatter, "cleanup is busy: {message}"),
            Self::Service(message) => write!(formatter, "cleanup failed: {message}"),
        }
    }
}

impl std::error::Error for CleanupControlError {}

pub async fn execute(
    service: &VerifiedCleanupService,
    request: CleanupRequest,
) -> Result<CleanupReceipt, CleanupControlError> {
    request
        .validate()
        .map_err(|error| CleanupControlError::InvalidRequest(error.to_string()))?;
    service.run(&request).await.map_err(classify_service_error)
}

pub fn status_code(error: &CleanupControlError) -> axum::http::StatusCode {
    match error {
        CleanupControlError::InvalidRequest(_) => axum::http::StatusCode::BAD_REQUEST,
        CleanupControlError::Busy(_) => axum::http::StatusCode::CONFLICT,
        CleanupControlError::Service(_) => axum::http::StatusCode::INTERNAL_SERVER_ERROR,
    }
}

pub fn kind(error: &CleanupControlError) -> &'static str {
    match error {
        CleanupControlError::InvalidRequest(_) => "invalid_request",
        CleanupControlError::Busy(_) => "busy",
        CleanupControlError::Service(_) => "service_error",
    }
}

fn classify_service_error(error: anyhow::Error) -> CleanupControlError {
    let message = error.to_string();
    if message.starts_with(LOCK_ERROR_PREFIX) {
        CleanupControlError::Busy(message)
    } else {
        CleanupControlError::Service(message)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use exomonad_core::services::{AgentResolver, GitWorktreeService, MutexRegistry};
    use std::sync::Arc;
    use tempfile::tempdir;

    #[test]
    fn request_errors_are_bad_requests() {
        let error = CleanupControlError::InvalidRequest("target is empty".to_string());
        assert_eq!(status_code(&error), axum::http::StatusCode::BAD_REQUEST);
        assert_eq!(kind(&error), "invalid_request");
    }

    #[test]
    fn lock_errors_are_conflicts() {
        let error = classify_service_error(anyhow::anyhow!(
            "cleanup is already in progress: another operator"
        ));
        assert_eq!(
            error,
            CleanupControlError::Busy(
                "cleanup is already in progress: another operator".to_string()
            )
        );
        assert_eq!(status_code(&error), axum::http::StatusCode::CONFLICT);
    }

    #[test]
    fn cleanup_requests_reject_unknown_fields() {
        let request = serde_json::json!({
            "sweep": true,
            "apply": false,
            "unexpected": true,
        });
        assert!(serde_json::from_value::<CleanupRequest>(request).is_err());
    }

    #[tokio::test]
    async fn dry_run_uses_the_shared_service_and_persists_a_receipt() {
        let project = tempdir().expect("temporary project");
        let resolver = Arc::new(AgentResolver::load(project.path().to_path_buf()).await);
        let service = VerifiedCleanupService::new(
            project.path().to_path_buf(),
            resolver,
            Arc::new(GitWorktreeService::new(project.path().to_path_buf())),
            None,
            Arc::new(MutexRegistry::new()),
        );

        let receipt = execute(&service, CleanupRequest::default())
            .await
            .expect("dry-run receipt");

        assert!(receipt.dry_run);
        assert!(service
            .receipt_dir()
            .join(format!("{}.json", receipt.operation_id))
            .is_file());
    }
}
