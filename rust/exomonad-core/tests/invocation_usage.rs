//! Per-invocation token usage on `agent.invocation.finished`.
//!
//! A one-shot Claude invocation writes its authoritative
//! `claude -p --output-format json` envelope to the invocation-owned usage
//! file. The finish path reads that file, sums the envelope's usage counters,
//! and puts the result on the finished row. Attribution is by invocation: the
//! source file is created by that invocation's own process, so a second
//! invocation in the same agent directory neither inherits nor rewrites it.
//!
//! These tests use only the public invocation API and the on-disk event view,
//! so they compile against the pre-change production files and fail there for
//! the right reason: the finished row carries no `usage` object.

use exomonad_core::domain::RoutingInfo;
use exomonad_core::services::agent_control::{
    finish_invocation, start_invocation, AgentType, InvocationFinishResult, InvocationRecord,
    InvocationStatus, InvocationTrigger,
};
use exomonad_core::services::tmux_ipc::WindowId;
use serde_json::Value;
use std::path::{Path, PathBuf};
use tempfile::TempDir;

const AGENT: &str = "usage-fixture-claude";

fn routing() -> RoutingInfo {
    RoutingInfo::window(WindowId::parse("@42").expect("valid test window"))
}

fn agent_dir(root: &Path) -> PathBuf {
    root.join(".exo").join("agents").join(AGENT)
}

/// The compatibility event view of one agent, one JSON object per line.
fn finished_rows(root: &Path) -> Vec<Value> {
    let path = root
        .join(".exo")
        .join("events")
        .join(format!("{AGENT}.jsonl"));
    let raw = std::fs::read_to_string(&path).expect("finished event view");
    raw.lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter(|row| row["type"] == "agent.invocation.finished")
        .collect()
}

fn finished_row_for(root: &Path, invocation_id: &str) -> Value {
    finished_rows(root)
        .into_iter()
        .find(|row| row["data"]["invocation_id"] == invocation_id)
        .unwrap_or_else(|| panic!("finished row for {invocation_id}"))
}

/// The canonical L1 ledger view the Python controller reads.
fn ledger_finished_row_for(root: &Path, invocation_id: &str) -> Value {
    let segments = root.join(".exo").join("ledger").join("segments");
    let mut match_row = None;
    for entry in std::fs::read_dir(&segments).expect("ledger segments") {
        let path = entry.expect("segment entry").path();
        let raw = std::fs::read_to_string(&path).expect("ledger segment");
        for line in raw.lines() {
            let Ok(row) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            if row["type"] == "agent.invocation.finished"
                && row["data"]["invocation_id"] == invocation_id
            {
                match_row = Some(row);
            }
        }
    }
    match_row.unwrap_or_else(|| panic!("ledger finished row for {invocation_id}"))
}

async fn start(root: &TempDir) -> String {
    start_invocation(
        &agent_dir(root.path()),
        AgentType::Claude,
        InvocationTrigger::Spawn,
        routing(),
        None,
        None,
    )
    .await
    .expect("start invocation")
    .invocation_id
}

async fn finish(root: &TempDir, invocation_id: &str) {
    let result = finish_invocation(
        &agent_dir(root.path()),
        invocation_id,
        InvocationStatus::Exited,
        Some(0),
    )
    .await
    .expect("finish invocation");
    assert!(matches!(result, InvocationFinishResult::Finished(_)));
}

fn claude_envelope() -> String {
    serde_json::json!({
        "type": "result",
        "subtype": "success",
        "is_error": false,
        "result": "assignment complete",
        "session_id": "session-usage-fixture",
        "usage": {
            "input_tokens": 120,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 50,
            "output_tokens": 25
        }
    })
    .to_string()
}

fn write_usage(root: &TempDir, contents: &str) {
    std::fs::write(agent_dir(root.path()).join("usage.json"), contents).expect("usage fixture");
}

#[tokio::test]
async fn finished_row_carries_the_summed_usage_for_this_invocation() {
    let root = TempDir::new().expect("temp project");
    std::fs::create_dir_all(agent_dir(root.path())).expect("agent directory");
    let invocation_id = start(&root).await;
    write_usage(&root, &claude_envelope());
    finish(&root, &invocation_id).await;

    let row = finished_row_for(root.path(), &invocation_id);
    assert_eq!(row["data"]["usage"]["input_tokens"], 200);
    assert_eq!(row["data"]["usage"]["output_tokens"], 25);
    assert_eq!(row["data"]["usage"]["total_tokens"], 225);
    let ledger_row = ledger_finished_row_for(root.path(), &invocation_id);
    assert_eq!(ledger_row["data"]["usage"]["total_tokens"], 225);
    assert!(
        !agent_dir(root.path()).join("usage.json").exists(),
        "the finished invocation consumes its own usage source"
    );
}

#[tokio::test]
async fn a_second_invocation_in_the_same_agent_directory_does_not_leak_usage() {
    let root = TempDir::new().expect("temp project");
    std::fs::create_dir_all(agent_dir(root.path())).expect("agent directory");

    let first = start(&root).await;
    write_usage(&root, &claude_envelope());
    finish(&root, &first).await;
    assert_eq!(
        finished_row_for(root.path(), &first)["data"]["usage"]["total_tokens"],
        225
    );

    let second = start(&root).await;
    finish(&root, &second).await;
    let row = finished_row_for(root.path(), &second);
    assert_eq!(
        row["data"].get("usage"),
        None,
        "no second invocation source"
    );
}

#[tokio::test]
async fn missing_partial_and_ambiguous_sources_write_no_usage() {
    let root = TempDir::new().expect("temp project");
    std::fs::create_dir_all(agent_dir(root.path())).expect("agent directory");

    let missing = start(&root).await;
    finish(&root, &missing).await;
    assert_eq!(
        finished_row_for(root.path(), &missing)["data"].get("usage"),
        None
    );

    let partial = start(&root).await;
    write_usage(&root, r#"{"usage":{"input_tokens":10}}"#);
    finish(&root, &partial).await;
    assert_eq!(
        finished_row_for(root.path(), &partial)["data"].get("usage"),
        None
    );

    let ambiguous = start(&root).await;
    write_usage(
        &root,
        concat!(
            r#"{"usage":{"input_tokens":10,"output_tokens":2}}"#,
            "\n",
            r#"{"usage":{"input_tokens":99,"output_tokens":1}}"#
        ),
    );
    finish(&root, &ambiguous).await;
    assert_eq!(
        finished_row_for(root.path(), &ambiguous)["data"].get("usage"),
        None
    );

    let malformed = start(&root).await;
    write_usage(&root, "not a claude envelope");
    finish(&root, &malformed).await;
    assert_eq!(
        finished_row_for(root.path(), &malformed)["data"].get("usage"),
        None
    );
}

#[test]
fn an_old_finished_row_decodes_unchanged() {
    let old = serde_json::json!({
        "invocation_id": "legacy",
        "runtime": "claude",
        "trigger": "spawn",
        "routing": {"window_id": "@1"},
        "started_at": 1,
        "status": "exited",
        "build": "test"
    });
    let record: InvocationRecord = serde_json::from_value(old).expect("legacy record");
    let encoded = serde_json::to_value(&record).expect("legacy record encodes");
    assert_eq!(
        encoded.get("usage"),
        None,
        "a row written before usage existed keeps its old shape"
    );
}
