use super::{
    reconcile_with_inspector, NativeLockRecoveryVerdict, QuarantineAuditData, QUARANTINED_EVENT,
    QUARANTINE_INTENT_EVENT,
};
use crate::services::event_log::EventLog;
use crate::services::git_lock_owner::{
    capture_managed_process_identity, record_managed_native_lock_proof, ManagedNativeLockProof,
    ManagedProcessIdentity, ProcFsInspector, ProcessInspector,
};
use crate::services::git_mutation::{
    GitMutationError, GitMutationLease, GitMutationResource, GitMutationSafety,
};
use std::cell::Cell;
use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
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
        let temp = tempfile::tempdir().expect("temporary root");
        let root = temp.path().join("repo");
        fs::create_dir(&root).expect("repository root");
        git(&root, &["init", "-q", "-b", "main"]);
        git(&root, &["config", "user.name", "lock recovery test"]);
        git(
            &root,
            &["config", "user.email", "lock-recovery@example.invalid"],
        );
        fs::write(root.join("tracked.txt"), "base\n").expect("tracked file");
        git(&root, &["add", "tracked.txt"]);
        git(&root, &["commit", "-q", "-m", "fixture"]);
        let event_log = EventLog::open(temp.path().join("project/.exo/events")).expect("event log");
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

    fn empty_lock(&self) {
        drop(File::create(self.lock_path()).expect("native lock"));
    }

    fn proven_lock(&self, contents: &[u8]) -> (GitMutationLease, ManagedNativeLockProof, String) {
        let lock_path = self.lock_path();
        fs::write(&lock_path, contents).expect("native lock contents");
        let lease = self
            .safety
            .acquire(
                &self.root,
                GitMutationResource::Index,
                "synchronization-retry-7",
                Duration::ZERO,
            )
            .expect("index mutation lease");
        let mut child = managed_lock_holder(&lock_path);
        let owner = capture_managed_process_identity(&child).expect("child process identity");
        let recorded = record_until_proven(&lease, &mut child, &owner, &self.event_log);
        let _ = child.kill();
        child.wait().expect("reap managed lock holder");
        let recorded = recorded.expect("proof the managed child held the exact lock inode");
        (lease, recorded.proof, recorded.event_id)
    }

    fn lease(&self) -> GitMutationLease {
        self.safety
            .acquire(
                &self.root,
                GitMutationResource::Index,
                "lock-recovery-preflight",
                Duration::ZERO,
            )
            .expect("index mutation lease")
    }
}

fn git(directory: &Path, args: &[&str]) -> Output {
    let output = Command::new("git")
        .args(args)
        .current_dir(directory)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .output()
        .expect("launch Git fixture command");
    assert!(output.status.success(), "git {args:?} failed");
    output
}

fn managed_lock_holder(lock_path: &Path) -> Child {
    Command::new("sh")
        .args(["-c", "exec 9<\"$1\"; sleep 30", "git-lock-holder"])
        .arg(lock_path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn managed lock holder")
}

fn record_until_proven(
    lease: &GitMutationLease,
    child: &mut Child,
    owner: &ManagedProcessIdentity,
    event_log: &EventLog,
) -> Result<super::super::git_lock_owner::RecordedManagedNativeLockProof, GitMutationError> {
    let started = std::time::Instant::now();
    loop {
        match record_managed_native_lock_proof(lease, child, owner, event_log) {
            Ok(recorded) => return Ok(recorded),
            Err(GitMutationError::Uncertain { .. })
                if started.elapsed() < Duration::from_secs(2) =>
            {
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(error) => return Err(error),
        }
    }
}

#[derive(Clone, Copy)]
enum OwnerState {
    Gone,
    Alive,
    Reused,
}

struct TableInspector {
    owner: ManagedProcessIdentity,
    owner_state: OwnerState,
    holders: Vec<ManagedProcessIdentity>,
    inaccessible: bool,
    holder_calls: Cell<usize>,
    report_holders_for_calls: usize,
    mutation_on_call: Option<(usize, PathBuf, Vec<u8>)>,
}

impl TableInspector {
    fn gone(owner: &ManagedProcessIdentity) -> Self {
        Self {
            owner: owner.clone(),
            owner_state: OwnerState::Gone,
            holders: Vec::new(),
            inaccessible: false,
            holder_calls: Cell::new(0),
            report_holders_for_calls: usize::MAX,
            mutation_on_call: None,
        }
    }

    fn holders(owner: &ManagedProcessIdentity, holders: Vec<ManagedProcessIdentity>) -> Self {
        Self {
            holders,
            ..Self::gone(owner)
        }
    }
}

impl ProcessInspector for TableInspector {
    fn start_time_ticks(&self, pid: u32) -> Result<Option<u64>, String> {
        if pid != self.owner.pid {
            return Ok(None);
        }
        match self.owner_state {
            OwnerState::Gone => Ok(None),
            OwnerState::Alive => Ok(Some(self.owner.start_time_ticks)),
            OwnerState::Reused => Ok(Some(self.owner.start_time_ticks + 1)),
        }
    }

    fn holds_inode(&self, _pid: u32, _device: u64, _inode: u64) -> Result<bool, String> {
        Ok(false)
    }

    fn process_holders(
        &self,
        _device: u64,
        _inode: u64,
    ) -> Result<Vec<ManagedProcessIdentity>, String> {
        if self.inaccessible {
            return Err(
                "cannot inspect one or more process descriptor directories (permission denied)"
                    .to_string(),
            );
        }
        let call = self.holder_calls.get() + 1;
        self.holder_calls.set(call);
        if let Some((mutation_call, path, contents)) = &self.mutation_on_call {
            if call == *mutation_call {
                fs::write(path, contents).map_err(|error| error.kind().to_string())?;
            }
        }
        if call <= self.report_holders_for_calls {
            return Ok(self.holders.clone());
        }
        Ok(Vec::new())
    }
}

fn reconcile(
    fixture: &Fixture,
    lease: &GitMutationLease,
    inspector: &dyn ProcessInspector,
    wait: Duration,
) -> NativeLockRecoveryVerdict {
    reconcile_with_inspector(&fixture.safety, lease, &fixture.event_log, inspector, wait)
}

#[test]
fn missing_native_lock_is_clear_without_creating_recovery_state() {
    let fixture = Fixture::new();
    let lease = fixture.lease();
    let fake_owner = ManagedProcessIdentity {
        pid: 73,
        start_time_ticks: 9,
    };
    let inspector = TableInspector::gone(&fake_owner);
    assert_eq!(
        reconcile(&fixture, &lease, &inspector, Duration::ZERO),
        NativeLockRecoveryVerdict::Clear
    );
    assert!(!fixture
        .root
        .join(".git/exomonad-mutation/quarantine")
        .exists());
}

#[test]
fn public_readiness_api_refuses_unattributed_legacy_lock_without_mutation() {
    let fixture = Fixture::new();
    fixture.empty_lock();
    let lease = fixture.lease();
    let before = fs::read(fixture.lock_path()).unwrap();

    let result =
        fixture
            .safety
            .reconcile_native_index_lock(&lease, &fixture.event_log, Duration::ZERO);
    assert!(matches!(
        result,
        NativeLockRecoveryVerdict::Uncertain { .. }
    ));
    assert_eq!(fs::read(fixture.lock_path()).unwrap(), before);
}

#[test]
fn proven_empty_managed_orphan_is_quarantined_and_replay_adopts_one_receipt() {
    let fixture = Fixture::new();
    fs::write(fixture.root.join("tracked.txt"), "staged\n").expect("staged edit");
    git(&fixture.root, &["add", "tracked.txt"]);
    fs::write(fixture.root.join("tracked.txt"), "unstaged\n").expect("unstaged edit");
    fs::write(fixture.root.join("untracked.txt"), "untracked\n").expect("untracked edit");
    let status_before =
        String::from_utf8(git(&fixture.root, &["status", "--porcelain"]).stdout).unwrap();
    let index_before = fs::read(fixture.root.join(".git/index")).expect("index before");
    let (lease, proof, proof_event_id) = fixture.proven_lock(b"");
    let inspector = TableInspector::gone(&proof.owner);

    let result = reconcile(&fixture, &lease, &inspector, Duration::ZERO);
    let NativeLockRecoveryVerdict::Recovered(receipt) = result else {
        panic!("expected proven orphan recovery, got {result:?}");
    };
    assert_eq!(receipt.proof_event_id, proof_event_id);
    assert!(!fixture.lock_path().exists());
    assert_eq!(fs::read(&receipt.quarantine_file).unwrap(), b"");
    assert_eq!(
        fs::read(fixture.root.join(".git/index")).unwrap(),
        index_before
    );
    assert_eq!(
        String::from_utf8(git(&fixture.root, &["status", "--porcelain"]).stdout).unwrap(),
        status_before
    );
    let event_count = fixture.event_log.ledger().read_events().unwrap().len();
    assert_eq!(event_count, 3, "proof, intent, and completion are durable");

    let replay = reconcile(&fixture, &lease, &inspector, Duration::ZERO);
    assert_eq!(replay, NativeLockRecoveryVerdict::Recovered(receipt));
    assert_eq!(
        fixture.event_log.ledger().read_events().unwrap().len(),
        event_count
    );
}

#[test]
fn active_managed_owner_and_external_holder_are_busy_without_mutation() {
    let fixture = Fixture::new();
    let (lease, proof, _) = fixture.proven_lock(b"");
    let lock_path = fixture.lock_path();
    let before = fs::read(&lock_path).unwrap();
    let managed = TableInspector {
        owner_state: OwnerState::Alive,
        ..TableInspector::gone(&proof.owner)
    };
    assert!(matches!(
        reconcile(&fixture, &lease, &managed, Duration::ZERO),
        NativeLockRecoveryVerdict::Busy { .. }
    ));
    assert_eq!(fs::read(&lock_path).unwrap(), before);

    let external_owner = ManagedProcessIdentity {
        pid: proof.owner.pid + 100,
        start_time_ticks: 123,
    };
    let external = TableInspector::holders(&proof.owner, vec![external_owner]);
    assert!(matches!(
        reconcile(&fixture, &lease, &external, Duration::ZERO),
        NativeLockRecoveryVerdict::Busy { .. }
    ));
    assert_eq!(fs::read(&lock_path).unwrap(), before);
}

#[test]
fn production_process_scanner_detects_an_external_open_lock_descriptor() {
    let fixture = Fixture::new();
    fixture.empty_lock();
    let lease = fixture.lease();
    let lock_path = fixture.lock_path();
    let proc_root = fixture._temp.path().join("synthetic-proc");
    let pid = 4819;
    let process_dir = proc_root.join(pid.to_string());
    fs::create_dir_all(process_dir.join("fd")).unwrap();
    let stat_fields = (0..19)
        .map(|index| {
            if index == 18 {
                "551".to_string()
            } else if index == 1 {
                "1".to_string()
            } else {
                "0".to_string()
            }
        })
        .collect::<Vec<_>>()
        .join(" ");
    fs::write(
        process_dir.join("stat"),
        format!("{pid} (external writer) S {stat_fields}\n"),
    )
    .unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(&lock_path, process_dir.join("fd/4")).unwrap();
    let inspector = ProcFsInspector::under(proc_root);

    let verdict = reconcile(&fixture, &lease, &inspector, Duration::ZERO);
    let NativeLockRecoveryVerdict::Busy { owners, .. } = verdict else {
        panic!("expected production process scanner to report Busy, got {verdict:?}");
    };
    assert_eq!(
        owners,
        vec![ManagedProcessIdentity {
            pid,
            start_time_ticks: 551
        }]
    );
    assert!(fixture.lock_path().exists());
    assert!(fixture.event_log.ledger().read_events().unwrap().is_empty());
}

#[test]
fn legacy_unknown_owner_and_partial_process_visibility_are_unchanged_uncertainty() {
    let fixture = Fixture::new();
    fixture.empty_lock();
    let lease = fixture.lease();
    let lock_path = fixture.lock_path();
    let before = fs::read(&lock_path).unwrap();
    let unknown = TableInspector::gone(&ManagedProcessIdentity {
        pid: 991,
        start_time_ticks: 20,
    });
    assert!(matches!(
        reconcile(&fixture, &lease, &unknown, Duration::ZERO),
        NativeLockRecoveryVerdict::Uncertain { .. }
    ));
    assert_eq!(fs::read(&lock_path).unwrap(), before);

    let second = Fixture::new();
    let (_, proof, _) = second.proven_lock(b"");
    let partial = TableInspector {
        inaccessible: true,
        ..TableInspector::gone(&proof.owner)
    };
    let lock_before = fs::read(second.lock_path()).unwrap();
    assert!(matches!(
        reconcile(&second, &second.lease(), &partial, Duration::ZERO),
        NativeLockRecoveryVerdict::Uncertain { .. }
    ));
    assert_eq!(fs::read(second.lock_path()).unwrap(), lock_before);
}

#[test]
fn pid_reuse_is_gone_only_when_no_process_holds_the_proven_inode() {
    let fixture = Fixture::new();
    let (lease, proof, _) = fixture.proven_lock(b"");
    let reused = TableInspector {
        owner_state: OwnerState::Reused,
        ..TableInspector::gone(&proof.owner)
    };
    assert!(matches!(
        reconcile(&fixture, &lease, &reused, Duration::ZERO),
        NativeLockRecoveryVerdict::Recovered(_)
    ));

    let second = Fixture::new();
    let (lease, proof, _) = second.proven_lock(b"");
    let replacement_holds_inode = TableInspector::holders(
        &proof.owner,
        vec![ManagedProcessIdentity {
            pid: proof.owner.pid,
            start_time_ticks: proof.owner.start_time_ticks + 1,
        }],
    );
    assert!(matches!(
        reconcile(&second, &lease, &replacement_holds_inode, Duration::ZERO),
        NativeLockRecoveryVerdict::Busy { .. }
    ));
}

#[test]
fn nonempty_or_changed_lock_evidence_is_never_quarantined() {
    let fixture = Fixture::new();
    let (lease, proof, _) = fixture.proven_lock(b"meaningful contents");
    let lock_path = fixture.lock_path();
    let before = fs::read(&lock_path).unwrap();
    let gone = TableInspector::gone(&proof.owner);
    assert!(matches!(
        reconcile(&fixture, &lease, &gone, Duration::ZERO),
        NativeLockRecoveryVerdict::Uncertain { .. }
    ));
    assert_eq!(fs::read(&lock_path).unwrap(), before);

    let second = Fixture::new();
    let (lease, proof, _) = second.proven_lock(b"");
    let changed_lock_path = second.lock_path();
    fs::write(&changed_lock_path, b"changed after owner proof").unwrap();
    let before = fs::read(&changed_lock_path).unwrap();
    assert!(matches!(
        reconcile(
            &second,
            &lease,
            &TableInspector::gone(&proof.owner),
            Duration::ZERO
        ),
        NativeLockRecoveryVerdict::Uncertain { .. }
    ));
    assert_eq!(fs::read(&changed_lock_path).unwrap(), before);
}

#[test]
fn quarantine_failure_and_full_bounded_store_preserve_native_lock() {
    let fixture = Fixture::new();
    let (lease, proof, _) = fixture.proven_lock(b"");
    let quarantine_dir = fixture.root.join(".git/exomonad-mutation/quarantine");
    fs::write(&quarantine_dir, b"do not overwrite").expect("block quarantine directory");
    let before = fs::read(fixture.lock_path()).unwrap();
    assert!(matches!(
        reconcile(
            &fixture,
            &lease,
            &TableInspector::gone(&proof.owner),
            Duration::ZERO
        ),
        NativeLockRecoveryVerdict::Uncertain { .. }
    ));
    assert_eq!(fs::read(fixture.lock_path()).unwrap(), before);

    let second = Fixture::new();
    let (lease, proof, _) = second.proven_lock(b"");
    let quarantine = second.root.join(".git/exomonad-mutation/quarantine");
    fs::create_dir_all(&quarantine).unwrap();
    for index in 0..64 {
        fs::write(quarantine.join(format!("retained-{index}.lock")), b"").unwrap();
    }
    let before = fs::read(second.lock_path()).unwrap();
    assert!(matches!(
        reconcile(
            &second,
            &lease,
            &TableInspector::gone(&proof.owner),
            Duration::ZERO
        ),
        NativeLockRecoveryVerdict::Uncertain { .. }
    ));
    assert_eq!(fs::read(second.lock_path()).unwrap(), before);
}

#[test]
fn path_swap_and_external_writer_race_are_rechecked_before_quarantine() {
    let fixture = Fixture::new();
    let (lease, proof, _) = fixture.proven_lock(b"");
    let external = fixture._temp.path().join("external-lock-target");
    fs::write(&external, b"foreign evidence").unwrap();
    fs::remove_file(fixture.lock_path()).unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(&external, fixture.lock_path()).unwrap();
    assert!(matches!(
        reconcile(
            &fixture,
            &lease,
            &TableInspector::gone(&proof.owner),
            Duration::ZERO
        ),
        NativeLockRecoveryVerdict::Uncertain { .. }
    ));
    assert_eq!(fs::read(external).unwrap(), b"foreign evidence");

    let second = Fixture::new();
    let (lease, proof, _) = second.proven_lock(b"");
    let changed_lock = second.lock_path();
    let race = TableInspector {
        mutation_on_call: Some((2, changed_lock.clone(), b"external writer raced".to_vec())),
        ..TableInspector::gone(&proof.owner)
    };
    assert!(matches!(
        reconcile(&second, &lease, &race, Duration::ZERO),
        NativeLockRecoveryVerdict::Uncertain { .. }
    ));
    assert_eq!(fs::read(changed_lock).unwrap(), b"external writer raced");
}

#[test]
fn bounded_wait_retries_after_busy_owner_then_recovers_once() {
    let fixture = Fixture::new();
    let (lease, proof, _) = fixture.proven_lock(b"");
    let inspector = TableInspector {
        holders: vec![proof.owner.clone()],
        report_holders_for_calls: 1,
        ..TableInspector::gone(&proof.owner)
    };
    let started = std::time::Instant::now();
    let result = reconcile(&fixture, &lease, &inspector, Duration::from_millis(120));
    assert!(matches!(result, NativeLockRecoveryVerdict::Recovered(_)));
    assert!(started.elapsed() >= Duration::from_millis(20));
    assert!(inspector.holder_calls.get() >= 3);
}

#[test]
fn bounded_wait_exhaustion_returns_busy_without_removing_lock() {
    let fixture = Fixture::new();
    let (lease, proof, _) = fixture.proven_lock(b"");
    let lock_path = fixture.lock_path();
    let before = fs::read(&lock_path).unwrap();
    let inspector = TableInspector::holders(&proof.owner, vec![proof.owner.clone()]);
    let started = std::time::Instant::now();

    let result = reconcile(&fixture, &lease, &inspector, Duration::from_millis(80));

    assert!(matches!(result, NativeLockRecoveryVerdict::Busy { .. }));
    assert!(started.elapsed() >= Duration::from_millis(70));
    assert!(started.elapsed() < Duration::from_secs(1));
    assert_eq!(fs::read(lock_path).unwrap(), before);
}

#[test]
fn interrupted_after_rename_is_adopted_and_receipted_without_second_move() {
    let fixture = Fixture::new();
    let (lease, proof, proof_event_id) = fixture.proven_lock(b"");
    let quarantine_file = super::quarantine_path(&lease.paths, &proof_event_id);
    fs::create_dir_all(quarantine_file.parent().unwrap()).unwrap();
    let audit = QuarantineAuditData {
        schema_version: 1,
        proof_event_id: proof_event_id.clone(),
        proof: proof.clone(),
        quarantine_file: quarantine_file.to_string_lossy().into_owned(),
    };
    fixture
        .event_log
        .append(
            QUARANTINE_INTENT_EVENT,
            "git-mutation",
            &serde_json::to_value(&audit).unwrap(),
        )
        .unwrap();
    fs::rename(fixture.lock_path(), &quarantine_file).unwrap();

    let result = reconcile(
        &fixture,
        &lease,
        &TableInspector::gone(&proof.owner),
        Duration::ZERO,
    );
    let NativeLockRecoveryVerdict::Recovered(receipt) = result else {
        panic!("expected interrupted quarantine adoption, got {result:?}");
    };
    assert_eq!(receipt.proof_event_id, proof_event_id);
    assert_eq!(receipt.quarantine_file, quarantine_file.to_string_lossy());
    assert!(!fixture.lock_path().exists());
    assert_eq!(
        fixture
            .event_log
            .ledger()
            .read_events()
            .unwrap()
            .iter()
            .filter(|event| event.event.event_type == QUARANTINED_EVENT)
            .count(),
        1
    );
}

#[test]
fn interrupted_rename_waits_until_the_original_writer_is_gone() {
    let fixture = Fixture::new();
    let lock_path = fixture.lock_path();
    fixture.empty_lock();
    let lease = fixture.lease();
    let mut child = managed_lock_holder(&lock_path);
    let owner = capture_managed_process_identity(&child).expect("child process identity");
    let recorded = record_until_proven(&lease, &mut child, &owner, &fixture.event_log)
        .expect("record live lock owner");
    let proof = recorded.proof;
    let quarantine_file = super::quarantine_path(&lease.paths, &recorded.event_id);
    fs::create_dir_all(quarantine_file.parent().unwrap()).unwrap();
    let audit = QuarantineAuditData {
        schema_version: 1,
        proof_event_id: recorded.event_id,
        proof: proof.clone(),
        quarantine_file: quarantine_file.to_string_lossy().into_owned(),
    };
    fixture
        .event_log
        .append(
            QUARANTINE_INTENT_EVENT,
            "git-mutation",
            &serde_json::to_value(&audit).unwrap(),
        )
        .unwrap();
    fs::rename(&lock_path, &quarantine_file).unwrap();

    let busy = TableInspector {
        owner_state: OwnerState::Alive,
        holders: vec![proof.owner.clone()],
        ..TableInspector::gone(&proof.owner)
    };
    assert!(matches!(
        reconcile(&fixture, &lease, &busy, Duration::ZERO),
        NativeLockRecoveryVerdict::Busy { .. }
    ));
    assert_eq!(
        fixture
            .event_log
            .ledger()
            .read_events()
            .unwrap()
            .iter()
            .filter(|event| event.event.event_type == QUARANTINED_EVENT)
            .count(),
        0
    );

    child.kill().expect("stop original writer");
    child.wait().expect("reap original writer");
    assert!(matches!(
        reconcile(
            &fixture,
            &lease,
            &TableInspector::gone(&proof.owner),
            Duration::ZERO
        ),
        NativeLockRecoveryVerdict::Recovered(_)
    ));
}

#[test]
fn interrupted_quarantine_must_reference_the_exact_ownership_event() {
    let fixture = Fixture::new();
    let (lease, proof, proof_event_id) = fixture.proven_lock(b"");
    let quarantine_file = super::quarantine_path(&lease.paths, &proof_event_id);
    fs::create_dir_all(quarantine_file.parent().unwrap()).unwrap();
    let mut forged_proof = proof.clone();
    forged_proof.owner.start_time_ticks += 1;
    let audit = QuarantineAuditData {
        schema_version: 1,
        proof_event_id,
        proof: forged_proof,
        quarantine_file: quarantine_file.to_string_lossy().into_owned(),
    };
    fixture
        .event_log
        .append(
            QUARANTINE_INTENT_EVENT,
            "git-mutation",
            &serde_json::to_value(&audit).unwrap(),
        )
        .unwrap();
    fs::rename(fixture.lock_path(), &quarantine_file).unwrap();

    assert!(matches!(
        reconcile(
            &fixture,
            &lease,
            &TableInspector::gone(&proof.owner),
            Duration::ZERO
        ),
        NativeLockRecoveryVerdict::Uncertain { .. }
    ));
    assert!(quarantine_file.exists(), "ambiguous evidence is retained");
    assert_eq!(
        fixture
            .event_log
            .ledger()
            .read_events()
            .unwrap()
            .iter()
            .filter(|event| event.event.event_type == QUARANTINED_EVENT)
            .count(),
        0
    );
}

#[test]
fn recovered_receipt_remains_auditable_and_exact_after_replay() {
    let fixture = Fixture::new();
    let (lease, proof, proof_event_id) = fixture.proven_lock(b"");
    let result = reconcile(
        &fixture,
        &lease,
        &TableInspector::gone(&proof.owner),
        Duration::ZERO,
    );
    let NativeLockRecoveryVerdict::Recovered(first) = result else {
        panic!("expected recovery, got {result:?}");
    };
    let rows = fixture.event_log.ledger().read_events().unwrap();
    assert!(rows
        .iter()
        .any(|row| row.event.event_type == QUARANTINE_INTENT_EVENT));
    assert!(rows
        .iter()
        .any(|row| row.event.event_type == QUARANTINED_EVENT));
    assert_eq!(first.proof_event_id, proof_event_id);
    assert_eq!(first.lock, proof.lock);

    let replay = reconcile(
        &fixture,
        &lease,
        &TableInspector::gone(&proof.owner),
        Duration::ZERO,
    );
    assert_eq!(replay, NativeLockRecoveryVerdict::Recovered(first));
    assert_eq!(
        fixture.event_log.ledger().read_events().unwrap().len(),
        rows.len()
    );
}
