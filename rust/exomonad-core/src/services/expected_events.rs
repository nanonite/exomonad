//! Versioned expected-event contracts and denominator reconciliation.

use crate::services::{sequence_status, LedgerRecord};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
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

pub fn load_contract() -> Result<ExpectedEventContract> {
    serde_json::from_str(CONTRACT_JSON).context("parse expected-event contract")
}

/// Reconcile required transitions by session, preserving missing outcomes as
/// missing denominator rows rather than treating them as zero-valued success.
pub fn reconcile(records: &[LedgerRecord]) -> Result<DenominatorReport> {
    let contract = load_contract()?;
    let mut rows = Vec::with_capacity(contract.rules.len());
    for rule in &contract.rules {
        let mut expected = 0;
        let mut observed = 0;
        let mut session_prerequisites = BTreeMap::<String, u64>::new();
        let mut session_required = BTreeMap::<String, u64>::new();
        for record in records {
            let session = record
                .event
                .session_id
                .as_deref()
                .unwrap_or("<unknown>")
                .to_string();
            if record.event.event_type == rule.prerequisite_event {
                *session_prerequisites.entry(session.clone()).or_default() += 1;
            }
            if required_matches(&rule.required_event, &record.event.event_type) {
                *session_required.entry(session).or_default() += 1;
            }
        }
        for (session, count) in session_prerequisites {
            expected += count;
            observed += session_required
                .get(&session)
                .copied()
                .unwrap_or_default()
                .min(count);
        }
        rows.push(DenominatorRow {
            rule_id: rule.rule_id.clone(),
            expected,
            observed,
            missing: expected.saturating_sub(observed),
        });
    }
    let completeness_status = match sequence_status(records) {
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

fn required_matches(required: &str, event_type: &str) -> bool {
    required
        .split(['/', '|'])
        .any(|candidate| candidate == event_type)
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
}
