use super::{
    capture_managed_process_identity_with, parse_start_time_ticks, record_proof_with,
    ManagedProcessIdentity, ProcFsInspector, ProcessInspector, LOCK_HELD_EVENT,
};
use crate::services::event_log::EventLog;
use crate::services::git_mutation::{GitMutationError, GitMutationResource, GitMutationSafety};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use tempfile::TempDir;

struct Fixture {
    _temp: TempDir,
    root: PathBuf,
    service: GitMutationSafety,
    event_log: EventLog,
}

impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().expect("temporary root");
        let root = temp.path().join("repo");
        fs::create_dir(&root).expect("repository root");
        git(&root, &["init", "-q", "-b", "main"]);
        git(&root, &["config", "user.name", "lock proof test"]);
        git(
            &root,
            &["config", "user.email", "lock-proof@example.invalid"],
        );
        fs::write(root.join("tracked.txt"), "base\n").expect("tracked file");
        git(&root, &["add", "tracked.txt"]);
        git(&root, &["commit", "-q", "-m", "fixture"]);
        let event_log = EventLog::open(temp.path().join("project/.exo/events")).expect("event log");
        Self {
            _temp: temp,
            service: GitMutationSafety::new(root.clone()),
            root,
            event_log,
        }
    }

    fn lock_path(&self) -> PathBuf {
        self.root.join(".git/index.lock")
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

#[derive(Clone)]
struct FakeInspector {
    pid: u32,
    start_ticks: Option<u64>,
    lock_path: Option<PathBuf>,
    inaccessible_fd_dir: bool,
}

impl ProcessInspector for FakeInspector {
    fn start_time_ticks(&self, pid: u32) -> Result<Option<u64>, String> {
        if pid != self.pid {
            return Err("unexpected process id".to_string());
        }
        Ok(self.start_ticks)
    }

    fn holds_inode(&self, pid: u32, device: u64, inode: u64) -> Result<bool, String> {
        if pid != self.pid {
            return Err("unexpected process id".to_string());
        }
        if self.inaccessible_fd_dir {
            return Err(
                "cannot inspect managed process descriptors (permission denied)".to_string(),
            );
        }
        let Some(lock) = self.lock_path.as_ref() else {
            return Ok(false);
        };
        let metadata = fs::metadata(lock).map_err(|_| "lock disappeared".to_string())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            Ok(metadata.dev() == device && metadata.ino() == inode)
        }
        #[cfg(not(unix))]
        {
            let _ = (metadata, device, inode);
            Ok(false)
        }
    }

    fn process_holders(
        &self,
        _device: u64,
        _inode: u64,
    ) -> Result<Vec<ManagedProcessIdentity>, String> {
        Ok(Vec::new())
    }
}

fn procfs_fixture(root: &Path, pid: u32, start_ticks: u64, lock_path: &Path) {
    let process_dir = root.join(pid.to_string());
    fs::create_dir_all(process_dir.join("fd")).expect("fake process descriptors");
    let suffix = (0..19)
        .map(|index| {
            if index == 18 {
                start_ticks.to_string()
            } else {
                "0".to_string()
            }
        })
        .collect::<Vec<_>>()
        .join(" ");
    fs::write(
        process_dir.join("stat"),
        format!("{pid} (worker (git)) S {suffix}\n"),
    )
    .expect("fake process stat");
    #[cfg(unix)]
    std::os::unix::fs::symlink(lock_path, process_dir.join("fd/7"))
        .expect("fake process descriptor link");
}

#[test]
fn production_procfs_parser_handles_parentheses_and_reads_start_identity() {
    let fixture = Fixture::new();
    let lock_path = fixture.lock_path();
    drop(
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&lock_path)
            .expect("native lock"),
    );
    let lease = fixture
        .service
        .acquire(
            &fixture.root,
            GitMutationResource::Index,
            "synchronization-operation",
            std::time::Duration::ZERO,
        )
        .expect("index lease");
    let mut child = managed_lock_holder(&lock_path);
    let owner = super::capture_managed_process_identity(&child).expect("child process identity");
    let result = record_until_proven(&lease, &mut child, &owner, &fixture.event_log);
    let _ = child.kill();
    child.wait().expect("reap managed lock holder");
    let recorded = result.expect("production /proc fd proof");
    assert_eq!(recorded.proof.owner, owner);
    assert_eq!(recorded.proof.lock.size, 0);
    assert_eq!(
        recorded.proof.index_file,
        fixture.root.join(".git/index").display().to_string()
    );
    assert_eq!(
        fixture.event_log.ledger().read_events().unwrap().len(),
        1,
        "proof is durable in the existing immutable ledger"
    );
}

fn managed_lock_holder(lock_path: &Path) -> Child {
    Command::new("sh")
        .args(["-c", "exec 9<\"$1\"; sleep 30", "git-lock-holder"])
        .arg(lock_path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn managed process holding lock inode")
}

fn record_until_proven(
    lease: &crate::services::git_mutation::GitMutationLease,
    child: &mut Child,
    owner: &ManagedProcessIdentity,
    event_log: &EventLog,
) -> Result<super::RecordedManagedNativeLockProof, GitMutationError> {
    let started = std::time::Instant::now();
    loop {
        match super::record_managed_native_lock_proof(lease, child, owner, event_log) {
            Ok(recorded) => return Ok(recorded),
            Err(GitMutationError::Uncertain { .. })
                if started.elapsed() < std::time::Duration::from_secs(2) =>
            {
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
            Err(error) => return Err(error),
        }
    }
}

#[test]
fn fake_procfs_evidence_is_bound_to_the_exact_paths_process_and_lock_inode() {
    let fixture = Fixture::new();
    let lock_path = fixture.lock_path();
    let _held_lock = File::create(&lock_path).expect("native lock");
    let lease = fixture
        .service
        .acquire(
            &fixture.root,
            GitMutationResource::Index,
            "sensitive-operation-token",
            std::time::Duration::ZERO,
        )
        .expect("index lease");
    let process_root = fixture._temp.path().join("proc");
    procfs_fixture(&process_root, 4217, 989, &lock_path);
    let inspector = ProcFsInspector::under(process_root);
    let owner =
        capture_managed_process_identity_with(&inspector, 4217).expect("fake process identity");

    let recorded = record_proof_with(&lease, &owner, &fixture.event_log, &inspector)
        .expect("same process holds same lock inode");
    assert_eq!(recorded.proof.owner.start_time_ticks, 989);
    assert_eq!(
        recorded.proof.index_lock_file,
        lock_path.display().to_string()
    );
    let stored = &fixture.event_log.ledger().read_events().unwrap()[0].event;
    assert_eq!(stored.event_type, LOCK_HELD_EVENT);
    assert_eq!(stored.data, serde_json::to_value(&recorded.proof).unwrap());
    assert!(!stored
        .data
        .to_string()
        .contains("sensitive-operation-token"));
    assert_eq!(
        record_proof_with(&lease, &owner, &fixture.event_log, &inspector)
            .unwrap()
            .event_id,
        recorded.event_id,
        "repeated proof is adopted by exact event identity"
    );
}

#[test]
fn wrong_process_identity_and_pid_reuse_fail_before_audit_mutation() {
    let fixture = Fixture::new();
    let lock_path = fixture.lock_path();
    let _held_lock = File::create(&lock_path).expect("native lock");
    let lease = fixture
        .service
        .acquire(
            &fixture.root,
            GitMutationResource::Index,
            "operation",
            std::time::Duration::ZERO,
        )
        .expect("index lease");
    let process_root = fixture._temp.path().join("proc");
    procfs_fixture(&process_root, 4217, 990, &lock_path);
    let inspector = ProcFsInspector::under(process_root);
    let stale_owner = ManagedProcessIdentity {
        pid: 4217,
        start_time_ticks: 989,
    };

    assert!(matches!(
        record_proof_with(&lease, &stale_owner, &fixture.event_log, &inspector),
        Err(GitMutationError::Uncertain { .. })
    ));
    assert!(fixture.event_log.ledger().read_events().unwrap().is_empty());
}

#[test]
fn missing_descriptor_proof_or_partial_proc_visibility_is_uncertain() {
    let fixture = Fixture::new();
    let lock_path = fixture.lock_path();
    let _held_lock = File::create(&lock_path).expect("native lock");
    let lease = fixture
        .service
        .acquire(
            &fixture.root,
            GitMutationResource::Index,
            "operation",
            std::time::Duration::ZERO,
        )
        .expect("index lease");
    let owner = ManagedProcessIdentity {
        pid: 4200,
        start_time_ticks: 42,
    };
    let no_descriptor = FakeInspector {
        pid: owner.pid,
        start_ticks: Some(owner.start_time_ticks),
        lock_path: None,
        inaccessible_fd_dir: false,
    };
    assert!(matches!(
        record_proof_with(&lease, &owner, &fixture.event_log, &no_descriptor),
        Err(GitMutationError::Uncertain { .. })
    ));

    let partial = FakeInspector {
        lock_path: Some(lock_path),
        inaccessible_fd_dir: true,
        ..no_descriptor
    };
    assert!(matches!(
        record_proof_with(&lease, &owner, &fixture.event_log, &partial),
        Err(GitMutationError::Uncertain { .. })
    ));
    assert!(fixture.event_log.ledger().read_events().unwrap().is_empty());
}

#[test]
fn lock_path_symlink_never_records_proof_or_changes_its_target() {
    let fixture = Fixture::new();
    let lock_path = fixture.lock_path();
    let external = fixture._temp.path().join("external-lock");
    fs::write(&external, b"leave untouched").expect("external lock target");
    let lease = fixture
        .service
        .acquire(
            &fixture.root,
            GitMutationResource::Index,
            "operation",
            std::time::Duration::ZERO,
        )
        .expect("index lease");
    #[cfg(unix)]
    std::os::unix::fs::symlink(&external, &lock_path).expect("native lock symlink");
    let owner = ManagedProcessIdentity {
        pid: 88,
        start_time_ticks: 9,
    };
    let inspector = FakeInspector {
        pid: owner.pid,
        start_ticks: Some(owner.start_time_ticks),
        lock_path: Some(lock_path),
        inaccessible_fd_dir: false,
    };
    assert!(matches!(
        record_proof_with(&lease, &owner, &fixture.event_log, &inspector),
        Err(GitMutationError::Uncertain { .. })
    ));
    assert_eq!(fs::read(external).unwrap(), b"leave untouched");
    assert!(fixture.event_log.ledger().read_events().unwrap().is_empty());
}

#[test]
fn production_procfs_permission_gap_is_uncertain() {
    let fixture = Fixture::new();
    let lock_path = fixture.lock_path();
    let _held_lock = File::create(&lock_path).expect("native lock");
    let lease = fixture
        .service
        .acquire(
            &fixture.root,
            GitMutationResource::Index,
            "operation",
            std::time::Duration::ZERO,
        )
        .expect("index lease");
    let process_root = fixture._temp.path().join("partial-proc");
    let pid = 7219;
    let process_dir = process_root.join(pid.to_string());
    fs::create_dir_all(&process_dir).expect("partial process directory");
    fs::write(process_dir.join("stat"), fake_stat(pid, 81)).expect("process identity");
    let owner = ManagedProcessIdentity {
        pid,
        start_time_ticks: 81,
    };
    let inspector = ProcFsInspector::under(process_root);

    assert!(matches!(
        record_proof_with(&lease, &owner, &fixture.event_log, &inspector),
        Err(GitMutationError::Uncertain { .. })
    ));
    assert!(fixture.event_log.ledger().read_events().unwrap().is_empty());
}

#[test]
fn a_lock_changed_after_ownership_observation_is_not_audited_as_proven() {
    let fixture = Fixture::new();
    let lock_path = fixture.lock_path();
    let _held_lock = File::create(&lock_path).expect("native lock");
    let lease = fixture
        .service
        .acquire(
            &fixture.root,
            GitMutationResource::Index,
            "operation",
            std::time::Duration::ZERO,
        )
        .expect("index lease");
    let owner = ManagedProcessIdentity {
        pid: 61,
        start_time_ticks: 8,
    };
    let inspector = MutatingInspector {
        pid: owner.pid,
        start_ticks: owner.start_time_ticks,
        lock_path: lock_path.clone(),
    };

    assert!(matches!(
        record_proof_with(&lease, &owner, &fixture.event_log, &inspector),
        Err(GitMutationError::Uncertain { .. })
    ));
    assert_eq!(
        fs::read(lock_path).unwrap(),
        b"changed during ownership check"
    );
    assert!(fixture.event_log.ledger().read_events().unwrap().is_empty());
}

struct MutatingInspector {
    pid: u32,
    start_ticks: u64,
    lock_path: PathBuf,
}

impl ProcessInspector for MutatingInspector {
    fn start_time_ticks(&self, pid: u32) -> Result<Option<u64>, String> {
        assert_eq!(pid, self.pid);
        Ok(Some(self.start_ticks))
    }

    fn holds_inode(&self, pid: u32, _device: u64, _inode: u64) -> Result<bool, String> {
        assert_eq!(pid, self.pid);
        let mut file = OpenOptions::new()
            .append(true)
            .open(&self.lock_path)
            .map_err(|error| error.to_string())?;
        file.write_all(b"changed during ownership check")
            .map_err(|error| error.to_string())?;
        Ok(true)
    }

    fn process_holders(
        &self,
        _device: u64,
        _inode: u64,
    ) -> Result<Vec<ManagedProcessIdentity>, String> {
        Ok(Vec::new())
    }
}

#[test]
fn parser_refuses_malformed_or_ambiguous_process_records() {
    assert_eq!(parse_start_time_ticks("12 (a process (name)) S 0"), None);
    let mut fields = (0..20).map(|_| "0").collect::<Vec<_>>();
    fields[19] = "bad";
    assert_eq!(
        parse_start_time_ticks(&format!("12 (worker) {}", fields.join(" "))),
        None
    );
    assert_eq!(parse_start_time_ticks(&fake_stat(12, 321)), Some(321));
}

fn fake_stat(pid: u32, start_ticks: u64) -> String {
    let suffix = (0..19)
        .map(|index| {
            if index == 18 {
                start_ticks.to_string()
            } else {
                "0".to_string()
            }
        })
        .collect::<Vec<_>>()
        .join(" ");
    format!("{pid} (worker) S {suffix}")
}
