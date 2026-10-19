use super::read_git_recovery_events;
use crate::services::event_log::EventLog;
use serde_json::json;
use std::fs::{self, OpenOptions};
use std::time::Duration;
use tempfile::TempDir;

fn event_log(temp: &TempDir) -> EventLog {
    EventLog::open(temp.path().join(".exo/logs")).expect("event log")
}

#[test]
fn legacy_python_envelopes_do_not_block_selected_rust_recovery_events() {
    let temp = tempfile::tempdir().expect("temporary project");
    let log = event_log(&temp);
    let legacy = json!({
        "agent_id": "root-controller",
        "data": {"intent_id": "historical"},
        "invocation_id": null,
        "lifecycle_state": "observed",
        "observed_at": "2026-10-17T00:00:00Z",
        "run_id": "root",
        "run_seq": 1,
        "type": "agent.spawned"
    });
    let segment = log
        .ledger()
        .segments_dir()
        .join("segment-000000000001.jsonl");
    fs::write(&segment, format!("{legacy}\n")).expect("write legacy event");

    log.append(
        "git.native_lock.held",
        "git-mutation",
        &json!({"schema_version": 2}),
    )
    .expect("append Rust recovery event");

    let events =
        read_git_recovery_events(&log, &["git.native_lock.held"], Duration::from_millis(100))
            .expect("scan legacy and current event shapes");
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].event_type, "git.native_lock.held");
    assert_eq!(events[0].data, json!({"schema_version": 2}));
}

#[test]
fn malformed_recovery_events_and_malformed_ledger_rows_fail_closed() {
    let temp = tempfile::tempdir().expect("temporary project");
    let log = event_log(&temp);
    let segment = log
        .ledger()
        .segments_dir()
        .join("segment-000000000001.jsonl");
    fs::write(
        &segment,
        "{\"type\":\"git.native_lock.held\",\"data\":{}}\n",
    )
    .expect("write malformed recovery row");
    assert!(read_git_recovery_events(&log, &["git.native_lock.held"], Duration::ZERO,).is_err());

    fs::write(&segment, "{\"type\":\"agent.spawned\",\"data\":\n")
        .expect("write malformed historical row");
    assert!(read_git_recovery_events(&log, &["git.native_lock.held"], Duration::ZERO).is_err());
}

#[test]
fn shared_read_lock_times_out_without_waiting_for_an_active_ledger_writer() {
    use nix::fcntl::{Flock, FlockArg};

    let temp = tempfile::tempdir().expect("temporary project");
    let log = event_log(&temp);
    let lock_path = log.ledger().segments_dir().join(".append.lock");
    let lock_file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(lock_path)
        .expect("open append lock");
    let _writer = Flock::lock(lock_file, FlockArg::LockExclusive).expect("lock writer");

    let started = std::time::Instant::now();
    let result =
        read_git_recovery_events(&log, &["git.native_lock.held"], Duration::from_millis(50));
    assert!(result.is_err());
    assert!(started.elapsed() >= Duration::from_millis(40));
    assert!(started.elapsed() < Duration::from_secs(1));
}
