use crate::domain::BranchName;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PrState {
    Open,
    Merged,
    Closed,
    Stuck,
}

impl Default for PrState {
    fn default() -> Self {
        Self::Open
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ForgejoReviewState {
    PendingReview,
    ChangesRequested,
    Approved,
}

impl Default for ForgejoReviewState {
    fn default() -> Self {
        Self::PendingReview
    }
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
