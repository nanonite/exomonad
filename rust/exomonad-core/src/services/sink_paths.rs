//! Verified project-owned sink destinations.
//!
//! Telemetry, logging, ledger, and health sinks must never create a planned or
//! residue worktree directory. Every sink resolves its destination here, at the
//! moment of the write, and falls back to the project-owned directory unless
//! the candidate is a live registered worktree of the same repository.
//!
//! Resolution happens under the project-scoped
//! [`worktree_lifecycle`](super::worktree_lifecycle) lock, held from
//! verification through the write. A cleanup pass takes that lock exclusively,
//! so a sink can never verify a worktree that cleanup is about to quarantine.
//! When the shared lock cannot be taken the sink writes to the project-owned
//! directory, never to the candidate worktree path.

use std::path::{Path, PathBuf};

use super::git_worktree::GitWorktreeService;
use super::worktree_lifecycle::{LifecycleGuard, LifecycleMode, SINK_TIMEOUT};

/// A resolved sink destination and the lifecycle lock that guards it.
///
/// The lock is held for as long as the value lives, so keep it in scope for the
/// whole write and drop it before any unrelated wait.
#[derive(Debug)]
pub(crate) struct SinkDestination {
    pub dir: PathBuf,
    _lifecycle: Option<LifecycleGuard>,
}

/// Return the candidate only when it is a live Git worktree registered to this
/// repository; otherwise return the project-owned directory so the caller
/// cannot materialize a planned or sink-created worktree path.
fn verified_candidate(project_root: &Path, candidate: &Path) -> PathBuf {
    if candidate == project_root {
        return project_root.to_path_buf();
    }
    let git_wt = GitWorktreeService::new(project_root.to_path_buf());
    match git_wt.is_registered_worktree(candidate) {
        Ok(true) => candidate.to_path_buf(),
        _ => project_root.to_path_buf(),
    }
}

/// Resolve the directory a sink should write into, under the shared lifecycle
/// lock.
///
/// The caller must hold the returned destination for the whole write: dropping it
/// releases the lock before the bytes land.
pub(crate) fn resolve_sink(project_root: &Path, candidate: &Path) -> SinkDestination {
    // The project-owned directory is never created, removed, or quarantined by a
    // lifecycle decision, so resolving to it needs no lock.
    if candidate == project_root {
        return SinkDestination {
            dir: project_root.to_path_buf(),
            _lifecycle: None,
        };
    }
    let lifecycle = match LifecycleGuard::try_acquire(
        project_root,
        LifecycleMode::Shared,
        SINK_TIMEOUT,
    ) {
        Ok(Some(lifecycle)) => Some(lifecycle),
        Ok(None) => {
            tracing::warn!(
                project_root = %project_root.display(),
                candidate = %candidate.display(),
                "worktree lifecycle lock is held; writing the sink record to the project-owned directory"
            );
            None
        }
        Err(error) => {
            tracing::error!(
                project_root = %project_root.display(),
                candidate = %candidate.display(),
                %error,
                "worktree lifecycle lock could not be taken; writing the sink record to the project-owned directory"
            );
            None
        }
    };
    let dir = match &lifecycle {
        Some(_) => verified_candidate(project_root, candidate),
        // Without the lock the worktree may be quarantined mid-write, so the
        // candidate is never a legal destination.
        None => project_root.to_path_buf(),
    };
    SinkDestination {
        dir,
        _lifecycle: lifecycle,
    }
}

/// Resolve the destination hint carried by an inbox message.
///
/// The hint is advisory: the write boundary re-resolves under the lifecycle
/// lock, so a planned worktree that appeared or vanished in the meantime can
/// never be written to.
pub(crate) fn sink_dir_hint(project_root: &Path, candidate: &Path) -> PathBuf {
    verified_candidate(project_root, candidate)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::BranchName;
    use std::time::Duration;
    use tempfile::TempDir;

    fn init_sink_repository() -> (tempfile::TempDir, PathBuf) {
        let temp = TempDir::new().unwrap();
        let project = temp.path().to_path_buf();
        exomonad_test_support::init_fixture_git_repository(&project).unwrap();
        exomonad_test_support::run_fixture_git_command(
            &project,
            &["config", "user.email", "sink@example.invalid"],
        )
        .unwrap();
        exomonad_test_support::run_fixture_git_command(
            &project,
            &["config", "user.name", "Sink Test"],
        )
        .unwrap();
        exomonad_test_support::run_fixture_git_command(
            &project,
            &["commit", "--allow-empty", "-m", "initial"],
        )
        .unwrap();
        (temp, project)
    }

    fn default_branch(project: &Path) -> String {
        let output =
            exomonad_test_support::run_fixture_git_command(project, &["branch", "--show-current"])
                .unwrap();
        String::from_utf8_lossy(&output.stdout).trim().to_string()
    }

    #[test]
    fn project_owned_destination_needs_no_lifecycle_lock() {
        let temp = TempDir::new().unwrap();
        let sink = resolve_sink(temp.path(), temp.path());
        assert_eq!(sink.dir, temp.path());
        assert!(sink._lifecycle.is_none());
    }

    #[test]
    fn an_exclusive_holder_forces_the_project_owned_destination() -> anyhow::Result<()> {
        let temp = TempDir::new()?;
        let candidate = temp.path().join(".exo/worktrees/leaf");
        let _held =
            LifecycleGuard::try_acquire(temp.path(), LifecycleMode::Exclusive, Duration::ZERO)?
                .expect("the test holds the lifecycle lock");
        let sink = resolve_sink(temp.path(), &candidate);
        assert!(sink._lifecycle.is_none());
        assert_eq!(sink.dir, temp.path());
        assert!(
            !candidate.exists(),
            "resolution must not create the candidate"
        );
        Ok(())
    }

    #[test]
    fn a_free_lock_selects_a_registered_worktree() -> anyhow::Result<()> {
        let (_temp, project) = init_sink_repository();
        let worktree = project.join(".exo/worktrees/leaf");
        let branch_name = default_branch(&project);
        let branch = BranchName::try_from_str(format!("{branch_name}.leaf").as_str())?;
        let base = BranchName::try_from_str(branch_name.as_str())?;
        GitWorktreeService::new(project.clone()).create_workspace(&worktree, &branch, &base)?;

        let sink = resolve_sink(&project, &worktree);
        assert!(sink._lifecycle.is_some());
        assert_eq!(sink.dir, worktree);
        Ok(())
    }

    #[test]
    fn a_free_lock_still_rejects_a_planned_worktree() -> anyhow::Result<()> {
        let (_temp, project) = init_sink_repository();
        let planned = project.join(".exo/worktrees/leaf");

        let sink = resolve_sink(&project, &planned);
        assert!(sink._lifecycle.is_some());
        assert_eq!(sink.dir, project);
        assert!(
            !planned.exists(),
            "resolution must not create the candidate"
        );
        Ok(())
    }
}
