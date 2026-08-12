//! Versioned expected-event contracts and denominator reconciliation.

use crate::services::LedgerRecord;
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

const CONTRACT_JSON: &str = include_str!("../../../../docs/observability/expected-events.v1.json");

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct ExpectedEventRule {
    pub rule_id: String,
    pub prerequisite_event: String,
    pub required_event: String,
    pub allowed_delay_ms: Option<u64>,
    pub allowed_window: Option<String>,
    pub applicable_sources: Vec<String>,
    pub legacy_confidence_rule: String,
    pub denominator_effect: String,
    #[serde(default)]
    pub prerequisite_data: BTreeMap<String, String>,
    #[serde(default)]
    pub required_data: BTreeMap<String, String>,
    #[serde(default)]
    pub correlation_fields: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct ExpectedEventContract {
    pub contract_id: String,
    pub version: u32,
    pub schema_version: u32,
    pub unit: String,
    pub rules: Vec<ExpectedEventRule>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DenominatorRow {
    pub rule_id: String,
    pub expected: u64,
    pub observed: u64,
    pub missing: u64,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DenominatorReport {
    pub contract_id: String,
    pub contract_version: u32,
    pub completeness_status: String,
    pub rows: Vec<DenominatorRow>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EventObservation {
    pub session_id: Option<String>,
    pub event_type: String,
    pub run_seq: Option<u64>,
    pub data: Value,
}

pub fn load_contract() -> Result<ExpectedEventContract> {
    serde_json::from_str(CONTRACT_JSON).context("parse expected-event contract")
}

/// Reconcile required transitions by session, preserving missing outcomes as
/// missing denominator rows rather than treating them as zero-valued success.
pub fn reconcile(records: &[LedgerRecord]) -> Result<DenominatorReport> {
    let observations = records
        .iter()
        .map(|record| EventObservation {
            session_id: record.event.session_id.clone(),
            event_type: record.event.event_type.clone(),
            run_seq: record.event.run_seq,
            data: record.event.data.clone(),
        })
        .collect::<Vec<_>>();
    reconcile_events(&observations)
}

pub fn reconcile_events(events: &[EventObservation]) -> Result<DenominatorReport> {
    let contract = load_contract()?;
    let mut rows = Vec::with_capacity(contract.rules.len());
    for rule in &contract.rules {
        let mut expected = 0;
        let mut observed = 0;
        let prerequisites = events
            .iter()
            .filter(|event| {
                event.event_type == rule.prerequisite_event
                    && event_data_matches(event, &rule.prerequisite_data)
            })
            .collect::<Vec<_>>();
        let mut matched_required = std::collections::HashSet::new();
        for prerequisite in prerequisites {
            expected += 1;
            if let Some((index, _)) = events.iter().enumerate().find(|(index, required)| {
                !matched_required.contains(index)
                    && same_session(prerequisite, required)
                    && required_matches(&rule.required_event, &required.event_type)
                    && required_data_matches(rule, prerequisite, required)
            }) {
                matched_required.insert(index);
                observed += 1;
            }
        }
        rows.push(DenominatorRow {
            rule_id: rule.rule_id.clone(),
            expected,
            observed,
            missing: expected.saturating_sub(observed),
        });
    }
    let completeness_status = match sequence_status_for_events(events) {
        "unknown" => "unknown",
        "partial" => "partial",
        "complete" if rows.iter().any(|row| row.missing > 0) => "partial",
        _ => "complete",
    }
    .to_string();
    Ok(DenominatorReport {
        contract_id: contract.contract_id,
        contract_version: contract.version,
        completeness_status,
        rows,
    })
}

fn sequence_status_for_events(events: &[EventObservation]) -> &'static str {
    let mut sequences = events
        .iter()
        .filter_map(|event| event.run_seq)
        .collect::<Vec<_>>();
    if sequences.is_empty() {
        return "unknown";
    }
    sequences.sort_unstable();
    if sequences
        .windows(2)
        .all(|window| window[1] == window[0].saturating_add(1))
    {
        "complete"
    } else {
        "partial"
    }
}

fn required_matches(required: &str, event_type: &str) -> bool {
    required
        .split(['/', '|'])
        .any(|candidate| candidate == event_type)
}

fn same_session(left: &EventObservation, right: &EventObservation) -> bool {
    left.session_id == right.session_id
}

fn required_data_matches(
    rule: &ExpectedEventRule,
    prerequisite: &EventObservation,
    required: &EventObservation,
) -> bool {
    if !rule.required_data.iter().all(|(field, expected)| {
        required
            .data
            .get(field)
            .and_then(Value::as_str)
            .is_some_and(|actual| actual == expected)
    }) {
        return false;
    }
    rule.correlation_fields.iter().all(|field| {
        let prerequisite_value = prerequisite.data.get(field);
        let required_value = required.data.get(field);
        prerequisite_value.is_some() && prerequisite_value == required_value
    })
}

fn event_data_matches(event: &EventObservation, expected: &BTreeMap<String, String>) -> bool {
    expected.iter().all(|(field, value)| {
        event
            .data
            .get(field)
            .and_then(Value::as_str)
            .is_some_and(|actual| actual == value)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::LedgerEvent;
    use std::path::PathBuf;

    fn record(event: LedgerEvent) -> LedgerRecord {
        LedgerRecord {
            segment: PathBuf::from("segment.jsonl"),
            line_number: 1,
            event,
        }
    }

    #[test]
    fn contract_is_versioned_and_reconciles_missing_outcomes() -> Result<()> {
        let mut spawned = LedgerEvent::new("agent.spawned", None, serde_json::Value::Null);
        spawned.session_id = Some("session-1".to_string());
        spawned.run_seq = Some(1);
        let report = reconcile(&[record(spawned)])?;
        let row = report
            .rows
            .iter()
            .find(|row| row.rule_id == "spawn_requires_invocation_start")
            .context("spawn rule")?;
        assert_eq!(row.expected, 1);
        assert_eq!(row.observed, 0);
        assert_eq!(row.missing, 1);
        assert_eq!(report.completeness_status, "partial");
        Ok(())
    }

    #[test]
    fn matching_events_reconcile_by_session() -> Result<()> {
        let mut started =
            LedgerEvent::new("agent.invocation.started", None, serde_json::Value::Null);
        started.session_id = Some("session-1".to_string());
        started.run_seq = Some(1);
        let mut finished =
            LedgerEvent::new("agent.invocation.finished", None, serde_json::Value::Null);
        finished.session_id = Some("session-1".to_string());
        finished.run_seq = Some(2);
        let report = reconcile(&[record(started), record(finished)])?;
        let row = report
            .rows
            .iter()
            .find(|row| row.rule_id == "invocation_requires_finish")
            .context("finish rule")?;
        assert_eq!((row.expected, row.observed, row.missing), (1, 1, 0));
        assert_eq!(report.completeness_status, "complete");
        Ok(())
    }

    #[test]
    fn merge_gate_contract_requires_approved_review_and_passing_ci_on_same_head() -> Result<()> {
        let mut requested = LedgerEvent::new(
            "pr.merge_requested",
            None,
            serde_json::json!({"pr_number": 42, "head_sha": "head-b"}),
        );
        requested.session_id = Some("session-merge".to_string());
        requested.run_seq = Some(1);
        let mut stale_review = LedgerEvent::new(
            "pr.review",
            None,
            serde_json::json!({
                "pr_number": 42,
                "head_sha": "head-a",
                "kind": "approved"
            }),
        );
        stale_review.session_id = Some("session-merge".to_string());
        stale_review.run_seq = Some(2);
        let mut stale_ci = LedgerEvent::new(
            "ci.status_changed",
            None,
            serde_json::json!({
                "pr_number": 42,
                "head_sha": "head-a",
                "status": "success"
            }),
        );
        stale_ci.session_id = Some("session-merge".to_string());
        stale_ci.run_seq = Some(3);
        let report = reconcile(&[record(requested), record(stale_review), record(stale_ci)])?;
        for rule_id in [
            "merge_request_requires_approved_current_head",
            "merge_request_requires_passing_ci_current_head",
        ] {
            let row = report
                .rows
                .iter()
                .find(|row| row.rule_id == rule_id)
                .context("merge gate rule")?;
            assert_eq!((row.expected, row.observed, row.missing), (1, 0, 1));
        }
        Ok(())
    }

    #[test]
    fn guidance_enqueue_requires_acceptance_or_explicit_abandonment() -> Result<()> {
        let mut enqueue = LedgerEvent::new(
            "inbox.state_changed",
            None,
            serde_json::json!({"operation": "enqueue", "batch_id": "batch-1"}),
        );
        enqueue.session_id = Some("session-guidance".to_string());
        enqueue.run_seq = Some(1);
        let mut accepted = LedgerEvent::new(
            "message.consumed",
            None,
            serde_json::json!({"batch_id": "batch-1", "ack_kind": "runtime_accepted"}),
        );
        accepted.session_id = Some("session-guidance".to_string());
        accepted.run_seq = Some(2);
        let mut abandoned_enqueue = LedgerEvent::new(
            "inbox.state_changed",
            None,
            serde_json::json!({"operation": "enqueue", "batch_id": "batch-2"}),
        );
        abandoned_enqueue.session_id = Some("session-guidance".to_string());
        abandoned_enqueue.run_seq = Some(3);
        let mut abandoned = LedgerEvent::new(
            "agent_inbox.messages_abandoned",
            None,
            serde_json::json!({"batch_id": "batch-2"}),
        );
        abandoned.session_id = Some("session-guidance".to_string());
        abandoned.run_seq = Some(4);

        let report = reconcile(&[
            record(enqueue),
            record(accepted),
            record(abandoned_enqueue),
            record(abandoned),
        ])?;
        let row = report
            .rows
            .iter()
            .find(|row| row.rule_id == "guidance_enqueue_requires_acceptance_or_abandonment")
            .context("guidance enqueue rule")?;
        assert_eq!((row.expected, row.observed, row.missing), (2, 2, 0));
        Ok(())
    }
}
