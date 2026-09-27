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
//!
//! Every attempt is non-blocking, so a contended acquisition spends its whole
//! timeout waiting rather than deciding. That wait must not block a runtime
//! worker, so [`LifecycleGuard::try_acquire`] yields to the runtime between
//! attempts and every caller uses it. [`LifecycleAcquisition`] is the single
//! acquisition implementation, so the decision — which mode, which deadline,
//! what an error means — is made in exactly one place.

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

/// A bounded acquisition in progress.
///
/// This is the single acquisition implementation: it owns the open lock file,
/// the mode, and the deadline, and it is the only code that interprets an
/// attempt. Callers differ only in how they wait between two attempts, which
/// the entry points on [`LifecycleGuard`] do.
#[derive(Debug)]
struct LifecycleAcquisition {
    /// The open lock file, `None` once it has been handed to a guard.
    file: Option<std::fs::File>,
    mode: FlockArg,
    lock_path: PathBuf,
    deadline: Instant,
}

impl LifecycleAcquisition {
    /// Open the project-scoped lock file and arm the deadline.
    fn start(project_root: &Path, mode: LifecycleMode, timeout: Duration) -> Result<Self> {
        let lock_path = LifecycleGuard::lock_path(project_root);
        let Some(parent) = lock_path.parent() else {
            return Err(anyhow::anyhow!(
                "lifecycle lock path {} has no parent directory",
                lock_path.display()
            ));
        };
        fs::create_dir_all(parent)
            .with_context(|| format!("create lifecycle lock directory {}", parent.display()))?;
        let file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(false)
            .open(&lock_path)
            .with_context(|| format!("open {}", lock_path.display()))?;
        let mode = match mode {
            LifecycleMode::Shared => FlockArg::LockSharedNonblock,
            LifecycleMode::Exclusive => FlockArg::LockExclusiveNonblock,
        };
        Ok(Self {
            file: Some(file),
            mode,
            lock_path,
            deadline: Instant::now() + timeout,
        })
    }

    /// Make one non-blocking attempt.
    ///
    /// `Ok(Some(guard))` is the acquired lock. `Ok(None)` means another caller
    /// holds it and the acquisition may continue; a hard error stops the
    /// acquisition and is surfaced.
    fn attempt(&mut self) -> Result<Option<LifecycleGuard>> {
        let Some(file) = self.file.take() else {
            return Err(anyhow::anyhow!(
                "lifecycle lock {} is no longer open",
                self.lock_path.display()
            ));
        };
        match Flock::lock(file, self.mode) {
            Ok(lock) => Ok(Some(LifecycleGuard { _lock: lock })),
            Err((returned, Errno::EWOULDBLOCK)) => {
                self.file = Some(returned);
                Ok(None)
            }
            Err((_returned, errno)) => Err(anyhow::anyhow!(
                "lock {}: {errno}",
                self.lock_path.display()
            )),
        }
    }

    /// Whether the timeout has not yet elapsed, so another attempt is allowed.
    fn may_retry(&self) -> bool {
        Instant::now() < self.deadline
    }
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
    /// single non-blocking attempt, which is how a test takes a lock in place of
    /// another process.
    ///
    /// Async because the wait between two non-blocking attempts yields to the
    /// runtime: a contended acquisition of `DECISION_TIMEOUT` must not park the
    /// worker it was awaited on. [`LifecycleAcquisition`] owns the decision.
    pub(crate) async fn try_acquire(
        project_root: &Path,
        mode: LifecycleMode,
        timeout: Duration,
    ) -> Result<Option<Self>> {
        let mut acquisition = LifecycleAcquisition::start(project_root, mode, timeout)?;
        loop {
            if let Some(guard) = acquisition.attempt()? {
                return Ok(Some(guard));
            }
            if !acquisition.may_retry() {
                return Ok(None);
            }
            tokio::time::sleep(RETRY_INTERVAL).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use tempfile::TempDir;
    use tokio::sync::oneshot;

    #[tokio::test]
    async fn exclusive_holder_blocks_every_other_mode() -> Result<()> {
        let temp = TempDir::new()?;
        let project = temp.path();
        let held = LifecycleGuard::try_acquire(project, LifecycleMode::Exclusive, Duration::ZERO)
            .await?
            .expect("first exclusive acquisition is uncontended");
        assert!(
            LifecycleGuard::try_acquire(project, LifecycleMode::Shared, Duration::ZERO)
                .await?
                .is_none()
        );
        assert!(
            LifecycleGuard::try_acquire(project, LifecycleMode::Exclusive, Duration::ZERO)
                .await?
                .is_none()
        );
        drop(held);
        assert!(
            LifecycleGuard::try_acquire(project, LifecycleMode::Shared, Duration::ZERO)
                .await?
                .is_some()
        );
        Ok(())
    }

    #[tokio::test]
    async fn shared_holders_are_compatible_and_block_exclusive() -> Result<()> {
        let temp = TempDir::new()?;
        let project = temp.path();
        let first = LifecycleGuard::try_acquire(project, LifecycleMode::Shared, Duration::ZERO)
            .await?
            .expect("first shared acquisition is uncontended");
        let second = LifecycleGuard::try_acquire(project, LifecycleMode::Shared, Duration::ZERO)
            .await?
            .expect("shared locks are compatible with each other");
        assert!(
            LifecycleGuard::try_acquire(project, LifecycleMode::Exclusive, Duration::ZERO)
                .await?
                .is_none()
        );
        drop(first);
        assert!(
            LifecycleGuard::try_acquire(project, LifecycleMode::Exclusive, Duration::ZERO)
                .await?
                .is_none()
        );
        drop(second);
        assert!(
            LifecycleGuard::try_acquire(project, LifecycleMode::Exclusive, Duration::ZERO)
                .await?
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

    /// A contended acquisition must yield between attempts, so a runtime worker
    /// is never parked for the length of the timeout.
    ///
    /// The runtime is `current_thread`, so the only way the ticker can run at all
    /// is if the acquisition reaches a suspension point. The ticker is released
    /// on the same turn that starts the acquisition, so it has run nothing before
    /// the acquisition begins, and the counter is read before the ticker is
    /// awaited. A blocking wait would freeze the single worker for the whole
    /// `SINK_TIMEOUT` and the counter would still be zero. No wall-clock duration
    /// is asserted, only that the other task made progress and that the
    /// acquisition still failed closed.
    #[tokio::test(flavor = "current_thread")]
    async fn a_contended_acquisition_does_not_block_the_runtime() -> Result<()> {
        let temp = TempDir::new()?;
        let project = temp.path();
        let _held = LifecycleGuard::try_acquire(project, LifecycleMode::Exclusive, Duration::ZERO)
            .await?
            .expect("the test holds the exclusive lifecycle lock");

        let progress = Arc::new(AtomicUsize::new(0));
        let (parked_tx, parked_rx) = oneshot::channel();
        let (release_tx, release_rx) = oneshot::channel();
        let ticker = tokio::spawn({
            let progress = Arc::clone(&progress);
            async move {
                parked_tx.send(()).ok();
                release_rx.await.ok();
                // Bounded, so the ready queue drains and the acquisition's own
                // timer can still fire.
                for _ in 0..4 {
                    tokio::task::yield_now().await;
                    progress.fetch_add(1, Ordering::SeqCst);
                }
            }
        });
        parked_rx.await?;
        let before = progress.load(Ordering::SeqCst);

        // Releasing the ticker and awaiting the acquisition happen in the same
        // turn, so the ticker cannot have run before the acquisition starts.
        release_tx.send(()).ok();
        let outcome =
            LifecycleGuard::try_acquire(project, LifecycleMode::Shared, SINK_TIMEOUT).await?;
        let observed = progress.load(Ordering::SeqCst);
        ticker.await?;

        assert!(
            outcome.is_none(),
            "a contended acquisition fails closed instead of waiting forever"
        );
        assert!(
            observed > before,
            "another task must make progress while a contended acquisition waits"
        );
        Ok(())
    }
}
