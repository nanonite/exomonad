use crate::domain::{BranchName, RoutingInfo};
use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use tokio::sync::Mutex;

pub const PUBLISHED_HEADS_FILENAME: &str = "published-heads.json";

/// A PR head that was confirmed by the Forgejo create/update response.
///
/// This is publication metadata for the existing issue-owned PR. It is not a
/// second owner or a process lifecycle record; invocation fields only explain
/// which attempt filed the publication when that metadata was available.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PublishedHead {
    pub pr_number: u64,
    pub head_branch: String,
    pub base_branch: String,
    pub head_sha: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub author_agent: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub author_role: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub invocation_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub invocation_trigger: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub invocation_runtime: Option<String>,
}

impl PublishedHead {
    pub fn matches_current(
        &self,
        pr_number: u64,
        head_branch: &str,
        base_branch: &str,
        head_sha: &str,
    ) -> bool {
        self.pr_number == pr_number
            && self.head_branch == head_branch
            && self.base_branch == base_branch
            && self.head_sha == head_sha
    }

    fn validate(&self) -> Result<()> {
        if self.pr_number == 0 {
            anyhow::bail!("verified publication must include a PR number");
        }
        for (field, value) in [
            ("head_branch", self.head_branch.as_str()),
            ("base_branch", self.base_branch.as_str()),
            ("head_sha", self.head_sha.as_str()),
        ] {
            if value.trim().is_empty() {
                anyhow::bail!("verified publication must include {field}");
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PublicationDisposition {
    Added,
    Duplicate,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct PublishedHeadsFile {
    #[serde(default)]
    heads: Vec<PublishedHead>,
}

fn published_heads_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

fn published_heads_path(project_dir: &Path) -> PathBuf {
    project_dir.join(".exo").join(PUBLISHED_HEADS_FILENAME)
}

async fn read_published_heads_locked(project_dir: &Path) -> Result<Vec<PublishedHead>> {
    let path = published_heads_path(project_dir);
    let contents = match tokio::fs::read_to_string(&path).await {
        Ok(contents) => contents,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => {
            return Err(error).with_context(|| format!("failed to read {}", path.display()))
        }
    };
    Ok(serde_json::from_str::<PublishedHeadsFile>(&contents)
        .with_context(|| format!("failed to parse {}", path.display()))?
        .heads)
}

pub async fn read_published_heads(project_dir: &Path) -> Result<Vec<PublishedHead>> {
    let _guard = published_heads_lock().lock().await;
    read_published_heads_locked(project_dir).await
}

/// Persist a verified publication without allowing duplicate events to grow
/// the registry. Different SHAs remain durable so the watcher can reject an
/// old publication when Forgejo has already confirmed a newer head.
pub async fn publish_verified_head(
    project_dir: &Path,
    publication: PublishedHead,
) -> Result<PublicationDisposition> {
    publication.validate()?;
    let _guard = published_heads_lock().lock().await;
    let mut file = PublishedHeadsFile {
        heads: read_published_heads_locked(project_dir).await?,
    };

    if file.heads.iter().any(|existing| {
        existing.pr_number == publication.pr_number
            && (existing.head_branch != publication.head_branch
                || existing.base_branch != publication.base_branch)
    }) {
        anyhow::bail!(
            "verified publication changes branch ownership for PR #{}",
            publication.pr_number
        );
    }

    if file.heads.iter().any(|existing| existing == &publication) {
        return Ok(PublicationDisposition::Duplicate);
    }

    file.heads.push(publication);
    let path = published_heads_path(project_dir);
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let temporary = path.with_extension("json.tmp");
    let bytes = serde_json::to_vec_pretty(&file)?;
    if let Err(error) = tokio::fs::write(&temporary, bytes).await {
        let _ = tokio::fs::remove_file(&temporary).await;
        return Err(error).with_context(|| format!("failed to write {}", temporary.display()));
    }
    if let Err(error) = tokio::fs::rename(&temporary, &path).await {
        let _ = tokio::fs::remove_file(&temporary).await;
        return Err(error).with_context(|| format!("failed to replace {}", path.display()));
    }
    Ok(PublicationDisposition::Added)
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PrState {
    #[default]
    Open,
    Merged,
    Closed,
    Stuck,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ForgejoReviewState {
    #[default]
    PendingReview,
    Commented,
    ChangesRequested,
    Approved,
}

/// Durable lifecycle for one reviewer process attempt.
///
/// The attempt is scoped to the existing PR owner and is never a workflow
/// owner.  Its PR/SHA/round tuple is the identity used to reject duplicate
/// spawns and stale verdicts across watcher restarts.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ReviewerAttemptPhase {
    #[default]
    Claimed,
    Running,
    Failed,
    Approved,
    ChangesRequested,
    Commented,
    Disposed,
    Stuck,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ReviewerAttempt {
    pub pr_number: u64,
    pub head_sha: String,
    pub round: u32,
    pub attempt_id: String,
    #[serde(default)]
    pub phase: ReviewerAttemptPhase,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub invocation_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reviewer_agent: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub routing: Option<RoutingInfo>,
    pub claimed_at: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub started_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub finished_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub failure: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PrEntry {
    pub number: u64,
    pub head_branch: String,
    pub base_branch: String,
    pub title: String,
    pub body: String,
    pub author_agent: String,
    pub author_role: String,
    pub created_at: DateTime<Utc>,
    #[serde(default)]
    pub state: PrState,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_review_at: Option<DateTime<Utc>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_head_sha: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub approved_at_sha: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reviewer_agent: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub reviewer_birth_branch: Option<String>,
    #[serde(default)]
    pub rounds: u32,
    #[serde(default)]
    pub stuck: bool,
    #[serde(default)]
    pub needs_human_review: bool,
    #[serde(default)]
    pub merge_blocked_on_ci: bool,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub chainlink_issue_id: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PrRegistry {
    pub prs: HashMap<u64, PrEntry>,
    #[serde(default = "default_next_number")]
    pub next_number: u64,
}

fn default_next_number() -> u64 {
    1
}

impl Default for PrRegistry {
    fn default() -> Self {
        Self {
            prs: HashMap::new(),
            next_number: 1,
        }
    }
}

impl PrRegistry {
    pub fn find_by_branch(&self, head_branch: &BranchName) -> Option<&PrEntry> {
        let branch_str = head_branch.as_str();
        self.prs.values().find(|pr| pr.head_branch == branch_str)
    }

    pub fn reviewer_for_pr(
        &self,
        pr_number: u64,
    ) -> Option<(BranchName, crate::services::agent_control::AgentType)> {
        let entry = self.prs.get(&pr_number)?;
        let birth_branch = entry.reviewer_birth_branch.as_ref()?;
        let agent_name = entry.reviewer_agent.as_ref()?;
        let agent_type = crate::services::agent_control::AgentType::from_dir_name(agent_name);
        Some((
            BranchName::try_from_str(birth_branch.as_str())
                .expect("validated string input is non-empty"),
            agent_type,
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn publication(sha: &str) -> PublishedHead {
        PublishedHead {
            pr_number: 42,
            head_branch: "main.feature-codex".to_string(),
            base_branch: "main".to_string(),
            head_sha: sha.to_string(),
            author_agent: Some("feature-codex".to_string()),
            author_role: Some("dev".to_string()),
            invocation_id: Some("invocation-1".to_string()),
            invocation_trigger: Some("resume_pr".to_string()),
            invocation_runtime: Some("codex".to_string()),
        }
    }

    #[tokio::test]
    async fn duplicate_publication_is_idempotent() {
        let directory = tempfile::tempdir().unwrap();
        let first = publication("sha-1");

        assert_eq!(
            publish_verified_head(directory.path(), first.clone())
                .await
                .unwrap(),
            PublicationDisposition::Added
        );
        assert_eq!(
            publish_verified_head(directory.path(), first)
                .await
                .unwrap(),
            PublicationDisposition::Duplicate
        );
        assert_eq!(
            read_published_heads(directory.path()).await.unwrap().len(),
            1
        );
    }

    #[tokio::test]
    async fn newer_head_is_retained_without_overwriting_publication_history() {
        let directory = tempfile::tempdir().unwrap();

        publish_verified_head(directory.path(), publication("sha-1"))
            .await
            .unwrap();
        publish_verified_head(directory.path(), publication("sha-2"))
            .await
            .unwrap();

        let heads = read_published_heads(directory.path()).await.unwrap();
        assert_eq!(heads.len(), 2);
        assert!(heads.iter().any(|head| head.matches_current(
            42,
            "main.feature-codex",
            "main",
            "sha-2"
        )));
        assert!(!heads[0].matches_current(42, "main.feature-codex", "main", "sha-2"));
    }

    #[test]
    fn stale_sha_does_not_match_the_current_forgejo_head() {
        let old = publication("sha-old");

        assert!(!old.matches_current(42, "main.feature-codex", "main", "sha-new"));
        assert!(old.matches_current(42, "main.feature-codex", "main", "sha-old"));
    }

    #[tokio::test]
    async fn missing_head_fields_cannot_be_published() {
        let directory = tempfile::tempdir().unwrap();
        let mut missing_sha = publication("sha-1");
        missing_sha.head_sha.clear();

        assert!(publish_verified_head(directory.path(), missing_sha)
            .await
            .is_err());
        assert!(!directory
            .path()
            .join(".exo")
            .join(PUBLISHED_HEADS_FILENAME)
            .exists());
    }
}
