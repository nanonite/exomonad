//! Read-only projection of durable TL run state and ledger transitions.

use serde_json::{Map, Value};
use std::{
    cmp::Reverse,
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

const DEFAULT_TRANSITION_LIMIT: usize = 20;
const MAX_TRANSITION_LIMIT: usize = 100;

#[derive(Debug)]
pub enum ReadModelError {
    InvalidIdentifier(&'static str),
    MissingRun,
    InvalidState(String),
    Io(std::io::Error),
}

impl std::fmt::Display for ReadModelError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidIdentifier(kind) => write!(formatter, "invalid {kind} identifier"),
            Self::MissingRun => formatter.write_str("run state not found"),
            Self::InvalidState(message) => write!(formatter, "invalid run state: {message}"),
            Self::Io(error) => write!(formatter, "could not read run state: {error}"),
        }
    }
}

impl std::error::Error for ReadModelError {}

impl From<std::io::Error> for ReadModelError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

pub fn read_run_model(
    project_dir: &Path,
    run_id: &str,
    transition_limit: Option<usize>,
) -> Result<Value, ReadModelError> {
    validate_identifier(run_id, "run")?;
    validate_transition_limit(transition_limit)?;
    let state = read_state(project_dir, run_id)?;
    let cursor = state
        .pointer("/events/last_consumed_offset")
        .and_then(Value::as_u64)
        .ok_or_else(|| ReadModelError::InvalidState("events.last_consumed_offset".to_string()))?;
    let events = read_transitions(project_dir, cursor)?;
    let limit = transition_limit.unwrap_or(DEFAULT_TRANSITION_LIMIT);
    let mut model = project_state(&state, cursor, &events, limit)?;
    model.insert("run_id".to_string(), Value::String(run_id.to_string()));
    Ok(Value::Object(model))
}

pub fn read_slice_model(
    project_dir: &Path,
    run_id: &str,
    slice_id: &str,
) -> Result<Value, ReadModelError> {
    validate_identifier(slice_id, "slice")?;
    let model = read_run_model(project_dir, run_id, Some(DEFAULT_TRANSITION_LIMIT))?;
    model
        .get("slices")
        .and_then(Value::as_object)
        .and_then(|slices| slices.get(slice_id))
        .cloned()
        .ok_or(ReadModelError::MissingRun)
}

pub fn read_transitions_model(
    project_dir: &Path,
    run_id: &str,
    transition_limit: Option<usize>,
) -> Result<Value, ReadModelError> {
    validate_identifier(run_id, "run")?;
    validate_transition_limit(transition_limit)?;
    let state = read_state(project_dir, run_id)?;
    let cursor = state
        .pointer("/events/last_consumed_offset")
        .and_then(Value::as_u64)
        .ok_or_else(|| ReadModelError::InvalidState("events.last_consumed_offset".to_string()))?;
    let limit = transition_limit.unwrap_or(DEFAULT_TRANSITION_LIMIT);
    Ok(Value::Array(
        read_transitions(project_dir, cursor)?
            .into_iter()
            .rev()
            .take(limit)
            .map(Value::Object)
            .collect(),
    ))
}

fn read_state(project_dir: &Path, run_id: &str) -> Result<Value, ReadModelError> {
    let state_path = project_dir
        .join(".exo")
        .join("tl-loop")
        .join(run_id)
        .join("run.json");
    let bytes = fs::read(&state_path).map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            ReadModelError::MissingRun
        } else {
            ReadModelError::Io(error)
        }
    })?;
    serde_json::from_slice(&bytes).map_err(|error| ReadModelError::InvalidState(error.to_string()))
}

fn project_state(
    state: &Value,
    cursor: u64,
    events: &[Map<String, Value>],
    transition_limit: usize,
) -> Result<Map<String, Value>, ReadModelError> {
    let root = state
        .as_object()
        .ok_or_else(|| ReadModelError::InvalidState("root must be an object".to_string()))?;
    let fsm = object_at(root, "fsm")?;
    let slices = object_at(root, "slices")?;
    let mut model = Map::new();
    model.insert("revision".to_string(), value_at(root, "revision")?.clone());
    model.insert("phase".to_string(), value_at(fsm, "phase")?.clone());
    model.insert("waiting".to_string(), value_at(fsm, "waiting")?.clone());
    model.insert("ledger_cursor".to_string(), Value::from(cursor));
    model.insert(
        "ledger_sequence_status".to_string(),
        Value::String(sequence_status(events)),
    );
    model.insert(
        "slices".to_string(),
        Value::Object(project_slices(slices, events)?),
    );
    model.insert(
        "budgets".to_string(),
        project_budgets(object_at(root, "budgets")?)?,
    );
    model.insert("gates".to_string(), project_gates(root.get("gates"))?);
    model.insert("park_causes".to_string(), project_park_causes(slices)?);
    model.insert(
        "recent_transitions".to_string(),
        Value::Array(
            events
                .iter()
                .rev()
                .take(transition_limit)
                .cloned()
                .map(Value::Object)
                .collect(),
        ),
    );
    Ok(model)
}

fn validate_transition_limit(limit: Option<usize>) -> Result<(), ReadModelError> {
    if limit.unwrap_or(DEFAULT_TRANSITION_LIMIT) > MAX_TRANSITION_LIMIT {
        return Err(ReadModelError::InvalidState(format!(
            "transition limit exceeds {MAX_TRANSITION_LIMIT}"
        )));
    }
    Ok(())
}

fn project_slices(
    slices: &Map<String, Value>,
    events: &[Map<String, Value>],
) -> Result<Map<String, Value>, ReadModelError> {
    slices
        .iter()
        .map(|(slice_id, raw)| {
            let slice = raw.as_object().ok_or_else(|| {
                ReadModelError::InvalidState(format!("slice {slice_id} must be an object"))
            })?;
            let mut projected = Map::new();
            for key in [
                "status",
                "paths",
                "depends_on",
                "base_ref",
                "agent_type",
                "model",
                "branch",
                "worktree",
                "pr_number",
                "reviewed_head",
                "attempts",
                "repair_attempts",
                "verdict",
                "park_cause",
                "park_issue_id",
                "blocked_by",
                "stall_classification",
            ] {
                if let Some(value) = slice.get(key) {
                    projected.insert(key.to_string(), value.clone());
                }
            }
            projected.insert(
                "heads".to_string(),
                Value::Array(project_heads(slice, events, slice_id)?),
            );
            Ok((slice_id.clone(), Value::Object(projected)))
        })
        .collect()
}

fn project_heads(
    slice: &Map<String, Value>,
    events: &[Map<String, Value>],
    slice_id: &str,
) -> Result<Vec<Value>, ReadModelError> {
    let mut heads = BTreeMap::<String, Map<String, Value>>::new();
    for (key, field) in [
        ("review_findings", "review_finding_count"),
        ("ci_state", "ci_status"),
    ] {
        if let Some(values) = slice.get(key).and_then(Value::as_object) {
            for (head, value) in values {
                let entry = heads.entry(head.clone()).or_default();
                if key == "review_findings" {
                    let count = value.as_array().map_or(0, Vec::len);
                    entry.insert(field.to_string(), Value::from(count));
                    if count > 0 {
                        entry.insert(
                            "review_state".to_string(),
                            Value::String("changes_requested".to_string()),
                        );
                    }
                } else {
                    entry.insert(field.to_string(), value.clone());
                }
            }
        }
    }
    if let Some(head) = slice.get("reviewer_attempt").and_then(Value::as_object) {
        for (head_sha, attempt) in head {
            heads
                .entry(head_sha.clone())
                .or_default()
                .insert("reviewer_attempt".to_string(), attempt.clone());
        }
    }
    if let Some(current) = slice.get("reviewed_head").and_then(Value::as_str) {
        heads
            .entry(current.to_string())
            .or_default()
            .insert("is_current".to_string(), Value::Bool(true));
    }
    for (head_sha, head) in &mut heads {
        head.entry("is_current".to_string())
            .or_insert(Value::Bool(false));
        for event in events.iter().filter(|event| {
            event.get("slice_id").and_then(Value::as_str) == Some(slice_id)
                && event.get("head_sha").and_then(Value::as_str) == Some(head_sha.as_str())
        }) {
            for key in ["review_state", "review_kind", "ci_status", "last_event_seq"] {
                if let Some(value) = event.get(key) {
                    head.insert(key.to_string(), value.clone());
                }
            }
        }
    }
    Ok(heads
        .into_iter()
        .map(|(head_sha, mut head)| {
            head.insert("head_sha".to_string(), Value::String(head_sha));
            Value::Object(head)
        })
        .collect())
}

fn project_budgets(budgets: &Map<String, Value>) -> Result<Value, ReadModelError> {
    let ledger = object_at(budgets, "ledger")?;
    let mut projected = Map::new();
    for key in [
        "tokens",
        "wall_seconds",
        "role_spent",
        "harness_spent",
        "role_reserved",
        "harness_reserved",
    ] {
        if let Some(value) = ledger.get(key) {
            projected.insert(key.to_string(), value.clone());
        }
    }
    if let Some(charges) = ledger.get("charges") {
        projected.insert("charges".to_string(), charges.clone());
    }
    Ok(Value::Object(projected))
}

fn project_gates(gates: Option<&Value>) -> Result<Value, ReadModelError> {
    let values = gates
        .and_then(Value::as_array)
        .ok_or_else(|| ReadModelError::InvalidState("gates must be an array".to_string()))?;
    Ok(Value::Array(
        values
            .iter()
            .filter_map(Value::as_object)
            .map(|gate| {
                let mut projected = Map::new();
                for key in ["name", "status"] {
                    if let Some(value) = gate.get(key) {
                        projected.insert(key.to_string(), value.clone());
                    }
                }
                Value::Object(projected)
            })
            .collect(),
    ))
}

fn project_park_causes(slices: &Map<String, Value>) -> Result<Value, ReadModelError> {
    Ok(Value::Object(
        slices
            .iter()
            .filter_map(|(slice_id, value)| {
                value
                    .get("park_cause")
                    .filter(|cause| !cause.is_null())
                    .map(|cause| (slice_id.clone(), cause.clone()))
            })
            .collect(),
    ))
}

fn read_transitions(
    project_dir: &Path,
    cursor: u64,
) -> Result<Vec<Map<String, Value>>, ReadModelError> {
    let segments_dir = project_dir.join(".exo").join("ledger").join("segments");
    let mut paths = fs::read_dir(&segments_dir)
        .map(|entries| {
            entries
                .filter_map(Result::ok)
                .map(|entry| entry.path())
                .filter(|path| path.extension().and_then(|ext| ext.to_str()) == Some("jsonl"))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    paths.sort();
    let mut events = Vec::new();
    for path in paths {
        let text = fs::read_to_string(path)?;
        for line in text.lines().filter(|line| !line.trim().is_empty()) {
            let raw: Value = serde_json::from_str(line)
                .map_err(|error| ReadModelError::InvalidState(error.to_string()))?;
            let Some(object) = raw.as_object() else {
                continue;
            };
            let Some(run_seq) = object.get("run_seq").and_then(Value::as_u64) else {
                continue;
            };
            if run_seq <= cursor {
                events.push(project_transition(object));
            }
        }
    }
    events.sort_by_key(|event| {
        Reverse(
            event
                .get("run_seq")
                .and_then(Value::as_u64)
                .unwrap_or_default(),
        )
    });
    events.reverse();
    Ok(events)
}

fn project_transition(raw: &Map<String, Value>) -> Map<String, Value> {
    let data = raw.get("data").and_then(Value::as_object);
    let mut transition = Map::new();
    for key in [
        "run_seq",
        "type",
        "observed_at",
        "lifecycle_state",
        "agent_id",
        "harness",
        "role",
    ] {
        if let Some(value) = raw.get(key) {
            transition.insert(
                if key == "type" { "event_type" } else { key }.to_string(),
                value.clone(),
            );
        }
    }
    for key in [
        "slice_id",
        "pr_number",
        "head_sha",
        "kind",
        "review_state",
        "ci_status",
    ] {
        if let Some(value) = data.and_then(|data| data.get(key)) {
            transition.insert(
                match key {
                    "kind" => "review_kind",
                    _ => key,
                }
                .to_string(),
                value.clone(),
            );
        }
    }
    if let Some(run_seq) = raw.get("run_seq") {
        transition.insert("last_event_seq".to_string(), run_seq.clone());
    }
    transition
}

fn sequence_status(events: &[Map<String, Value>]) -> String {
    let mut sequences = events
        .iter()
        .filter_map(|event| event.get("run_seq").and_then(Value::as_u64))
        .collect::<Vec<_>>();
    sequences.sort_unstable();
    if sequences.is_empty() {
        return "unknown".to_string();
    }
    if sequences.windows(2).all(|pair| pair[1] == pair[0] + 1) {
        "complete".to_string()
    } else {
        "partial".to_string()
    }
}

fn object_at<'a>(
    object: &'a Map<String, Value>,
    key: &str,
) -> Result<&'a Map<String, Value>, ReadModelError> {
    object
        .get(key)
        .and_then(Value::as_object)
        .ok_or_else(|| ReadModelError::InvalidState(format!("{key} must be an object")))
}

fn value_at<'a>(object: &'a Map<String, Value>, key: &str) -> Result<&'a Value, ReadModelError> {
    object
        .get(key)
        .ok_or_else(|| ReadModelError::InvalidState(format!("missing {key}")))
}

fn validate_identifier(value: &str, kind: &'static str) -> Result<(), ReadModelError> {
    if value.is_empty()
        || value == "."
        || value == ".."
        || PathBuf::from(value)
            .file_name()
            .and_then(|name| name.to_str())
            != Some(value)
    {
        return Err(ReadModelError::InvalidIdentifier(kind));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    #[test]
    fn read_projection_is_body_free_and_cursor_bounded() {
        let project = tempdir().unwrap();
        let state_dir = project.path().join(".exo/tl-loop/run-1");
        fs::create_dir_all(&state_dir).unwrap();
        fs::write(
            state_dir.join("run.json"),
            r#"{
                "revision": 3,
                "run_id": "run-1",
                "fsm": {"phase": "TLWaiting", "waiting": ["slice-a"]},
                "slices": {
                    "slice-a": {
                        "status": "in_review",
                        "paths": ["src/a.rs"],
                        "depends_on": [],
                        "base_ref": "main",
                        "agent_type": "codex",
                        "model": "gpt-5",
                        "branch": "task/a",
                        "worktree": ".worktrees/a",
                        "pr_number": 12,
                        "reviewed_head": "head-a",
                        "attempts": 1,
                        "verdict": null,
                        "review_findings": {
                            "head-a": [{
                                "severity": "blocking",
                                "path": "src/a.rs",
                                "rationale": "private body"
                            }]
                        },
                        "ci_state": {"head-a": "success"},
                        "reviewer_attempt": {"head-a": 2},
                        "repair_attempts": 0,
                        "park_cause": null,
                        "park_issue_id": null,
                        "blocked_by": null,
                        "stall_classification": null
                    }
                },
                "budgets": {"ledger": {"tokens": 10, "wall_seconds": 2}},
                "gates": [],
                "events": {"last_consumed_offset": 2}
            }"#,
        )
        .unwrap();
        let ledger_dir = project.path().join(".exo/ledger/segments");
        fs::create_dir_all(&ledger_dir).unwrap();
        fs::write(
            ledger_dir.join("segment-000000000001.jsonl"),
            r#"{"run_seq":1,"type":"pr.published","observed_at":"2026-08-12T00:00:00Z","lifecycle_state":"observed","agent_id":"slice-a","data":{"slice_id":"slice-a","head_sha":"head-a","pr_number":12,"body":"private body"}}
{"run_seq":2,"type":"pr.review","observed_at":"2026-08-12T00:00:01Z","lifecycle_state":"observed","agent_id":"slice-a","data":{"slice_id":"slice-a","head_sha":"head-a","review_state":"approved","kind":"review"}}
{"run_seq":3,"type":"ci.status_changed","observed_at":"2026-08-12T00:00:02Z","lifecycle_state":"observed","agent_id":"slice-a","data":{"slice_id":"slice-a","head_sha":"head-a","status":"failure","body":"not consumed"}}
"#,
        )
        .unwrap();

        let model = read_run_model(project.path(), "run-1", None).unwrap();
        assert_eq!(model["ledger_cursor"], 2);
        assert_eq!(model["ledger_sequence_status"], "complete");
        assert_eq!(model["recent_transitions"].as_array().unwrap().len(), 2);
        assert_eq!(
            model["slices"]["slice-a"]["heads"][0]["ci_status"],
            "success"
        );
        let serialized = serde_json::to_string(&model).unwrap();
        assert!(!serialized.contains("private body"));
        assert!(!serialized.contains("rationale"));
        assert!(!serialized.contains("not consumed"));
    }

    #[test]
    fn read_projection_rejects_path_escape_and_large_limits() {
        let project = tempdir().unwrap();
        assert!(matches!(
            read_run_model(project.path(), "../outside", None),
            Err(ReadModelError::InvalidIdentifier("run"))
        ));
        assert!(matches!(
            read_transitions_model(project.path(), "run-1", Some(MAX_TRANSITION_LIMIT + 1)),
            Err(ReadModelError::InvalidState(message)) if message.contains("limit")
        ));
    }
}
