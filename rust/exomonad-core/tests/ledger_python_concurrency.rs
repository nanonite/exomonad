//! Cross-language regression coverage for active ledger writes.
//!
//! The Rust writer deliberately exposes an incomplete final JSONL record while
//! the Python reader is polling the same active segment. The reader must retry
//! that tail and consume the event once the writer appends the remainder.

use exomonad_core::services::{LedgerEvent, LedgerWriter};
use serde_json::json;
use std::env;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::process::Command;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread;
use std::time::{Duration, Instant};
use tempfile::TempDir;

#[test]
fn python_reader_retries_a_rust_written_active_tail() {
    let temp = TempDir::new().expect("temporary ledger directory");
    let segments = temp.path().join("segments");
    let writer = LedgerWriter::open(&segments).expect("open ledger writer");
    let segment = segments.join("segment-000000000000.jsonl");
    drop(writer);

    let mut event = LedgerEvent::new(
        "agent.spawned",
        Some("worker-a".to_string()),
        json!({
            "slice_id": "slice-a",
            "intent_id": "intent-a",
            "payload": "x".repeat(32_768),
        }),
    );
    event.run_seq = Some(101);
    event.run_id = Some("run-a".to_string());
    event.session_id = Some("session-a".to_string());
    let mut encoded = serde_json::to_vec(&event).expect("serialize ledger event");
    encoded.push(b'\n');
    let split = encoded.len() / 2;
    let release = Arc::new(AtomicBool::new(false));
    let writer_release = Arc::clone(&release);
    let writer_segment = segment.clone();
    let writer_thread = thread::spawn(move || {
        let mut file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(writer_segment)
            .expect("open active segment");
        file.write_all(&encoded[..split])
            .expect("write first record half");
        file.flush().expect("flush first record half");
        while !writer_release.load(Ordering::Acquire) {
            thread::sleep(Duration::from_millis(2));
        }
        file.write_all(&encoded[split..])
            .expect("write record remainder");
        file.sync_data().expect("sync completed record");
    });

    let project_root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("workspace root")
        .to_path_buf();
    let observed_marker = temp.path().join("python-observed-active-tail");
    let release_marker = temp.path().join("rust-writer-release");
    let python = env::var("EXOMONAD_TL_LOOP_PYTHON").unwrap_or_else(|_| "python3".to_string());
    let python_path = match env::var("PYTHONPATH") {
        Ok(existing) => format!("{}:{}", project_root.display(), existing),
        Err(_) => project_root.display().to_string(),
    };
    let script = r#"
import sys
import time
from pathlib import Path

from tl_loop.events.reader import LedgerReader

segments = Path(sys.argv[1])
observed = Path(sys.argv[2])
release = Path(sys.argv[3])
deadline = time.monotonic() + 5
observed_tail = False
while time.monotonic() < deadline:
    result = LedgerReader(segments).read_from(0)
    if result.active_tail is not None:
        observed.write_text(
            f"{result.active_tail.segment}:{result.active_tail.line_number}:"
            f"{result.active_tail.byte_length}",
            encoding="utf-8",
        )
        observed_tail = True
        break
    time.sleep(0.005)
if not observed_tail:
    raise SystemExit("reader did not observe the incomplete active tail")
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.005)
if not release.exists():
    raise SystemExit("writer release marker was not delivered")
while time.monotonic() < deadline:
    result = LedgerReader(segments).read_from(0)
    if [event.run_seq for event in result.events] == [101]:
        raise SystemExit(0)
    time.sleep(0.005)
raise SystemExit("reader did not observe the completed event")
"#;
    let child = Command::new(&python)
        .args([
            "-c",
            script,
            &segments.to_string_lossy(),
            &observed_marker.to_string_lossy(),
            &release_marker.to_string_lossy(),
        ])
        .env("PYTHONPATH", python_path)
        .current_dir(&project_root)
        .spawn()
        .expect("run Python ledger reader");

    let observed_deadline = Instant::now() + Duration::from_secs(5);
    while !observed_marker.exists() && Instant::now() < observed_deadline {
        thread::sleep(Duration::from_millis(5));
    }
    let observed_before_release = observed_marker.exists();
    fs::write(&release_marker, "release").expect("write writer release marker");
    release.store(true, Ordering::Release);
    writer_thread.join().expect("Rust writer thread");
    let output = child
        .wait_with_output()
        .expect("wait for Python ledger reader");

    assert!(
        output.status.success(),
        "Python reader failed: stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        observed_before_release,
        "Python reader did not report observing the incomplete active tail"
    );
    assert_eq!(
        fs::read_to_string(&segment)
            .expect("read completed segment")
            .bytes()
            .filter(|byte| *byte == b'\n')
            .count(),
        1
    );
}
