use super::{
    apply_legacy_git_lock, inspect_legacy_git_lock, inspect_legacy_git_lock_with,
    LegacyManagedActivity, LegacyRecoveryData, LEGACY_INTENT_EVENT, LEGACY_RECOVERED_EVENT,
};
use crate::services::event_log::EventLog;
use crate::services::git_lock_owner::{ManagedProcessIdentity, ProcessInspector};
use crate::services::git_lock_recovery::atomic_move_lock;
use crate::services::git_mutation::{GitMutationResource, GitMutationSafety};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;
use tempfile::TempDir;

struct Fixture {
    _temp: TempDir,
    root: PathBuf,
    safety: GitMutationSafety,
    event_log: EventLog,
}

impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().expect("temporary workspace");
        let root = temp.path().join("repo");
        fs::create_dir(&root).expect("repository root");
        git(&root, &["init", "-q", "-b", "main"]);
        git(&root, &["config", "user.name", "legacy recovery test"]);
        git(
            &root,
            &["config", "user.email", "legacy-recovery@example.invalid"],
        );
        fs::write(root.join("tracked.txt"), "base\n").expect("tracked file");
        git(&root, &["add", "tracked.txt"]);
        git(&root, &["commit", "-q", "-m", "fixture"]);
        let event_log =
            EventLog::open(temp.path().join("project/.exo/logs")).expect("project event log");
        Self {
            _temp: temp,
            safety: GitMutationSafety::new(root.clone()),
            root,
            event_log,
        }
    }

    fn lock_path(&self) -> PathBuf {
        self.root.join(".git/index.lock")
    }

    fn segments(&self) -> &Path {
        self.event_log.ledger().segments_dir()
    }

    fn inspect(&self) -> super::LegacyGitLockInspection {
        inspect_legacy_git_lock(&self.safety, &self.root, self.segments())
            .expect("legacy lock inspection")
    }
}

fn git(directory: &Path, args: &[&str]) {
    let output = std::process::Command::new("git")
        .args(args)
        .current_dir(directory)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .output()
        .expect("launch Git fixture command");
    assert!(output.status.success(), "git {args:?} failed");
}

#[test]
fn dry_run_is_read_only_and_fingerprints_paths_index_and_nonempty_lock() {
    let fixture = Fixture::new();
    fs::write(fixture.root.join("tracked.txt"), "staged edit\n").unwrap();
    git(&fixture.root, &["add", "tracked.txt"]);
    fs::write(fixture.root.join("tracked.txt"), "unstaged edit\n").unwrap();
    fs::write(fixture.root.join("untracked.txt"), "preserve\n").unwrap();
    let index = fixture.root.join(".git/index");
    let index_before = fs::read(&index).unwrap();
    let status_before = git_output(&fixture.root, &["status", "--porcelain"]);
    fs::write(fixture.lock_path(), b"legacy bytes are retained exactly").unwrap();
    let lock_before = fs::read(fixture.lock_path()).unwrap();
    let events_before = fixture.event_log.ledger().read_events().unwrap().len();

    let report = fixture.inspect();

    assert_eq!(report.worktree_root, fixture.root.canonicalize().unwrap());
    assert_eq!(report.index_lock_file, fixture.lock_path());
    assert_eq!(report.lock.as_ref().unwrap().size, lock_before.len() as u64);
    assert!(report.recovery_fingerprint.as_ref().unwrap().len() == 64);
    assert!(!report.matching_managed_proof);
    assert!(matches!(
        report.managed_activity,
        LegacyManagedActivity::Clear { .. }
    ));
    assert!(report
        .operator_scope
        .contains("host-wide external-process census is not performed"));
    assert_eq!(fs::read(index).unwrap(), index_before);
    assert_eq!(fs::read(fixture.lock_path()).unwrap(), lock_before);
    assert_eq!(
        git_output(&fixture.root, &["status", "--porcelain"]),
        status_before
    );
    assert_eq!(
        fixture.event_log.ledger().read_events().unwrap().len(),
        events_before
    );
}

#[test]
fn apply_requires_explicit_authorization_and_fresh_fingerprint() {
    let fixture = Fixture::new();
    fs::write(fixture.lock_path(), b"").unwrap();
    let report = fixture.inspect();
    let fingerprint = report.recovery_fingerprint.unwrap();
    assert!(apply_legacy_git_lock(
        &fixture.safety,
        &fixture.root,
        &fixture.event_log,
        &fingerprint,
        false,
        Duration::ZERO,
    )
    .unwrap_err()
    .contains("--writers-stopped"));
    assert!(fixture.lock_path().exists());

    fs::write(fixture.lock_path(), b"changed lock evidence").unwrap();
    assert!(apply_legacy_git_lock(
        &fixture.safety,
        &fixture.root,
        &fixture.event_log,
        &fingerprint,
        true,
        Duration::ZERO,
    )
    .unwrap_err()
    .contains("fingerprint is stale"));
    assert_eq!(
        fs::read(fixture.lock_path()).unwrap(),
        b"changed lock evidence"
    );
    assert_eq!(fixture.event_log.ledger().read_events().unwrap().len(), 0);
}

#[test]
fn apply_refuses_a_cooperating_active_writer_and_quarantine_failure() {
    let fixture = Fixture::new();
    fs::write(fixture.lock_path(), b"preserve on refusal").unwrap();
    let fingerprint = fixture.inspect().recovery_fingerprint.unwrap();
    let writer = fixture
        .safety
        .acquire(
            &fixture.root,
            GitMutationResource::Index,
            "active-writer",
            Duration::ZERO,
        )
        .unwrap();
    let error = apply_legacy_git_lock(
        &fixture.safety,
        &fixture.root,
        &fixture.event_log,
        &fingerprint,
        true,
        Duration::ZERO,
    )
    .unwrap_err();
    assert!(error.contains("remained busy"));
    assert_eq!(
        fs::read(fixture.lock_path()).unwrap(),
        b"preserve on refusal"
    );
    drop(writer);

    let second = Fixture::new();
    fs::write(second.lock_path(), b"preserve failed quarantine").unwrap();
    let fingerprint = second.inspect().recovery_fingerprint.unwrap();
    let quarantine = second.root.join(".git/exomonad-mutation/quarantine");
    fs::create_dir_all(quarantine.parent().unwrap()).unwrap();
    fs::write(&quarantine, b"do not replace").unwrap();
    let error = apply_legacy_git_lock(
        &second.safety,
        &second.root,
        &second.event_log,
        &fingerprint,
        true,
        Duration::ZERO,
    )
    .unwrap_err();
    assert!(error.contains("not a directory"));
    assert_eq!(
        fs::read(second.lock_path()).unwrap(),
        b"preserve failed quarantine"
    );
    assert_eq!(second.event_log.ledger().read_events().unwrap().len(), 0);
}

#[cfg(unix)]
#[test]
fn inspection_and_apply_refuse_a_lock_path_symlink() {
    let fixture = Fixture::new();
    let outside = fixture._temp.path().join("outside.lock");
    fs::write(&outside, b"foreign").unwrap();
    std::os::unix::fs::symlink(&outside, fixture.lock_path()).unwrap();
    let error =
        inspect_legacy_git_lock(&fixture.safety, &fixture.root, fixture.segments()).unwrap_err();
    assert!(error.contains("symlink"));
    assert_eq!(fs::read(outside).unwrap(), b"foreign");
}

#[test]
fn authorized_apply_quarantines_once_preserves_edits_and_never_removes_replacement() {
    let fixture = Fixture::new();
    fs::write(fixture.root.join("tracked.txt"), "staged edit\n").unwrap();
    git(&fixture.root, &["add", "tracked.txt"]);
    fs::write(fixture.root.join("tracked.txt"), "unstaged edit\n").unwrap();
    fs::write(fixture.root.join("untracked.txt"), "untracked edit\n").unwrap();
    let index = fixture.root.join(".git/index");
    let index_before = fs::read(&index).unwrap();
    let status_before = git_output(&fixture.root, &["status", "--porcelain"]);
    let old_lock = b"retained legacy lock evidence";
    fs::write(fixture.lock_path(), old_lock).unwrap();
    let report = fixture.inspect();
    let fingerprint = report.recovery_fingerprint.clone().unwrap();

    let result = apply_legacy_git_lock(
        &fixture.safety,
        &fixture.root,
        &fixture.event_log,
        &fingerprint,
        true,
        Duration::ZERO,
    )
    .expect("authorized exact-fingerprint apply");
    assert!(!result.already_applied);
    assert!(!fixture.lock_path().exists());
    assert_eq!(fs::read(&result.receipt.quarantine_file).unwrap(), old_lock);
    assert_eq!(fs::read(index).unwrap(), index_before);
    assert_eq!(
        git_output(&fixture.root, &["status", "--porcelain"]),
        status_before
    );
    let history = fixture.event_log.ledger().read_events().unwrap();
    assert_eq!(
        history
            .iter()
            .filter(|record| record.event.event_type == LEGACY_INTENT_EVENT)
            .count(),
        1
    );
    assert_eq!(
        history
            .iter()
            .filter(|record| record.event.event_type == LEGACY_RECOVERED_EVENT)
            .count(),
        1
    );
    assert!(!history
        .iter()
        .any(|record| record.event.event_type == "git.native_lock.held"));

    fs::write(fixture.lock_path(), b"replacement lock must remain").unwrap();
    let replay = apply_legacy_git_lock(
        &fixture.safety,
        &fixture.root,
        &fixture.event_log,
        &fingerprint,
        true,
        Duration::ZERO,
    )
    .expect("repeat apply adopts exact prior receipt");
    assert!(replay.already_applied);
    assert_eq!(
        fs::read(fixture.lock_path()).unwrap(),
        b"replacement lock must remain"
    );
    assert_eq!(
        replay.receipt.receipt_event_id,
        result.receipt.receipt_event_id
    );
    let history = fixture.event_log.ledger().read_events().unwrap();
    assert_eq!(
        history
            .iter()
            .filter(|record| record.event.event_type == LEGACY_INTENT_EVENT)
            .count(),
        1
    );
    assert_eq!(
        history
            .iter()
            .filter(|record| record.event.event_type == LEGACY_RECOVERED_EVENT)
            .count(),
        1
    );
}

#[test]
fn apply_recovers_crash_after_quarantine_before_receipt_under_the_same_lease() {
    let fixture = Fixture::new();
    fs::write(fixture.lock_path(), b"interrupted exact lock").unwrap();
    let report = fixture.inspect();
    let fingerprint = report.recovery_fingerprint.clone().unwrap();
    let target = report.quarantine_file.clone().unwrap();
    let lock = report.lock.clone().unwrap();
    let data = LegacyRecoveryData {
        schema_version: super::LEGACY_SCHEMA_VERSION,
        recovery_fingerprint: fingerprint.clone(),
        writer_stopped_authorization: true,
        actor: super::current_actor().unwrap(),
        worktree_root: report.worktree_root,
        git_dir: report.git_dir,
        common_git_dir: report.common_git_dir,
        index_file: report.index_file,
        index_lock_file: report.index_lock_file.clone(),
        index: report.index,
        original_lock: lock.clone(),
        quarantine_file: target.clone(),
    };
    fs::create_dir_all(target.parent().unwrap()).unwrap();
    let intent_id = fixture
        .event_log
        .append(
            LEGACY_INTENT_EVENT,
            "git-mutation",
            &serde_json::to_value(&data).unwrap(),
        )
        .unwrap();
    atomic_move_lock(&report.index_lock_file, &target, &lock).unwrap();

    let replay = apply_legacy_git_lock(
        &fixture.safety,
        &fixture.root,
        &fixture.event_log,
        &fingerprint,
        true,
        Duration::ZERO,
    )
    .expect("restart completes the receipt");
    assert!(replay.already_applied);
    assert_eq!(replay.receipt.intent_event_id, intent_id);
    assert!(!fixture.lock_path().exists());
    assert_eq!(fs::read(target).unwrap(), b"interrupted exact lock");
    assert_eq!(
        fixture
            .event_log
            .ledger()
            .read_events()
            .unwrap()
            .iter()
            .filter(|record| record.event.event_type == LEGACY_RECOVERED_EVENT)
            .count(),
        1
    );
}

struct ActivityInspector {
    owner: ManagedProcessIdentity,
    inaccessible: bool,
    owner_alive: bool,
    group_active: bool,
}

impl ProcessInspector for ActivityInspector {
    fn start_time_ticks(&self, pid: u32) -> Result<Option<u64>, String> {
        if self.inaccessible {
            return Err("permission denied for relevant managed PID".into());
        }
        Ok((pid == self.owner.pid && self.owner_alive).then_some(self.owner.start_time_ticks))
    }

    fn holds_inode(&self, _pid: u32, _device: u64, _inode: u64) -> Result<bool, String> {
        Ok(false)
    }

    fn process_group_exists(&self, process_group_id: u32) -> Result<bool, String> {
        assert_eq!(process_group_id, self.owner.pid);
        Ok(self.group_active)
    }
}

#[test]
fn relevant_live_process_or_inaccessible_identity_blocks_legacy_route() {
    let fixture = Fixture::new();
    fs::write(fixture.lock_path(), b"").unwrap();
    let paths = fixture
        .safety
        .resolve_paths_with_index_override(&fixture.root, None)
        .unwrap();
    let owner = ManagedProcessIdentity {
        pid: 77231,
        start_time_ticks: 17,
    };
    fixture
        .event_log
        .append(
            "git.process.started",
            "git-process",
            &serde_json::json!({
                "schema_version": 1,
                "operation_id_sha256": "a".repeat(64),
                "worktree_root": paths.worktree_root,
                "git_dir": paths.git_dir,
                "common_git_dir": paths.common_git_dir,
                "index_file": paths.index_file,
                "index_lock_file": paths.index_lock_file,
                "process_id": owner.pid,
                "process_start_identity": owner.start_time_ticks,
                "process_group_id": owner.pid,
            }),
        )
        .unwrap();
    let active = inspect_legacy_git_lock_with(
        &fixture.safety,
        &fixture.root,
        fixture.segments(),
        &ActivityInspector {
            owner: owner.clone(),
            inaccessible: false,
            owner_alive: true,
            group_active: true,
        },
    )
    .unwrap();
    assert!(matches!(
        active.managed_activity,
        LegacyManagedActivity::Active { .. }
    ));
    let inaccessible = inspect_legacy_git_lock_with(
        &fixture.safety,
        &fixture.root,
        fixture.segments(),
        &ActivityInspector {
            owner,
            inaccessible: true,
            owner_alive: false,
            group_active: false,
        },
    )
    .unwrap();
    assert!(matches!(
        inaccessible.managed_activity,
        LegacyManagedActivity::Uncertain { .. }
    ));
}

#[test]
fn terminal_leader_record_does_not_hide_a_surviving_managed_helper() {
    let fixture = Fixture::new();
    fs::write(fixture.lock_path(), b"legacy lock").unwrap();
    let paths = fixture
        .safety
        .resolve_paths_with_index_override(&fixture.root, None)
        .unwrap();
    let owner = ManagedProcessIdentity {
        pid: 77232,
        start_time_ticks: 18,
    };
    let operation_id_sha256 = "b".repeat(64);
    let process_data = serde_json::json!({
        "schema_version": 1,
        "operation_id_sha256": operation_id_sha256,
        "worktree_root": paths.worktree_root,
        "git_dir": paths.git_dir,
        "common_git_dir": paths.common_git_dir,
        "index_file": paths.index_file,
        "index_lock_file": paths.index_lock_file,
        "process_id": owner.pid,
        "process_start_identity": owner.start_time_ticks,
        "process_group_id": owner.pid,
    });
    fixture
        .event_log
        .append("git.process.started", "git-process", &process_data)
        .unwrap();
    fixture
        .event_log
        .append("git.process.finished", "git-process", &process_data)
        .unwrap();

    let report = inspect_legacy_git_lock_with(
        &fixture.safety,
        &fixture.root,
        fixture.segments(),
        &ActivityInspector {
            owner,
            inaccessible: false,
            owner_alive: false,
            group_active: true,
        },
    )
    .unwrap();
    assert!(matches!(
        report.managed_activity,
        LegacyManagedActivity::Active { .. }
    ));
}

#[test]
fn inspection_without_lock_is_clear_and_does_not_create_git_or_ledger_files() {
    let fixture = Fixture::new();
    let before = fs::read_dir(fixture.root.join(".git")).unwrap().count();
    let report = fixture.inspect();
    assert!(report.lock.is_none());
    assert!(report.recovery_fingerprint.is_none());
    assert!(!fixture.root.join(".git/exomonad-mutation").exists());
    assert_eq!(
        fs::read_dir(fixture.root.join(".git")).unwrap().count(),
        before
    );
    assert!(fixture.event_log.ledger().read_events().unwrap().is_empty());
}

fn git_output(directory: &Path, args: &[&str]) -> Vec<u8> {
    let output = std::process::Command::new("git")
        .args(args)
        .current_dir(directory)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .output()
        .unwrap();
    assert!(output.status.success());
    output.stdout
}
