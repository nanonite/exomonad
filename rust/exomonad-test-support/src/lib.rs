//! Shared test scaffolding for the ExoMonad workspace.
//!
//! This unpublished crate is consumed only as a dev-dependency.

use std::path::{Path, PathBuf};

/// Return the git repository root containing `path`, if any.
pub fn git_repository_root(path: &Path) -> Option<PathBuf> {
    let output = std::process::Command::new("git")
        .args(["-C"])
        .arg(path)
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .expect("failed to run git repository detection");
    if output.status.success() {
        return Some(PathBuf::from(
            String::from_utf8_lossy(&output.stdout).trim(),
        ));
    }
    None
}

/// Create a temp directory outside any git repository.
pub fn tempdir_outside_any_repo() -> tempfile::TempDir {
    let mut rejected = Vec::new();
    let candidates = [
        tempfile::tempdir(),
        tempfile::tempdir_in("/tmp"),
        tempfile::tempdir_in("/var/tmp"),
    ];

    for result in candidates {
        let Ok(dir) = result else {
            continue;
        };
        if let Some(repo) = git_repository_root(dir.path()) {
            rejected.push(format!(
                "{} (found .git at {})",
                dir.path().display(),
                repo.display()
            ));
            continue;
        }
        return dir;
    }

    panic!(
        "failed to create a test directory outside any git repository; rejected={rejected:?}, TMPDIR={:?}",
        std::env::var("TMPDIR").ok(),
    );
}
