//! Deterministic markdown rendering for continuation briefs.
//!
//! Rendering is intentionally pure.  The adapters own I/O and source
//! availability; this module only turns their typed snapshots and ledger rows
//! into stable, bounded markdown.

use super::adapters::{AgentSummary, ChainlinkIssue, PrSummary};
use super::{BriefInputs, ChildSlice, SectionData};
use crate::services::{MemoryKind, MemoryRecordRow};
use chrono::DateTime;

const MAX_BRIEF_BYTES: usize = 4096;
const BRIEF_OPEN: &str = "<exomonad-continuation-brief>";
const BRIEF_CLOSE: &str = "</exomonad-continuation-brief>";

/// Renders the full root/TL continuation brief.
pub fn render_tl(inputs: &BriefInputs, ledger: &[MemoryRecordRow]) -> String {
    fit_brief(
        &inputs.run_id,
        ledger,
        |dropped, count| render_tl_uncapped(inputs, ledger, dropped, count),
        minimal_tl_brief,
    )
}

/// Renders the parent context and the scoped work slice for one child.
pub fn render_child(
    inputs: &BriefInputs,
    ledger: &[MemoryRecordRow],
    slice: &ChildSlice,
) -> String {
    fit_brief(
        &inputs.run_id,
        ledger,
        |dropped, count| render_child_uncapped(inputs, ledger, slice, dropped, count),
        minimal_child_brief,
    )
}

fn fit_brief<F, M>(run_id: &str, ledger: &[MemoryRecordRow], render: F, minimal: M) -> String
where
    F: Fn(&[i64], usize) -> String,
    M: Fn(usize) -> String,
{
    let mut candidates = scoped_records(run_id, ledger, None);
    candidates.sort_by(record_priority);
    let mut dropped = Vec::new();
    let mut output = render(&dropped, 0);
    if output.len() <= MAX_BRIEF_BYTES {
        return output;
    }

    for record in candidates {
        dropped.push(record.id);
        output = render(&dropped, dropped.len());
        if output.len() <= MAX_BRIEF_BYTES {
            return output;
        }
    }

    minimal(dropped.len())
}

fn render_tl_uncapped(
    inputs: &BriefInputs,
    ledger: &[MemoryRecordRow],
    dropped: &[i64],
    dropped_count: usize,
) -> String {
    let records = scoped_records(inputs.run_id.as_str(), ledger, Some(dropped));
    let mut lines = vec![BRIEF_OPEN.to_string()];
    append_section(
        &mut lines,
        "Original plan",
        latest_record_lines(&records, MemoryKind::OriginalPlan),
        None,
    );
    append_section(
        &mut lines,
        "Current objective",
        latest_record_lines(&records, MemoryKind::WavePlan),
        None,
    );
    append_issue_section(&mut lines, inputs);
    append_child_section(&mut lines, inputs, &records, None);
    append_new_since_section(&mut lines, inputs, &records);
    append_section(
        &mut lines,
        "Decisions made",
        record_lines(&records, MemoryKind::Decision),
        None,
    );
    append_risk_section(&mut lines, inputs, &records);
    append_recommendation_section(&mut lines, inputs, &records);
    append_footer(&mut lines, dropped_count);
    lines.join("\n")
}

fn render_child_uncapped(
    inputs: &BriefInputs,
    ledger: &[MemoryRecordRow],
    slice: &ChildSlice,
    dropped: &[i64],
    dropped_count: usize,
) -> String {
    let records = scoped_records(inputs.run_id.as_str(), ledger, Some(dropped));
    let mut lines = vec![BRIEF_OPEN.to_string()];
    lines.push("## Parent plan context".to_string());
    append_values(
        &mut lines,
        latest_record_lines(&records, MemoryKind::OriginalPlan),
        None,
    );
    append_values(
        &mut lines,
        latest_record_lines(&records, MemoryKind::WavePlan),
        None,
    );
    append_child_workstream_values(&mut lines, inputs, &records, Some(&slice.agent_id));
    lines.push("## Your specific slice".to_string());
    append_child_issue(&mut lines, inputs, slice);
    append_child_pr(&mut lines, inputs, slice);
    let scoped = records
        .iter()
        .copied()
        .filter(|record| {
            record.agent_id == slice.agent_id && record.issue_id == Some(slice.issue_id)
        })
        .collect::<Vec<_>>();
    append_values(
        &mut lines,
        scoped_record_lines(
            &scoped,
            &[MemoryKind::FixDirection, MemoryKind::ReviewFeedback],
        ),
        None,
    );
    if dropped_count > 0 {
        lines.push(format!(
            "_(dropped {dropped_count} ledger records to fit the 4096-byte cap)_"
        ));
    }
    lines.push(BRIEF_CLOSE.to_string());
    lines.join("\n")
}

fn append_issue_section(lines: &mut Vec<String>, inputs: &BriefInputs) {
    let (values, unavailable) = match &inputs.issues {
        SectionData::Available(issues) => (active_issue_lines(issues), None),
        SectionData::Unavailable { reason } => (Vec::new(), Some(reason.as_str())),
    };
    append_section(lines, "Active issues", values, unavailable);
}

fn append_child_section(
    lines: &mut Vec<String>,
    inputs: &BriefInputs,
    records: &[&MemoryRecordRow],
    excluded_agent: Option<&str>,
) {
    lines.push("## Child workstreams".to_string());
    append_child_workstream_values(lines, inputs, records, excluded_agent);
}

fn append_child_workstream_values(
    lines: &mut Vec<String>,
    inputs: &BriefInputs,
    records: &[&MemoryRecordRow],
    excluded_agent: Option<&str>,
) {
    let agent_values = match &inputs.agents {
        SectionData::Available(agents) => live_child_lines(agents, inputs, records, excluded_agent),
        SectionData::Unavailable { reason } => {
            vec![unavailable_line(&format!("agents: {reason}"))]
        }
    };
    let prs_unavailable = match &inputs.open_prs {
        SectionData::Available(_) => None,
        SectionData::Unavailable { reason } => Some(unavailable_line(&format!("PRs: {reason}"))),
    };
    if agent_values.is_empty() && prs_unavailable.is_none() {
        lines.push("_(empty)_".to_string());
    } else {
        lines.extend(agent_values);
        if let Some(line) = prs_unavailable {
            lines.push(line);
        }
    }
}

fn append_new_since_section(
    lines: &mut Vec<String>,
    inputs: &BriefInputs,
    records: &[&MemoryRecordRow],
) {
    lines.push("## New since last run".to_string());
    let previous = latest_record(records, MemoryKind::SessionSummary);
    let cutoff = previous.map(|record| (record.created_at, record.id));
    let mut values = records
        .iter()
        .copied()
        .filter(|record| cutoff.is_none_or(|point| (record.created_at, record.id) > point))
        .map(format_record)
        .collect::<Vec<_>>();
    values.extend(match &inputs.issues {
        SectionData::Available(issues) => new_issue_lines(issues, cutoff),
        SectionData::Unavailable { reason } => {
            vec![unavailable_line(&format!("Chainlink issues: {reason}"))]
        }
    });
    values.sort();
    append_values(lines, values, None);
}

fn append_risk_section(
    lines: &mut Vec<String>,
    inputs: &BriefInputs,
    records: &[&MemoryRecordRow],
) {
    let mut values = record_lines(records, MemoryKind::Blocker);
    match &inputs.open_prs {
        SectionData::Available(prs) => {
            values.extend(prs.iter().filter(|pr| is_pr_risk(pr)).map(format_risk_pr))
        }
        SectionData::Unavailable { reason } => {
            values.push(unavailable_line(&format!("PRs: {reason}")));
        }
    }
    values.sort();
    append_section(lines, "Blockers / risks", values, None);
}

fn append_recommendation_section(
    lines: &mut Vec<String>,
    inputs: &BriefInputs,
    records: &[&MemoryRecordRow],
) {
    let mut actions = Vec::new();
    match &inputs.open_prs {
        SectionData::Available(prs) => {
            let mut merge_ready = prs
                .iter()
                .filter(|pr| is_merge_ready(pr))
                .collect::<Vec<_>>();
            merge_ready.sort_by_key(|pr| pr.number);
            actions.extend(
                merge_ready
                    .into_iter()
                    .map(|pr| format!("merge_pr #{} [MERGE READY]", pr.number)),
            );
            let mut feedback = prs
                .iter()
                .filter(|pr| has_review_feedback(pr))
                .collect::<Vec<_>>();
            feedback.sort_by_key(|pr| pr.number);
            actions.extend(feedback.into_iter().map(|pr| {
                format!(
                    "resume_pr #{} ({})",
                    pr.number,
                    pr.owner_agent.as_deref().unwrap_or(pr.head_branch.as_str())
                )
            }));
        }
        SectionData::Unavailable { reason } => {
            actions.push(unavailable_line(&format!("PRs: {reason}")));
        }
    }
    actions.extend(
        record_lines(records, MemoryKind::NextAction)
            .into_iter()
            .map(|line| line.trim_start_matches("- ").to_string()),
    );
    actions.extend(unread_actions(&inputs.unread_summary));
    actions.extend(unassigned_issue_actions(inputs));
    let mut next_number = 1;
    let numbered = actions
        .into_iter()
        .map(|action| {
            if action.starts_with("_(unavailable:") {
                action
            } else {
                let numbered = format!("{}. {action}", next_number);
                next_number += 1;
                numbered
            }
        })
        .collect::<Vec<_>>();
    append_section(lines, "Recommended next action", numbered, None);
}

fn append_child_issue(lines: &mut Vec<String>, inputs: &BriefInputs, slice: &ChildSlice) {
    match &inputs.issues {
        SectionData::Available(issues) => {
            let value = issues
                .iter()
                .find(|issue| issue.id == slice.issue_id)
                .map(format_issue)
                .unwrap_or_else(|| format!("- issue #{} (not in issue snapshot)", slice.issue_id));
            lines.push(value);
        }
        SectionData::Unavailable { reason } => {
            lines.push(unavailable_line(&format!("Chainlink issues: {reason}")));
        }
    }
}

fn append_child_pr(lines: &mut Vec<String>, inputs: &BriefInputs, slice: &ChildSlice) {
    let Some(number) = slice.pr_number else {
        lines.push("- PR: none recorded".to_string());
        return;
    };
    match &inputs.open_prs {
        SectionData::Available(prs) => {
            let value = prs
                .iter()
                .find(|pr| pr.number == number)
                .map(format_pr)
                .unwrap_or_else(|| format!("- PR #{number} (not in PR snapshot)"));
            lines.push(value);
        }
        SectionData::Unavailable { reason } => {
            lines.push(unavailable_line(&format!("PRs: {reason}")));
        }
    }
}

fn active_issue_lines(issues: &[ChainlinkIssue]) -> Vec<String> {
    let mut active = issues
        .iter()
        .filter(|issue| issue.status.eq_ignore_ascii_case("open"))
        .map(format_issue)
        .collect::<Vec<_>>();
    active.sort();
    active
}

fn new_issue_lines(issues: &[ChainlinkIssue], cutoff: Option<(i64, i64)>) -> Vec<String> {
    let mut values = issues
        .iter()
        .filter(|issue| {
            cutoff.is_none_or(|point| {
                issue
                    .created_at
                    .as_deref()
                    .and_then(parse_timestamp)
                    .is_some_and(|created| created > point.0)
            })
        })
        .map(|issue| {
            format!(
                "- Chainlink #{} {} ({})",
                issue.id,
                one_line(&issue.title),
                display_or_unknown(&issue.status)
            )
        })
        .collect::<Vec<_>>();
    values.sort();
    values
}

fn live_child_lines(
    agents: &[AgentSummary],
    inputs: &BriefInputs,
    records: &[&MemoryRecordRow],
    excluded_agent: Option<&str>,
) -> Vec<String> {
    let mut children = agents
        .iter()
        .filter(|agent| agent.alive && Some(agent.agent_id.as_str()) != excluded_agent)
        .collect::<Vec<_>>();
    children.sort_by(|left, right| left.agent_id.cmp(&right.agent_id));
    children
        .into_iter()
        .map(|agent| format_child_line(agent, inputs, records))
        .collect()
}

fn format_child_line(
    agent: &AgentSummary,
    inputs: &BriefInputs,
    records: &[&MemoryRecordRow],
) -> String {
    let mut line = format!("- {} ({})", agent.agent_id, agent.role);
    let mut agent_prs = available_prs(inputs)
        .iter()
        .filter(|pr| pr.owner_agent.as_deref() == Some(agent.agent_id.as_str()))
        .collect::<Vec<_>>();
    agent_prs.sort_by_key(|pr| pr.number);
    let prs = agent_prs
        .into_iter()
        .map(|pr| {
            format!(
                "PR #{} review={} ci={}",
                pr.number, pr.review_state, pr.ci_state
            )
        })
        .collect::<Vec<_>>();
    if !prs.is_empty() {
        line.push_str(": ");
        line.push_str(&prs.join("; "));
    }
    let mut handoffs = records
        .iter()
        .filter(|record| {
            record.agent_id == agent.agent_id && record.kind == MemoryKind::ChildHandoff
        })
        .map(|record| one_line(&record.summary))
        .collect::<Vec<_>>();
    handoffs.sort();
    if !handoffs.is_empty() {
        line.push_str(if prs.is_empty() { ": " } else { "; " });
        line.push_str("handoff: ");
        line.push_str(&handoffs.join(" | "));
    }
    if let SectionData::Available(unread) = &inputs.unread_summary {
        if let Some(summary) = unread.iter().find(|item| item.agent_id == agent.agent_id) {
            if summary.unread_count > 0 {
                line.push_str(&format!("; unread={}", summary.unread_count));
            }
        }
    }
    line
}

fn available_prs(inputs: &BriefInputs) -> &[PrSummary] {
    match &inputs.open_prs {
        SectionData::Available(prs) => prs,
        SectionData::Unavailable { .. } => &[],
    }
}

fn unread_actions(data: &SectionData<Vec<super::adapters::AgentInboxSummary>>) -> Vec<String> {
    let SectionData::Available(summaries) = data else {
        return match data {
            SectionData::Unavailable { reason } => {
                vec![unavailable_line(&format!("inbox: {reason}"))]
            }
            SectionData::Available(_) => Vec::new(),
        };
    };
    let mut values = summaries
        .iter()
        .filter(|summary| summary.unread_count > 0)
        .map(|summary| {
            format!(
                "check_inbox {} ({} unread)",
                summary.agent_id, summary.unread_count
            )
        })
        .collect::<Vec<_>>();
    values.sort();
    values
}

fn unassigned_issue_actions(inputs: &BriefInputs) -> Vec<String> {
    let SectionData::Available(issues) = &inputs.issues else {
        return Vec::new();
    };
    let SectionData::Available(sessions) = &inputs.sessions else {
        return match &inputs.sessions {
            SectionData::Unavailable { reason } => {
                vec![unavailable_line(&format!("sessions: {reason}"))]
            }
            SectionData::Available(_) => Vec::new(),
        };
    };
    let assigned = sessions
        .iter()
        .filter_map(|session| session.active_issue_id)
        .collect::<Vec<_>>();
    let mut values = issues
        .iter()
        .filter(|issue| issue.status.eq_ignore_ascii_case("open") && !assigned.contains(&issue.id))
        .map(|issue| format!("work on unassigned issue #{}", issue.id))
        .collect::<Vec<_>>();
    values.sort();
    values
}

fn record_lines(records: &[&MemoryRecordRow], kind: MemoryKind) -> Vec<String> {
    let mut selected = records
        .iter()
        .copied()
        .filter(|record| record.kind == kind)
        .collect::<Vec<_>>();
    selected.sort_by_key(|record| (record.created_at, record.id));
    selected.into_iter().map(format_record).collect()
}

fn scoped_record_lines(records: &[&MemoryRecordRow], kinds: &[MemoryKind]) -> Vec<String> {
    let mut selected = records
        .iter()
        .copied()
        .filter(|record| kinds.contains(&record.kind))
        .collect::<Vec<_>>();
    selected.sort_by_key(|record| (record.created_at, record.id));
    selected.into_iter().map(format_record).collect()
}

fn latest_record_lines(records: &[&MemoryRecordRow], kind: MemoryKind) -> Vec<String> {
    latest_record(records, kind)
        .map(format_record)
        .into_iter()
        .collect()
}

fn latest_record<'a>(
    records: &[&'a MemoryRecordRow],
    kind: MemoryKind,
) -> Option<&'a MemoryRecordRow> {
    records
        .iter()
        .copied()
        .filter(|record| record.kind == kind)
        .max_by_key(|record| (record.created_at, record.id))
}

fn scoped_records<'a>(
    run_id: &str,
    ledger: &'a [MemoryRecordRow],
    dropped: Option<&[i64]>,
) -> Vec<&'a MemoryRecordRow> {
    ledger
        .iter()
        .filter(|record| {
            record.run_id == run_id && dropped.is_none_or(|ids| !ids.contains(&record.id))
        })
        .collect()
}

fn record_priority(left: &&MemoryRecordRow, right: &&MemoryRecordRow) -> std::cmp::Ordering {
    left.importance
        .cmp(&right.importance)
        .then_with(|| left.created_at.cmp(&right.created_at))
        .then_with(|| left.id.cmp(&right.id))
}

fn append_section(
    lines: &mut Vec<String>,
    title: &str,
    values: Vec<String>,
    unavailable: Option<&str>,
) {
    lines.push(format!("## {title}"));
    if let Some(reason) = unavailable {
        lines.push(unavailable_line(reason));
    } else {
        append_values(lines, values, None);
    }
}

fn append_values(lines: &mut Vec<String>, values: Vec<String>, unavailable: Option<&str>) {
    if let Some(reason) = unavailable {
        lines.push(unavailable_line(reason));
    } else if values.is_empty() {
        lines.push("_(empty)_".to_string());
    } else {
        lines.extend(values);
    }
}

fn append_footer(lines: &mut Vec<String>, dropped_count: usize) {
    if dropped_count > 0 {
        lines.push(format!(
            "_(dropped {dropped_count} ledger records to fit the 4096-byte cap)_"
        ));
    }
    lines.push(BRIEF_CLOSE.to_string());
}

fn minimal_tl_brief(dropped_count: usize) -> String {
    let headings = [
        "Original plan",
        "Current objective",
        "Active issues",
        "Child workstreams",
        "New since last run",
        "Decisions made",
        "Blockers / risks",
        "Recommended next action",
    ];
    let mut lines = vec![BRIEF_OPEN.to_string()];
    for heading in headings {
        lines.push(format!("## {heading}"));
        lines.push("_(empty)_".to_string());
    }
    append_footer(&mut lines, dropped_count);
    lines.join("\n")
}

fn minimal_child_brief(dropped_count: usize) -> String {
    let mut lines = vec![BRIEF_OPEN.to_string(), "## Parent plan context".to_string()];
    lines.push("_(empty)_".to_string());
    lines.push("## Your specific slice".to_string());
    lines.push("_(empty)_".to_string());
    append_footer(&mut lines, dropped_count);
    lines.join("\n")
}

fn format_issue(issue: &ChainlinkIssue) -> String {
    format!(
        "- #{} {} ({})",
        issue.id,
        one_line(&issue.title),
        display_or_unknown(&issue.status)
    )
}

fn format_pr(pr: &PrSummary) -> String {
    format!(
        "- PR #{} {} review={} ci={}",
        pr.number, pr.head_branch, pr.review_state, pr.ci_state
    )
}

fn format_risk_pr(pr: &PrSummary) -> String {
    format!(
        "- PR #{} risk: review={} ci={}",
        pr.number, pr.review_state, pr.ci_state
    )
}

fn format_record(record: &MemoryRecordRow) -> String {
    let mut line = format!("- {}: {}", record.kind, one_line(&record.summary));
    if let Some(detail) = &record.detail {
        if !detail.trim().is_empty() {
            line.push_str(" — ");
            line.push_str(&one_line(detail));
        }
    }
    line
}

fn is_merge_ready(pr: &PrSummary) -> bool {
    pr.review_state.eq_ignore_ascii_case("approved")
        && matches!(
            pr.ci_state.to_ascii_lowercase().as_str(),
            "success" | "neutral"
        )
}

fn has_review_feedback(pr: &PrSummary) -> bool {
    matches!(
        pr.review_state.to_ascii_lowercase().as_str(),
        "changes_requested" | "commented" | "comment"
    )
}

fn is_pr_risk(pr: &PrSummary) -> bool {
    let review = pr.review_state.to_ascii_lowercase();
    let ci = pr.ci_state.to_ascii_lowercase();
    review.contains("stuck")
        || review.contains("failing")
        || ci.contains("stuck")
        || ci.contains("failing")
        || ci.contains("failed")
        || ci == "failure"
}

fn unavailable_line(reason: &str) -> String {
    format!("_(unavailable: {})_", one_line(reason))
}

fn display_or_unknown(value: &str) -> &str {
    if value.trim().is_empty() {
        "unknown"
    } else {
        value
    }
}

fn one_line(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn parse_timestamp(value: &str) -> Option<i64> {
    DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|timestamp| timestamp.timestamp())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::continuation::adapters::{
        AgentInboxSummary, ChainlinkIssue, ChainlinkSession, PrSummary,
    };

    fn record(
        id: i64,
        kind: MemoryKind,
        importance: i32,
        created_at: i64,
        summary: &str,
    ) -> MemoryRecordRow {
        MemoryRecordRow {
            id,
            run_id: "run-1".to_string(),
            agent_id: "root".to_string(),
            birth_branch: "main".to_string(),
            issue_id: Some(620),
            kind,
            importance,
            summary: summary.to_string(),
            detail: None,
            created_at,
            supersedes_id: None,
            metadata_json: None,
        }
    }

    fn inputs() -> BriefInputs {
        BriefInputs {
            run_id: "run-1".to_string(),
            agent_id: "root".to_string(),
            role: "tl".to_string(),
            issues: SectionData::Available(vec![
                ChainlinkIssue {
                    id: 622,
                    title: "Adapters".to_string(),
                    status: "open".to_string(),
                    priority: "high".to_string(),
                    description: None,
                    parent_id: Some(620),
                    created_at: Some("2026-08-04T10:00:00Z".to_string()),
                    updated_at: None,
                },
                ChainlinkIssue {
                    id: 623,
                    title: "Renderer".to_string(),
                    status: "open".to_string(),
                    priority: "high".to_string(),
                    description: None,
                    parent_id: Some(620),
                    created_at: Some("2026-08-04T11:00:00Z".to_string()),
                    updated_at: None,
                },
            ]),
            issue_detail: SectionData::Available(
                crate::services::continuation::adapters::ChainlinkIssueDetail {
                    id: 623,
                    title: "Renderer".to_string(),
                    status: "open".to_string(),
                    priority: "high".to_string(),
                    description: None,
                    parent_id: Some(620),
                    labels: vec!["session-memory".to_string()],
                    comments: Vec::new(),
                    created_at: None,
                    updated_at: None,
                },
            ),
            sessions: SectionData::Available(vec![ChainlinkSession {
                agent_id: Some("root".to_string()),
                active_issue_id: Some(623),
                handoff_notes: None,
                last_action: None,
                session_id: Some(1),
                started_at: None,
            }]),
            unread_summary: SectionData::Available(vec![AgentInboxSummary {
                agent_id: "leaf-a".to_string(),
                unread_count: 2,
                last_checked_at: None,
            }]),
            agents: SectionData::Available(vec![
                AgentSummary {
                    agent_id: "leaf-b".to_string(),
                    birth_branch: Some("main.leaf-b".to_string()),
                    role: "dev".to_string(),
                    alive: false,
                },
                AgentSummary {
                    agent_id: "leaf-a".to_string(),
                    birth_branch: Some("main.leaf-a".to_string()),
                    role: "dev".to_string(),
                    alive: true,
                },
            ]),
            open_prs: SectionData::Available(vec![
                PrSummary {
                    number: 42,
                    head_branch: "main.leaf-a".to_string(),
                    head_sha: Some("abc".to_string()),
                    owner_agent: Some("leaf-a".to_string()),
                    review_state: "approved".to_string(),
                    ci_state: "success".to_string(),
                },
                PrSummary {
                    number: 43,
                    head_branch: "main.leaf-b".to_string(),
                    head_sha: Some("def".to_string()),
                    owner_agent: Some("leaf-b".to_string()),
                    review_state: "changes_requested".to_string(),
                    ci_state: "failure".to_string(),
                },
            ]),
        }
    }

    #[test]
    fn renders_full_tl_golden() {
        let ledger = vec![
            record(
                1,
                MemoryKind::OriginalPlan,
                90,
                1,
                "Build continuation memory",
            ),
            record(
                2,
                MemoryKind::WavePlan,
                90,
                2,
                "Render deterministic markdown",
            ),
            record(3, MemoryKind::ChildHandoff, 70, 3, "Waiting for review"),
            record(4, MemoryKind::Decision, 80, 4, "Keep adapters pure"),
            record(5, MemoryKind::Blocker, 85, 5, "Forgejo is unavailable"),
            record(6, MemoryKind::SessionSummary, 60, 0, "Previous run"),
            record(7, MemoryKind::NextAction, 50, 6, "Run the renderer tests"),
        ];
        assert_eq!(
            render_tl(&inputs(), &ledger),
            include_str!("../../../tests/fixtures/brief/full_tl.md").trim_end()
        );
    }

    #[test]
    fn renders_empty_tl_golden() {
        let empty = BriefInputs {
            run_id: "run-empty".to_string(),
            agent_id: "root".to_string(),
            role: "tl".to_string(),
            issues: SectionData::Available(Vec::new()),
            issue_detail: SectionData::Available(
                crate::services::continuation::adapters::ChainlinkIssueDetail {
                    id: 0,
                    title: String::new(),
                    status: String::new(),
                    priority: String::new(),
                    description: None,
                    parent_id: None,
                    labels: Vec::new(),
                    comments: Vec::new(),
                    created_at: None,
                    updated_at: None,
                },
            ),
            sessions: SectionData::Available(Vec::new()),
            unread_summary: SectionData::Available(Vec::new()),
            agents: SectionData::Available(Vec::new()),
            open_prs: SectionData::Available(Vec::new()),
        };
        assert_eq!(
            render_tl(&empty, &[]),
            include_str!("../../../tests/fixtures/brief/empty_tl.md").trim_end()
        );
    }

    #[test]
    fn renders_child_golden_and_scopes_records() {
        let mut child_record = record(8, MemoryKind::FixDirection, 80, 7, "Refresh the fixture");
        child_record.agent_id = "leaf-a".to_string();
        child_record.issue_id = Some(623);
        let mut sibling_blocker = record(9, MemoryKind::Blocker, 100, 8, "Sibling blocker secret");
        sibling_blocker.agent_id = "leaf-b".to_string();
        sibling_blocker.issue_id = Some(622);
        let slice = ChildSlice {
            agent_id: "leaf-a".to_string(),
            issue_id: 623,
            pr_number: Some(42),
        };
        assert_eq!(
            render_child(&inputs(), &[child_record, sibling_blocker], &slice),
            include_str!("../../../tests/fixtures/brief/child.md").trim_end()
        );
    }

    #[test]
    fn renders_unavailable_sections_explicitly() {
        let mut unavailable = inputs();
        unavailable.issues = SectionData::Unavailable {
            reason: "Chainlink command failed".to_string(),
        };
        unavailable.agents = SectionData::Unavailable {
            reason: "agent registry failed".to_string(),
        };
        unavailable.open_prs = SectionData::Unavailable {
            reason: "Forgejo timed out".to_string(),
        };
        assert_eq!(
            render_tl(&unavailable, &[]),
            include_str!("../../../tests/fixtures/brief/unavailable.md").trim_end()
        );
    }

    #[test]
    fn rendering_is_byte_deterministic() {
        let ledger = vec![
            record(2, MemoryKind::Decision, 80, 2, "second"),
            record(1, MemoryKind::Decision, 80, 1, "first"),
        ];
        assert_eq!(render_tl(&inputs(), &ledger), render_tl(&inputs(), &ledger));
    }

    #[test]
    fn shuffled_inputs_have_identical_output() {
        let first = inputs();
        let mut second = inputs();
        if let SectionData::Available(issues) = &mut second.issues {
            issues.reverse();
        }
        if let SectionData::Available(agents) = &mut second.agents {
            agents.reverse();
        }
        if let SectionData::Available(prs) = &mut second.open_prs {
            prs.reverse();
        }
        let first_ledger = vec![
            record(2, MemoryKind::Decision, 80, 2, "second"),
            record(1, MemoryKind::Decision, 80, 1, "first"),
        ];
        let second_ledger = vec![first_ledger[1].clone(), first_ledger[0].clone()];
        assert_eq!(
            render_tl(&first, &first_ledger),
            render_tl(&second, &second_ledger)
        );
    }

    #[test]
    fn cap_drops_low_importance_old_records_and_preserves_high_records() {
        let mut ledger = Vec::new();
        for id in 0..80 {
            ledger.push(record(
                id,
                MemoryKind::Decision,
                if id == 79 { 100 } else { 1 },
                id,
                &format!("record-{id}-{}", "x".repeat(100)),
            ));
        }
        let output = render_tl(&inputs(), &ledger);
        assert!(output.len() <= MAX_BRIEF_BYTES);
        assert!(output.contains("dropped"));
        assert!(output.contains("record-79"));
    }

    #[test]
    fn child_brief_excludes_unrelated_sibling_blockers() {
        let mut blocker = record(10, MemoryKind::Blocker, 90, 1, "unrelated sibling blocker");
        blocker.agent_id = "leaf-b".to_string();
        blocker.issue_id = Some(622);
        let slice = ChildSlice {
            agent_id: "leaf-a".to_string(),
            issue_id: 623,
            pr_number: None,
        };
        assert!(!render_child(&inputs(), &[blocker], &slice).contains("unrelated sibling blocker"));
    }
}
