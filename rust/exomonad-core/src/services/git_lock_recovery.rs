//! Evidence-backed reconciliation and quarantine of native Git index locks.

use crate::services::event_log::EventLog;
use crate::services::git_lock_owner::{
    digest_git_path, ManagedNativeLockProof, ManagedProcessIdentity, NativeGitLockSnapshot,
    ProcFsInspector, ProcessInspector, LOCK_HELD_EVENT, PROOF_SCHEMA_VERSION,
};
use crate::services::git_mutation::{
    ensure_safe_lease_directory, GitMutationError, GitMutationLease, GitMutationResource,
    GitMutationSafety,
};
use crate::services::git_recovery_ledger::read_git_recovery_events;
#[cfg(target_os = "linux")]
use nix::fcntl::{renameat2, RenameFlags};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io;
#[cfg(target_os = "linux")]
use std::os::fd::AsRawFd;
#[cfg(unix)]
use std::os::unix::fs::MetadataExt;
#[cfg(target_os = "linux")]
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

const QUARANTINE_DIRECTORY: &str = "quarantine";
const QUARANTINE_CAPACITY: usize = 64;
const QUARANTINE_POLL: Duration = Duration::from_millis(25);
const QUARANTINE_INTENT_EVENT: &str = "git.native_lock.quarantine_intent";
const QUARANTINED_EVENT: &str = "git.native_lock.quarantined";
const RECOVERY_SCHEMA_VERSION: u32 = 1;

/// Closed outcome for the managed index-lock readiness boundary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NativeLockRecoveryVerdict {
    Clear,
    Busy {
        path: PathBuf,
        owners: Vec<ManagedProcessIdentity>,
        reason: String,
    },
    Recovered(GitLockRecoveryReceipt),
    Uncertain {
        path: PathBuf,
        reason: String,
    },
}

/// Durable identity for one proven lock quarantine.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GitLockRecoveryReceipt {
    pub proof_event_id: String,
    pub intent_event_id: String,
    pub receipt_event_id: String,
    pub quarantine_file: String,
    pub lock: NativeGitLockSnapshot,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct QuarantineAuditData {
    schema_version: u32,
    proof_event_id: String,
    proof: ManagedNativeLockProof,
    quarantine_file: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct QuarantineReceiptData {
    schema_version: u32,
    proof_event_id: String,
    intent_event_id: String,
    proof: ManagedNativeLockProof,
    quarantine_file: String,
    quarantined_lock: NativeGitLockSnapshot,
}

impl GitMutationSafety {
    /// Inspect a lock under its index lease; only exact durable process proof can authorize quarantine.
    pub fn reconcile_native_index_lock(
        &self,
        lease: &GitMutationLease,
        event_log: &EventLog,
        wait: Duration,
    ) -> NativeLockRecoveryVerdict {
        reconcile_with_inspector(self, lease, event_log, &ProcFsInspector::default(), wait)
    }
}

fn reconcile_with_inspector(
    safety: &GitMutationSafety,
    lease: &GitMutationLease,
    event_log: &EventLog,
    inspector: &dyn ProcessInspector,
    wait: Duration,
) -> NativeLockRecoveryVerdict {
    let paths = &lease.paths;
    if lease.resource != GitMutationResource::Index {
        return uncertain(
            &paths.index_lock_file,
            "native lock recovery requires the index lease",
        );
    }
    let deadline = Instant::now() + wait;
    loop {
        if let Err(error) = validate_current_paths(safety, lease) {
            return uncertain(&paths.index_lock_file, &error.to_string());
        }
        let verdict = reconcile_once(safety, lease, event_log, inspector);
        match verdict {
            NativeLockRecoveryVerdict::Busy { .. } if Instant::now() < deadline => {
                thread::sleep(
                    QUARANTINE_POLL.min(deadline.saturating_duration_since(Instant::now())),
                );
            }
            other => return other,
        }
    }
}

fn reconcile_once(
    safety: &GitMutationSafety,
    lease: &GitMutationLease,
    event_log: &EventLog,
    inspector: &dyn ProcessInspector,
) -> NativeLockRecoveryVerdict {
    let paths = &lease.paths;
    match fs::symlink_metadata(&paths.index_lock_file) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            return uncertain(&paths.index_lock_file, "native index lock is a symlink");
        }
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return reconcile_absent_lock(paths, event_log, inspector);
        }
        Err(error) => {
            return uncertain(
                &paths.index_lock_file,
                &format!("cannot inspect native index lock ({})", error.kind()),
            );
        }
    }

    let snapshot = match super::git_lock_owner::snapshot_lock(&paths.index_lock_file) {
        Ok(snapshot) => snapshot,
        Err(error) => return uncertain(&paths.index_lock_file, &error.to_string()),
    };
    let holders = match inspector.process_holders(snapshot.device, snapshot.inode) {
        Ok(holders) => holders,
        Err(reason) => return uncertain(&paths.index_lock_file, &reason),
    };
    if !holders.is_empty() {
        return NativeLockRecoveryVerdict::Busy {
            path: paths.index_lock_file.clone(),
            owners: holders,
            reason: "a process currently holds the native index lock inode".to_string(),
        };
    }

    let proof = match matching_proof(event_log, paths, &snapshot) {
        Ok(Some(proof)) => proof,
        Ok(None) => {
            return uncertain(
                &paths.index_lock_file,
                "no durable managed-process proof matches this lock inode and Git path binding",
            );
        }
        Err(reason) => return uncertain(&paths.index_lock_file, &reason),
    };
    match inspector.start_time_ticks(proof.proof.owner.pid) {
        Ok(Some(current)) if current == proof.proof.owner.start_time_ticks => {
            return NativeLockRecoveryVerdict::Busy {
                path: paths.index_lock_file.clone(),
                owners: vec![proof.proof.owner],
                reason: "the recorded managed Git process is still alive".to_string(),
            };
        }
        Ok(_) => {}
        Err(reason) => return uncertain(&paths.index_lock_file, &reason),
    }
    if snapshot.size != 0 {
        return uncertain(
            &paths.index_lock_file,
            "managed lock evidence is nonempty; automatic quarantine preserves its contents",
        );
    }

    quarantine_proven_lock(safety, lease, event_log, inspector, proof)
}

fn validate_current_paths(
    safety: &GitMutationSafety,
    lease: &GitMutationLease,
) -> Result<(), GitMutationError> {
    let current = safety.resolve_paths_with_index_override(
        &lease.paths.worktree_root,
        lease.index_override.as_deref().map(Path::as_os_str),
    )?;
    if current != lease.paths {
        return Err(GitMutationError::Uncertain {
            path: lease.paths.worktree_root.clone(),
            reason: "Git metadata paths changed after mutation lease acquisition".to_string(),
        });
    }
    Ok(())
}

fn matching_proof(
    event_log: &EventLog,
    paths: &super::git_mutation::GitMutationPaths,
    snapshot: &NativeGitLockSnapshot,
) -> Result<Option<LocatedProof>, String> {
    let records = read_git_recovery_events(event_log, &[LOCK_HELD_EVENT], Duration::from_secs(2))?;
    let mut matches = Vec::new();
    for record in records {
        let proof_version = record
            .data
            .get("schema_version")
            .and_then(serde_json::Value::as_u64)
            .ok_or_else(|| "native lock ownership event has no supported schema".to_string())?;
        if proof_version != u64::from(PROOF_SCHEMA_VERSION) {
            if record
                .data
                .get("index_lock_file")
                .and_then(serde_json::Value::as_str)
                == Some(paths.index_lock_file.to_string_lossy().as_ref())
            {
                return Err(
                    "native lock ownership event uses an unsupported proof version".to_string(),
                );
            }
            continue;
        }
        let proof: ManagedNativeLockProof = serde_json::from_value(record.data)
            .map_err(|_| "native lock ownership event is malformed".to_string())?;
        if proof_matches_paths(&proof, paths) && proof.lock == *snapshot {
            matches.push(LocatedProof {
                event_id: record.event_id,
                proof,
            });
        }
    }
    if matches.len() > 1 {
        return Err("multiple durable owners claim the same native lock inode".to_string());
    }
    Ok(matches.pop())
}

fn proof_matches_paths(
    proof: &ManagedNativeLockProof,
    paths: &super::git_mutation::GitMutationPaths,
) -> bool {
    proof.worktree_root == paths.worktree_root.to_string_lossy()
        && proof.worktree_root_sha256 == digest_git_path(&paths.worktree_root)
        && proof.git_dir == paths.git_dir.to_string_lossy()
        && proof.git_dir_sha256 == digest_git_path(&paths.git_dir)
        && proof.common_git_dir == paths.common_git_dir.to_string_lossy()
        && proof.common_git_dir_sha256 == digest_git_path(&paths.common_git_dir)
        && proof.index_file == paths.index_file.to_string_lossy()
        && proof.index_file_sha256 == digest_git_path(&paths.index_file)
        && proof.index_lock_file == paths.index_lock_file.to_string_lossy()
        && proof.index_lock_file_sha256 == digest_git_path(&paths.index_lock_file)
        && proof.index_lease_file_sha256 == digest_git_path(&paths.index_lease_file)
}

#[derive(Debug, Clone)]
struct LocatedProof {
    event_id: String,
    proof: ManagedNativeLockProof,
}

fn reconcile_absent_lock(
    paths: &super::git_mutation::GitMutationPaths,
    event_log: &EventLog,
    inspector: &dyn ProcessInspector,
) -> NativeLockRecoveryVerdict {
    let records = match read_git_recovery_events(
        event_log,
        &[QUARANTINED_EVENT, QUARANTINE_INTENT_EVENT],
        Duration::from_secs(2),
    ) {
        Ok(records) => records,
        Err(error) => {
            return uncertain(
                &paths.index_lock_file,
                &format!("cannot inspect prior lock recovery events: {error:?}"),
            )
        }
    };
    for record in records.iter().rev() {
        if record.event_type == QUARANTINED_EVENT {
            let audit: QuarantineReceiptData = match serde_json::from_value(record.data.clone()) {
                Ok(audit) => audit,
                Err(_) => {
                    return uncertain(&paths.index_lock_file, "quarantine receipt is malformed")
                }
            };
            if !proof_matches_paths(&audit.proof, paths) {
                continue;
            }
            return verify_quarantine_receipt(
                paths,
                event_log,
                inspector,
                record.event_id.clone(),
                audit,
            );
        }
    }
    for record in records.iter().rev() {
        if record.event_type == QUARANTINE_INTENT_EVENT {
            let audit: QuarantineAuditData = match serde_json::from_value(record.data.clone()) {
                Ok(audit) => audit,
                Err(_) => {
                    return uncertain(&paths.index_lock_file, "quarantine intent is malformed")
                }
            };
            if !proof_matches_paths(&audit.proof, paths) {
                continue;
            }
            return complete_interrupted_quarantine(
                paths,
                event_log,
                inspector,
                record.event_id.clone(),
                audit,
            );
        }
    }
    NativeLockRecoveryVerdict::Clear
}

fn verify_quarantine_receipt(
    paths: &super::git_mutation::GitMutationPaths,
    event_log: &EventLog,
    inspector: &dyn ProcessInspector,
    event_id: String,
    audit: QuarantineReceiptData,
) -> NativeLockRecoveryVerdict {
    let expected = quarantine_path(paths, &audit.proof_event_id);
    if audit.quarantine_file != expected.to_string_lossy() {
        return uncertain(
            &paths.index_lock_file,
            "quarantine receipt names an unexpected target path",
        );
    }
    if let Err(verdict) = verify_proof_event(
        event_log,
        &audit.proof_event_id,
        &audit.proof,
        &paths.index_lock_file,
    ) {
        return *verdict;
    }
    let intent = QuarantineAuditData {
        schema_version: RECOVERY_SCHEMA_VERSION,
        proof_event_id: audit.proof_event_id.clone(),
        proof: audit.proof.clone(),
        quarantine_file: audit.quarantine_file.clone(),
    };
    let intent_value = match serde_json::to_value(intent) {
        Ok(value) => value,
        Err(_) => return uncertain(&paths.index_lock_file, "cannot validate quarantine intent"),
    };
    let intent_event_id = match find_audit_event(
        event_log,
        QUARANTINE_INTENT_EVENT,
        &intent_value,
        &paths.index_lock_file,
    ) {
        Ok(Some(event_id)) => event_id,
        Ok(None) => {
            return uncertain(
                &paths.index_lock_file,
                "quarantine receipt has no matching durable intent",
            )
        }
        Err(reason) => return uncertain(&paths.index_lock_file, &reason),
    };
    if intent_event_id != audit.intent_event_id {
        return uncertain(
            &paths.index_lock_file,
            "quarantine receipt has no matching durable intent",
        );
    }
    match super::git_lock_owner::snapshot_lock(&expected) {
        Ok(snapshot)
            if snapshot == audit.quarantined_lock
                && same_quarantined_inode(&audit.proof.lock, &snapshot) =>
        {
            if let Err(verdict) =
                verify_quarantined_owner(inspector, &audit.proof, &snapshot, &expected)
            {
                return *verdict;
            }
            NativeLockRecoveryVerdict::Recovered(recovery_result(event_id, audit))
        }
        Ok(_) => uncertain(
            &expected,
            "quarantined lock differs from its retained receipt",
        ),
        Err(error) => uncertain(&expected, &error.to_string()),
    }
}

fn complete_interrupted_quarantine(
    paths: &super::git_mutation::GitMutationPaths,
    event_log: &EventLog,
    inspector: &dyn ProcessInspector,
    intent_event_id: String,
    audit: QuarantineAuditData,
) -> NativeLockRecoveryVerdict {
    let expected = quarantine_path(paths, &audit.proof_event_id);
    if audit.quarantine_file != expected.to_string_lossy() {
        return uncertain(
            &paths.index_lock_file,
            "quarantine intent names an unexpected target path",
        );
    }
    if let Err(verdict) = verify_proof_event(
        event_log,
        &audit.proof_event_id,
        &audit.proof,
        &paths.index_lock_file,
    ) {
        return *verdict;
    }
    let quarantined_lock = match super::git_lock_owner::snapshot_lock(&expected) {
        Ok(snapshot) if same_quarantined_inode(&audit.proof.lock, &snapshot) => snapshot,
        Ok(_) => {
            return uncertain(
                &expected,
                "interrupted quarantine target differs from its proof",
            )
        }
        Err(error) => return uncertain(&expected, &error.to_string()),
    };
    if let Err(verdict) =
        verify_quarantined_owner(inspector, &audit.proof, &quarantined_lock, &expected)
    {
        return *verdict;
    }
    let receipt = QuarantineReceiptData {
        schema_version: RECOVERY_SCHEMA_VERSION,
        proof_event_id: audit.proof_event_id,
        intent_event_id,
        proof: audit.proof,
        quarantine_file: audit.quarantine_file,
        quarantined_lock,
    };
    let event_id = match append_audit_once(
        event_log,
        QUARANTINED_EVENT,
        &receipt,
        &paths.index_lock_file,
    ) {
        Ok(event_id) => event_id,
        Err(reason) => return uncertain(&paths.index_lock_file, &reason),
    };
    NativeLockRecoveryVerdict::Recovered(recovery_result(event_id, receipt))
}

fn quarantine_proven_lock(
    safety: &GitMutationSafety,
    lease: &GitMutationLease,
    event_log: &EventLog,
    inspector: &dyn ProcessInspector,
    located: LocatedProof,
) -> NativeLockRecoveryVerdict {
    let paths = &lease.paths;
    let quarantine_dir = paths
        .common_git_dir
        .join("exomonad-mutation")
        .join(QUARANTINE_DIRECTORY);
    if let Err(error) = ensure_safe_lease_directory(&quarantine_dir) {
        return uncertain(&quarantine_dir, &error.to_string());
    }
    let quarantine_file = quarantine_path(paths, &located.event_id);
    if let Err(reason) = check_quarantine_capacity(&quarantine_dir, &quarantine_file) {
        return uncertain(&quarantine_dir, &reason);
    }
    let audit = QuarantineAuditData {
        schema_version: RECOVERY_SCHEMA_VERSION,
        proof_event_id: located.event_id,
        proof: located.proof,
        quarantine_file: quarantine_file.to_string_lossy().into_owned(),
    };
    let intent_event_id = match append_audit_once(
        event_log,
        QUARANTINE_INTENT_EVENT,
        &audit,
        &paths.index_lock_file,
    ) {
        Ok(event_id) => event_id,
        Err(reason) => return uncertain(&paths.index_lock_file, &reason),
    };
    if let Err(error) =
        revalidate_before_quarantine(safety, lease, event_log, inspector, &audit.proof)
    {
        return uncertain(&paths.index_lock_file, &error);
    }
    match atomic_move_lock(&paths.index_lock_file, &quarantine_file, &audit.proof.lock) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
            return uncertain(
                &quarantine_file,
                "quarantine target already exists while source lock remains",
            );
        }
        Err(error) => {
            return uncertain(
                &paths.index_lock_file,
                &format!(
                    "cannot atomically quarantine proven native lock ({})",
                    error.kind()
                ),
            );
        }
    }
    match fs::symlink_metadata(&paths.index_lock_file) {
        Ok(_) => {
            return uncertain(
                &paths.index_lock_file,
                "a new native index lock appeared immediately after quarantine; retry init after its owner exits",
            );
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => {
            return uncertain(
                &paths.index_lock_file,
                &format!(
                    "cannot recheck native lock after quarantine ({})",
                    error.kind()
                ),
            );
        }
    }
    let quarantined_lock = match super::git_lock_owner::snapshot_lock(&quarantine_file) {
        Ok(snapshot) if same_quarantined_inode(&audit.proof.lock, &snapshot) => snapshot,
        Ok(_) => {
            return uncertain(
                &quarantine_file,
                "quarantined lock changed during atomic move",
            )
        }
        Err(error) => return uncertain(&quarantine_file, &error.to_string()),
    };
    if let Err(verdict) =
        verify_quarantined_owner(inspector, &audit.proof, &quarantined_lock, &quarantine_file)
    {
        return *verdict;
    }
    let receipt = QuarantineReceiptData {
        schema_version: RECOVERY_SCHEMA_VERSION,
        proof_event_id: audit.proof_event_id,
        intent_event_id,
        proof: audit.proof,
        quarantine_file: audit.quarantine_file,
        quarantined_lock,
    };
    let receipt_event_id = match append_audit_once(
        event_log,
        QUARANTINED_EVENT,
        &receipt,
        &paths.index_lock_file,
    ) {
        Ok(event_id) => event_id,
        Err(reason) => return uncertain(&paths.index_lock_file, &reason),
    };
    NativeLockRecoveryVerdict::Recovered(recovery_result(receipt_event_id, receipt))
}

fn verify_proof_event(
    event_log: &EventLog,
    proof_event_id: &str,
    expected: &ManagedNativeLockProof,
    path: &Path,
) -> Result<(), Box<NativeLockRecoveryVerdict>> {
    let records = read_git_recovery_events(event_log, &[LOCK_HELD_EVENT], Duration::from_secs(2))
        .map_err(|_| {
        Box::new(uncertain(
            path,
            "cannot verify the ownership proof linked by lock recovery",
        ))
    })?;
    let matching = records
        .into_iter()
        .filter(|record| record.event_id == proof_event_id)
        .collect::<Vec<_>>();
    if matching.len() != 1 {
        return Err(Box::new(uncertain(
            path,
            "lock recovery references a missing or ambiguous ownership proof event",
        )));
    }
    let proof: ManagedNativeLockProof =
        serde_json::from_value(matching[0].data.clone()).map_err(|_| {
            Box::new(uncertain(
                path,
                "referenced native lock ownership proof is malformed",
            ))
        })?;
    if proof != *expected {
        return Err(Box::new(uncertain(
            path,
            "referenced native lock ownership proof differs from recovery evidence",
        )));
    }
    Ok(())
}

fn verify_quarantined_owner(
    inspector: &dyn ProcessInspector,
    proof: &ManagedNativeLockProof,
    quarantined: &NativeGitLockSnapshot,
    path: &Path,
) -> Result<(), Box<NativeLockRecoveryVerdict>> {
    let holders = inspector
        .process_holders(quarantined.device, quarantined.inode)
        .map_err(|_| {
            Box::new(uncertain(
                path,
                "cannot verify holders of the quarantined lock inode",
            ))
        })?;
    if !holders.is_empty() {
        return Err(Box::new(NativeLockRecoveryVerdict::Busy {
            path: path.to_path_buf(),
            owners: holders,
            reason: "a process still holds the quarantined native index lock inode".to_string(),
        }));
    }
    match inspector.start_time_ticks(proof.owner.pid) {
        Ok(Some(current)) if current == proof.owner.start_time_ticks => {
            Err(Box::new(NativeLockRecoveryVerdict::Busy {
                path: path.to_path_buf(),
                owners: vec![proof.owner.clone()],
                reason: "the recorded managed Git process is still alive".to_string(),
            }))
        }
        Ok(_) => Ok(()),
        Err(_) => Err(Box::new(uncertain(
            path,
            "cannot revalidate the recorded managed Git process identity",
        ))),
    }
}

fn revalidate_before_quarantine(
    safety: &GitMutationSafety,
    lease: &GitMutationLease,
    event_log: &EventLog,
    inspector: &dyn ProcessInspector,
    proof: &ManagedNativeLockProof,
) -> Result<(), String> {
    let paths = &lease.paths;
    validate_current_paths(safety, lease)
        .map_err(|_| "Git worktree or active index changed before lock quarantine".to_string())?;
    let current = super::git_lock_owner::snapshot_lock(&paths.index_lock_file)
        .map_err(|_| "native lock disappeared or changed before quarantine".to_string())?;
    if current != proof.lock || current.size != 0 {
        return Err("native lock no longer matches the empty proven inode".to_string());
    }
    let holders = inspector
        .process_holders(current.device, current.inode)
        .map_err(|_| "process visibility became incomplete before quarantine".to_string())?;
    if !holders.is_empty() {
        return Err("a process began holding the native lock before quarantine".to_string());
    }
    match inspector.start_time_ticks(proof.owner.pid) {
        Ok(Some(start)) if start == proof.owner.start_time_ticks => {
            return Err("the original managed Git process is still active".to_string());
        }
        Ok(_) => {}
        Err(_) => return Err("the original process identity cannot be revalidated".to_string()),
    }
    let matches = matching_proof(event_log, paths, &current)
        .map_err(|reason| format!("cannot revalidate ownership evidence ({reason})"))?;
    if matches
        .as_ref()
        .is_none_or(|candidate| candidate.proof != *proof)
    {
        return Err("durable lock ownership evidence changed before quarantine".to_string());
    }
    Ok(())
}

fn check_quarantine_capacity(directory: &Path, target: &Path) -> Result<(), String> {
    if target.exists() {
        return Err("quarantine target already exists".to_string());
    }
    let count = fs::read_dir(directory)
        .map_err(|error| format!("cannot inspect quarantine capacity ({})", error.kind()))?
        .count();
    if count >= QUARANTINE_CAPACITY {
        return Err("native lock quarantine reached its bounded 64-entry capacity".to_string());
    }
    Ok(())
}

fn quarantine_path(paths: &super::git_mutation::GitMutationPaths, proof_event_id: &str) -> PathBuf {
    let proof_digest = digest(proof_event_id.as_bytes());
    paths
        .common_git_dir
        .join("exomonad-mutation")
        .join(QUARANTINE_DIRECTORY)
        .join(format!("{proof_digest}.lock"))
}

fn atomic_move_lock(
    source: &Path,
    target: &Path,
    expected: &NativeGitLockSnapshot,
) -> io::Result<()> {
    #[cfg(not(target_os = "linux"))]
    {
        let _ = (source, target, expected);
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "atomic no-replace lock quarantine is unavailable on this platform",
        ));
    }
    #[cfg(target_os = "linux")]
    {
        let source_parent = source
            .parent()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "lock has no parent"))?;
        let target_parent = target.parent().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "quarantine has no parent")
        })?;
        let source_name = source
            .file_name()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "lock has no name"))?;
        let target_name = target
            .file_name()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "quarantine has no name"))?;
        let source_dir = open_directory_nofollow(source_parent)?;
        let target_dir = open_directory_nofollow(target_parent)?;
        let current = super::git_lock_owner::snapshot_lock(source)
            .map_err(|_| io::Error::other("source lock changed before rename"))?;
        if current != *expected {
            return Err(io::Error::other(
                "source lock evidence changed before rename",
            ));
        }
        renameat2(
            Some(source_dir.as_raw_fd()),
            source_name,
            Some(target_dir.as_raw_fd()),
            target_name,
            RenameFlags::RENAME_NOREPLACE,
        )
        .map_err(|error| io::Error::from_raw_os_error(error as i32))?;
        source_dir.sync_all()?;
        target_dir.sync_all()?;
        Ok(())
    }
}

#[cfg(target_os = "linux")]
fn open_directory_nofollow(path: &Path) -> io::Result<File> {
    if fs::canonicalize(path)? != path {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "Git directory path is not canonical",
        ));
    }
    let before = fs::symlink_metadata(path)?;
    if before.file_type().is_symlink() || !before.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "Git directory changed or is a symlink",
        ));
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)?;
    let opened = file.metadata()?;
    let current = fs::symlink_metadata(path)?;
    if fs::canonicalize(path)? != path
        || before.dev() != opened.dev()
        || before.ino() != opened.ino()
        || before.dev() != current.dev()
        || before.ino() != current.ino()
    {
        return Err(io::Error::other("Git directory changed during inspection"));
    }
    Ok(file)
}

fn append_audit_once<T: Serialize>(
    event_log: &EventLog,
    event_type: &str,
    data: &T,
    path: &Path,
) -> Result<String, String> {
    let value =
        serde_json::to_value(data).map_err(|_| "cannot encode lock recovery audit".to_string())?;
    if let Some(event_id) = find_audit_event(event_log, event_type, &value, path)? {
        return Ok(event_id);
    }
    match event_log.append(event_type, "git-mutation", &value) {
        Ok(event_id) => Ok(event_id),
        Err(_) => find_audit_event(event_log, event_type, &value, path)?.ok_or_else(|| {
            "lock recovery audit append failed and no durable event can be recovered".to_string()
        }),
    }
}

fn find_audit_event(
    event_log: &EventLog,
    event_type: &str,
    data: &serde_json::Value,
    path: &Path,
) -> Result<Option<String>, String> {
    let records = read_git_recovery_events(event_log, &[event_type], Duration::from_secs(2))
        .map_err(|_| format!("cannot inspect lock recovery audit at {:?}", path))?;
    Ok(records.into_iter().find_map(|record| {
        (record.event_type == event_type && record.data == *data).then_some(record.event_id)
    }))
}

fn recovery_result(
    receipt_event_id: String,
    audit: QuarantineReceiptData,
) -> GitLockRecoveryReceipt {
    GitLockRecoveryReceipt {
        proof_event_id: audit.proof_event_id,
        intent_event_id: audit.intent_event_id,
        receipt_event_id,
        quarantine_file: audit.quarantine_file,
        lock: audit.proof.lock,
    }
}

fn same_quarantined_inode(
    source: &NativeGitLockSnapshot,
    quarantined: &NativeGitLockSnapshot,
) -> bool {
    source.device == quarantined.device
        && source.inode == quarantined.inode
        && source.size == quarantined.size
        && source.modified_seconds == quarantined.modified_seconds
        && source.modified_nanoseconds == quarantined.modified_nanoseconds
        && source.contents_sha256 == quarantined.contents_sha256
}

fn digest(value: &[u8]) -> String {
    format!("{:x}", Sha256::digest(value))
}

fn uncertain(path: &Path, reason: &str) -> NativeLockRecoveryVerdict {
    NativeLockRecoveryVerdict::Uncertain {
        path: path.to_path_buf(),
        reason: reason.to_string(),
    }
}

#[cfg(test)]
#[path = "git_lock_recovery_tests.rs"]
mod tests;
