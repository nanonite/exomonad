//! Explicit, fingerprint-bound recovery for pre-ownership Git index locks.

use crate::services::event_log::EventLog;
use crate::services::git_lock_owner::{
    snapshot_lock, ManagedNativeLockProof, ManagedProcessIdentity, NativeGitLockSnapshot,
    ProcFsInspector, ProcessInspector,
};
use crate::services::git_lock_recovery::{
    append_audit_once, atomic_move_lock, check_quarantine_capacity, proof_matches_paths,
};
use crate::services::git_mutation::{
    ensure_safe_lease_directory, GitMutationResource, GitMutationSafety,
};
use crate::services::git_recovery_ledger::{
    read_git_recovery_events, read_git_recovery_events_at, GitRecoveryEvent,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::{self, OpenOptions};
use std::io::{self, Read};
#[cfg(unix)]
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

const LEGACY_INTENT_EVENT: &str = "git.native_lock.legacy_recovery_intent";
const LEGACY_RECOVERED_EVENT: &str = "git.native_lock.legacy_recovered";
const LEGACY_SCHEMA_VERSION: u32 = 1;
const INDEX_EVIDENCE_MAX_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const LEGACY_QUARANTINE_DIRECTORY: &str = "quarantine";

/// Read-only fingerprint of an index file, including inode and content identity.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GitFileFingerprint {
    pub device: u64,
    pub inode: u64,
    pub size: u64,
    pub modified_seconds: i64,
    pub modified_nanoseconds: i64,
    pub changed_seconds: i64,
    pub changed_nanoseconds: i64,
    pub contents_sha256: String,
}

/// Activity result limited to durable managed-process records for this Git index.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum LegacyManagedActivity {
    Clear {
        inspected_event_ids: Vec<String>,
    },
    Active {
        processes: Vec<ManagedProcessIdentity>,
    },
    Uncertain {
        reason: String,
    },
}

/// Dry-run result for an unowned native index lock.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LegacyGitLockInspection {
    pub schema_version: u32,
    pub worktree_root: PathBuf,
    pub git_dir: PathBuf,
    pub common_git_dir: PathBuf,
    pub index_file: PathBuf,
    pub index_lock_file: PathBuf,
    pub index: GitFileFingerprint,
    pub lock: Option<NativeGitLockSnapshot>,
    pub managed_activity: LegacyManagedActivity,
    pub matching_managed_proof: bool,
    pub recovery_fingerprint: Option<String>,
    pub quarantine_file: Option<PathBuf>,
    pub operator_scope: String,
}

/// Durable evidence for one explicitly authorized legacy lock quarantine.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LegacyGitLockRecoveryReceipt {
    pub recovery_fingerprint: String,
    pub intent_event_id: String,
    pub receipt_event_id: String,
    pub quarantine_file: PathBuf,
    pub original_lock: NativeGitLockSnapshot,
    pub index: GitFileFingerprint,
}

/// Outcome of applying a previously displayed fingerprint.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LegacyGitLockApplyResult {
    pub receipt: LegacyGitLockRecoveryReceipt,
    pub already_applied: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct LegacyRecoveryData {
    schema_version: u32,
    recovery_fingerprint: String,
    writer_stopped_authorization: bool,
    actor: ManagedProcessIdentity,
    worktree_root: PathBuf,
    git_dir: PathBuf,
    common_git_dir: PathBuf,
    index_file: PathBuf,
    index_lock_file: PathBuf,
    index: GitFileFingerprint,
    original_lock: NativeGitLockSnapshot,
    quarantine_file: PathBuf,
}

#[derive(Debug, Serialize)]
struct FingerprintPayload<'a> {
    schema_version: u32,
    worktree_root: &'a Path,
    git_dir: &'a Path,
    common_git_dir: &'a Path,
    index_file: &'a Path,
    index_lock_file: &'a Path,
    index: &'a GitFileFingerprint,
    lock: &'a NativeGitLockSnapshot,
    activity: &'a LegacyManagedActivity,
    matching_managed_proof: bool,
}

/// Inspect a legacy lock without creating files or requiring a running server.
pub fn inspect_legacy_git_lock(
    safety: &GitMutationSafety,
    worktree: &Path,
    ledger_segments: &Path,
) -> Result<LegacyGitLockInspection, String> {
    inspect_legacy_git_lock_with(
        safety,
        worktree,
        ledger_segments,
        &ProcFsInspector::default(),
    )
}

fn inspect_legacy_git_lock_with(
    safety: &GitMutationSafety,
    worktree: &Path,
    ledger_segments: &Path,
    inspector: &dyn ProcessInspector,
) -> Result<LegacyGitLockInspection, String> {
    let paths = safety
        .resolve_paths_with_index_override(worktree, None)
        .map_err(|error| error.to_string())?;
    let index = fingerprint_file(&paths.index_file)?;
    let lock = match fs::symlink_metadata(&paths.index_lock_file) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => {
            return Err("native index lock is not a regular file".into());
        }
        Ok(_) => Some(snapshot_lock(&paths.index_lock_file).map_err(|error| error.to_string())?),
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => {
            return Err(format!(
                "cannot inspect native index lock: {}",
                error.kind()
            ))
        }
    };
    let matching_managed_proof = match &lock {
        Some(lock) => has_matching_managed_proof(ledger_segments, &paths, lock)?,
        None => false,
    };
    let managed_activity = inspect_managed_activity(ledger_segments, &paths, inspector)?;
    let recovery_fingerprint = lock
        .as_ref()
        .map(|lock| {
            recovery_fingerprint(
                &paths,
                &index,
                lock,
                &managed_activity,
                matching_managed_proof,
            )
        })
        .transpose()?;
    let quarantine_file = recovery_fingerprint.as_ref().map(|fingerprint| {
        paths
            .common_git_dir
            .join("exomonad-mutation")
            .join(LEGACY_QUARANTINE_DIRECTORY)
            .join(format!("legacy-{fingerprint}.lock"))
    });
    Ok(LegacyGitLockInspection {
        schema_version: LEGACY_SCHEMA_VERSION,
        worktree_root: paths.worktree_root,
        git_dir: paths.git_dir,
        common_git_dir: paths.common_git_dir,
        index_file: paths.index_file,
        index_lock_file: paths.index_lock_file,
        index,
        lock,
        managed_activity,
        matching_managed_proof,
        recovery_fingerprint,
        quarantine_file,
        operator_scope: "Recorded ExoMonad process scopes are checked by PID/start identity and process group. A host-wide external-process census is not performed; --writers-stopped is the operator authorization for other repository writers.".to_string(),
    })
}

/// Apply a dry-run fingerprint under the shared index lease and preserve the exact lock inode.
pub fn apply_legacy_git_lock(
    safety: &GitMutationSafety,
    worktree: &Path,
    event_log: &EventLog,
    expected_fingerprint: &str,
    writers_stopped: bool,
    wait: Duration,
) -> Result<LegacyGitLockApplyResult, String> {
    if !writers_stopped {
        return Err("explicit --writers-stopped authorization is required".into());
    }
    validate_fingerprint(expected_fingerprint)?;
    let paths = safety
        .resolve_paths_with_index_override(worktree, None)
        .map_err(|error| error.to_string())?;
    if let Some(receipt) = find_completed_receipt(event_log, expected_fingerprint, &paths)? {
        return verify_applied_receipt(receipt, true);
    }
    let operation_id = format!("legacy-lock-{}", &expected_fingerprint[..32]);
    let lease = safety
        .acquire(worktree, GitMutationResource::Index, &operation_id, wait)
        .map_err(|error| error.to_string())?;
    if let Some(receipt) =
        complete_interrupted_recovery(safety, worktree, event_log, expected_fingerprint, &paths)?
    {
        return verify_applied_receipt(receipt, true);
    }
    let before = inspect_legacy_git_lock(safety, worktree, event_log.ledger().segments_dir())?;
    ensure_eligible(&before, expected_fingerprint)?;
    let lock = before.lock.clone().ok_or("native index lock disappeared")?;
    let target = before
        .quarantine_file
        .clone()
        .ok_or("recovery quarantine target is unavailable")?;
    let target_dir = target.parent().ok_or("quarantine target has no parent")?;
    ensure_safe_lease_directory(target_dir).map_err(|error| error.to_string())?;
    check_quarantine_capacity(target_dir, &target)?;
    let actor = current_actor()?;
    let data = LegacyRecoveryData {
        schema_version: LEGACY_SCHEMA_VERSION,
        recovery_fingerprint: expected_fingerprint.to_string(),
        writer_stopped_authorization: writers_stopped,
        actor,
        worktree_root: before.worktree_root.clone(),
        git_dir: before.git_dir.clone(),
        common_git_dir: before.common_git_dir.clone(),
        index_file: before.index_file.clone(),
        index_lock_file: before.index_lock_file.clone(),
        index: before.index.clone(),
        original_lock: lock.clone(),
        quarantine_file: target.clone(),
    };
    let intent_event_id = append_audit_once(
        event_log,
        LEGACY_INTENT_EVENT,
        &data,
        &before.index_lock_file,
    )?;
    let current_paths = safety
        .resolve_paths_with_index_override(worktree, None)
        .map_err(|error| format!("Git path revalidation failed: {error}"))?;
    if current_paths != lease.paths {
        return Err("Git paths changed after acquiring the index mutation lease".into());
    }
    let after = inspect_legacy_git_lock(safety, worktree, event_log.ledger().segments_dir())?;
    ensure_eligible(&after, expected_fingerprint)?;
    if after != before {
        return Err("legacy lock recovery evidence changed after dry-run; inspect again for a new fingerprint".into());
    }
    atomic_move_lock(&before.index_lock_file, &target, &lock)
        .map_err(|error| format!("cannot quarantine approved native lock: {}", error.kind()))?;
    let moved = snapshot_lock(&target).map_err(|error| error.to_string())?;
    if !same_lock_inode(&lock, &moved) {
        return Err("quarantined lock differs from the approved inode and content".into());
    }
    let receipt =
        append_recovery_receipt(event_log, &data, &intent_event_id, &before.index_lock_file)?;
    verify_applied_receipt(receipt, false)
}

fn ensure_eligible(inspection: &LegacyGitLockInspection, expected: &str) -> Result<(), String> {
    if inspection.recovery_fingerprint.as_deref() != Some(expected) {
        return Err("legacy recovery fingerprint is stale; inspect the lock again".into());
    }
    if inspection.lock.is_none() {
        return Err("native index lock is absent and no matching recovery receipt exists".into());
    }
    if inspection.matching_managed_proof {
        return Err(
            "this lock has managed ownership proof; retry init for automatic managed recovery"
                .into(),
        );
    }
    match &inspection.managed_activity {
        LegacyManagedActivity::Clear { .. } => Ok(()),
        LegacyManagedActivity::Active { .. } => {
            Err("an active recorded ExoMonad Git process or helper prevents legacy recovery".into())
        }
        LegacyManagedActivity::Uncertain { reason } => {
            Err(format!("managed process activity is uncertain: {reason}"))
        }
    }
}

fn inspect_managed_activity(
    ledger_segments: &Path,
    paths: &crate::services::GitMutationPaths,
    inspector: &dyn ProcessInspector,
) -> Result<LegacyManagedActivity, String> {
    const STARTED: &str = "git.process.started";
    const FINISHED: &str = "git.process.finished";
    let records = read_git_recovery_events_at(
        ledger_segments,
        &[STARTED, FINISHED],
        Duration::from_secs(2),
    )?;
    let started = records
        .iter()
        .filter(|record| record.event_type == STARTED)
        .collect::<Vec<_>>();
    let mut inspected_event_ids = Vec::new();
    let mut active = Vec::new();
    for record in started {
        if !event_is_relevant_to_lock(record, paths) {
            continue;
        }
        inspected_event_ids.push(record.event_id.clone());
        let scope = parse_process_scope(record, paths)?;
        matching_finish(&records, &scope, record)?;
        match inspector.start_time_ticks(scope.owner.pid) {
            Ok(Some(current)) if current == scope.owner.start_time_ticks => {
                active.push(scope.owner);
                continue;
            }
            Ok(Some(_)) | Ok(None) => {}
            Err(reason) => return Ok(LegacyManagedActivity::Uncertain { reason }),
        }
        match inspector.process_group_exists(scope.process_group_id) {
            Ok(true) => active.push(scope.owner),
            Ok(false) => {}
            Err(reason) => return Ok(LegacyManagedActivity::Uncertain { reason }),
        }
    }
    active.sort_by_key(|process| (process.pid, process.start_time_ticks));
    active.dedup();
    if active.is_empty() {
        Ok(LegacyManagedActivity::Clear {
            inspected_event_ids,
        })
    } else {
        Ok(LegacyManagedActivity::Active { processes: active })
    }
}

fn event_is_relevant_to_lock(
    record: &GitRecoveryEvent,
    paths: &crate::services::GitMutationPaths,
) -> bool {
    let lock_path = paths.index_lock_file.to_string_lossy();
    let common_dir = paths.common_git_dir.to_string_lossy();
    record
        .data
        .get("index_lock_file")
        .and_then(serde_json::Value::as_str)
        == Some(lock_path.as_ref())
        || record
            .data
            .get("common_git_dir")
            .and_then(serde_json::Value::as_str)
            == Some(common_dir.as_ref())
            && record
                .data
                .get("worktree_root")
                .and_then(serde_json::Value::as_str)
                == Some(paths.worktree_root.to_string_lossy().as_ref())
}

fn parse_process_scope(
    record: &GitRecoveryEvent,
    paths: &crate::services::GitMutationPaths,
) -> Result<ProcessScope, String> {
    let process_id = required_u32(&record.data, "process_id")?;
    let start_time_ticks = record
        .data
        .get("process_start_identity")
        .and_then(serde_json::Value::as_u64)
        .ok_or("relevant managed process record has no start identity")?;
    let process_group_id = required_u32(&record.data, "process_group_id")?;
    let operation_hash = record
        .data
        .get("operation_id_sha256")
        .and_then(serde_json::Value::as_str)
        .filter(|value| value.len() == 64)
        .ok_or("relevant managed process record has no operation identity")?;
    if process_group_id != process_id
        || record
            .data
            .get("worktree_root")
            .and_then(serde_json::Value::as_str)
            != Some(paths.worktree_root.to_string_lossy().as_ref())
        || record
            .data
            .get("git_dir")
            .and_then(serde_json::Value::as_str)
            != Some(paths.git_dir.to_string_lossy().as_ref())
        || record
            .data
            .get("common_git_dir")
            .and_then(serde_json::Value::as_str)
            != Some(paths.common_git_dir.to_string_lossy().as_ref())
        || record
            .data
            .get("index_file")
            .and_then(serde_json::Value::as_str)
            != Some(paths.index_file.to_string_lossy().as_ref())
        || record
            .data
            .get("index_lock_file")
            .and_then(serde_json::Value::as_str)
            != Some(paths.index_lock_file.to_string_lossy().as_ref())
    {
        return Err("relevant managed process record conflicts with canonical Git paths".into());
    }
    Ok(ProcessScope {
        owner: ManagedProcessIdentity {
            pid: process_id,
            start_time_ticks,
        },
        process_group_id,
        operation_hash: operation_hash.to_string(),
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ProcessScope {
    owner: ManagedProcessIdentity,
    process_group_id: u32,
    operation_hash: String,
}

fn matching_finish(
    records: &[GitRecoveryEvent],
    scope: &ProcessScope,
    start: &GitRecoveryEvent,
) -> Result<bool, String> {
    let finishes = records
        .iter()
        .filter(|record| {
            record.event_type == "git.process.finished"
                && record
                    .data
                    .get("operation_id_sha256")
                    .and_then(serde_json::Value::as_str)
                    == Some(scope.operation_hash.as_str())
        })
        .collect::<Vec<_>>();
    if finishes.len() > 1 {
        return Err(format!(
            "managed process {} has multiple terminal records",
            start.event_id
        ));
    }
    let Some(finish) = finishes.first() else {
        return Ok(false);
    };
    let finish_pid = finish
        .data
        .get("process_id")
        .and_then(serde_json::Value::as_u64)
        .and_then(|value| u32::try_from(value).ok());
    let finish_start = finish
        .data
        .get("process_start_identity")
        .and_then(serde_json::Value::as_u64);
    let finish_group = finish
        .data
        .get("process_group_id")
        .and_then(serde_json::Value::as_u64)
        .and_then(|value| u32::try_from(value).ok());
    if finish_pid != Some(scope.owner.pid)
        || finish_start != Some(scope.owner.start_time_ticks)
        || finish_group != Some(scope.process_group_id)
    {
        return Err("managed process terminal record conflicts with its start identity".into());
    }
    Ok(true)
}

fn required_u32(value: &serde_json::Value, key: &str) -> Result<u32, String> {
    value
        .get(key)
        .and_then(serde_json::Value::as_u64)
        .and_then(|number| u32::try_from(number).ok())
        .ok_or_else(|| format!("relevant managed process record has no valid {key}"))
}

fn has_matching_managed_proof(
    ledger_segments: &Path,
    paths: &crate::services::GitMutationPaths,
    snapshot: &NativeGitLockSnapshot,
) -> Result<bool, String> {
    let records = read_git_recovery_events_at(
        ledger_segments,
        &["git.native_lock.held"],
        Duration::from_secs(2),
    )?;
    for record in records {
        if record
            .data
            .get("index_lock_file")
            .and_then(serde_json::Value::as_str)
            != Some(paths.index_lock_file.to_string_lossy().as_ref())
        {
            continue;
        }
        let version = record
            .data
            .get("schema_version")
            .and_then(serde_json::Value::as_u64)
            .ok_or("native lock ownership event has no schema version")?;
        if version != 2 {
            return Err(
                "native lock ownership event has an unsupported schema; legacy apply is blocked"
                    .into(),
            );
        }
        let proof: ManagedNativeLockProof = serde_json::from_value(record.data)
            .map_err(|_| "native lock ownership event is malformed".to_string())?;
        if proof_matches_paths(&proof, paths) && proof.lock == *snapshot {
            return Ok(true);
        }
    }
    Ok(false)
}

fn recovery_fingerprint(
    paths: &crate::services::GitMutationPaths,
    index: &GitFileFingerprint,
    lock: &NativeGitLockSnapshot,
    activity: &LegacyManagedActivity,
    matching_managed_proof: bool,
) -> Result<String, String> {
    let payload = FingerprintPayload {
        schema_version: LEGACY_SCHEMA_VERSION,
        worktree_root: &paths.worktree_root,
        git_dir: &paths.git_dir,
        common_git_dir: &paths.common_git_dir,
        index_file: &paths.index_file,
        index_lock_file: &paths.index_lock_file,
        index,
        lock,
        activity,
        matching_managed_proof,
    };
    let bytes = serde_json::to_vec(&payload)
        .map_err(|_| "cannot encode recovery fingerprint".to_string())?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

fn fingerprint_file(path: &Path) -> Result<GitFileFingerprint, String> {
    #[cfg(not(unix))]
    {
        let _ = path;
        return Err("legacy Git lock fingerprinting is unsupported on this platform".into());
    }
    #[cfg(unix)]
    {
        let before = fs::symlink_metadata(path)
            .map_err(|error| format!("cannot inspect index file: {}", error.kind()))?;
        if before.file_type().is_symlink()
            || !before.is_file()
            || before.len() > INDEX_EVIDENCE_MAX_BYTES
        {
            return Err("active Git index is not a supported regular file".into());
        }
        let mut options = OpenOptions::new();
        options.read(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
        }
        let mut file = options
            .open(path)
            .map_err(|error| format!("cannot open index file: {}", error.kind()))?;
        let opened = file
            .metadata()
            .map_err(|error| format!("cannot inspect opened index: {}", error.kind()))?;
        if !same_metadata(&before, &opened) {
            return Err("active Git index changed while opening".into());
        }
        let mut hasher = Sha256::new();
        let mut buffer = [0_u8; 16 * 1024];
        let mut bytes = 0_u64;
        loop {
            let count = file
                .read(&mut buffer)
                .map_err(|error| format!("cannot read index file: {}", error.kind()))?;
            if count == 0 {
                break;
            }
            bytes = bytes.saturating_add(count as u64);
            if bytes > INDEX_EVIDENCE_MAX_BYTES {
                return Err("active Git index exceeds the evidence size limit".into());
            }
            hasher.update(&buffer[..count]);
        }
        let after = file
            .metadata()
            .map_err(|error| format!("cannot recheck opened index: {}", error.kind()))?;
        let current = fs::symlink_metadata(path)
            .map_err(|error| format!("active Git index path changed: {}", error.kind()))?;
        if !same_metadata(&before, &after)
            || !same_metadata(&before, &current)
            || bytes != before.len()
        {
            return Err("active Git index changed while fingerprinting".into());
        }
        Ok(GitFileFingerprint {
            device: before.dev(),
            inode: before.ino(),
            size: before.len(),
            modified_seconds: before.mtime(),
            modified_nanoseconds: before.mtime_nsec(),
            changed_seconds: before.ctime(),
            changed_nanoseconds: before.ctime_nsec(),
            contents_sha256: format!("{:x}", hasher.finalize()),
        })
    }
}

fn same_metadata(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    #[cfg(unix)]
    {
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

fn validate_fingerprint(value: &str) -> Result<(), String> {
    if value.len() != 64 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("expected fingerprint must be a 64-character hexadecimal SHA-256 value".into());
    }
    Ok(())
}

fn current_actor() -> Result<ManagedProcessIdentity, String> {
    let pid = std::process::id();
    let start_time_ticks = ProcFsInspector::default()
        .start_time_ticks(pid)?
        .ok_or("current operator process identity is unavailable")?;
    Ok(ManagedProcessIdentity {
        pid,
        start_time_ticks,
    })
}

fn find_completed_receipt(
    event_log: &EventLog,
    fingerprint: &str,
    paths: &crate::services::GitMutationPaths,
) -> Result<Option<LegacyGitLockRecoveryReceipt>, String> {
    let records =
        read_git_recovery_events(event_log, &[LEGACY_RECOVERED_EVENT], Duration::from_secs(2))?;
    let intents =
        read_git_recovery_events(event_log, &[LEGACY_INTENT_EVENT], Duration::from_secs(2))?;
    let mut matches = Vec::new();
    for record in records {
        let intent_event_id = record
            .data
            .get("intent_event_id")
            .and_then(serde_json::Value::as_str)
            .ok_or("legacy recovery receipt has no intent event ID")?
            .to_string();
        let data: LegacyRecoveryData = serde_json::from_value(record.data)
            .map_err(|_| "legacy Git lock recovery receipt is malformed".to_string())?;
        verify_authorized_intent(&intents, &data, &intent_event_id)?;
        if data.recovery_fingerprint == fingerprint
            && data.worktree_root == paths.worktree_root
            && data.git_dir == paths.git_dir
            && data.common_git_dir == paths.common_git_dir
            && data.index_file == paths.index_file
            && data.index_lock_file == paths.index_lock_file
        {
            matches.push(recovery_receipt(record.event_id, &intent_event_id, data));
        }
    }
    if matches.len() > 1 {
        return Err("duplicate legacy recovery receipts exist for this fingerprint".into());
    }
    Ok(matches.pop())
}

fn complete_interrupted_recovery(
    safety: &GitMutationSafety,
    worktree: &Path,
    event_log: &EventLog,
    fingerprint: &str,
    paths: &crate::services::GitMutationPaths,
) -> Result<Option<LegacyGitLockRecoveryReceipt>, String> {
    let records =
        read_git_recovery_events(event_log, &[LEGACY_INTENT_EVENT], Duration::from_secs(2))?;
    let mut matching = Vec::new();
    for record in &records {
        let data: LegacyRecoveryData = serde_json::from_value(record.data.clone())
            .map_err(|_| "legacy recovery intent is malformed".to_string())?;
        if data.recovery_fingerprint == fingerprint && data.worktree_root == paths.worktree_root {
            matching.push((record.event_id.clone(), data));
        }
    }
    if matching.len() > 1 {
        return Err("multiple legacy recovery intents claim the same fingerprint".into());
    }
    let Some((intent_id, data)) = matching.pop() else {
        return Ok(None);
    };
    verify_authorized_intent(&records, &data, &intent_id)?;
    validate_intent_paths(&data, paths)?;
    if safety
        .resolve_paths_with_index_override(worktree, None)
        .map_err(|error| error.to_string())?
        != *paths
    {
        return Err("Git paths changed since legacy recovery was authorized".into());
    }
    match snapshot_lock(&data.quarantine_file) {
        Ok(quarantined) if same_lock_inode(&data.original_lock, &quarantined) => {}
        Ok(_) => return Err("interrupted legacy quarantine differs from its intent".into()),
        Err(_) => return Ok(None),
    }
    append_recovery_receipt(event_log, &data, &intent_id, &data.index_lock_file).map(Some)
}

fn verify_authorized_intent(
    intents: &[GitRecoveryEvent],
    data: &LegacyRecoveryData,
    intent_event_id: &str,
) -> Result<(), String> {
    if data.schema_version != LEGACY_SCHEMA_VERSION || !data.writer_stopped_authorization {
        return Err("legacy recovery intent lacks supported operator authorization".into());
    }
    validate_fingerprint(&data.recovery_fingerprint)?;
    let matching = intents
        .iter()
        .filter(|record| record.event_id == intent_event_id)
        .collect::<Vec<_>>();
    if matching.len() != 1 {
        return Err("legacy recovery receipt has no unique matching operator intent".into());
    }
    let intent: LegacyRecoveryData = serde_json::from_value(matching[0].data.clone())
        .map_err(|_| "legacy recovery intent is malformed".to_string())?;
    if intent != *data {
        return Err("legacy recovery receipt conflicts with its operator intent".into());
    }
    Ok(())
}

fn append_recovery_receipt(
    event_log: &EventLog,
    data: &LegacyRecoveryData,
    intent_event_id: &str,
    path: &Path,
) -> Result<LegacyGitLockRecoveryReceipt, String> {
    let value = serde_json::json!({
        "schema_version": data.schema_version,
        "recovery_fingerprint": data.recovery_fingerprint,
        "writer_stopped_authorization": data.writer_stopped_authorization,
        "actor": data.actor,
        "worktree_root": data.worktree_root,
        "git_dir": data.git_dir,
        "common_git_dir": data.common_git_dir,
        "index_file": data.index_file,
        "index_lock_file": data.index_lock_file,
        "index": data.index,
        "original_lock": data.original_lock,
        "quarantine_file": data.quarantine_file,
        "intent_event_id": intent_event_id,
    });
    let receipt_event_id = append_audit_once(event_log, LEGACY_RECOVERED_EVENT, &value, path)?;
    Ok(recovery_receipt(
        receipt_event_id,
        intent_event_id,
        data.clone(),
    ))
}

fn recovery_receipt(
    receipt_event_id: String,
    intent_event_id: &str,
    data: LegacyRecoveryData,
) -> LegacyGitLockRecoveryReceipt {
    LegacyGitLockRecoveryReceipt {
        recovery_fingerprint: data.recovery_fingerprint,
        intent_event_id: intent_event_id.to_string(),
        receipt_event_id,
        quarantine_file: data.quarantine_file,
        original_lock: data.original_lock,
        index: data.index,
    }
}

fn verify_applied_receipt(
    receipt: LegacyGitLockRecoveryReceipt,
    already_applied: bool,
) -> Result<LegacyGitLockApplyResult, String> {
    let quarantined = snapshot_lock(&receipt.quarantine_file)
        .map_err(|error| format!("retained legacy lock receipt cannot be verified: {error}"))?;
    if !same_lock_inode(&receipt.original_lock, &quarantined) {
        return Err("retained legacy lock differs from its durable recovery receipt".into());
    }
    Ok(LegacyGitLockApplyResult {
        receipt,
        already_applied,
    })
}

fn validate_intent_paths(
    data: &LegacyRecoveryData,
    paths: &crate::services::GitMutationPaths,
) -> Result<(), String> {
    let expected_quarantine_dir = paths
        .common_git_dir
        .join("exomonad-mutation")
        .join(LEGACY_QUARANTINE_DIRECTORY);
    let expected_name = format!("legacy-{}.lock", data.recovery_fingerprint);
    if data.worktree_root != paths.worktree_root
        || data.git_dir != paths.git_dir
        || data.common_git_dir != paths.common_git_dir
        || data.index_file != paths.index_file
        || data.index_lock_file != paths.index_lock_file
        || data.quarantine_file.parent() != Some(expected_quarantine_dir.as_path())
        || data
            .quarantine_file
            .file_name()
            .and_then(|name| name.to_str())
            != Some(expected_name.as_str())
    {
        return Err("legacy recovery intent does not match canonical Git paths".into());
    }
    Ok(())
}

fn same_lock_inode(expected: &NativeGitLockSnapshot, actual: &NativeGitLockSnapshot) -> bool {
    expected.device == actual.device
        && expected.inode == actual.inode
        && expected.size == actual.size
        && expected.contents_sha256 == actual.contents_sha256
        && expected.modified_seconds == actual.modified_seconds
        && expected.modified_nanoseconds == actual.modified_nanoseconds
}

#[cfg(test)]
#[path = "git_legacy_lock_tests.rs"]
mod tests;
