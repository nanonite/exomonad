use crate::services::agent_resources::{dispose_agent_resources, dispose_reviewers_for_pr};
use crate::services::file_pr_local::{read_pr_registry, PrState};
use crate::services::git_worktree::GitWorktreeService;
use anyhow::{Context, Result};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;
use tokio::process::Command;
use tracing::{info, warn};

pub async fn run_orphan_reconciler(
    project_dir: Arc<std::path::PathBuf>,
    git_wt: Arc<GitWorktreeService>,
    interval: Duration,
) {
    let mut ticker = tokio::time::interval(interval);
    loop {
        ticker.tick().await;
        if let Err(err) = reconcile_once(&project_dir, git_wt.clone()).await {
            warn!(error = %err, "orphan reconciler tick failed");
        }
    }
}

pub async fn reconcile_once(project_dir: &Path, git_wt: Arc<GitWorktreeService>) -> Result<()> {
    reconcile_issue_worktrees(project_dir, git_wt.clone()).await?;
    reconcile_reviewer_worktrees(project_dir, git_wt).await?;
    Ok(())
}

async fn reconcile_issue_worktrees(
    project_dir: &Path,
    git_wt: Arc<GitWorktreeService>,
) -> Result<()> {
    let worktrees_dir = project_dir.join(".exo/worktrees");
    let Ok(mut entries) = tokio::fs::read_dir(&worktrees_dir).await else {
        return Ok(());
    };

    while let Some(entry) = entries.next_entry().await? {
        if !entry.file_type().await?.is_dir() {
            continue;
        }
        let Some(slug) = entry.file_name().to_str().map(str::to_string) else {
            continue;
        };
        let Some(issue_id) = issue_id_from_slug(&slug) else {
            continue;
        };
        if issue_closed_event_already_recorded(project_dir, issue_id).await? {
            continue;
        }
        if chainlink_issue_is_closed(project_dir, issue_id).await? {
            append_issue_closed_event(project_dir, issue_id, "orphan_reconciler").await?;
            dispose_agent_resources(project_dir, git_wt.clone(), &slug).await;
            info!(issue_id, agent = %slug, "Reconciled closed Chainlink issue for live worktree");
        }
    }
    Ok(())
}

async fn reconcile_reviewer_worktrees(
    project_dir: &Path,
    git_wt: Arc<GitWorktreeService>,
) -> Result<()> {
    let prs_path = project_dir.join(".exo/prs.json");
    let Ok(registry) = read_pr_registry(&prs_path).await else {
        return Ok(());
    };

    for (pr_number, pr) in registry.prs {
        if matches!(pr.state, PrState::Merged | PrState::Closed) {
            dispose_reviewers_for_pr(project_dir, git_wt.clone(), pr_number).await;
        }
    }
    Ok(())
}

async fn chainlink_issue_is_closed(project_dir: &Path, issue_id: u64) -> Result<bool> {
    let issue_arg = issue_id.to_string();
    let output = Command::new("chainlink")
        .current_dir(project_dir)
        .args(["show", &issue_arg])
        .output()
        .await
        .with_context(|| format!("failed to run chainlink show {issue_id}"))?;
    if !output.status.success() {
        warn!(
            issue_id,
            stderr = %String::from_utf8_lossy(&output.stderr).trim(),
            "chainlink show failed during orphan reconciliation"
        );
        return Ok(false);
    }
    Ok(chainlink_show_output_is_closed(&String::from_utf8_lossy(
        &output.stdout,
    )))
}

fn chainlink_show_output_is_closed(output: &str) -> bool {
    output
        .lines()
        .any(|line| line.trim().eq_ignore_ascii_case("Status: closed"))
}

fn issue_id_from_slug(slug: &str) -> Option<u64> {
    let rest = slug.strip_prefix("issue-")?;
    let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    if digits.is_empty() {
        None
    } else {
        digits.parse().ok()
    }
}

async fn issue_closed_event_already_recorded(project_dir: &Path, issue_id: u64) -> Result<bool> {
    let path = project_dir.join(".exo/events/issue_closed.jsonl");
    let Ok(content) = tokio::fs::read_to_string(path).await else {
        return Ok(false);
    };
    Ok(content
        .lines()
        .any(|line| issue_closed_line_matches(line, issue_id)))
}

fn issue_closed_line_matches(line: &str, issue_id: u64) -> bool {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(line) else {
        return false;
    };
    value
        .get("payload")
        .and_then(|payload| payload.get("issue_id"))
        .and_then(serde_json::Value::as_u64)
        == Some(issue_id)
}

async fn append_issue_closed_event(
    project_dir: &Path,
    issue_id: u64,
    closed_by: &str,
) -> Result<()> {
    let events_dir = project_dir.join(".exo/events");
    tokio::fs::create_dir_all(&events_dir).await?;
    let event = serde_json::json!({
        "event_type": "issue_closed",
        "payload": {
            "issue_id": issue_id,
            "closed_by": closed_by,
        }
    });
    let line = serde_json::to_string(&event)? + "\n";
    let path = events_dir.join("issue_closed.jsonl");
    let mut options = tokio::fs::OpenOptions::new();
    options.create(true).append(true);
    let mut file = options.open(path).await?;
    use tokio::io::AsyncWriteExt;
    file.write_all(line.as_bytes()).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_issue_id_from_worktree_slug() {
        assert_eq!(
            issue_id_from_slug("issue-313-runtime-hook-codex"),
            Some(313)
        );
        assert_eq!(issue_id_from_slug("review-pr-313-codex"), None);
        assert_eq!(issue_id_from_slug("issue-runtime-hook"), None);
    }

    #[test]
    fn parses_closed_chainlink_show_output() {
        let output = "Issue #313: test\nStatus: closed\nPriority: medium\n";
        assert!(chainlink_show_output_is_closed(output));
        assert!(!chainlink_show_output_is_closed("Status: open\n"));
    }

    #[test]
    fn detects_recorded_issue_closed_lines() {
        let line = r#"{"event_type":"issue_closed","payload":{"issue_id":313,"closed_by":"test"}}"#;
        assert!(issue_closed_line_matches(line, 313));
        assert!(!issue_closed_line_matches(line, 312));
    }
}
