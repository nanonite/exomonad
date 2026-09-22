//! Verified project-owned sink destinations.
//!
//! Telemetry, logging, ledger, and health sinks must never create a planned or
//! residue worktree directory. Every sink resolves its destination here, at the
//! moment of the write, and falls back to the project-owned directory unless
//! the candidate is a live registered worktree of the same repository.

use std::path::{Path, PathBuf};

use super::git_worktree::GitWorktreeService;

/// Resolve the directory a sink should write into.
///
/// `candidate` is used only when it is a live Git worktree registered to this
/// repository; otherwise `project_dir` is used so the write cannot materialize a
/// planned or sink-created worktree path.
pub(crate) fn sink_project_dir(project_dir: &Path, candidate: PathBuf) -> PathBuf {
    let git_wt = GitWorktreeService::new(project_dir.to_path_buf());
    match git_wt.is_registered_worktree(&candidate) {
        Ok(true) => candidate,
        _ => project_dir.to_path_buf(),
    }
}
