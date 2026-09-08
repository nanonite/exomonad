//! Operator-facing cleanup command backed by the authenticated control API.

use crate::control;
use crate::uds_client::{self, ControlRequestError, ServerClient};
use anyhow::{Context, Result};
use clap::Args;
use exomonad_core::services::{
    CleanupReceipt, CleanupReceiptEntry, CleanupReceiptStatus, CleanupRequest,
};
use std::path::Path;
use tracing::{info, warn};

#[derive(Args, Debug, Clone, PartialEq, Eq)]
pub(crate) struct CleanArgs {
    /// Clean one managed agent by name or slug.
    #[arg(long, value_name = "NAME", conflicts_with = "sweep")]
    pub(crate) name: Option<String>,
    /// Inspect or clean every eligible managed agent.
    #[arg(long, conflicts_with = "name")]
    pub(crate) sweep: bool,
    /// Confirm resource removal. Without this flag the command is a dry run.
    #[arg(long)]
    pub(crate) apply: bool,
}

impl CleanArgs {
    pub(crate) fn request(&self) -> Result<CleanupRequest> {
        let request = CleanupRequest {
            target: self.name.clone(),
            sweep: self.sweep,
            apply: self.apply,
        };
        request
            .validate()
            .context("invalid exomonad clean arguments")?;
        Ok(request)
    }
}

pub(crate) async fn run(args: CleanArgs) -> Result<()> {
    let request = args.request()?;
    let socket = uds_client::find_server_socket().context(
        "cannot run cleanup: no project server socket found; start exomonad serve first",
    )?;
    let token = control_token()?;
    let client = ServerClient::new(socket);
    let receipt = client
        .post_control_json("/control/cleanup", &request, &token)
        .await
        .map_err(|error| anyhow::anyhow!("{}", control_error_message(&error)))?;
    println!("{}", render_receipt(&receipt));
    Ok(())
}

pub(crate) async fn query_continue_cleanup(project_dir: &Path) -> Result<CleanupReceipt> {
    let socket = project_dir.join(".exo/server.sock");
    if !socket.exists() {
        anyhow::bail!("project server socket is unavailable");
    }
    let socket = socket
        .canonicalize()
        .with_context(|| format!("failed to resolve {}", socket.display()))?;
    let token = control_token()?;
    let client = ServerClient::new(socket);
    client
        .post_control_json("/control/cleanup", &continue_cleanup_request(), &token)
        .await
}

pub(crate) async fn report_continue_cleanup(project_dir: &Path) {
    match query_continue_cleanup(project_dir).await {
        Ok(receipt) => report_continue_suggestion(&receipt),
        Err(error) => warn!(
            error = %error,
            "Unable to obtain cleanup suggestions; continuing without cleanup"
        ),
    }
}

pub(crate) fn continue_cleanup_request() -> CleanupRequest {
    CleanupRequest {
        target: None,
        sweep: true,
        apply: false,
    }
}

pub(crate) fn report_continue_suggestion(receipt: &CleanupReceipt) {
    if let Some(message) = continue_suggestion(receipt) {
        info!("{message}");
    }
}

pub(crate) fn continue_suggestion(receipt: &CleanupReceipt) -> Option<String> {
    let count = receipt
        .entries
        .iter()
        .filter(|entry| entry.status == CleanupReceiptStatus::WouldClean)
        .count();
    if count == 0 {
        return None;
    }
    Some(format!(
        "Found {count} verified cleanup candidate(s). Review with exomonad clean --sweep; apply only after review with exomonad clean --sweep --apply."
    ))
}

pub(crate) fn render_receipt(receipt: &CleanupReceipt) -> String {
    let mode = if receipt.dry_run {
        "dry-run (no resources were deleted)"
    } else {
        "apply"
    };
    let mut lines = vec![
        format!("Cleanup operation: {}", receipt.operation_id),
        format!("Cleanup plan: {}", receipt.plan_id),
        format!("Mode: {mode}"),
    ];
    append_receipt_entries(&mut lines, receipt);
    lines.join("\n")
}

fn append_receipt_entries(lines: &mut Vec<String>, receipt: &CleanupReceipt) {
    for entry in &receipt.entries {
        let name = entry_name(entry);
        match entry.status {
            CleanupReceiptStatus::WouldClean => {
                lines.push(format!("Would clean: {name}"));
            }
            CleanupReceiptStatus::Cleaned if receipt.dry_run => {
                lines.push(format!("Would clean: {name}"));
            }
            CleanupReceiptStatus::Cleaned => {
                lines.push(format!("Cleaned: {name}"));
            }
            CleanupReceiptStatus::Skipped => {
                append_reason(lines, "Skipped", name, entry);
            }
            CleanupReceiptStatus::Refused => {
                append_reason(lines, "Refused", name, entry);
            }
            CleanupReceiptStatus::Failed => {
                append_reason(lines, "Failed", name, entry);
            }
            CleanupReceiptStatus::InProgress => {
                append_reason(lines, "In progress", name, entry);
            }
        }
    }
}

fn append_reason(lines: &mut Vec<String>, status: &str, name: &str, entry: &CleanupReceiptEntry) {
    let reason = entry.reason.as_deref().unwrap_or("no reason provided");
    lines.push(format!("{status}: {name} — {reason}"));
}

fn entry_name(entry: &CleanupReceiptEntry) -> &str {
    if entry.agent_name.is_empty() {
        &entry.candidate_id
    } else {
        &entry.agent_name
    }
}

fn control_token() -> Result<String> {
    control_token_value(std::env::var(control::CONTROL_CREDENTIAL_ENV).ok())
}

fn control_token_value(value: Option<String>) -> Result<String> {
    value
        .filter(|value| !value.trim().is_empty())
        .context("cannot run cleanup: EXOMONAD_CONTROL_TOKEN is not configured")
}

fn control_error_message(error: &anyhow::Error) -> String {
    if let Some(error) = error.downcast_ref::<ControlRequestError>() {
        let kind = error
            .kind
            .as_deref()
            .map(|kind| format!(", {kind}"))
            .unwrap_or_default();
        return format!(
            "cleanup control request failed (HTTP {}{}): {}",
            error.status, kind, error.message
        );
    }
    format!("cleanup control request failed: {error:#}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::{Parser, Subcommand};

    #[derive(Parser)]
    #[command(name = "exomonad")]
    struct TestCli {
        #[command(subcommand)]
        command: TestCommand,
    }

    #[derive(Subcommand)]
    enum TestCommand {
        Clean(CleanArgs),
    }

    fn parse(args: &[&str]) -> Result<CleanArgs, clap::error::Error> {
        TestCli::try_parse_from(args).map(|cli| match cli.command {
            TestCommand::Clean(args) => args,
        })
    }

    fn receipt(dry_run: bool) -> CleanupReceipt {
        CleanupReceipt {
            schema_version: 1,
            operation_id: "operation-1".to_string(),
            plan_id: "plan-1".to_string(),
            started_at: 1,
            finished_at: 2,
            dry_run,
            entries: vec![
                CleanupReceiptEntry {
                    candidate_id: "would-id".to_string(),
                    agent_name: "would-clean".to_string(),
                    agent_slug: "would-slug".to_string(),
                    identity_snapshot: None,
                    pull_request: None,
                    status: CleanupReceiptStatus::WouldClean,
                    actions: Vec::new(),
                    reason: None,
                },
                CleanupReceiptEntry {
                    candidate_id: "cleaned-id".to_string(),
                    agent_name: "cleaned".to_string(),
                    agent_slug: "cleaned-slug".to_string(),
                    identity_snapshot: None,
                    pull_request: None,
                    status: CleanupReceiptStatus::Cleaned,
                    actions: Vec::new(),
                    reason: None,
                },
                CleanupReceiptEntry {
                    candidate_id: "refused-id".to_string(),
                    agent_name: "refused".to_string(),
                    agent_slug: "refused-slug".to_string(),
                    identity_snapshot: None,
                    pull_request: None,
                    status: CleanupReceiptStatus::Refused,
                    actions: Vec::new(),
                    reason: Some("agent is still live".to_string()),
                },
                CleanupReceiptEntry {
                    candidate_id: "skipped-id".to_string(),
                    agent_name: "skipped".to_string(),
                    agent_slug: "skipped-slug".to_string(),
                    identity_snapshot: None,
                    pull_request: None,
                    status: CleanupReceiptStatus::Skipped,
                    actions: Vec::new(),
                    reason: Some("worktree is dirty".to_string()),
                },
            ],
        }
    }

    #[test]
    fn clean_arguments_default_to_a_named_dry_run() {
        let args = parse(&["exomonad", "clean", "--name", "leaf"]).unwrap();
        assert_eq!(
            args.request().unwrap(),
            CleanupRequest {
                target: Some("leaf".to_string()),
                sweep: false,
                apply: false,
            }
        );
    }

    #[test]
    fn clean_arguments_map_explicit_apply() {
        let args = parse(&["exomonad", "clean", "--sweep", "--apply"]).unwrap();
        assert_eq!(
            args.request().unwrap(),
            CleanupRequest {
                target: None,
                sweep: true,
                apply: true,
            }
        );
    }

    #[test]
    fn clean_arguments_reject_mutually_exclusive_targets() {
        assert!(parse(&["exomonad", "clean", "--name", "leaf", "--sweep"]).is_err());
        assert!(parse(&["exomonad", "clean"]).unwrap().request().is_err());
    }

    #[test]
    fn clean_arguments_reuse_cleanup_target_validation() {
        let invalid = ["", "a/b"];
        for name in invalid {
            let args = CleanArgs {
                name: Some(name.to_string()),
                sweep: false,
                apply: false,
            };
            assert!(
                args.request().is_err(),
                "target should be rejected: {name:?}"
            );
        }
        let args = CleanArgs {
            name: Some("x".repeat(257)),
            sweep: false,
            apply: false,
        };
        assert!(args.request().is_err());
    }

    #[test]
    fn receipt_rendering_distinguishes_preview_and_applied_cleanup() {
        let preview = render_receipt(&receipt(true));
        assert!(preview.contains("Mode: dry-run (no resources were deleted)"));
        assert!(preview.contains("Would clean: would-clean"));
        assert!(!preview.contains("Cleaned:"));
        assert!(preview.contains("Refused: refused — agent is still live"));
        assert!(preview.contains("Skipped: skipped — worktree is dirty"));
        assert!(!preview.contains("Deleted"));

        let applied = render_receipt(&receipt(false));
        assert!(applied.contains("Mode: apply"));
        assert!(applied.contains("Cleaned: cleaned"));
    }

    #[test]
    fn continue_cleanup_request_can_never_apply() {
        let request = continue_cleanup_request();
        assert!(request.sweep);
        assert!(!request.apply);
    }

    #[test]
    fn missing_or_blank_control_tokens_are_rejected() {
        assert!(control_token_value(None).is_err());
        assert!(control_token_value(Some("  ".to_string())).is_err());
        assert_eq!(
            control_token_value(Some("secret".to_string())).unwrap(),
            "secret"
        );
    }

    #[test]
    fn control_errors_keep_http_status_and_structured_kind() {
        let source = ControlRequestError {
            status: 409,
            path: "/control/cleanup".to_string(),
            kind: Some("busy".to_string()),
            message: "cleanup is already running".to_string(),
        };
        let error: anyhow::Error = source.into();
        let rendered = control_error_message(&error);
        assert!(rendered.contains("HTTP 409, busy"));
        assert!(rendered.contains("cleanup is already running"));
    }

    #[tokio::test]
    async fn continue_cleanup_query_requires_a_running_server() {
        let project = tempfile::tempdir().unwrap();
        let error = query_continue_cleanup(project.path()).await.unwrap_err();
        assert!(error.to_string().contains("server socket is unavailable"));
    }

    #[test]
    fn continue_cleanup_suggestion_reports_only_would_clean_entries() {
        let message = continue_suggestion(&receipt(true)).unwrap();
        assert!(message.contains("1 verified cleanup candidate(s)"));
        assert!(message.contains("exomonad clean --sweep --apply"));

        let mut empty = receipt(true);
        empty.entries.clear();
        assert!(continue_suggestion(&empty).is_none());
    }
}
