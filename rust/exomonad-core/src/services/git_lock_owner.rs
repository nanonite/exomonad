//! Durable proof that a managed process held one exact native Git lock inode.

use crate::services::event_log::EventLog;
use crate::services::git_mutation::{GitMutationError, GitMutationLease, GitMutationResource};
use crate::services::git_recovery_ledger::read_git_recovery_events;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::Child;
use std::time::Duration;

pub(crate) const PROOF_SCHEMA_VERSION: u32 = 2;
const LOCK_EVIDENCE_MAX_BYTES: u64 = 16 * 1024 * 1024;
pub(crate) const LOCK_HELD_EVENT: &str = "git.native_lock.held";

/// A PID paired with its kernel process start identity to reject PID reuse.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ManagedProcessIdentity {
    pub pid: u32,
    pub start_time_ticks: u64,
}

/// Stable metadata and a content digest for one native index lock inode.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NativeGitLockSnapshot {
    pub device: u64,
    pub inode: u64,
    pub size: u64,
    pub modified_seconds: i64,
    pub modified_nanoseconds: i64,
    pub changed_seconds: i64,
    pub changed_nanoseconds: i64,
    pub contents_sha256: String,
}

/// Audit evidence linking a managed operation and process to an open lock inode.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ManagedNativeLockProof {
    pub schema_version: u32,
    pub operation_id_sha256: String,
    pub worktree_root: String,
    pub worktree_root_sha256: String,
    pub git_dir: String,
    pub git_dir_sha256: String,
    pub common_git_dir: String,
    pub common_git_dir_sha256: String,
    pub index_file: String,
    pub index_file_sha256: String,
    pub index_lock_file: String,
    pub index_lock_file_sha256: String,
    pub index_lease_file_sha256: String,
    pub owner: ManagedProcessIdentity,
    pub lock: NativeGitLockSnapshot,
}

/// The proof plus its immutable event ID in the existing project ledger.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordedManagedNativeLockProof {
    pub event_id: String,
    pub proof: ManagedNativeLockProof,
}

pub(crate) trait ProcessInspector {
    fn start_time_ticks(&self, pid: u32) -> Result<Option<u64>, String>;
    fn holds_inode(&self, pid: u32, device: u64, inode: u64) -> Result<bool, String>;
    fn process_holders(
        &self,
        device: u64,
        inode: u64,
    ) -> Result<Vec<ManagedProcessIdentity>, String>;
}

/// Capture the process start identity immediately after spawning a managed Git child.
pub fn capture_managed_process_identity(
    child: &Child,
) -> Result<ManagedProcessIdentity, GitMutationError> {
    capture_managed_process_identity_with(&ProcFsInspector::default(), child.id())
}

/// Record an exact lock-holding observation through the existing immutable event ledger.
pub fn record_managed_native_lock_proof(
    lease: &GitMutationLease,
    child: &Child,
    owner: &ManagedProcessIdentity,
    event_log: &EventLog,
) -> Result<RecordedManagedNativeLockProof, GitMutationError> {
    if child.id() != owner.pid {
        return Err(uncertain(
            &lease.paths.index_lock_file,
            "process identity does not belong to the managed child handle",
        ));
    }
    record_proof_with(lease, owner, event_log, &ProcFsInspector::default())
}

fn capture_managed_process_identity_with(
    inspector: &dyn ProcessInspector,
    pid: u32,
) -> Result<ManagedProcessIdentity, GitMutationError> {
    let start_time_ticks = inspector
        .start_time_ticks(pid)
        .map_err(|reason| uncertain(Path::new("/proc"), &reason))?
        .ok_or_else(|| {
            uncertain(
                Path::new("/proc"),
                "managed process exited before identity capture",
            )
        })?;
    Ok(ManagedProcessIdentity {
        pid,
        start_time_ticks,
    })
}

fn record_proof_with(
    lease: &GitMutationLease,
    owner: &ManagedProcessIdentity,
    event_log: &EventLog,
    inspector: &dyn ProcessInspector,
) -> Result<RecordedManagedNativeLockProof, GitMutationError> {
    let paths = &lease.paths;
    if lease.resource != GitMutationResource::Index {
        return Err(uncertain(
            &paths.index_lock_file,
            "native index lock proof requires the matching index mutation lease",
        ));
    }

    verify_process_identity(inspector, owner, &paths.index_lock_file)?;
    let lock = snapshot_lock(&paths.index_lock_file)?;
    if !inspector
        .holds_inode(owner.pid, lock.device, lock.inode)
        .map_err(|reason| uncertain(&paths.index_lock_file, &reason))?
    {
        return Err(uncertain(
            &paths.index_lock_file,
            "managed process does not hold the current native lock inode open",
        ));
    }
    verify_process_identity(inspector, owner, &paths.index_lock_file)?;
    if snapshot_lock(&paths.index_lock_file)? != lock {
        return Err(uncertain(
            &paths.index_lock_file,
            "native lock changed while process ownership was being recorded",
        ));
    }

    let proof = ManagedNativeLockProof {
        schema_version: PROOF_SCHEMA_VERSION,
        operation_id_sha256: digest(lease.operation_id.as_bytes()),
        worktree_root: paths.worktree_root.to_string_lossy().into_owned(),
        worktree_root_sha256: digest_git_path(&paths.worktree_root),
        git_dir: paths.git_dir.to_string_lossy().into_owned(),
        git_dir_sha256: digest_git_path(&paths.git_dir),
        common_git_dir: paths.common_git_dir.to_string_lossy().into_owned(),
        common_git_dir_sha256: digest_git_path(&paths.common_git_dir),
        index_file: paths.index_file.to_string_lossy().into_owned(),
        index_file_sha256: digest_git_path(&paths.index_file),
        index_lock_file: paths.index_lock_file.to_string_lossy().into_owned(),
        index_lock_file_sha256: digest_git_path(&paths.index_lock_file),
        index_lease_file_sha256: digest_git_path(&paths.index_lease_file),
        owner: owner.clone(),
        lock,
    };
    let data = serde_json::to_value(&proof)
        .map_err(|_| uncertain(&paths.index_lock_file, "cannot encode native lock proof"))?;
    let event_id = append_proof_once(event_log, &data)?;
    Ok(RecordedManagedNativeLockProof { event_id, proof })
}

fn verify_process_identity(
    inspector: &dyn ProcessInspector,
    owner: &ManagedProcessIdentity,
    lock_path: &Path,
) -> Result<(), GitMutationError> {
    let current = inspector
        .start_time_ticks(owner.pid)
        .map_err(|reason| uncertain(lock_path, &reason))?;
    if current != Some(owner.start_time_ticks) {
        return Err(uncertain(
            lock_path,
            "managed process is absent or its PID was reused",
        ));
    }
    Ok(())
}

fn append_proof_once(
    event_log: &EventLog,
    proof: &serde_json::Value,
) -> Result<String, GitMutationError> {
    if let Some(event_id) = find_proof_event(event_log, proof)? {
        return Ok(event_id);
    }
    match event_log.append(LOCK_HELD_EVENT, "git-mutation", proof) {
        Ok(event_id) => Ok(event_id),
        Err(_) => find_proof_event(event_log, proof)?.ok_or_else(|| {
            uncertain(
                event_log.dir(),
                "native lock proof could not be durably recorded in the event ledger",
            )
        }),
    }
}

fn find_proof_event(
    event_log: &EventLog,
    proof: &serde_json::Value,
) -> Result<Option<String>, GitMutationError> {
    let records = read_git_recovery_events(event_log, &[LOCK_HELD_EVENT], Duration::from_secs(2))
        .map_err(|_| {
        uncertain(
            event_log.dir(),
            "cannot inspect prior native lock proof events",
        )
    })?;
    Ok(records.into_iter().find_map(|record| {
        (record.event_type == LOCK_HELD_EVENT && record.data == *proof).then_some(record.event_id)
    }))
}

pub(crate) fn snapshot_lock(path: &Path) -> Result<NativeGitLockSnapshot, GitMutationError> {
    let before = fs::symlink_metadata(path).map_err(|error| {
        uncertain(
            path,
            &format!("cannot inspect native lock ({})", error.kind()),
        )
    })?;
    if before.file_type().is_symlink() || !before.is_file() {
        return Err(uncertain(path, "native lock is not a regular file"));
    }
    if before.len() > LOCK_EVIDENCE_MAX_BYTES {
        return Err(uncertain(
            path,
            "native lock exceeds the evidence size limit",
        ));
    }

    let file = open_readonly_nofollow(path)?;
    let opened = file.metadata().map_err(|error| {
        uncertain(
            path,
            &format!("cannot inspect native lock inode ({})", error.kind()),
        )
    })?;
    verify_same_inode(path, &before, &opened)?;
    let contents_sha256 = hash_file(path, &file)?;
    let after = file.metadata().map_err(|error| {
        uncertain(
            path,
            &format!("cannot recheck native lock ({})", error.kind()),
        )
    })?;
    let current = fs::symlink_metadata(path).map_err(|error| {
        uncertain(
            path,
            &format!("native lock path changed ({})", error.kind()),
        )
    })?;
    verify_same_inode(path, &before, &after)?;
    verify_same_inode(path, &before, &current)?;
    if before.len() != after.len() || before.len() != current.len() {
        return Err(uncertain(
            path,
            "native lock changed while being fingerprinted",
        ));
    }

    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        Ok(NativeGitLockSnapshot {
            device: before.dev(),
            inode: before.ino(),
            size: before.len(),
            modified_seconds: before.mtime(),
            modified_nanoseconds: before.mtime_nsec(),
            changed_seconds: before.ctime(),
            changed_nanoseconds: before.ctime_nsec(),
            contents_sha256,
        })
    }
    #[cfg(not(unix))]
    {
        let _ = contents_sha256;
        Err(GitMutationError::UnsupportedPlatform)
    }
}

fn verify_same_inode(
    path: &Path,
    expected: &fs::Metadata,
    observed: &fs::Metadata,
) -> Result<(), GitMutationError> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if expected.dev() != observed.dev() || expected.ino() != observed.ino() {
            return Err(uncertain(
                path,
                "native lock inode changed during inspection",
            ));
        }
        if expected.len() != observed.len()
            || expected.mtime() != observed.mtime()
            || expected.mtime_nsec() != observed.mtime_nsec()
            || expected.ctime() != observed.ctime()
            || expected.ctime_nsec() != observed.ctime_nsec()
        {
            return Err(uncertain(
                path,
                "native lock metadata changed during inspection",
            ));
        }
        Ok(())
    }
    #[cfg(not(unix))]
    {
        let _ = (expected, observed);
        Err(GitMutationError::UnsupportedPlatform)
    }
}

fn open_readonly_nofollow(path: &Path) -> Result<File, GitMutationError> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    }
    options
        .open(path)
        .map_err(|error| uncertain(path, &format!("cannot open native lock ({})", error.kind())))
}

fn hash_file(path: &Path, file: &File) -> Result<String, GitMutationError> {
    let mut reader = file;
    let mut buffer = [0_u8; 8192];
    let mut hasher = Sha256::new();
    let mut bytes_read = 0_u64;
    loop {
        let read = reader.read(&mut buffer).map_err(|error| {
            uncertain(path, &format!("cannot read native lock ({})", error.kind()))
        })?;
        if read == 0 {
            break;
        }
        bytes_read = bytes_read.saturating_add(read as u64);
        if bytes_read > LOCK_EVIDENCE_MAX_BYTES {
            return Err(uncertain(
                path,
                "native lock exceeds the evidence size limit",
            ));
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

pub(crate) fn digest_git_path(path: &Path) -> String {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        digest(path.as_os_str().as_bytes())
    }
    #[cfg(not(unix))]
    {
        digest(path.to_string_lossy().as_bytes())
    }
}

fn digest(value: &[u8]) -> String {
    format!("{:x}", Sha256::digest(value))
}

fn uncertain(path: &Path, reason: &str) -> GitMutationError {
    GitMutationError::Uncertain {
        path: path.to_path_buf(),
        reason: reason.to_string(),
    }
}

#[derive(Debug, Clone)]
pub(crate) struct ProcFsInspector {
    root: PathBuf,
}

impl Default for ProcFsInspector {
    fn default() -> Self {
        Self {
            root: PathBuf::from("/proc"),
        }
    }
}

impl ProcFsInspector {
    #[cfg(test)]
    pub(crate) fn under(root: PathBuf) -> Self {
        Self { root }
    }
}

impl ProcessInspector for ProcFsInspector {
    fn start_time_ticks(&self, pid: u32) -> Result<Option<u64>, String> {
        let stat_path = self.root.join(pid.to_string()).join("stat");
        let stat = match fs::read_to_string(&stat_path) {
            Ok(stat) => stat,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => {
                return Err(format!(
                    "cannot inspect process identity ({})",
                    error.kind()
                ))
            }
        };
        parse_start_time_ticks(&stat).map(Some).ok_or_else(|| {
            "process identity record is malformed; lock ownership is uncertain".to_string()
        })
    }

    fn holds_inode(&self, pid: u32, device: u64, inode: u64) -> Result<bool, String> {
        let fd_dir = self.root.join(pid.to_string()).join("fd");
        let entries = fs::read_dir(&fd_dir).map_err(|error| {
            format!(
                "cannot inspect managed process descriptors ({})",
                error.kind()
            )
        })?;
        for entry in entries {
            let entry = entry.map_err(|error| {
                format!(
                    "cannot enumerate managed process descriptors ({})",
                    error.kind()
                )
            })?;
            let metadata = match fs::metadata(entry.path()) {
                Ok(metadata) => metadata,
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) => {
                    return Err(format!(
                        "cannot inspect managed process descriptor ({})",
                        error.kind()
                    ));
                }
            };
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                if metadata.dev() == device && metadata.ino() == inode {
                    return Ok(true);
                }
            }
            #[cfg(not(unix))]
            {
                let _ = (device, inode, metadata);
                return Err(
                    "process descriptor identity is unsupported on this platform".to_string(),
                );
            }
        }
        Ok(false)
    }

    fn process_holders(
        &self,
        device: u64,
        inode: u64,
    ) -> Result<Vec<ManagedProcessIdentity>, String> {
        let processes = fs::read_dir(&self.root)
            .map_err(|error| format!("cannot enumerate process identities ({})", error.kind()))?;
        let mut holders = Vec::new();
        for entry in processes {
            let entry = entry.map_err(|error| {
                format!("cannot enumerate process identities ({})", error.kind())
            })?;
            let Some(pid) = entry
                .file_name()
                .to_str()
                .and_then(|name| name.parse().ok())
            else {
                continue;
            };
            let Some(start_time_ticks) = self.start_time_ticks(pid)? else {
                continue;
            };
            if !self.holds_inode(pid, device, inode)? {
                continue;
            }
            if self.start_time_ticks(pid)? != Some(start_time_ticks) {
                return Err("process identity changed during descriptor inspection".to_string());
            }
            holders.push(ManagedProcessIdentity {
                pid,
                start_time_ticks,
            });
        }
        Ok(holders)
    }
}

fn parse_start_time_ticks(stat: &str) -> Option<u64> {
    let (_, fields) = stat.rsplit_once(')')?;
    fields.split_whitespace().nth(19)?.parse().ok()
}

#[cfg(test)]
#[path = "git_lock_owner_tests.rs"]
mod tests;
