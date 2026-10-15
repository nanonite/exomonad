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
use std::path::Path;
use std::process::Command;
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

#[test]
fn test_init_and_new_advertise_the_stale_codex_root_config_flag(
) -> Result<(), Box<dyn std::error::Error>> {
    for subcommand in ["init", "new"] {
        let output = cargo_bin_cmd!("exomonad")
            .args([subcommand, "--help"])
            .assert()
            .success()
            .get_output()
            .stdout
            .clone();
        let help = String::from_utf8(output)?;

        assert!(
            help.contains("--retire-stale-codex-root-config"),
            "`exomonad {subcommand} --help` does not offer the flag:\n{help}"
        );
    }

    Ok(())
}

/// A project still carrying an ExoMonad-written project-root `.codex/config.toml`
/// must not start: Codex applies that file to every session opened in the project
/// (chainlink #1145). The refusal names the file and the exact repair.
#[test]
fn test_init_refuses_a_stale_exomonad_generated_project_root_codex_config() {
    let project = tempdir().expect("create temporary project");
    fs::create_dir_all(project.path().join(".exo")).expect("create .exo directory");
    fs::write(project.path().join(".exo/config.toml"), "")
        .expect("mark the directory as an ExoMonad project");
    let stale = project.path().join(".codex/config.toml");
    fs::create_dir_all(stale.parent().expect("config parent")).expect("create .codex directory");
    fs::write(&stale, generated_root_codex_config()).expect("write the stale root config");

    cargo_bin_cmd!("exomonad")
        .args(["init", "--continue"])
        .current_dir(project.path())
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "mv .codex/config.toml .codex/config.toml.exomonad-stale.1.bak",
        ))
        .stderr(predicate::str::contains("--retire-stale-codex-root-config"));

    assert!(
        stale.exists(),
        "the refusal must not have edited or deleted the operator's file"
    );
}

#[test]
fn test_init_continue_requires_audited_migration_for_legacy_aggregate_defaults() {
    let project = tempdir().expect("create temporary project");
    let home = tempdir().expect("create isolated home");
    let repository = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .expect("resolve repository root");
    let archive = home.path().join(".exo/tl_loop.pyz");
    fs::create_dir_all(archive.parent().expect("archive parent"))
        .expect("create isolated runtime directory");
    let archive_status = Command::new("python3")
        .arg(repository.join("scripts/build_tl_loop_archive.py"))
        .args(["--source"])
        .arg(repository.join("tl_loop"))
        .args(["--output"])
        .arg(&archive)
        .status()
        .expect("build isolated TL controller archive");
    assert!(archive_status.success());

    let fixture_script = concat!(
        "import sys; from pathlib import Path; ",
        "from tl_loop.tests.test_plan_reconciliation import _project; ",
        "from tl_loop.fsm.scope import TLPRFiled; ",
        "from tl_loop.state.plan_reconciliation import analyze_plan_reconciliation; ",
        "_, _, child, accepted, candidate = _project(Path(sys.argv[1]), candidate_issue_id=None, ",
        "legacy_aggregate_review_default=True, ",
        "legacy_completed_issue_binding=True, ",
        "include_completed_spawn_journal=True, legacy_completed_dispatch=True, ",
        "legacy_integration_coordinator_binding=True, ",
        "terminal_aggregate_handoff=True, controller_alias='architecture', ",
        "five_ordered_stages=True); ",
        "assert accepted == candidate; ",
        "assert isinstance(child.load().recursive_fsm, TLPRFiled); ",
        "assert analyze_plan_reconciliation(Path(sys.argv[1])).can_apply"
    );
    let fixture_status = Command::new("python3")
        .arg("-c")
        .arg(fixture_script)
        .arg(project.path())
        .env("PYTHONPATH", &repository)
        .status()
        .expect("build historical workspace fixture");
    assert!(fixture_status.success());
    fs::write(project.path().join(".exo/config.toml"), "")
        .expect("mark fixture as an ExoMonad project");
    for name in ["harness_policy.toml", "review-policy.toml"] {
        fs::copy(
            repository.join(".exo").join(name),
            project.path().join(".exo").join(name),
        )
        .expect("copy required startup policy");
    }

    let snapshot = project.path().join(".exo/tl-loop/plan.snapshot");
    let candidate = project.path().join(".exo/tl-loop/plan.json");
    let before_snapshot = fs::read(&snapshot).expect("read accepted snapshot");
    let before_candidate = fs::read(&candidate).expect("read unchanged candidate");
    assert_eq!(before_snapshot, before_candidate);

    let dry_run = cargo_bin_cmd!("exomonad")
        .args(["plan", "reconcile", "--dry-run"])
        .env("HOME", home.path())
        .current_dir(project.path())
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let report: serde_json::Value =
        serde_json::from_slice(&dry_run).expect("decode production reconciliation report");
    assert!(report["can_apply"]
        .as_bool()
        .expect("dry-run can_apply boolean"));
    let fingerprint = report["evidence_fingerprint"]
        .as_str()
        .expect("dry-run evidence fingerprint");

    cargo_bin_cmd!("exomonad")
        .args(["init", "--continue"])
        .env("HOME", home.path())
        .current_dir(project.path())
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "persisted plan migration is required before continuation",
        ))
        .stderr(predicate::str::contains("--expected-fingerprint"));

    cargo_bin_cmd!("exomonad")
        .args([
            "plan",
            "reconcile",
            "--apply",
            "--reason",
            "Apply the verified TLPRFiled legacy completion migration",
            "--expected-fingerprint",
            fingerprint,
        ])
        .env("HOME", home.path())
        .current_dir(project.path())
        .assert()
        .success()
        .stdout(predicate::str::contains("\"applied\": true"));

    assert_eq!(
        fs::read(&snapshot).expect("snapshot remains unchanged after migration"),
        before_snapshot
    );
    assert_eq!(
        fs::read(&candidate).expect("candidate remains unchanged after migration"),
        before_candidate
    );
}

/// The project-root Codex config an ExoMonad older than the per-agent layout
/// wrote for a Codex root agent, generated by the same renderer init uses today.
fn generated_root_codex_config() -> String {
    exomonad_core::codex_config::render_codex_config(
        "root",
        "tl",
        "# Root TL protocol\n\nRetired root protocol text.",
        None,
        &std::collections::HashMap::new(),
        std::path::Path::new("/usr/local/bin/exomonad"),
        std::path::Path::new("/tmp/exomonad-stale-root-project"),
    )
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
fn test_claude_model_list_includes_haiku_5_5_and_haiku_4_5(
) -> Result<(), Box<dyn std::error::Error>> {
    let output = cargo_bin_cmd!("exomonad")
        .args(["models", "claude"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let models = String::from_utf8(output)?;

    assert!(models.lines().any(|line| line == "claude-haiku-5-5"));
    assert!(models
        .lines()
        .any(|line| line == "claude-haiku-4-5-20251001"));

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

#[test]
fn test_record_plan_snapshot_cli_rejects_prepared_recreate_journal() {
    let project = tempdir().expect("create temporary project");
    let state = project.path().join(".exo/tl-loop");
    fs::create_dir_all(&state).expect("create TL state directory");
    let journal = state.join("plan-transition.json");
    let prepared = br#"{"phase":"prepared","archive_name":"root.previous"}"#;
    fs::write(&journal, prepared).expect("prepare recreate journal");
    let accepted = br#"{"plan":{"leaves":[{"name":"late"}]}}"#;
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
        .failure()
        .stderr(predicate::str::contains("plan transition is in progress"));

    assert_eq!(fs::read(journal).expect("read prepared journal"), prepared);
    assert!(!state.join("plan.snapshot").exists());
    assert!(!state.join("plan.snapshot.sha256").exists());
}
