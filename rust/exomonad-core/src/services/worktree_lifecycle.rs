//! Project-scoped advisory lock for worktree lifecycle decisions.
//!
//! Worktree ownership has two writers: spawn, which creates or attaches a leaf
//! worktree, and residue cleanup, which quarantines a directory it has proven
//! disposable. Both derive their decision from the Git worktree registry and
//! the on-disk tree, so a decision taken from a view that a concurrent writer
//! has already invalidated can destroy a live worktree. Event sinks read the
//! same state and must not resolve a destination while a decision is in flight.
//!
//! The lock lives at `.exo/worktree-lifecycle.lock` inside the project and is
//! taken with `flock(2)`, the primitive the sink health, event log, and ledger
//! writers already use:
//!
//! - Exclusive across a worktree create/attach decision and across a residue
//!   cleanup pass, so neither can interleave with the other.
//! - Shared across a sink verification and its write, so cleanup drains
//!   in-flight writers instead of quarantining a directory they write into.
//!
//! Acquisition is bounded. A sink that cannot take the shared lock writes to the
//! project-owned sink directory and never to a candidate worktree path; a
//! cleanup that cannot take the exclusive lock skips the pass and leaves residue
//! untouched; a spawn that cannot take it fails closed. No caller waits
//! indefinitely and no caller proceeds on an unverified view.

use anyhow::{Context, Result};
use nix::errno::Errno;
use nix::fcntl::{Flock, FlockArg};
use std::fs::{self, OpenOptions};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

/// Lock file location, relative to the project root.
pub(crate) const LOCK_RELATIVE_PATH: &str = ".exo/worktree-lifecycle.lock";

/// Bounded wait for the shared sink lock.
///
/// A sink write is on the delivery path, so it waits briefly and then writes to
/// the project-owned directory rather than holding up a message.
pub(crate) const SINK_TIMEOUT: Duration = Duration::from_millis(250);

/// Bounded wait for the exclusive lifecycle lock.
///
/// Only a create/attach decision or a cleanup pass takes this lock, and both
/// fail closed on timeout, so the wait can be longer than a sink's without
/// blocking delivery.
pub(crate) const DECISION_TIMEOUT: Duration = Duration::from_millis(2_000);

/// Interval between bounded acquisition attempts.
const RETRY_INTERVAL: Duration = Duration::from_millis(5);

/// Which side of the lifecycle decision a caller is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum LifecycleMode {
    /// A sink verifying its destination and writing to it.
    Shared,
    /// A worktree create/attach decision or a residue cleanup pass.
    Exclusive,
}

/// Held lifecycle lock. The underlying `flock` is released on drop.
#[derive(Debug)]
pub(crate) struct LifecycleGuard {
    _lock: Flock<std::fs::File>,
}

impl LifecycleGuard {
    /// Path of the project-scoped lock file.
    pub(crate) fn lock_path(project_root: &Path) -> PathBuf {
        project_root.join(LOCK_RELATIVE_PATH)
    }

    /// Try to take the lifecycle lock, waiting at most `timeout`.
    ///
    /// Returns `Ok(None)` when the lock is still held by another caller after
    /// the wait; the caller must then fail closed. A `timeout` of zero makes a
    /// single non-blocking attempt.
    pub(crate) fn try_acquire(
        project_root: &Path,
        mode: LifecycleMode,
        timeout: Duration,
    ) -> Result<Option<Self>> {
        let lock_path = Self::lock_path(project_root);
        let Some(parent) = lock_path.parent() else {
            return Err(anyhow::anyhow!(
                "lifecycle lock path {} has no parent directory",
                lock_path.display()
            ));
        };
        fs::create_dir_all(parent)
            .with_context(|| format!("create lifecycle lock directory {}", parent.display()))?;
        let mut file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(false)
            .open(&lock_path)
            .with_context(|| format!("open {}", lock_path.display()))?;
        let nonblocking = match mode {
            LifecycleMode::Shared => FlockArg::LockSharedNonblock,
            LifecycleMode::Exclusive => FlockArg::LockExclusiveNonblock,
        };
        let deadline = Instant::now() + timeout;
        loop {
            match Flock::lock(file, nonblocking) {
                Ok(lock) => return Ok(Some(Self { _lock: lock })),
                Err((returned, Errno::EWOULDBLOCK)) => {
                    file = returned;
                    if Instant::now() >= deadline {
                        return Ok(None);
                    }
                    std::thread::sleep(RETRY_INTERVAL);
                }
                Err((_returned, errno)) => {
                    return Err(anyhow::anyhow!("lock {}: {errno}", lock_path.display()));
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn exclusive_holder_blocks_every_other_mode() -> Result<()> {
        let temp = TempDir::new()?;
        let project = temp.path();
        let held = LifecycleGuard::try_acquire(project, LifecycleMode::Exclusive, Duration::ZERO)?
            .expect("first exclusive acquisition is uncontended");
        assert!(
            LifecycleGuard::try_acquire(project, LifecycleMode::Shared, Duration::ZERO)?.is_none()
        );
        assert!(
            LifecycleGuard::try_acquire(project, LifecycleMode::Exclusive, Duration::ZERO)?
                .is_none()
        );
        drop(held);
        assert!(
            LifecycleGuard::try_acquire(project, LifecycleMode::Shared, Duration::ZERO)?.is_some()
        );
        Ok(())
    }

    #[test]
    fn shared_holders_are_compatible_and_block_exclusive() -> Result<()> {
        let temp = TempDir::new()?;
        let project = temp.path();
        let first = LifecycleGuard::try_acquire(project, LifecycleMode::Shared, Duration::ZERO)?
            .expect("first shared acquisition is uncontended");
        let second = LifecycleGuard::try_acquire(project, LifecycleMode::Shared, Duration::ZERO)?
            .expect("shared locks are compatible with each other");
        assert!(
            LifecycleGuard::try_acquire(project, LifecycleMode::Exclusive, Duration::ZERO)?
                .is_none()
        );
        drop(first);
        assert!(
            LifecycleGuard::try_acquire(project, LifecycleMode::Exclusive, Duration::ZERO)?
                .is_none()
        );
        drop(second);
        assert!(
            LifecycleGuard::try_acquire(project, LifecycleMode::Exclusive, Duration::ZERO)?
                .is_some()
        );
        Ok(())
    }

    #[test]
    fn lock_file_lives_in_the_project_exo_directory() {
        let temp = TempDir::new().unwrap();
        let lock_path = LifecycleGuard::lock_path(temp.path());
        assert_eq!(lock_path, temp.path().join(LOCK_RELATIVE_PATH));
    }
}
