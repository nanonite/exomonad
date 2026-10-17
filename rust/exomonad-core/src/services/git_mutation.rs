//! Shared, worktree-scoped coordination for ExoMonad-controlled Git mutations.
//!
//! The advisory lease coordinates ExoMonad instances that use this service. Git's
//! native lockfiles remain authoritative against other Git processes.

#[cfg(all(unix, not(any(target_os = "redox", target_os = "solaris"))))]
use nix::errno::Errno;
#[cfg(all(unix, not(any(target_os = "redox", target_os = "solaris"))))]
use nix::fcntl::{Flock, FlockArg};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread;
use std::time::{Duration, Instant};
use thiserror::Error;

const LEASE_DIRECTORY: &str = "exomonad-mutation";
const LEASE_SCHEMA_VERSION: u32 = 1;
const LEASE_POLL_INTERVAL: Duration = Duration::from_millis(20);

#[cfg(all(unix, not(any(target_os = "redox", target_os = "solaris"))))]
type NativeLeaseLock = Flock<File>;
#[cfg(not(all(unix, not(any(target_os = "redox", target_os = "solaris")))))]
struct NativeLeaseLock;

/// Git paths resolved for one registered worktree and its active index.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GitMutationPaths {
    pub worktree_root: PathBuf,
    pub git_dir: PathBuf,
    pub common_git_dir: PathBuf,
    pub index_file: PathBuf,
    pub index_lock_file: PathBuf,
    pub index_lease_file: PathBuf,
    pub refs_lease_file: PathBuf,
}

/// Native Git resources protected by ExoMonad's cooperative mutation lease.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum GitMutationResource {
    /// Shared references and remote-tracking state in the common Git directory.
    SharedRefs,
    /// The active index for this specific worktree/index override.
    Index,
}

impl GitMutationResource {
    fn lease_path(self, paths: &GitMutationPaths) -> &Path {
        match self {
            Self::SharedRefs => &paths.refs_lease_file,
            Self::Index => &paths.index_lease_file,
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::SharedRefs => "shared Git refs",
            Self::Index => "worktree index",
        }
    }
}

/// Errors from Git path resolution or coordinated lease acquisition.
#[derive(Debug, Error)]
pub enum GitMutationError {
    #[error("Git mutation readiness is uncertain for {path}: {reason}")]
    Uncertain { path: PathBuf, reason: String },
    #[error("Git mutation lease for {resource} remained busy at {path} for {waited_ms}ms")]
    Busy {
        resource: &'static str,
        path: PathBuf,
        waited_ms: u128,
    },
    #[error("Git operation identity must be 1–128 printable non-space ASCII characters")]
    InvalidOperationId,
    #[error("coordinated Git mutation leases are unsupported on this platform")]
    UnsupportedPlatform,
}

/// Minimal durable identity written while an ExoMonad lease is held.
///
/// It deliberately omits command lines, environment variables, and remote URLs.
#[derive(Debug, Clone, Serialize)]
struct LeaseRecord {
    schema_version: u32,
    operation_id_sha256: String,
    resource: &'static str,
    resource_sha256: String,
    owner_pid: u32,
    owner_start_identity: Option<u64>,
}

/// RAII owner for one cooperative Git mutation lease.
#[derive(Debug)]
pub struct GitMutationLease {
    _lock: NativeLeaseLock,
    pub paths: GitMutationPaths,
    pub resource: GitMutationResource,
    pub operation_id: String,
    pub(crate) index_override: Option<PathBuf>,
}

/// Several leases acquired in the shared global order: refs before index.
#[derive(Debug)]
pub struct GitMutationLeaseSet {
    leases: Vec<GitMutationLease>,
}

impl GitMutationLeaseSet {
    pub fn leases(&self) -> &[GitMutationLease] {
        &self.leases
    }
}

/// Resolves the real Git metadata paths and acquires cooperative mutation leases.
#[derive(Debug, Clone)]
pub struct GitMutationSafety {
    managed_worktree: PathBuf,
}

impl GitMutationSafety {
    pub fn new(managed_worktree: PathBuf) -> Self {
        Self { managed_worktree }
    }

    /// Resolve the canonical worktree, git dirs and active index without mutation.
    pub fn resolve_paths(&self, worktree: &Path) -> Result<GitMutationPaths, GitMutationError> {
        self.resolve_paths_with_index_override(
            worktree,
            std::env::var_os("GIT_INDEX_FILE").as_deref(),
        )
    }

    /// Resolve paths using the caller's effective `GIT_INDEX_FILE` value.
    /// `None` explicitly removes an inherited override for deterministic callers.
    pub fn resolve_paths_with_index_override(
        &self,
        worktree: &Path,
        index_override: Option<&std::ffi::OsStr>,
    ) -> Result<GitMutationPaths, GitMutationError> {
        let managed_root = canonical_directory(&self.managed_worktree, "managed worktree")?;
        let root = absolute_from(&managed_root, worktree);
        reject_symlink(&root, "worktree path")?;
        let root = canonical_directory(&root, "worktree")?;
        let managed_top = git_path(&managed_root, "--show-toplevel", None)?;
        if canonical_directory(Path::new(&managed_top), "managed repository root")? != managed_root
        {
            return Err(uncertain(
                &managed_root,
                "configured path is not a worktree root",
            ));
        }
        let root_top = git_path(&root, "--show-toplevel", index_override)?;
        if canonical_directory(Path::new(&root_top), "Git worktree root")? != root {
            return Err(uncertain(&root, "requested path is not the worktree root"));
        }

        let common_git_dir = canonical_directory(
            Path::new(&git_path(&root, "--git-common-dir", index_override)?),
            "common Git directory",
        )?;
        let managed_common = canonical_directory(
            Path::new(&git_path(&managed_root, "--git-common-dir", None)?),
            "managed common Git directory",
        )?;
        if common_git_dir != managed_common {
            return Err(uncertain(
                &root,
                "worktree belongs to a different common Git directory",
            ));
        }

        let git_dir = canonical_directory(
            Path::new(&git_path(&root, "--absolute-git-dir", index_override)?),
            "worktree Git directory",
        )?;
        let raw_index = PathBuf::from(git_path(&root, "--git-path index", index_override)?);
        let index_file = canonicalize_leaf_parent(&raw_index, "active Git index")?;
        validate_index_ownership(&root, &git_dir, &common_git_dir, &index_file)?;
        reject_optional_symlink(&index_file, "active Git index")?;

        let index_lock_file = append_suffix(&index_file, ".lock");
        reject_optional_symlink(&index_lock_file, "native Git index lock")?;
        let lease_dir = common_git_dir.join(LEASE_DIRECTORY);
        reject_optional_symlink(&lease_dir, "Git mutation lease directory")?;
        let index_lease_file = lease_dir.join(format!("index-{}.lease", path_digest(&index_file)));
        let refs_lease_file = common_git_dir.join(LEASE_DIRECTORY).join("refs.lease");
        reject_optional_symlink(&refs_lease_file, "shared refs lease")?;

        Ok(GitMutationPaths {
            worktree_root: root,
            git_dir,
            common_git_dir,
            index_file,
            index_lock_file,
            index_lease_file,
            refs_lease_file,
        })
    }

    /// Acquire one bounded cooperative lease. This never removes a native Git lock.
    pub fn acquire(
        &self,
        worktree: &Path,
        resource: GitMutationResource,
        operation_id: &str,
        wait: Duration,
    ) -> Result<GitMutationLease, GitMutationError> {
        let override_value = std::env::var_os("GIT_INDEX_FILE");
        self.acquire_with_index_override(
            worktree,
            resource,
            operation_id,
            wait,
            override_value.as_deref(),
        )
    }

    /// Acquire a lease using the same explicit index override for resolution and revalidation.
    pub fn acquire_with_index_override(
        &self,
        worktree: &Path,
        resource: GitMutationResource,
        operation_id: &str,
        wait: Duration,
        index_override: Option<&std::ffi::OsStr>,
    ) -> Result<GitMutationLease, GitMutationError> {
        validate_operation_id(operation_id)?;
        let paths = self.resolve_paths_with_index_override(worktree, index_override)?;
        self.acquire_resolved(paths, resource, operation_id, wait, index_override)
    }

    /// Acquire multiple resources in a consistent order to avoid lease deadlocks.
    pub fn acquire_set(
        &self,
        worktree: &Path,
        resources: &[GitMutationResource],
        operation_id: &str,
        wait: Duration,
    ) -> Result<GitMutationLeaseSet, GitMutationError> {
        let override_value = std::env::var_os("GIT_INDEX_FILE");
        self.acquire_set_with_index_override(
            worktree,
            resources,
            operation_id,
            wait,
            override_value.as_deref(),
        )
    }

    /// Acquire multiple leases against one explicit active-index identity.
    pub fn acquire_set_with_index_override(
        &self,
        worktree: &Path,
        resources: &[GitMutationResource],
        operation_id: &str,
        wait: Duration,
        index_override: Option<&std::ffi::OsStr>,
    ) -> Result<GitMutationLeaseSet, GitMutationError> {
        validate_operation_id(operation_id)?;
        let paths = self.resolve_paths_with_index_override(worktree, index_override)?;
        let deadline = Instant::now() + wait;
        let mut ordered = resources.to_vec();
        ordered.sort_unstable();
        ordered.dedup();
        let mut leases = Vec::with_capacity(ordered.len());
        for resource in ordered {
            let remaining = deadline.saturating_duration_since(Instant::now());
            leases.push(self.acquire_resolved(
                paths.clone(),
                resource,
                operation_id,
                remaining,
                index_override,
            )?);
        }
        Ok(GitMutationLeaseSet { leases })
    }

    fn acquire_resolved(
        &self,
        paths: GitMutationPaths,
        resource: GitMutationResource,
        operation_id: &str,
        wait: Duration,
        index_override: Option<&std::ffi::OsStr>,
    ) -> Result<GitMutationLease, GitMutationError> {
        #[cfg(not(all(unix, not(any(target_os = "redox", target_os = "solaris")))))]
        return Err(GitMutationError::UnsupportedPlatform);

        #[cfg(all(unix, not(any(target_os = "redox", target_os = "solaris"))))]
        {
            let lease_path = resource.lease_path(&paths).to_path_buf();
            ensure_safe_lease_directory(lease_path.parent().unwrap_or(&paths.git_dir))?;
            let started = Instant::now();
            loop {
                let file = open_lease_file(&lease_path)?;
                match try_exclusive_lock(file) {
                    Ok(Some(lock)) => {
                        verify_open_path(&lock, &lease_path)?;
                        let refreshed = self.resolve_paths_with_index_override(
                            &paths.worktree_root,
                            index_override,
                        )?;
                        if refreshed != paths {
                            return Err(uncertain(
                                &paths.worktree_root,
                                "Git metadata paths changed while acquiring the mutation lease",
                            ));
                        }
                        let mut lock = lock;
                        write_lease_record(&mut lock, &paths, resource, operation_id)?;
                        return Ok(GitMutationLease {
                            _lock: lock,
                            paths,
                            resource,
                            operation_id: operation_id.to_string(),
                            index_override: index_override.map(PathBuf::from),
                        });
                    }
                    Ok(None) if started.elapsed() < wait => {
                        thread::sleep(
                            LEASE_POLL_INTERVAL.min(wait.saturating_sub(started.elapsed())),
                        );
                    }
                    Ok(None) => {
                        return Err(GitMutationError::Busy {
                            resource: resource.label(),
                            path: lease_path,
                            waited_ms: started.elapsed().as_millis(),
                        });
                    }
                    Err(error) => return Err(uncertain(&lease_path, &error.to_string())),
                }
            }
        }
    }
}

fn validate_operation_id(operation_id: &str) -> Result<(), GitMutationError> {
    if operation_id.is_empty()
        || operation_id.len() > 128
        || !operation_id.bytes().all(|byte| byte.is_ascii_graphic())
    {
        return Err(GitMutationError::InvalidOperationId);
    }
    Ok(())
}

fn git_path(
    worktree: &Path,
    argument: &str,
    index_override: Option<&std::ffi::OsStr>,
) -> Result<String, GitMutationError> {
    let mut command = Command::new("git");
    command
        .current_dir(worktree)
        .args(["rev-parse", "--path-format=absolute"]);
    if argument == "--git-path index" {
        command.args(["--git-path", "index"]);
    } else {
        command.arg(argument);
    }
    command
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .env_remove("GIT_COMMON_DIR");
    if let Some(index_override) = index_override {
        command.env("GIT_INDEX_FILE", index_override);
    } else {
        command.env_remove("GIT_INDEX_FILE");
    }
    let output = command.output().map_err(|error| {
        uncertain(
            worktree,
            &format!("cannot inspect Git paths ({})", error.kind()),
        )
    })?;
    if !output.status.success() {
        return Err(uncertain(
            worktree,
            &format!(
                "git rev-parse {argument} failed (exit {:?})",
                output.status.code()
            ),
        ));
    }
    String::from_utf8(output.stdout)
        .map(|value| value.trim().to_string())
        .map_err(|_| uncertain(worktree, "git rev-parse returned a non-UTF-8 path"))
}

fn canonical_directory(path: &Path, label: &str) -> Result<PathBuf, GitMutationError> {
    let canonical = fs::canonicalize(path)
        .map_err(|error| uncertain(path, &format!("cannot resolve {label} ({})", error.kind())))?;
    if !canonical.is_dir() {
        return Err(uncertain(path, &format!("{label} is not a directory")));
    }
    Ok(canonical)
}

fn absolute_from(base: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        base.join(path)
    }
}

fn canonicalize_leaf_parent(path: &Path, label: &str) -> Result<PathBuf, GitMutationError> {
    if !path.is_absolute() {
        return Err(uncertain(path, "Git returned a non-absolute index path"));
    }
    let parent = path
        .parent()
        .ok_or_else(|| uncertain(path, &format!("{label} has no parent directory")))?;
    let parent = fs::canonicalize(parent).map_err(|error| {
        uncertain(
            parent,
            &format!("cannot resolve {label} parent ({})", error.kind()),
        )
    })?;
    let name = path
        .file_name()
        .ok_or_else(|| uncertain(path, &format!("{label} has no file name")))?;
    Ok(parent.join(name))
}

fn validate_index_ownership(
    worktree: &Path,
    git_dir: &Path,
    common_git_dir: &Path,
    index_file: &Path,
) -> Result<(), GitMutationError> {
    if index_file.starts_with(worktree)
        || index_file.starts_with(git_dir)
        || index_file.starts_with(common_git_dir)
    {
        return Ok(());
    }
    Err(uncertain(
        index_file,
        "active index is outside the managed worktree and its Git directories",
    ))
}

fn reject_symlink(path: &Path, label: &str) -> Result<(), GitMutationError> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| uncertain(path, &format!("cannot inspect {label} ({})", error.kind())))?;
    if metadata.file_type().is_symlink() {
        return Err(uncertain(path, &format!("{label} is a symlink")));
    }
    Ok(())
}

fn reject_optional_symlink(path: &Path, label: &str) -> Result<(), GitMutationError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            Err(uncertain(path, &format!("{label} is a symlink")))
        }
        Ok(_) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(uncertain(
            path,
            &format!("cannot inspect {label} ({})", error.kind()),
        )),
    }
}

fn append_suffix(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_os_string();
    name.push(suffix);
    PathBuf::from(name)
}

fn path_digest(path: &Path) -> String {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        format!("{:x}", Sha256::digest(path.as_os_str().as_bytes()))
    }
    #[cfg(not(unix))]
    {
        format!("{:x}", Sha256::digest(path.to_string_lossy().as_bytes()))
    }
}

pub(crate) fn ensure_safe_lease_directory(path: &Path) -> Result<(), GitMutationError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            return Err(uncertain(path, "mutation lease directory is a symlink"));
        }
        Ok(metadata) if !metadata.is_dir() => {
            return Err(uncertain(path, "mutation lease path is not a directory"));
        }
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            if let Err(error) = fs::create_dir(path) {
                if error.kind() != io::ErrorKind::AlreadyExists {
                    return Err(uncertain(
                        path,
                        &format!("cannot create mutation lease directory ({})", error.kind()),
                    ));
                }
                let metadata = fs::symlink_metadata(path).map_err(|inspect| {
                    uncertain(
                        path,
                        &format!(
                            "cannot verify concurrent lease directory ({})",
                            inspect.kind()
                        ),
                    )
                })?;
                if metadata.file_type().is_symlink() || !metadata.is_dir() {
                    return Err(uncertain(
                        path,
                        "concurrent lease-directory creation produced an unsafe path",
                    ));
                }
            }
        }
        Err(error) => {
            return Err(uncertain(
                path,
                &format!("cannot inspect mutation lease directory ({})", error.kind()),
            ));
        }
    }
    let canonical = fs::canonicalize(path).map_err(|error| {
        uncertain(
            path,
            &format!("cannot verify lease directory ({})", error.kind()),
        )
    })?;
    if canonical != path {
        return Err(uncertain(
            path,
            "mutation lease directory path changed during inspection",
        ));
    }
    Ok(())
}

fn open_lease_file(path: &Path) -> Result<File, GitMutationError> {
    reject_optional_symlink(path, "mutation lease file")?;
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true).truncate(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    }
    let file = options.open(path).map_err(|error| {
        uncertain(
            path,
            &format!("cannot open mutation lease ({})", error.kind()),
        )
    })?;
    verify_open_path(&file, path)?;
    Ok(file)
}

fn verify_open_path(file: &File, path: &Path) -> Result<(), GitMutationError> {
    let file_metadata = file.metadata().map_err(|error| {
        uncertain(
            path,
            &format!("cannot inspect opened lease ({})", error.kind()),
        )
    })?;
    let path_metadata = fs::symlink_metadata(path)
        .map_err(|error| uncertain(path, &format!("lease path changed ({})", error.kind())))?;
    if !file_metadata.is_file() || path_metadata.file_type().is_symlink() {
        return Err(uncertain(path, "mutation lease is not a regular file"));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if file_metadata.dev() != path_metadata.dev() || file_metadata.ino() != path_metadata.ino()
        {
            return Err(uncertain(
                path,
                "mutation lease inode changed during inspection",
            ));
        }
    }
    Ok(())
}

#[cfg(all(unix, not(any(target_os = "redox", target_os = "solaris"))))]
fn try_exclusive_lock(file: File) -> Result<Option<NativeLeaseLock>, io::Error> {
    match Flock::lock(file, FlockArg::LockExclusiveNonblock) {
        Ok(lock) => Ok(Some(lock)),
        Err((_file, error)) if error == Errno::EAGAIN || error == Errno::EWOULDBLOCK => Ok(None),
        Err((_file, error)) => Err(io::Error::from_raw_os_error(error as i32)),
    }
}

#[cfg(not(all(unix, not(any(target_os = "redox", target_os = "solaris")))))]
fn try_exclusive_lock(_file: File) -> Result<Option<NativeLeaseLock>, io::Error> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "coordinated Git mutation leases are unsupported on this platform",
    ))
}

#[cfg(all(unix, not(any(target_os = "redox", target_os = "solaris"))))]
fn write_lease_record(
    lock: &mut NativeLeaseLock,
    paths: &GitMutationPaths,
    resource: GitMutationResource,
    operation_id: &str,
) -> Result<(), GitMutationError> {
    let record = LeaseRecord {
        schema_version: LEASE_SCHEMA_VERSION,
        operation_id_sha256: format!("{:x}", Sha256::digest(operation_id.as_bytes())),
        resource: resource.label(),
        resource_sha256: path_digest(resource.lease_path(paths)),
        owner_pid: std::process::id(),
        owner_start_identity: process_start_identity(std::process::id()),
    };
    let bytes = serde_json::to_vec(&record)
        .map_err(|_| uncertain(resource.lease_path(paths), "cannot encode lease identity"))?;
    let file = lock;
    file.set_len(0)
        .and_then(|()| file.seek(SeekFrom::Start(0)))
        .and_then(|_| file.write_all(&bytes))
        .and_then(|()| file.sync_data())
        .map_err(|error| {
            uncertain(
                resource.lease_path(paths),
                &format!("cannot persist lease identity ({})", error.kind()),
            )
        })
}

fn process_start_identity(pid: u32) -> Option<u64> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let fields = stat
        .rsplit_once(')')?
        .1
        .split_whitespace()
        .collect::<Vec<_>>();
    fields.get(19)?.parse().ok()
}

fn uncertain(path: &Path, reason: &str) -> GitMutationError {
    GitMutationError::Uncertain {
        path: path.to_path_buf(),
        reason: reason.to_string(),
    }
}

#[cfg(test)]
#[path = "git_mutation_tests.rs"]
mod tests;
