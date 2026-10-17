//! Bounded, compatible reads of Git recovery events from the shared TL ledger.

use crate::services::event_log::EventLog;
use nix::errno::Errno;
use nix::fcntl::{Flock, FlockArg};
use serde_json::Value;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader};
use std::path::Path;
use std::thread;
use std::time::{Duration, Instant};

const MAX_EVENT_BYTES: usize = 16 * 1024 * 1024;
const LOCK_POLL: Duration = Duration::from_millis(10);

#[derive(Debug, Clone, PartialEq)]
pub(crate) struct GitRecoveryEvent {
    pub event_id: String,
    pub event_type: String,
    pub data: Value,
}

/// Read selected recovery events under the ledger append lock.
///
/// Other historical rows are parsed as JSON but are not forced through the
/// current Rust envelope schema; Python TL events intentionally retain older
/// envelope shapes in this same immutable ledger.
pub(crate) fn read_git_recovery_events(
    event_log: &EventLog,
    selected_types: &[&str],
    wait: Duration,
) -> Result<Vec<GitRecoveryEvent>, String> {
    let segments_dir = event_log.ledger().segments_dir().to_path_buf();
    let _ledger_read_lock = acquire_shared_ledger_lock(&segments_dir, wait)?;
    let segments = event_log
        .ledger()
        .segments()
        .map_err(|error| format!("cannot enumerate Git recovery ledger segments: {error:?}"))?;
    let mut selected = Vec::new();
    for segment in segments {
        read_segment(&segment, selected_types, &mut selected)?;
    }
    Ok(selected)
}

fn acquire_shared_ledger_lock(segments_dir: &Path, wait: Duration) -> Result<Flock<File>, String> {
    let lock_path = segments_dir.join(".append.lock");
    let mut file = open_append_lock(&lock_path)?;
    let deadline = Instant::now() + wait;
    loop {
        match Flock::lock(file, FlockArg::LockSharedNonblock) {
            Ok(lock) => return Ok(lock),
            Err((returned, error)) => {
                file = returned;
                if error == Errno::EWOULDBLOCK || error == Errno::EAGAIN {
                    if Instant::now() >= deadline {
                        return Err("shared TL ledger append lock remained busy".to_string());
                    }
                    thread::sleep(
                        LOCK_POLL.min(deadline.saturating_duration_since(Instant::now())),
                    );
                } else {
                    return Err(format!(
                        "cannot acquire shared TL ledger read lock: {error}"
                    ));
                }
            }
        }
    }
}

fn open_append_lock(path: &Path) -> Result<File, String> {
    let parent = path
        .parent()
        .ok_or_else(|| "TL ledger append lock has no parent".to_string())?;
    let directory = fs::symlink_metadata(parent)
        .map_err(|error| format!("cannot inspect TL ledger directory: {}", error.kind()))?;
    if directory.file_type().is_symlink() || !directory.is_dir() {
        return Err("TL ledger directory is not a regular directory".to_string());
    }
    let mut options = OpenOptions::new();
    options.create(true).read(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    }
    let file = options
        .open(path)
        .map_err(|error| format!("cannot open TL ledger append lock: {}", error.kind()))?;
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("cannot recheck TL ledger append lock: {}", error.kind()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err("TL ledger append lock is not a regular file".to_string());
    }
    let opened = file
        .metadata()
        .map_err(|error| format!("cannot inspect TL ledger append lock: {}", error.kind()))?;
    if !same_file(&metadata, &opened) {
        return Err("TL ledger append lock changed while opening".to_string());
    }
    Ok(file)
}

fn read_segment(
    segment: &Path,
    selected_types: &[&str],
    selected: &mut Vec<GitRecoveryEvent>,
) -> Result<(), String> {
    let before = fs::symlink_metadata(segment)
        .map_err(|error| format!("cannot inspect ledger segment: {}", error.kind()))?;
    if before.file_type().is_symlink() || !before.is_file() {
        return Err("TL ledger segment is not a regular file".to_string());
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    }
    let file = options
        .open(segment)
        .map_err(|error| format!("cannot open ledger segment: {}", error.kind()))?;
    let opened = file
        .metadata()
        .map_err(|error| format!("cannot inspect opened ledger segment: {}", error.kind()))?;
    if !same_file(&before, &opened) {
        return Err("TL ledger segment changed while opening".to_string());
    }
    let mut reader = BufReader::new(file);
    while let Some(line) = read_bounded_line(&mut reader)? {
        let document: Value = serde_json::from_slice(&line)
            .map_err(|_| "TL ledger contains malformed JSON evidence".to_string())?;
        let object = document
            .as_object()
            .ok_or_else(|| "TL ledger event is not a JSON object".to_string())?;
        let event_type = match (object.get("type"), object.get("event_type")) {
            (Some(Value::String(left)), Some(Value::String(right))) if left != right => {
                return Err("TL ledger event has conflicting type aliases".to_string());
            }
            (Some(Value::String(value)), _) | (_, Some(Value::String(value))) => value.as_str(),
            (Some(_), _) | (_, Some(_)) => {
                return Err("TL ledger event type is malformed".to_string());
            }
            (None, None) => continue,
        };
        if !selected_types.contains(&event_type) {
            continue;
        }
        let event_id = event_identity(object)?;
        let data = object
            .get("data")
            .filter(|value| value.is_object())
            .cloned()
            .ok_or_else(|| "Git recovery ledger event has malformed data".to_string())?;
        selected.push(GitRecoveryEvent {
            event_id,
            event_type: event_type.to_string(),
            data,
        });
    }
    let after = fs::symlink_metadata(segment)
        .map_err(|error| format!("ledger segment changed while reading: {}", error.kind()))?;
    let opened_after = reader
        .get_ref()
        .metadata()
        .map_err(|error| format!("cannot recheck opened ledger segment: {}", error.kind()))?;
    if !same_file(&before, &after) || !same_file(&opened, &opened_after) {
        return Err("TL ledger segment changed while reading recovery evidence".to_string());
    }
    Ok(())
}

fn event_identity(object: &serde_json::Map<String, Value>) -> Result<String, String> {
    let event_id = object
        .get("event_id")
        .and_then(Value::as_str)
        .or_else(|| object.get("id").and_then(Value::as_str))
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "Git recovery event has no durable event identity".to_string())?;
    if let (Some(primary), Some(alias)) = (
        object.get("event_id").and_then(Value::as_str),
        object.get("id").and_then(Value::as_str),
    ) {
        if primary != alias {
            return Err("Git recovery event has conflicting event identity aliases".to_string());
        }
    }
    Ok(event_id.to_string())
}

fn read_bounded_line(reader: &mut impl BufRead) -> Result<Option<Vec<u8>>, String> {
    let mut line = Vec::new();
    loop {
        let available = reader
            .fill_buf()
            .map_err(|error| format!("cannot read TL ledger segment: {}", error.kind()))?;
        if available.is_empty() {
            return if line.is_empty() {
                Ok(None)
            } else {
                Ok(Some(line))
            };
        }
        let end = available
            .iter()
            .position(|byte| *byte == b'\n')
            .map_or(available.len(), |position| position + 1);
        if line.len().saturating_add(end) > MAX_EVENT_BYTES {
            return Err("TL ledger event exceeds the recovery read limit".to_string());
        }
        let complete = available[end - 1] == b'\n';
        line.extend_from_slice(&available[..end]);
        reader.consume(end);
        if complete {
            return Ok(Some(line));
        }
    }
}

fn same_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        left.dev() == right.dev()
            && left.ino() == right.ino()
            && left.len() == right.len()
            && left.mtime() == right.mtime()
            && left.mtime_nsec() == right.mtime_nsec()
            && left.ctime() == right.ctime()
            && left.ctime_nsec() == right.ctime_nsec()
    }
    #[cfg(not(unix))]
    {
        left.len() == right.len() && left.modified().ok() == right.modified().ok()
    }
}

#[cfg(test)]
#[path = "git_recovery_ledger_tests.rs"]
mod tests;
