//! State adapters used to assemble continuation-brief inputs.

use super::SectionData;
use crate::domain::CIStatus;
use crate::handlers::agent::resolve_agent_liveness;
use crate::services::agent_control::AgentControlService;
use crate::services::agent_resolver::AgentResolver;
use crate::services::forgejo::{ForgejoClient, ForgejoPullRequestReview};
use crate::services::inbox_store::InboxStore;
use crate::services::pr_registry::read_published_heads;
use crate::services::{
    repo, HasAgentResolver, HasGitHubClient, HasGitWorktreeService, HasProjectDir, HasTeamRegistry,
};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use tokio::process::Command;

const CHAINLINK_DB_ENV: &str = "CHAINLINK_DB";

/// A Chainlink issue as returned by `chainlink issue list --json`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChainlinkIssue {
    pub id: i64,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub priority: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub parent_id: Option<i64>,
    #[serde(default)]
    pub created_at: Option<String>,
    #[serde(default)]
    pub updated_at: Option<String>,
}

/// Full issue data returned by `chainlink issue show <id> --json`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChainlinkIssueDetail {
    pub id: i64,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub priority: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub parent_id: Option<i64>,
    #[serde(default)]
    pub labels: Vec<String>,
    #[serde(default)]
    pub comments: Vec<ChainlinkComment>,
    #[serde(default)]
    pub created_at: Option<String>,
    #[serde(default)]
    pub updated_at: Option<String>,
}

/// A durable comment attached to a Chainlink issue.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChainlinkComment {
    #[serde(default)]
    pub id: i64,
    #[serde(default)]
    pub content: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub created_at: Option<String>,
}

/// Session state from `chainlink session status --json`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChainlinkSession {
    pub agent_id: Option<String>,
    pub active_issue_id: Option<i64>,
    pub handoff_notes: Option<String>,
    pub last_action: Option<String>,
    pub session_id: Option<i64>,
    pub started_at: Option<String>,
}

/// Metadata-only inbox state for one recipient.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentInboxSummary {
    pub agent_id: String,
    pub unread_count: usize,
    pub last_checked_at: Option<i64>,
}

/// Agent identity and the same liveness result exposed by `AgentHandler::list`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentSummary {
    pub agent_id: String,
    pub birth_branch: Option<String>,
    pub role: String,
    pub alive: bool,
}

/// An open Forgejo PR with the state needed by the continuation renderer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PrSummary {
    pub number: u64,
    pub head_branch: String,
    pub head_sha: Option<String>,
    pub owner_agent: Option<String>,
    pub review_state: String,
    pub ci_state: String,
}

/// Shells out to the Chainlink CLI using the repository-local database.
#[derive(Clone)]
pub struct ChainlinkAdapter {
    project_dir: PathBuf,
}

impl ChainlinkAdapter {
    pub fn new(project_dir: impl Into<PathBuf>) -> Self {
        Self {
            project_dir: project_dir.into(),
        }
    }

    pub async fn issues(&self) -> SectionData<Vec<ChainlinkIssue>> {
        self.fetch_json(&["issue", "list", "--json"])
            .await
            .and_then(parse_json)
            .into_section()
    }

    pub async fn issue_detail(&self, id: i64) -> SectionData<ChainlinkIssueDetail> {
        let id = id.to_string();
        self.fetch_json(&["issue", "show", &id, "--json"])
            .await
            .and_then(parse_json)
            .into_section()
    }

    pub async fn sessions(&self) -> SectionData<Vec<ChainlinkSession>> {
        self.fetch_json(&["session", "status", "--json"])
            .await
            .and_then(|json| parse_sessions(&json))
            .into_section()
    }

    async fn fetch_json(&self, args: &[&str]) -> Result<String, String> {
        let command_line = std::iter::once("chainlink")
            .chain(args.iter().copied())
            .collect::<Vec<_>>()
            .join(" ");
        let db_path = self.project_dir.join(".chainlink").join("issues.db");
        tracing::info!(command = %command_line, "Continuation Chainlink command starting");

        let output = Command::new("chainlink")
            .args(args)
            .current_dir(&self.project_dir)
            .env(CHAINLINK_DB_ENV, &db_path)
            .output()
            .await
            .map_err(|error| {
                let reason = format!("failed to execute {command_line}: {error}");
                tracing::error!(command = %command_line, error = %error, "Continuation Chainlink command failed to launch");
                reason
            })?;
        let exit_code = output.status.code().unwrap_or(-1);
        tracing::info!(command = %command_line, exit_code, "Continuation Chainlink command completed");
        let stdout = String::from_utf8_lossy(&output.stdout).into_owned();
        let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
        decode_json_output(exit_code, &stdout, &stderr)
    }
}

/// Reads unread counts and inbox-check timestamps without changing messages.
#[derive(Clone)]
pub struct InboxAdapter {
    store: Arc<InboxStore>,
}

impl InboxAdapter {
    pub fn new(store: Arc<InboxStore>) -> Self {
        Self { store }
    }

    pub fn unread_summary(&self) -> SectionData<Vec<AgentInboxSummary>> {
        let candidates = match self.store.agents_needing_poke(0) {
            Ok(candidates) => candidates,
            Err(error) => return unavailable(error.to_string()),
        };
        let summaries = candidates
            .into_iter()
            .map(|candidate| {
                let last_checked_at = self.store.last_check_inbox_at(&candidate.agent_id)?;
                let unread_count = if self.store.has_unread(&candidate.agent_id)? {
                    candidate.unread_count
                } else {
                    0
                };
                Ok(AgentInboxSummary {
                    agent_id: candidate.agent_id,
                    unread_count,
                    last_checked_at,
                })
            })
            .collect::<anyhow::Result<Vec<_>>>();
        match summaries {
            Ok(summaries) => SectionData::Available(summaries),
            Err(error) => unavailable(error.to_string()),
        }
    }
}

/// Reads resolver identities while taking liveness from the canonical agent listing.
#[derive(Clone)]
pub struct AgentAdapter<C> {
    agent_control: Arc<AgentControlService<C>>,
    resolver: Arc<AgentResolver>,
}

impl<C> AgentAdapter<C>
where
    C: HasGitHubClient
        + HasTeamRegistry
        + HasAgentResolver
        + HasProjectDir
        + HasGitWorktreeService
        + 'static,
{
    pub fn new(agent_control: Arc<AgentControlService<C>>, resolver: Arc<AgentResolver>) -> Self {
        Self {
            agent_control,
            resolver,
        }
    }

    pub async fn agents(&self) -> SectionData<Vec<AgentSummary>> {
        let infos = match self.agent_control.list_agents().await {
            Ok(infos) => infos,
            Err(error) => return unavailable(error.to_string()),
        };
        let mut summaries = Vec::with_capacity(infos.len());
        for info in infos {
            let (alive, _) = resolve_agent_liveness(&info).await;
            let identity = self.resolver.get(&info.internal_name).await;
            summaries.push(AgentSummary {
                agent_id: info.internal_name.to_string(),
                birth_branch: identity
                    .as_ref()
                    .map(|record| record.birth_branch.to_string()),
                role: info
                    .agent_type
                    .map(|agent_type| agent_type.suffix().to_string())
                    .unwrap_or_else(|| "unknown".to_string()),
                alive,
            });
        }
        SectionData::Available(summaries)
    }
}

/// Reads open PR, review, and CI state from the configured Forgejo client.
#[derive(Clone)]
pub struct ForgejoAdapter {
    project_dir: PathBuf,
    client: Option<Arc<ForgejoClient>>,
}

impl ForgejoAdapter {
    pub fn new(project_dir: impl Into<PathBuf>, client: Option<Arc<ForgejoClient>>) -> Self {
        Self {
            project_dir: project_dir.into(),
            client,
        }
    }

    pub async fn open_prs(&self) -> SectionData<Vec<PrSummary>> {
        let Some(client) = &self.client else {
            return unavailable("Forgejo is not configured");
        };
        let repo_info = match repo::get_repo_info(&self.project_dir).await {
            Ok(repo_info) => repo_info,
            Err(error) => return unavailable(error.to_string()),
        };
        let prs = match client
            .list_open_pull_requests(&repo_info.owner, &repo_info.repo)
            .await
        {
            Ok(prs) => prs,
            Err(error) => return unavailable(error.to_string()),
        };
        let published = match read_published_heads(&self.project_dir).await {
            Ok(heads) => heads,
            Err(error) => return unavailable(error.to_string()),
        };
        let owners = published
            .into_iter()
            .map(|head| (head.pr_number, head.author_agent))
            .collect::<HashMap<_, _>>();
        let mut summaries = Vec::with_capacity(prs.len());
        for pr in prs {
            let reviews = match client
                .list_pull_request_reviews(&repo_info.owner, &repo_info.repo, pr.number)
                .await
            {
                Ok(reviews) => reviews,
                Err(error) => return unavailable(error.to_string()),
            };
            let ci_state = match &pr.head_sha {
                Some(sha) => match client
                    .list_commit_statuses(&repo_info.owner, &repo_info.repo, sha)
                    .await
                {
                    Ok(statuses) => ci_state(&statuses),
                    Err(error) => return unavailable(error.to_string()),
                },
                None => "unknown".to_string(),
            };
            summaries.push(PrSummary {
                number: pr.number.as_u64(),
                head_branch: pr.head_ref.to_string(),
                head_sha: pr.head_sha,
                owner_agent: owners.get(&pr.number.as_u64()).cloned().flatten(),
                review_state: review_state(&reviews),
                ci_state,
            });
        }
        SectionData::Available(summaries)
    }
}

fn review_state(reviews: &[ForgejoPullRequestReview]) -> String {
    if reviews
        .iter()
        .any(|review| review.state.eq_ignore_ascii_case("changes_requested"))
    {
        return "changes_requested".to_string();
    }
    if reviews
        .iter()
        .any(|review| review.state.eq_ignore_ascii_case("approved"))
    {
        return "approved".to_string();
    }
    if reviews.iter().any(|review| {
        matches!(
            review.state.to_ascii_lowercase().as_str(),
            "comment" | "commented"
        )
    }) {
        return "commented".to_string();
    }
    "pending_review".to_string()
}

fn ci_state(statuses: &[crate::services::forgejo::ForgejoCommitStatus]) -> String {
    if statuses.is_empty() {
        return "unknown".to_string();
    }
    if statuses
        .iter()
        .any(|status| status.status == CIStatus::Failure)
    {
        return CIStatus::Failure.to_string();
    }
    if statuses
        .iter()
        .any(|status| status.status == CIStatus::Pending)
    {
        return CIStatus::Pending.to_string();
    }
    if statuses
        .iter()
        .all(|status| status.status == CIStatus::Success)
    {
        return CIStatus::Success.to_string();
    }
    CIStatus::Neutral.to_string()
}

fn parse_sessions(json: &str) -> Result<Vec<ChainlinkSession>, String> {
    let value = serde_json::from_str::<Value>(json).map_err(|error| error.to_string())?;
    let records = match value {
        Value::Array(records) => records,
        record @ Value::Object(_) => vec![record],
        _ => return Err("session status JSON must be an object or array".to_string()),
    };
    records
        .iter()
        .map(parse_session)
        .collect::<Result<Vec<_>, _>>()
}

fn parse_session(value: &Value) -> Result<ChainlinkSession, String> {
    let object = value
        .as_object()
        .ok_or_else(|| "session status entry must be an object".to_string())?;
    let active_issue_id = object
        .get("active_issue_id")
        .and_then(json_i64)
        .or_else(|| {
            object
                .get("active_issue")
                .and_then(|issue| issue.get("id"))
                .and_then(json_i64)
        });
    Ok(ChainlinkSession {
        agent_id: json_string(object.get("agent_id").or_else(|| object.get("agent"))),
        active_issue_id,
        handoff_notes: json_string(object.get("handoff_notes").or_else(|| object.get("notes"))),
        last_action: json_string(object.get("last_action")),
        session_id: object.get("session_id").and_then(json_i64),
        started_at: json_string(object.get("started_at")),
    })
}

fn json_i64(value: &Value) -> Option<i64> {
    value
        .as_i64()
        .or_else(|| value.as_str().and_then(|value| value.parse().ok()))
}

fn json_string(value: Option<&Value>) -> Option<String> {
    value.and_then(|value| value.as_str().map(str::to_string))
}

fn parse_json<T: DeserializeOwned>(json: String) -> Result<T, String> {
    serde_json::from_str(&json).map_err(|error| error.to_string())
}

fn decode_json_output<T: DeserializeOwned>(
    exit_code: i32,
    stdout: &str,
    stderr: &str,
) -> Result<T, String> {
    if exit_code != 0 {
        let reason = if stderr.trim().is_empty() {
            format!("chainlink exited with status {exit_code}")
        } else {
            stderr.trim().to_string()
        };
        tracing::error!(exit_code, stderr = %stderr.trim(), "Continuation Chainlink command returned a failure");
        return Err(reason);
    }
    serde_json::from_str(stdout).map_err(|error| {
        tracing::error!(error = %error, "Continuation Chainlink JSON parse failed");
        format!("invalid Chainlink JSON: {error}")
    })
}

fn unavailable<T>(reason: impl Into<String>) -> SectionData<T> {
    SectionData::Unavailable {
        reason: reason.into(),
    }
}

trait SectionResult<T> {
    fn into_section(self) -> SectionData<T>;
}

impl<T> SectionResult<T> for Result<T, String> {
    fn into_section(self) -> SectionData<T> {
        match self {
            Ok(value) => SectionData::Available(value),
            Err(reason) => unavailable(reason),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const ISSUE_LIST_JSON: &str =
        r#"[{"id":622,"title":"Adapters","status":"open","priority":"high"}]"#;
    const ISSUE_DETAIL_JSON: &str = r#"{
        "id": 622,
        "title": "Adapters",
        "status": "open",
        "priority": "high",
        "description": "typed state",
        "labels": ["session-memory"],
        "comments": [{"id": 1, "content": "ready", "kind": "note"}]
    }"#;
    const SESSION_JSON: &str = r#"{
        "session_id": 139,
        "active_issue": {"id": 622},
        "handoff_notes": "continue",
        "last_action": "session work"
    }"#;

    #[test]
    fn chainlink_issue_fixture_parses() {
        let issues: Vec<ChainlinkIssue> = parse_json(ISSUE_LIST_JSON.to_string()).unwrap();
        assert_eq!(issues[0].id, 622);
        assert_eq!(issues[0].priority, "high");
    }

    #[test]
    fn chainlink_issue_detail_fixture_parses() {
        let detail: ChainlinkIssueDetail = parse_json(ISSUE_DETAIL_JSON.to_string()).unwrap();
        assert_eq!(detail.labels, vec!["session-memory"]);
        assert_eq!(detail.comments[0].content, "ready");
    }

    #[test]
    fn chainlink_session_fixture_parses() {
        let sessions = parse_sessions(SESSION_JSON).unwrap();
        assert_eq!(sessions[0].session_id, Some(139));
        assert_eq!(sessions[0].active_issue_id, Some(622));
        assert_eq!(sessions[0].handoff_notes.as_deref(), Some("continue"));
    }

    #[test]
    fn nonzero_exit_becomes_unavailable_with_stderr_reason() {
        let result = decode_json_output::<Value>(2, "", "database unavailable").into_section();
        assert!(matches!(
            result,
            SectionData::Unavailable { reason } if reason == "database unavailable"
        ));
    }

    #[test]
    fn malformed_json_becomes_unavailable() {
        let result = decode_json_output::<Value>(0, "not json", "").into_section();
        assert!(matches!(
            result,
            SectionData::Unavailable { reason } if reason.contains("invalid Chainlink JSON")
        ));
    }

    #[test]
    fn inbox_adapter_does_not_mark_messages_notified() {
        let store = Arc::new(InboxStore::open_in_memory().unwrap());
        store.write_message("root", "leaf", "one", None).unwrap();
        store.write_message("root", "leaf", "two", None).unwrap();

        let result = InboxAdapter::new(store.clone()).unread_summary();
        assert!(
            matches!(result, SectionData::Available(ref values) if values[0].unread_count == 2)
        );

        let still_unnotified = store.peek_unnotified("leaf").unwrap();
        assert_eq!(still_unnotified.len(), 2);
    }

    #[tokio::test]
    async fn forgejo_without_configuration_is_unavailable() {
        let result = ForgejoAdapter::new(".", None).open_prs().await;
        assert!(
            matches!(result, SectionData::Unavailable { reason } if reason == "Forgejo is not configured")
        );
    }
}
