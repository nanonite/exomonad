//! Shared request, plan, candidate, and receipt types for verified cleanup.

use crate::services::agent_resolver::AgentIdentityRecord;
use crate::services::forgejo::ForgejoPullRequest;
use crate::services::repo::RepositoryIdentity;
use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
pub const CLEANUP_PLAN_SCHEMA_VERSION: u32 = 1;
pub const CLEANUP_RECEIPT_SCHEMA_VERSION: u32 = 1;

const MAX_CLEANUP_TARGET_BYTES: usize = 256;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CleanupRequest {
    #[serde(default)]
    pub target: Option<String>,
    #[serde(default = "default_sweep")]
    pub sweep: bool,
    #[serde(default)]
    pub apply: bool,
}

const fn default_sweep() -> bool {
    true
}

impl Default for CleanupRequest {
    fn default() -> Self {
        Self {
            target: None,
            sweep: true,
            apply: false,
        }
    }
}

impl CleanupRequest {
    pub fn named(target: impl Into<String>) -> Self {
        Self {
            target: Some(target.into()),
            sweep: false,
            apply: false,
        }
    }

    pub fn validate(&self) -> Result<()> {
        if self.target.is_some() && self.sweep {
            bail!("cleanup target and sweep cannot be requested together");
        }
        if !self.sweep && self.target.is_none() {
            bail!("cleanup requires a named target or sweep=true");
        }
        if let Some(target) = &self.target {
            if target.trim().is_empty() {
                bail!("cleanup target cannot be empty");
            }
            if target.len() > MAX_CLEANUP_TARGET_BYTES {
                bail!("cleanup target exceeds the maximum length");
            }
            if target.contains('/') || target.contains('\\') {
                bail!("cleanup target must be an agent name or slug, not a path");
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CleanupLiveness {
    Live,
    Dead,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CleanupDecision {
    Cleanable,
    Refused { reason: String },
}

impl CleanupDecision {
    pub(super) fn refusal(reason: impl Into<String>) -> Self {
        Self::Refused {
            reason: reason.into(),
        }
    }

    pub fn is_cleanable(&self) -> bool {
        matches!(self, Self::Cleanable)
    }

    pub fn reason(&self) -> Option<&str> {
        match self {
            Self::Cleanable => None,
            Self::Refused { reason } => Some(reason),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CleanupPullRequest {
    pub number: u64,
    pub head_ref: String,
    pub base_ref: String,
    pub state: String,
    pub merged: bool,
    pub head_sha: Option<String>,
    pub merge_commit_sha: Option<String>,
}

impl From<ForgejoPullRequest> for CleanupPullRequest {
    fn from(pr: ForgejoPullRequest) -> Self {
        Self {
            number: pr.number.as_u64(),
            head_ref: pr.head_ref.to_string(),
            base_ref: pr.base_ref.to_string(),
            state: pr.state,
            merged: pr.merged,
            head_sha: pr.head_sha,
            merge_commit_sha: pr.merge_commit_sha,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CleanupCandidate {
    pub id: String,
    pub managed: bool,
    #[serde(default)]
    pub resolver_only: bool,
    #[serde(default)]
    pub recovery_receipt: bool,
    pub agent_name: String,
    pub issue: Option<String>,
    pub agent_dir: PathBuf,
    pub worktree_path: Option<PathBuf>,
    pub local_branch: Option<String>,
    pub local_head_sha: Option<String>,
    pub remote_branch: Option<String>,
    pub remote_head_sha: Option<String>,
    pub pull_request: Option<CleanupPullRequest>,
    pub liveness: CleanupLiveness,
    pub dirty: Option<bool>,
    pub protected: bool,
    pub identity_drift: bool,
    pub identity_error: Option<String>,
    pub head_matches_pull_request: Option<bool>,
    pub remote_head_matches_pull_request: Option<bool>,
    pub identity: Option<AgentIdentityRecord>,
    pub decision: CleanupDecision,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CleanupPlan {
    pub schema_version: u32,
    pub plan_id: String,
    pub generated_at: u64,
    pub project_dir: PathBuf,
    pub repository: Option<RepositoryIdentity>,
    pub repository_error: Option<String>,
    pub candidates: Vec<CleanupCandidate>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CleanupReceiptStatus {
    WouldClean,
    InProgress,
    Cleaned,
    Skipped,
    Refused,
    Failed,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CleanupReceiptEntry {
    pub candidate_id: String,
    #[serde(default)]
    pub agent_name: String,
    #[serde(default)]
    pub agent_slug: String,
    #[serde(default)]
    pub identity_snapshot: Option<AgentIdentityRecord>,
    #[serde(default)]
    pub pull_request: Option<CleanupPullRequest>,
    pub status: CleanupReceiptStatus,
    pub actions: Vec<String>,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CleanupReceipt {
    pub schema_version: u32,
    pub operation_id: String,
    pub plan_id: String,
    pub started_at: u64,
    pub finished_at: u64,
    pub dry_run: bool,
    pub entries: Vec<CleanupReceiptEntry>,
}
