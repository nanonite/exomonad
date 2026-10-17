use super::{GitMutationError, GitMutationResource, GitMutationSafety, LEASE_DIRECTORY};
use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::thread;
use std::time::Duration;
use tempfile::TempDir;

struct RepositoryFixture {
    _temp: TempDir,
    root: PathBuf,
    service: GitMutationSafety,
}

impl RepositoryFixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().expect("temporary directory");
        let root = temp.path().join("repo");
        fs::create_dir(&root).expect("repository directory");
        git(&root, &["init", "-q", "-b", "main"]);
        git(&root, &["config", "user.name", "Git mutation fixture"]);
        git(
            &root,
            &["config", "user.email", "git-mutation@example.invalid"],
        );
        fs::write(root.join("tracked.txt"), "base\n").expect("tracked fixture file");
        git(&root, &["add", "tracked.txt"]);
        git(&root, &["commit", "-q", "-m", "fixture base"]);
        let service = GitMutationSafety::new(root.clone());
        Self {
            _temp: temp,
            root,
            service,
        }
    }

    fn linked_worktree(&self) -> PathBuf {
        let path = self.root.parent().expect("fixture parent").join("linked");
        git(
            &self.root,
            &[
                "worktree",
                "add",
                "-q",
                "-b",
                "linked",
                path.to_str().unwrap(),
            ],
        );
        path
    }
}

fn git(directory: &Path, args: &[&str]) -> Output {
    let output = Command::new("git")
        .args(args)
        .current_dir(directory)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .output()
        .expect("start Git fixture command");
    assert!(
        output.status.success(),
        "git {args:?} failed with {:?}: {}",
        output.status.code(),
        String::from_utf8_lossy(&output.stderr)
    );
    output
}

fn porcelain(directory: &Path) -> String {
    String::from_utf8(git(directory, &["status", "--porcelain"]).stdout)
        .expect("status output is UTF-8")
}

fn is_uncertain(error: GitMutationError) -> bool {
    matches!(error, GitMutationError::Uncertain { .. })
}

#[test]
fn resolves_main_and_linked_worktree_git_paths_without_assuming_dot_git_is_a_directory() {
    let fixture = RepositoryFixture::new();
    let main = fixture
        .service
        .resolve_paths_with_index_override(&fixture.root, None)
        .expect("main worktree paths");
    let linked_root = fixture.linked_worktree();
    let linked = fixture
        .service
        .resolve_paths_with_index_override(&linked_root, None)
        .expect("linked worktree paths");

    assert!(
        fs::symlink_metadata(linked.worktree_root.join(".git"))
            .expect("linked worktree .git marker")
            .is_file(),
        "linked worktree metadata is represented by a .git file"
    );
    assert_eq!(main.worktree_root, fixture.root);
    assert_eq!(main.common_git_dir, linked.common_git_dir);
    assert_ne!(main.git_dir, linked.git_dir);
    assert_ne!(main.index_file, linked.index_file);
    assert_ne!(main.index_lease_file, linked.index_lease_file);
    assert_eq!(main.refs_lease_file, linked.refs_lease_file);
    assert_eq!(
        main.index_lock_file,
        PathBuf::from(format!("{}.lock", main.index_file.display()))
    );
}

#[test]
fn resolves_alternate_absolute_and_relative_index_overrides_inside_managed_paths() {
    let fixture = RepositoryFixture::new();
    let absolute = fixture.root.join(".git").join("index.alternate");
    let paths = fixture
        .service
        .resolve_paths_with_index_override(&fixture.root, Some(absolute.as_os_str()))
        .expect("absolute alternate index");
    assert_eq!(paths.index_file, absolute);

    let relative = PathBuf::from(".git/index.relative");
    let paths = fixture
        .service
        .resolve_paths_with_index_override(&fixture.root, Some(relative.as_os_str()))
        .expect("relative alternate index");
    assert_eq!(paths.index_file, fixture.root.join(relative));

    let outside = fixture
        .root
        .parent()
        .expect("fixture parent")
        .join("foreign-index");
    assert!(is_uncertain(
        fixture
            .service
            .resolve_paths_with_index_override(&fixture.root, Some(outside.as_os_str()))
            .expect_err("outside index is not owned")
    ));
}

#[test]
fn relative_worktree_paths_resolve_against_the_declared_managed_root() {
    let fixture = RepositoryFixture::new();
    let paths = fixture
        .service
        .resolve_paths_with_index_override(Path::new("."), None)
        .expect("relative root under configured project");
    assert_eq!(paths.worktree_root, fixture.root);
}

#[test]
fn lease_serializes_same_index_with_bounded_busy_result_and_minimal_owner_record() {
    let fixture = RepositoryFixture::new();
    let status_before = porcelain(&fixture.root);
    let index_before = fs::read(fixture.root.join(".git/index")).expect("index before");
    let lease = fixture
        .service
        .acquire(
            &fixture.root,
            GitMutationResource::Index,
            "sync-action-1",
            Duration::ZERO,
        )
        .expect("first lease");
    let record: Value = serde_json::from_slice(
        &fs::read(&lease.paths.index_lease_file).expect("durable owner record"),
    )
    .expect("JSON lease owner record");
    let operation_digest = record["operation_id_sha256"]
        .as_str()
        .expect("operation identity digest");
    assert_eq!(operation_digest.len(), 64);
    assert!(operation_digest
        .bytes()
        .all(|byte| byte.is_ascii_hexdigit()));
    assert_eq!(record["resource"], "worktree index");
    assert!(record.get("owner_pid").and_then(Value::as_u64).is_some());
    let rendered = record.to_string();
    assert!(!rendered.contains("token"));
    assert!(!rendered.contains("remote"));
    assert!(!rendered.contains("sync-action-1"));

    let service = fixture.service.clone();
    let root = fixture.root.clone();
    let second = thread::spawn(move || {
        service.acquire(
            &root,
            GitMutationResource::Index,
            "sync-action-2",
            Duration::from_millis(60),
        )
    })
    .join()
    .expect("lease waiter thread");
    assert!(matches!(second, Err(GitMutationError::Busy { .. })));
    drop(lease);

    assert_eq!(porcelain(&fixture.root), status_before);
    assert_eq!(
        fs::read(fixture.root.join(".git/index")).unwrap(),
        index_before
    );
}

#[test]
fn linked_worktrees_have_independent_index_leases_and_shared_refs_lease() {
    let fixture = RepositoryFixture::new();
    let linked = fixture.linked_worktree();
    let main_index = fixture
        .service
        .acquire(
            &fixture.root,
            GitMutationResource::Index,
            "main-index",
            Duration::ZERO,
        )
        .expect("main index lease");
    let linked_index = fixture
        .service
        .acquire(
            &linked,
            GitMutationResource::Index,
            "linked-index",
            Duration::ZERO,
        )
        .expect("linked index lease does not collide");
    assert_ne!(
        main_index.paths.index_lease_file,
        linked_index.paths.index_lease_file
    );

    let main_refs = fixture
        .service
        .acquire(
            &fixture.root,
            GitMutationResource::SharedRefs,
            "main-refs",
            Duration::ZERO,
        )
        .expect("shared refs lease");
    let result = fixture.service.acquire(
        &linked,
        GitMutationResource::SharedRefs,
        "linked-refs",
        Duration::ZERO,
    );
    assert!(matches!(result, Err(GitMutationError::Busy { .. })));
    drop(main_refs);
}

#[test]
fn linked_worktrees_share_a_lease_for_the_same_alternate_index() {
    let fixture = RepositoryFixture::new();
    let linked = fixture.linked_worktree();
    let shared_index = fixture.root.join(".git/index.shared");
    let main = fixture
        .service
        .resolve_paths_with_index_override(&fixture.root, Some(shared_index.as_os_str()))
        .expect("main alternate index");
    let linked_paths = fixture
        .service
        .resolve_paths_with_index_override(&linked, Some(shared_index.as_os_str()))
        .expect("linked alternate index");
    assert_eq!(main.index_lease_file, linked_paths.index_lease_file);

    let _lease = fixture
        .service
        .acquire_with_index_override(
            &fixture.root,
            GitMutationResource::Index,
            "main-shared-index",
            Duration::ZERO,
            Some(shared_index.as_os_str()),
        )
        .expect("first shared alternate-index lease");
    let result = fixture.service.acquire_with_index_override(
        &linked,
        GitMutationResource::Index,
        "linked-shared-index",
        Duration::ZERO,
        Some(shared_index.as_os_str()),
    );
    assert!(matches!(result, Err(GitMutationError::Busy { .. })));
}

#[test]
fn acquire_set_uses_refs_before_index_and_deduplicates_resources() {
    let fixture = RepositoryFixture::new();
    let leases = fixture
        .service
        .acquire_set(
            &fixture.root,
            &[
                GitMutationResource::Index,
                GitMutationResource::SharedRefs,
                GitMutationResource::Index,
            ],
            "combined-operation",
            Duration::from_millis(100),
        )
        .expect("ordered lease set");
    assert_eq!(leases.leases().len(), 2);
    assert_eq!(leases.leases()[0].resource, GitMutationResource::SharedRefs);
    assert_eq!(leases.leases()[1].resource, GitMutationResource::Index);
}

#[cfg(unix)]
#[test]
fn symlinked_worktree_index_and_lease_directory_fail_closed() {
    use std::os::unix::fs::symlink;

    let fixture = RepositoryFixture::new();
    let alias = fixture.root.parent().unwrap().join("repo-alias");
    symlink(&fixture.root, &alias).expect("worktree symlink");
    assert!(is_uncertain(
        fixture
            .service
            .resolve_paths_with_index_override(&alias, None)
            .expect_err("symlink worktree is rejected")
    ));

    let paths = fixture
        .service
        .resolve_paths_with_index_override(&fixture.root, None)
        .expect("normal paths");
    let external = fixture.root.parent().unwrap().join("external-lease-dir");
    fs::create_dir(&external).expect("external target");
    symlink(&external, paths.common_git_dir.join(LEASE_DIRECTORY))
        .expect("lease directory symlink");
    assert!(is_uncertain(
        fixture
            .service
            .acquire(
                &fixture.root,
                GitMutationResource::Index,
                "symlinked-lease",
                Duration::ZERO,
            )
            .expect_err("lease symlink is rejected")
    ));
    assert_eq!(fs::read_dir(external).unwrap().count(), 0);
}

#[cfg(unix)]
#[test]
fn inaccessible_lease_directory_fails_without_partial_lease_or_worktree_changes() {
    use std::os::unix::fs::PermissionsExt;

    let fixture = RepositoryFixture::new();
    let paths = fixture
        .service
        .resolve_paths_with_index_override(&fixture.root, None)
        .expect("normal paths");
    let lease_dir = paths.common_git_dir.join(LEASE_DIRECTORY);
    fs::create_dir(&lease_dir).expect("lease directory");
    fs::set_permissions(&lease_dir, fs::Permissions::from_mode(0o500))
        .expect("remove lease directory write permission");

    let result = fixture.service.acquire(
        &fixture.root,
        GitMutationResource::Index,
        "restricted-permission-test",
        Duration::ZERO,
    );
    fs::set_permissions(&lease_dir, fs::Permissions::from_mode(0o700))
        .expect("restore temporary permissions");

    assert!(matches!(result, Err(GitMutationError::Uncertain { .. })));
    assert!(!paths.index_lease_file.exists());
    assert_eq!(porcelain(&fixture.root), "");
}

#[test]
fn mutation_lease_preserves_staged_unstaged_and_untracked_edits() {
    let fixture = RepositoryFixture::new();
    fs::write(fixture.root.join("tracked.txt"), "staged\n").expect("staged content");
    git(&fixture.root, &["add", "tracked.txt"]);
    fs::write(fixture.root.join("tracked.txt"), "unstaged\n").expect("unstaged content");
    fs::write(fixture.root.join("untracked.txt"), "untracked\n").expect("untracked content");
    let before = porcelain(&fixture.root);
    let index_before = fs::read(fixture.root.join(".git/index")).expect("index before");

    let _lease = fixture
        .service
        .acquire(
            &fixture.root,
            GitMutationResource::Index,
            "preflight-dirty",
            Duration::ZERO,
        )
        .expect("lease with unrelated edits");

    assert_eq!(porcelain(&fixture.root), before);
    assert_eq!(
        fs::read(fixture.root.join(".git/index")).unwrap(),
        index_before
    );
    assert_eq!(
        fs::read_to_string(fixture.root.join("tracked.txt")).unwrap(),
        "unstaged\n"
    );
    assert_eq!(
        fs::read_to_string(fixture.root.join("untracked.txt")).unwrap(),
        "untracked\n"
    );
}

#[test]
fn invalid_operation_identity_and_unrelated_repository_are_rejected() {
    let fixture = RepositoryFixture::new();
    assert!(matches!(
        fixture.service.acquire(
            &fixture.root,
            GitMutationResource::Index,
            "bad operation id\n",
            Duration::ZERO,
        ),
        Err(GitMutationError::InvalidOperationId)
    ));

    let foreign_root = fixture.root.parent().unwrap().join("foreign");
    fs::create_dir(&foreign_root).expect("foreign repository directory");
    git(&foreign_root, &["init", "-q"]);
    assert!(is_uncertain(
        fixture
            .service
            .resolve_paths_with_index_override(&foreign_root, None)
            .expect_err("foreign repository cannot borrow managed service")
    ));
}

#[cfg(unix)]
#[test]
fn native_index_lock_symlink_is_not_followed_or_removed() {
    use std::os::unix::fs::symlink;

    let fixture = RepositoryFixture::new();
    let paths = fixture
        .service
        .resolve_paths_with_index_override(&fixture.root, None)
        .expect("normal paths");
    let external = fixture.root.parent().unwrap().join("foreign-index-lock");
    fs::write(&external, b"foreign lock evidence").expect("foreign lock");
    symlink(&external, &paths.index_lock_file).expect("native lock symlink");
    assert!(is_uncertain(
        fixture
            .service
            .resolve_paths_with_index_override(&fixture.root, None)
            .expect_err("native lock symlink is uncertain")
    ));
    assert_eq!(fs::read(&external).unwrap(), b"foreign lock evidence");
}
