//! Typed inputs for the continuation brief.
//!
//! Adapters in [`adapters`] gather external state and preserve source failures
//! as [`SectionData::Unavailable`]. Rendering is deliberately a separate
//! concern so every consumer can make the same availability decision.

pub mod adapters;
pub mod renderer;

use adapters::{
    AgentInboxSummary, AgentSummary, ChainlinkIssue, ChainlinkIssueDetail, ChainlinkSession,
    PrSummary,
};

/// Result of gathering one continuation-brief section.
///
/// An available source with no records is represented by `Available(vec![])`.
/// There is intentionally no empty or default state: source failures remain
/// visible to the renderer and to callers assembling a brief.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SectionData<T> {
    Available(T),
    Unavailable { reason: String },
}

/// All state consumed by the continuation renderer.
///
/// `issue_detail` is the selected issue's full Chainlink record; callers that
/// do not have a selected issue should preserve that fact as an unavailable
/// section rather than inventing an empty detail record.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BriefInputs {
    pub run_id: String,
    pub agent_id: String,
    pub role: String,
    pub issues: SectionData<Vec<ChainlinkIssue>>,
    pub issue_detail: SectionData<ChainlinkIssueDetail>,
    pub sessions: SectionData<Vec<ChainlinkSession>>,
    pub unread_summary: SectionData<Vec<AgentInboxSummary>>,
    pub agents: SectionData<Vec<AgentSummary>>,
    pub open_prs: SectionData<Vec<PrSummary>>,
}

/// The subset of continuation state that belongs to one child agent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChildSlice {
    pub agent_id: String,
    pub issue_id: i64,
    pub pr_number: Option<u64>,
}
