//! CLI Integration Tests for exomonad
//!
//! Tests CLI behavior for the thin HTTP hook client.
//! `exomonad hook` is now a thin HTTP forwarder to the server. When the server
//! is unreachable, it fails open (prints `{"continue":true}` and exits 0).
//! Full E2E tests require a running server — see `tests/mcp_integration.rs`.

use assert_cmd::cargo::cargo_bin_cmd;
use predicates::prelude::*;
use sha2::{Digest, Sha256};
use std::fs;
use tempfile::tempdir;

fn test_hook_json() -> String {
    r#"{
        "session_id": "test-session-123",
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/test.txt", "content": "hello world"},
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/home/test",
        "permission_mode": "default"
    }"#
    .to_string()
}

fn minimal_hook_json() -> String {
    r#"{
        "session_id": "s",
        "hook_event_name": "PreToolUse",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": "/",
        "permission_mode": "default"
    }"#
    .to_string()
}

/// When the server is not running, hook should fail open: exit 0, print allow JSON.
#[test]
fn test_hook_fails_open_when_server_unreachable() -> Result<(), Box<dyn std::error::Error>> {
    let mut cmd = cargo_bin_cmd!("exomonad");

    cmd.args(["hook", "pre-tool-use"])
        .write_stdin(test_hook_json())
        .assert()
        .success()
        .stdout(predicate::str::contains(r#""continue":true"#));

    Ok(())
}

#[test]
fn test_init_help_describes_worker_and_reviewer_flags() -> Result<(), Box<dyn std::error::Error>> {
    let mut cmd = cargo_bin_cmd!("exomonad");

    let output = cmd
        .args(["init", "--help"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let help = String::from_utf8(output)?;

    assert_eq!(
        help.matches("valid: claude|opencode|codex|shoal").count(),
        1
    );
    assert!(!help.contains("--tl"));
    assert!(!help.contains("--opencode-as-tl"));
    assert!(help.contains("CLI effort flags override config.toml"));
    assert!(help.contains("forked TLs, leaves, ephemeral workers, and companions"));
    assert!(help.contains("Maximum reviewer rounds before a PR is escalated to Stuck"));

    Ok(())
}

/// Minimal input also fails open when server is unreachable.
#[test]
fn test_hook_minimal_input_fails_open() -> Result<(), Box<dyn std::error::Error>> {
    let mut cmd = cargo_bin_cmd!("exomonad");

    cmd.args(["hook", "pre-tool-use"])
        .write_stdin(minimal_hook_json())
        .assert()
        .success()
        .stdout(predicate::str::contains(r#""continue":true"#));

    Ok(())
}

/// Empty stdin still fails open (server gets empty body, returns error, client allows).
#[test]
fn test_hook_empty_stdin_fails_open() -> Result<(), Box<dyn std::error::Error>> {
    let mut cmd = cargo_bin_cmd!("exomonad");

    cmd.args(["hook", "pre-tool-use"])
        .write_stdin("")
        .assert()
        .success()
        .stdout(predicate::str::contains(r#""continue":true"#));

    Ok(())
}

/// Invalid JSON still fails open.
#[test]
fn test_hook_invalid_json_fails_open() -> Result<(), Box<dyn std::error::Error>> {
    let mut cmd = cargo_bin_cmd!("exomonad");

    cmd.args(["hook", "pre-tool-use"])
        .write_stdin("not valid json")
        .assert()
        .success()
        .stdout(predicate::str::contains(r#""continue":true"#));

    Ok(())
}

/// Codex hooks fail open with Codex's no-op stdout shape.
#[test]
fn test_codex_hook_fails_open_when_server_unreachable() -> Result<(), Box<dyn std::error::Error>> {
    let mut cmd = cargo_bin_cmd!("exomonad");

    cmd.args(["hook", "pre-tool-use", "--runtime", "codex"])
        .write_stdin(r#"{"event":"pre-tool-use","tool":"bash","args":{"command":"ls"}}"#)
        .assert()
        .success()
        .stdout(predicate::str::contains("{}"));

    Ok(())
}

/// Different hook types all work as thin client.
#[test]
fn test_hook_other_event_types_fail_open() -> Result<(), Box<dyn std::error::Error>> {
    for event in &["post-tool-use", "stop", "session-end", "subagent-stop"] {
        let mut cmd = cargo_bin_cmd!("exomonad");
        cmd.args(["hook", event])
            .write_stdin(minimal_hook_json())
            .assert()
            .success()
            .stdout(predicate::str::contains(r#""continue":true"#));
    }

    Ok(())
}

/// Subcommand is required (no more Option<Commands> fallback).
#[test]
fn test_no_subcommand_shows_help() -> Result<(), Box<dyn std::error::Error>> {
    let mut cmd = cargo_bin_cmd!("exomonad");

    cmd.assert()
        .failure()
        .stderr(predicate::str::contains("Usage"));

    Ok(())
}

#[test]
fn test_clean_help_exposes_safe_targeting_and_apply_flags() -> Result<(), Box<dyn std::error::Error>>
{
    let mut cmd = cargo_bin_cmd!("exomonad");

    let output = cmd
        .args(["clean", "--help"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let help = String::from_utf8(output)?;
    assert!(help.contains("--name <NAME>"));
    assert!(help.contains("--sweep"));
    assert!(help.contains("--apply"));
    assert!(help.contains("--reason <REASON>"));
    assert!(help.contains("--preserve-unique-commits"));
    assert!(help.contains("--allow-no-pr"));
    assert!(help.contains("--discard-dirty"));
    Ok(())
}

#[test]
fn test_clean_requires_a_target_before_contacting_server() {
    let mut cmd = cargo_bin_cmd!("exomonad");
    cmd.args(["clean"])
        .assert()
        .failure()
        .stderr(predicate::str::contains("invalid exomonad clean arguments"));
}

#[test]
fn test_clean_rejects_name_and_sweep_together() {
    let mut cmd = cargo_bin_cmd!("exomonad");
    cmd.args(["clean", "--name", "leaf", "--sweep"])
        .assert()
        .failure()
        .stderr(predicate::str::contains("cannot be used with"));
}

#[test]
fn test_record_plan_snapshot_cli_persists_stdin_identity() {
    let project = tempdir().expect("create temporary project");
    let accepted = br#"{"plan":{"leaves":[{"name":"waited"}]}}"#;
    let digest = format!("{:x}", Sha256::digest(accepted));

    cargo_bin_cmd!("exomonad")
        .args([
            "record-plan-snapshot",
            "--project-root",
            project.path().to_str().expect("temporary path is UTF-8"),
            "--expected-digest",
            &digest,
        ])
        .write_stdin(accepted)
        .assert()
        .success();

    assert_eq!(
        fs::read(project.path().join(".exo/tl-loop/plan.snapshot"))
            .expect("read persisted plan snapshot"),
        accepted
    );
    assert_eq!(
        fs::read_to_string(project.path().join(".exo/tl-loop/plan.snapshot.sha256"))
            .expect("read persisted plan digest"),
        format!("{digest}\n")
    );
    assert!(!project
        .path()
        .join(".exo/tl-loop/plan-transition.json")
        .exists());
}
