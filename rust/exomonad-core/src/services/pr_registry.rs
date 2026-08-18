use crate::domain::{BranchName, RoutingInfo};
use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use tokio::sync::Mutex;

pub const PUBLISHED_HEADS_FILENAME: &str = "published-heads.json";
const PUBLISHED_HEADS_SCHEMA_VERSION: u32 = 1;

/// Identifies whether publication metadata came from the current ledger-owned
/// filing boundary or from a migrated pre-provenance record.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PublicationProvenance {
    Legacy,
    LedgerOwned,
}

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
    /// Explicitly identifies the publication's provenance contract.
    pub provenance: PublicationProvenance,
    /// The server-owned TL slice identifier for ledger-owned publications.
    ///
    /// Migrated legacy publications may omit this field; the watcher must
    /// never invent a slice from a branch slug.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub slice_id: Option<String>,
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
        if self
            .slice_id
            .as_deref()
            .is_some_and(|value| value.trim().is_empty())
        {
            anyhow::bail!("verified publication must not contain an empty slice_id");
        }
        if self.provenance == PublicationProvenance::LedgerOwned {
            if self
                .slice_id
                .as_deref()
                .is_none_or(|value| value.trim().is_empty())
            {
                anyhow::bail!(
                    "ledger-owned publication must include a non-empty resolver-backed slice_id"
                );
            }
            if self
                .invocation_id
                .as_deref()
                .is_none_or(|value| value.trim().is_empty())
            {
                anyhow::bail!("ledger-owned publication must include a non-empty invocation_id");
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PublicationDisposition {
    Added,
    Duplicate,
    Replaced,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PublishedHeadsFile {
    schema_version: u32,
    heads: Vec<PublishedHead>,
}

#[derive(Debug, Deserialize)]
struct RawPublishedHeadsFile {
    #[serde(default)]
    schema_version: Option<u32>,
    #[serde(default)]
    heads: Vec<serde_json::Value>,
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
    let raw = serde_json::from_str::<RawPublishedHeadsFile>(&contents)
        .with_context(|| format!("failed to parse {}", path.display()))?;
    let schema_version = raw.schema_version.unwrap_or(0);
    if schema_version > PUBLISHED_HEADS_SCHEMA_VERSION {
        anyhow::bail!(
            "{} uses unsupported publication registry schema version {}",
            path.display(),
            schema_version
        );
    }
    let heads = raw
        .heads
        .into_iter()
        .enumerate()
        .map(|(index, mut value)| {
            if schema_version == 0 {
                let object = value.as_object_mut().ok_or_else(|| {
                    anyhow::anyhow!("{} head {} must be a JSON object", path.display(), index)
                })?;
                object.insert("provenance".to_string(), serde_json::json!("legacy"));
            }
            serde_json::from_value::<PublishedHead>(value).with_context(|| {
                format!(
                    "failed to parse publication registry head {} in {}",
                    index,
                    path.display()
                )
            })
        })
        .collect::<Result<Vec<_>>>()?;

    let mut deduplicated = Vec::with_capacity(heads.len());
    for head in heads {
        let Some(existing) = deduplicated
            .iter_mut()
            .find(|existing: &&mut PublishedHead| {
                existing.matches_current(
                    head.pr_number,
                    head.head_branch.as_str(),
                    head.base_branch.as_str(),
                    head.head_sha.as_str(),
                )
            })
        else {
            deduplicated.push(head);
            continue;
        };
        if existing.provenance == PublicationProvenance::Legacy
            || head.provenance == PublicationProvenance::LedgerOwned
        {
            *existing = head;
        }
    }
    Ok(deduplicated)
}

pub async fn read_published_heads(project_dir: &Path) -> Result<Vec<PublishedHead>> {
    let _guard = published_heads_lock().lock().await;
    read_published_heads_locked(project_dir).await
}

/// Recover a slice's PR number from the durable publication registry when the
/// checkpoint never persisted it (e.g. a crash between `pr.filed` being
/// acknowledged and identity association). Ledger-owned publications are
/// preferred over migrated legacy ones; among equally-provenanced matches,
/// the most recently published entry wins.
pub fn resolve_pr_number_for_slice(heads: &[PublishedHead], slice_id: &str) -> Option<u64> {
    if slice_id.trim().is_empty() {
        return None;
    }
    let matching = || {
        heads
            .iter()
            .filter(|head| head.slice_id.as_deref() == Some(slice_id))
    };
    matching()
        .rfind(|head| head.provenance == PublicationProvenance::LedgerOwned)
        .or_else(|| matching().next_back())
        .map(|head| head.pr_number)
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
        schema_version: PUBLISHED_HEADS_SCHEMA_VERSION,
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

    let disposition = if let Some(index) = file.heads.iter().position(|existing| {
        existing.matches_current(
            publication.pr_number,
            publication.head_branch.as_str(),
            publication.base_branch.as_str(),
            publication.head_sha.as_str(),
        )
    }) {
        if file.heads[index].provenance == PublicationProvenance::LedgerOwned
            && publication.provenance == PublicationProvenance::Legacy
        {
            return Ok(PublicationDisposition::Duplicate);
        }
        file.heads[index] = publication;
        PublicationDisposition::Replaced
    } else {
        file.heads.push(publication);
        PublicationDisposition::Added
    };
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
    Ok(disposition)
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
            provenance: PublicationProvenance::Legacy,
            slice_id: None,
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

    #[tokio::test]
    async fn same_head_publication_upgrades_legacy_authority() {
        let directory = tempfile::tempdir().unwrap();
        publish_verified_head(directory.path(), publication("sha-1"))
            .await
            .unwrap();

        let mut authoritative = publication("sha-1");
        authoritative.provenance = PublicationProvenance::LedgerOwned;
        authoritative.slice_id = Some("slice-a".to_string());
        assert_eq!(
            publish_verified_head(directory.path(), authoritative)
                .await
                .unwrap(),
            PublicationDisposition::Replaced
        );

        let heads = read_published_heads(directory.path()).await.unwrap();
        assert_eq!(heads.len(), 1);
        assert_eq!(heads[0].provenance, PublicationProvenance::LedgerOwned);
        assert_eq!(heads[0].slice_id.as_deref(), Some("slice-a"));
    }

    #[tokio::test]
    async fn legacy_publication_cannot_shadow_ledger_owned_head() {
        let directory = tempfile::tempdir().unwrap();
        let mut authoritative = publication("sha-1");
        authoritative.provenance = PublicationProvenance::LedgerOwned;
        authoritative.slice_id = Some("slice-a".to_string());
        publish_verified_head(directory.path(), authoritative)
            .await
            .unwrap();

        assert_eq!(
            publish_verified_head(directory.path(), publication("sha-1"))
                .await
                .unwrap(),
            PublicationDisposition::Duplicate
        );
        let heads = read_published_heads(directory.path()).await.unwrap();
        assert_eq!(heads.len(), 1);
        assert_eq!(heads[0].provenance, PublicationProvenance::LedgerOwned);
    }

    #[test]
    fn stale_sha_does_not_match_the_current_forgejo_head() {
        let old = publication("sha-old");

        assert!(!old.matches_current(42, "main.feature-codex", "main", "sha-new"));
        assert!(old.matches_current(42, "main.feature-codex", "main", "sha-old"));
    }

    #[test]
    fn ledger_owned_publication_requires_slice_and_invocation() {
        let mut ledger_owned = publication("sha-1");
        ledger_owned.provenance = PublicationProvenance::LedgerOwned;
        assert!(ledger_owned
            .validate()
            .unwrap_err()
            .to_string()
            .contains("slice_id"));

        ledger_owned.slice_id = Some("slice-a".to_string());
        ledger_owned.invocation_id = None;
        assert!(ledger_owned
            .validate()
            .unwrap_err()
            .to_string()
            .contains("invocation_id"));
    }

    #[tokio::test]
    async fn legacy_registry_entries_are_explicitly_migrated() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join(".exo").join(PUBLISHED_HEADS_FILENAME);
        tokio::fs::create_dir_all(path.parent().unwrap())
            .await
            .unwrap();
        tokio::fs::write(
            &path,
            br#"{"heads":[{"pr_number":42,"head_branch":"main.feature-codex","base_branch":"main","head_sha":"sha-1","author_agent":"feature-codex","author_role":"dev","provenance":"ledger_owned"}]}"#,
        )
        .await
        .unwrap();

        let heads = read_published_heads(directory.path()).await.unwrap();
        assert_eq!(heads[0].provenance, PublicationProvenance::Legacy);
        assert!(heads[0].slice_id.is_none());
    }

    #[tokio::test]
    async fn new_registry_entries_persist_explicit_provenance() {
        let directory = tempfile::tempdir().unwrap();
        let mut publication = publication("sha-1");
        publication.provenance = PublicationProvenance::LedgerOwned;
        publication.slice_id = Some("slice-a".to_string());

        publish_verified_head(directory.path(), publication)
            .await
            .unwrap();
        let document: serde_json::Value = serde_json::from_slice(
            &tokio::fs::read(directory.path().join(".exo").join(PUBLISHED_HEADS_FILENAME))
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(document["schema_version"], PUBLISHED_HEADS_SCHEMA_VERSION);
        assert_eq!(document["heads"][0]["provenance"], "ledger_owned");
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

    #[test]
    fn resolve_pr_number_for_slice_prefers_ledger_owned_over_legacy() {
        let mut legacy = publication("sha-1");
        legacy.slice_id = Some("slice-a".to_string());
        legacy.provenance = PublicationProvenance::Legacy;

        let mut ledger_owned = publication("sha-2");
        ledger_owned.pr_number = 43;
        ledger_owned.slice_id = Some("slice-a".to_string());
        ledger_owned.provenance = PublicationProvenance::LedgerOwned;

        let heads = vec![legacy, ledger_owned];
        assert_eq!(resolve_pr_number_for_slice(&heads, "slice-a"), Some(43));
    }

    #[test]
    fn resolve_pr_number_for_slice_falls_back_to_legacy_when_no_ledger_owned_match() {
        let mut legacy = publication("sha-1");
        legacy.slice_id = Some("slice-a".to_string());
        legacy.provenance = PublicationProvenance::Legacy;

        let heads = vec![legacy];
        assert_eq!(resolve_pr_number_for_slice(&heads, "slice-a"), Some(42));
    }

    #[test]
    fn resolve_pr_number_for_slice_returns_none_when_unmatched_or_empty() {
        let mut other = publication("sha-1");
        other.slice_id = Some("slice-b".to_string());

        let heads = vec![other];
        assert_eq!(resolve_pr_number_for_slice(&heads, "slice-a"), None);
        assert_eq!(resolve_pr_number_for_slice(&heads, ""), None);
        assert_eq!(resolve_pr_number_for_slice(&[], "slice-a"), None);
    }
}
