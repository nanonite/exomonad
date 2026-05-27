//! Git worktree management service.
//!
//! All operations shell out to git.

use crate::domain::BranchName;
use crate::effects::EffectError;
use std::path::{Path, PathBuf};
use thiserror::Error;
use tracing::{error, info, warn};

/// Custom error type for git worktree operations.
#[derive(Debug, Error)]
pub enum WorktreeError {
    #[error("Branch already exists: {branch}")]
    BranchExists { branch: String },
    #[error("Path already exists: {path}")]
    PathExists { path: String },
    #[error("Base branch not found: {branch}")]
    BaseBranchNotFound { branch: String },
    #[error("Git lock file conflict: {message}")]
    LockFileConflict { message: String },
    #[error("Push rejected (non-fast-forward?): {message}")]
    PushRejected { message: String },
    #[error("Git error: {message}")]
    GitError { message: String },
}

impl From<WorktreeError> for EffectError {
    fn from(err: WorktreeError) -> Self {
        match err {
            WorktreeError::BranchExists { branch } => EffectError::custom(
                "worktree.branch_exists",
                format!("Branch already exists: {}", branch),
            ),
            WorktreeError::PathExists { path } => EffectError::custom(
                "worktree.path_exists",
                format!("Path already exists: {}", path),
            ),
            WorktreeError::BaseBranchNotFound { branch } => EffectError::custom(
                "worktree.base_branch_not_found",
                format!("Base branch not found: {}", branch),
            ),
            WorktreeError::LockFileConflict { message } => {
                EffectError::custom("worktree.lock_conflict", message)
            }
            WorktreeError::PushRejected { message } => {
                EffectError::custom("worktree.push_rejected", message)
            }
            WorktreeError::GitError { message } => {
                EffectError::custom("worktree.git_error", message)
            }
        }
    }
}

/// Service for git worktree operations via git CLI.
pub struct GitWorktreeService {
    project_dir: PathBuf,
}

pub(crate) fn headless_git_command() -> std::process::Command {
    let mut command = std::process::Command::new("git");
    apply_headless_git_env(&mut command);
    command
}

pub(crate) fn apply_headless_git_env(
    command: &mut std::process::Command,
) -> &mut std::process::Command {
    command
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GCM_INTERACTIVE", "never")
        .env("GIT_ASKPASS", "")
        .env("SSH_ASKPASS", "")
        .env("SSH_ASKPASS_REQUIRE", "never");

    command.env("GIT_SSH_COMMAND", headless_git_ssh_command());

    command
}

fn headless_git_ssh_command() -> String {
    let existing = std::env::var("GIT_SSH_COMMAND").unwrap_or_else(|_| "ssh".to_string());
    if existing.contains("BatchMode") {
        existing
    } else {
        format!("{existing} -o BatchMode=yes")
    }
}

impl GitWorktreeService {
    pub fn new(project_dir: PathBuf) -> Self {
        Self { project_dir }
    }

    /// Create a new git worktree with a new branch based on a given base.
    ///
    /// Equivalent to: `git worktree add -b {branch} {path} {base}`
    pub fn create_workspace(
        &self,
        path: &Path,
        branch: &BranchName,
        base: &BranchName,
    ) -> Result<(), WorktreeError> {
        info!(path = %path.display(), branch = %branch, base = %base, "Creating git worktree");

        let output = headless_git_command()
            .args([
                "worktree",
                "add",
                "-b",
                branch.as_str(),
                &path.to_string_lossy(),
                base.as_str(),
            ])
            .current_dir(&self.project_dir)
            .output()
            .map_err(|e| WorktreeError::GitError {
                message: format!("Failed to run git worktree add: {}", e),
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            error!(stderr = %stderr, "git worktree add failed");
            return Err(self.parse_git_stderr(&stderr));
        }

        info!(path = %path.display(), branch = %branch, "Worktree created successfully");

        // Set per-agent git identity so commits are attributed to the agent.
        // The last dot-segment of the birth-branch is the canonical agent name.
        let agent_name = branch
            .as_str()
            .rsplit('.')
            .next()
            .unwrap_or(branch.as_str());
        let git_user_name = format!("exomonad-{}", agent_name);
        let git_user_email = format!("{}@exomonad.local", agent_name);

        for (key, value) in [
            ("user.name", git_user_name.as_str()),
            ("user.email", git_user_email.as_str()),
        ] {
            let out = headless_git_command()
                .args(["config", "--local", key, value])
                .current_dir(path)
                .output()
                .map_err(|e| WorktreeError::GitError {
                    message: format!("Failed to set git config {}: {}", key, e),
                })?;
            if !out.status.success() {
                let stderr = String::from_utf8_lossy(&out.stderr);
                error!(key, stderr = %stderr, "git config --local failed");
                return Err(WorktreeError::GitError {
                    message: format!("git config --local {} failed: {}", key, stderr),
                });
            }
        }
        info!(user_name = %git_user_name, user_email = %git_user_email, "Set worktree git identity");

        Ok(())
    }

    /// Create a detached-HEAD worktree at the tip of an existing branch or ref.
    ///
    /// Unlike `create_workspace`, this does not create a new branch — the
    /// worktree is in detached HEAD state. Used for read-only agents (reviewers)
    /// that need the same code as a worker without competing for the branch.
    pub fn create_workspace_detached(
        &self,
        path: &Path,
        at_ref: &str,
        identity_name: &str,
    ) -> Result<(), WorktreeError> {
        info!(path = %path.display(), at_ref, "Creating detached reviewer worktree");

        let output = headless_git_command()
            .args([
                "worktree",
                "add",
                "--detach",
                &path.to_string_lossy(),
                at_ref,
            ])
            .current_dir(&self.project_dir)
            .output()
            .map_err(|e| WorktreeError::GitError {
                message: format!("Failed to run git worktree add --detach: {}", e),
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            error!(stderr = %stderr, "git worktree add --detach failed");
            return Err(self.parse_git_stderr(&stderr));
        }

        let git_user_name = format!("exomonad-{}", identity_name);
        let git_user_email = format!("{}@exomonad.local", identity_name);
        for (key, value) in [
            ("user.name", git_user_name.as_str()),
            ("user.email", git_user_email.as_str()),
        ] {
            let out = headless_git_command()
                .args(["config", "--local", key, value])
                .current_dir(path)
                .output()
                .map_err(|e| WorktreeError::GitError {
                    message: format!("Failed to set git config {}: {}", key, e),
                })?;
            if !out.status.success() {
                warn!(key, stderr = %String::from_utf8_lossy(&out.stderr), "git config --local failed in reviewer worktree (non-fatal)");
            }
        }
        info!(path = %path.display(), at_ref, "Reviewer worktree created (detached HEAD)");
        Ok(())
    }

    /// Remove a git worktree.
    ///
    /// Equivalent to: `git worktree remove --force {path}`
    pub fn remove_workspace(&self, path: &Path) -> Result<(), WorktreeError> {
        info!(path = %path.display(), "Removing git worktree");

        let output = headless_git_command()
            .args(["worktree", "remove", "--force", &path.to_string_lossy()])
            .current_dir(&self.project_dir)
            .output()
            .map_err(|e| WorktreeError::GitError {
                message: format!("Failed to run git worktree remove: {}", e),
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            // If the worktree dir doesn't exist, git worktree remove fails — clean up manually
            if path.exists() {
                warn!(stderr = %stderr, "git worktree remove failed, removing directory manually");
                std::fs::remove_dir_all(path).map_err(|e| WorktreeError::GitError {
                    message: format!("Failed to remove worktree dir {}: {}", path.display(), e),
                })?;
            } else {
                warn!(stderr = %stderr, "git worktree remove failed (directory already gone)");
            }
            // Also prune stale worktree entries
            let _ = headless_git_command()
                .args(["worktree", "prune"])
                .current_dir(&self.project_dir)
                .output();
        }

        info!(path = %path.display(), "Worktree removed");
        Ok(())
    }

    /// Push a branch to the remote.
    ///
    /// Equivalent to: `git push origin {branch}` (run in workspace_path)
    pub fn push_bookmark(
        &self,
        workspace_path: &Path,
        branch: &BranchName,
    ) -> Result<(), WorktreeError> {
        info!(branch = %branch, path = %workspace_path.display(), "Pushing branch");

        let output = headless_git_command()
            .args(["push", "origin", branch.as_str()])
            .current_dir(workspace_path)
            .output()
            .map_err(|e| WorktreeError::GitError {
                message: format!("Failed to run git push: {}", e),
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            error!(stderr = %stderr, "git push failed");
            return Err(self.parse_git_stderr(&stderr));
        }

        info!(branch = %branch, "Branch pushed successfully");
        Ok(())
    }

    /// Push a branch to a named remote.
    ///
    /// Equivalent to: `git push {remote} {branch}` (run in workspace_path)
    pub fn push_to_remote(
        &self,
        workspace_path: &Path,
        branch: &BranchName,
        remote: &str,
    ) -> Result<(), WorktreeError> {
        info!(branch = %branch, remote = %remote, path = %workspace_path.display(), "Pushing branch to remote");

        let output = headless_git_command()
            .args(["push", remote, branch.as_str()])
            .current_dir(workspace_path)
            .output()
            .map_err(|e| WorktreeError::GitError {
                message: format!("Failed to run git push: {}", e),
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            error!(stderr = %stderr, "git push failed");
            return Err(self.parse_git_stderr(&stderr));
        }

        info!(branch = %branch, remote = %remote, "Branch pushed successfully");
        Ok(())
    }

    /// Fetch from remote.
    ///
    /// Equivalent to: `git fetch` (run in workspace_path)
    pub fn fetch(&self, workspace_path: &Path) -> Result<(), WorktreeError> {
        info!(path = %workspace_path.display(), "git fetch");

        let output = headless_git_command()
            .args(["fetch"])
            .current_dir(workspace_path)
            .output()
            .map_err(|e| WorktreeError::GitError {
                message: format!("Failed to run git fetch: {}", e),
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            error!(stderr = %stderr, "git fetch failed");
            return Err(self.parse_git_stderr(&stderr));
        }

        info!("git fetch succeeded");
        Ok(())
    }

    /// Get the current branch name in a workspace.
    ///
    /// Equivalent to: `git rev-parse --abbrev-ref HEAD`
    pub fn get_workspace_bookmark(
        &self,
        workspace_path: &Path,
    ) -> Result<Option<String>, WorktreeError> {
        let output = headless_git_command()
            .args(["branch", "--show-current"])
            .current_dir(workspace_path)
            .output()
            .map_err(|e| WorktreeError::GitError {
                message: format!("Failed to run git rev-parse: {}", e),
            })?;

        if output.status.success() {
            let branch = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if branch != "HEAD" && !branch.is_empty() {
                return Ok(Some(branch));
            }
        }
        Ok(None)
    }

    /// Delete a local branch.
    ///
    /// Equivalent to: `git branch -D {name}` (from project_dir)
    pub fn delete_bookmark(&self, name: &BranchName) -> Result<(), WorktreeError> {
        info!(branch = %name, "Deleting local branch");

        let output = headless_git_command()
            .args(["branch", "-D", name.as_str()])
            .current_dir(&self.project_dir)
            .output()
            .map_err(|e| WorktreeError::GitError {
                message: format!("Failed to run git branch -D: {}", e),
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            error!(stderr = %stderr, "git branch -D failed");
            return Err(self.parse_git_stderr(&stderr));
        }

        info!(branch = %name, "Branch deleted");
        Ok(())
    }

    /// Create a local branch, optionally at a specific revision.
    ///
    /// Equivalent to: `git branch {name} [revision]`
    pub fn create_bookmark(
        &self,
        workspace_path: &Path,
        name: &BranchName,
        revision: Option<&crate::domain::Revision>,
    ) -> Result<(), WorktreeError> {
        info!(branch = %name, revision = ?revision, path = %workspace_path.display(), "Creating local branch");

        let mut args = vec!["branch", name.as_str()];
        let rev_str;
        if let Some(rev) = revision {
            rev_str = rev.as_str().to_string();
            args.push(&rev_str);
        }

        let output = headless_git_command()
            .args(&args)
            .current_dir(workspace_path)
            .output()
            .map_err(|e| WorktreeError::GitError {
                message: format!("Failed to run git branch: {}", e),
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            error!(stderr = %stderr, "git branch failed");
            return Err(self.parse_git_stderr(&stderr));
        }

        info!(branch = %name, "Branch created");
        Ok(())
    }

    /// Parse git stderr into a WorktreeError.
    fn parse_git_stderr(&self, stderr: &str) -> WorktreeError {
        if stderr.contains("already exists") {
            if stderr.contains("branch named") {
                let branch = stderr.split('\'').nth(1).unwrap_or("unknown").to_string();
                WorktreeError::BranchExists { branch }
            } else {
                let path = stderr
                    .trim_start_matches("fatal: ")
                    .trim_end_matches(" already exists")
                    .to_string();
                WorktreeError::PathExists { path }
            }
        } else if stderr.contains("not a valid object")
            || stderr.contains("not a commit")
            || stderr.contains("invalid reference")
        {
            let branch = stderr.split('\'').nth(1).unwrap_or("unknown").to_string();
            WorktreeError::BaseBranchNotFound { branch }
        } else if stderr.contains(".lock") {
            WorktreeError::LockFileConflict {
                message: stderr.trim().to_string(),
            }
        } else if stderr.contains("non-fast-forward") || stderr.contains("rejected") {
            WorktreeError::PushRejected {
                message: stderr.trim().to_string(),
            }
        } else {
            WorktreeError::GitError {
                message: stderr.trim().to_string(),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;
    use tempfile::TempDir;

    #[test]
    fn headless_git_command_sets_noninteractive_auth_env() {
        let command = headless_git_command();
        let envs = command
            .get_envs()
            .filter_map(|(key, value)| {
                value.map(|value| (key.to_string_lossy(), value.to_string_lossy()))
            })
            .collect::<std::collections::HashMap<_, _>>();

        assert_eq!(
            envs.get("GIT_TERMINAL_PROMPT").map(|v| v.as_ref()),
            Some("0")
        );
        assert_eq!(
            envs.get("GCM_INTERACTIVE").map(|v| v.as_ref()),
            Some("never")
        );
        assert_eq!(envs.get("GIT_ASKPASS").map(|v| v.as_ref()), Some(""));
        assert_eq!(
            envs.get("SSH_ASKPASS_REQUIRE").map(|v| v.as_ref()),
            Some("never")
        );
        assert!(envs
            .get("GIT_SSH_COMMAND")
            .is_some_and(|value| value.contains("BatchMode=yes")));
    }

    fn init_test_repo() -> (TempDir, GitWorktreeService) {
        let temp = TempDir::new().expect("failed to create temp dir");
        let repo_dir = temp.path();

        let run = |args: &[&str]| {
            let status = Command::new("git")
                .args(args)
                .current_dir(repo_dir)
                .status()
                .expect("failed to run git command");
            assert!(status.success(), "git command failed: {:?}", args);
        };

        run(&["init"]);
        run(&["config", "user.email", "test@example.com"]);
        run(&["config", "user.name", "Test User"]);
        run(&["commit", "--allow-empty", "-m", "Initial commit"]);

        let service = GitWorktreeService::new(repo_dir.to_path_buf());
        (temp, service)
    }

    fn get_default_branch(repo_dir: &std::path::Path) -> String {
        let output = Command::new("git")
            .args(["branch", "--show-current"])
            .current_dir(repo_dir)
            .output()
            .expect("failed to get default branch");
        String::from_utf8_lossy(&output.stdout).trim().to_string()
    }

    #[test]
    fn test_create_workspace_happy_path() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());
        let worktree_path = temp.path().join("worktree-1");
        let branch =
            BranchName::try_from_str("test-branch").expect("literal validated string is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        service
            .create_workspace(&worktree_path, &branch, &base)
            .unwrap();

        assert!(worktree_path.exists());
        assert!(worktree_path.join(".git").exists());
    }

    #[test]
    fn test_remove_workspace_happy_path() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());
        let worktree_path = temp.path().join("worktree-1");
        let branch =
            BranchName::try_from_str("test-branch").expect("literal validated string is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        service
            .create_workspace(&worktree_path, &branch, &base)
            .unwrap();
        assert!(worktree_path.exists());

        service.remove_workspace(&worktree_path).unwrap();
        assert!(!worktree_path.exists());
    }

    #[test]
    fn test_create_bookmark_delete_bookmark_roundtrip() {
        let (temp, service) = init_test_repo();
        let branch =
            BranchName::try_from_str("test-branch").expect("literal validated string is non-empty");

        service.create_bookmark(temp.path(), &branch, None).unwrap();

        let output = Command::new("git")
            .args(["branch", "--list", "test-branch"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        assert!(String::from_utf8_lossy(&output.stdout).contains("test-branch"));

        service.delete_bookmark(&branch).unwrap();

        let output = Command::new("git")
            .args(["branch", "--list", "test-branch"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        assert!(!String::from_utf8_lossy(&output.stdout).contains("test-branch"));
    }

    #[test]
    fn test_get_workspace_bookmark() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());
        let worktree_path = temp.path().join("worktree-1");
        let branch =
            BranchName::try_from_str("test-branch").expect("literal validated string is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        service
            .create_workspace(&worktree_path, &branch, &base)
            .unwrap();

        let current = service.get_workspace_bookmark(&worktree_path).unwrap();
        assert_eq!(current, Some("test-branch".to_string()));
    }

    #[test]
    fn test_create_workspace_duplicate_branch() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());
        let branch =
            BranchName::try_from_str("test-branch").expect("literal validated string is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        service
            .create_workspace(&temp.path().join("wt1"), &branch, &base)
            .unwrap();
        let result = service.create_workspace(&temp.path().join("wt2"), &branch, &base);

        assert!(
            matches!(result, Err(WorktreeError::BranchExists { .. })),
            "Expected BranchExists, got: {:?}",
            result
        );
    }

    #[test]
    fn test_create_workspace_non_existent_base() {
        let (temp, service) = init_test_repo();
        let worktree_path = temp.path().join("worktree-1");
        let branch =
            BranchName::try_from_str("test-branch").expect("literal validated string is non-empty");
        let base = BranchName::try_from_str("nonexistent-base-xyz")
            .expect("literal validated string is non-empty");

        let result = service.create_workspace(&worktree_path, &branch, &base);

        assert!(
            matches!(result, Err(WorktreeError::BaseBranchNotFound { .. })),
            "Expected BaseBranchNotFound, got: {:?}",
            result
        );
    }

    #[test]
    fn test_remove_workspace_non_existent_path() {
        let (temp, service) = init_test_repo();
        let path = temp.path().join("nonexistent-worktree-xyz");

        // Should succeed (idempotent)
        service.remove_workspace(&path).unwrap();
    }

    #[test]
    fn test_push_bookmark_without_remote() {
        let (temp, service) = init_test_repo();
        let branch =
            BranchName::try_from_str("test-branch").expect("literal validated string is non-empty");
        service.create_bookmark(temp.path(), &branch, None).unwrap();

        let result = service.push_bookmark(temp.path(), &branch);

        assert!(result.is_err());
    }

    #[test]
    fn test_push_to_remote_without_remote() {
        let (temp, service) = init_test_repo();
        let branch =
            BranchName::try_from_str("test-branch").expect("literal validated string is non-empty");
        service.create_bookmark(temp.path(), &branch, None).unwrap();

        let result = service.push_to_remote(temp.path(), &branch, "origin");
        assert!(result.is_err());

        let result = service.push_to_remote(temp.path(), &branch, "tangled");
        assert!(result.is_err());
    }

    #[test]
    fn test_push_to_remote_with_remote() {
        let bare = TempDir::new().expect("failed to create bare dir");
        let work = TempDir::new().expect("failed to create work dir");
        let work_dir = work.path();

        // Create a bare "remote"
        let run_bare = |args: &[&str]| {
            Command::new("git")
                .args(args)
                .current_dir(bare.path())
                .status()
                .expect("git failed")
        };
        run_bare(&["init", "--bare"]);

        // Create working repo with tangled remote pointing at the bare repo
        let run = |args: &[&str]| {
            let status = Command::new("git")
                .args(args)
                .current_dir(work_dir)
                .status()
                .expect("git failed");
            assert!(status.success(), "git {:?} failed", args);
        };
        run(&["init"]);
        run(&["config", "user.email", "test@example.com"]);
        run(&["config", "user.name", "Test User"]);
        run(&["commit", "--allow-empty", "-m", "Initial commit"]);
        run(&["remote", "add", "tangled", bare.path().to_str().unwrap()]);

        let service = GitWorktreeService::new(work_dir.to_path_buf());
        let default_branch = get_default_branch(work_dir);
        let branch = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        let result = service.push_to_remote(work_dir, &branch, "tangled");
        assert!(result.is_ok(), "push_to_remote failed: {:?}", result);
    }

    /// End-to-end: simulates the file_pr resolution chain.
    ///
    /// Given a dot-separated birth_branch (e.g. "main.remove-option-mcp"):
    /// 1. resolve_working_dir → ".exo/worktrees/remove-option-mcp/"
    /// 2. Create a worktree there on that branch
    /// 3. get_workspace_bookmark → must return "main.remove-option-mcp"
    ///
    /// This is the exact chain file_pr uses. If step 3 returns a different
    /// branch, file_pr will find/update the wrong PR.
    #[test]
    fn test_file_pr_resolution_chain_dot_branch() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());

        // Simulate exomonad's dot-separated branch naming (suffixed agent names)
        let birth_branch = format!("{}.remove-option-mcp-gemini", default_branch);
        let branch = BranchName::try_from_str(birth_branch.as_str())
            .expect("validated string input is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        // Step 1: resolve_working_dir (same logic as EffectContext construction)
        let relative_dir = crate::services::agent_control::resolve_working_dir(&birth_branch);
        assert_eq!(
            relative_dir,
            std::path::PathBuf::from(".exo/worktrees/remove-option-mcp-gemini/")
        );

        // Step 2: create worktree at the resolved path (relative to project root)
        let worktree_path = temp.path().join(&relative_dir);
        std::fs::create_dir_all(worktree_path.parent().unwrap()).unwrap();
        service
            .create_workspace(&worktree_path, &branch, &base)
            .unwrap();

        // Step 3: get_workspace_bookmark must return the dot-separated branch
        let resolved = service.get_workspace_bookmark(&worktree_path).unwrap();
        assert_eq!(
            resolved,
            Some(birth_branch.clone()),
            "get_workspace_bookmark must return the exact birth_branch"
        );
    }

    /// Same chain but with a deeply nested branch (3 levels).
    #[test]
    fn test_file_pr_resolution_chain_deep_nesting() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());

        let birth_branch = format!(
            "{}.tui-port-2-claude.pdv-snapshot-enums-gemini",
            default_branch
        );
        let branch = BranchName::try_from_str(birth_branch.as_str())
            .expect("validated string input is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        let relative_dir = crate::services::agent_control::resolve_working_dir(&birth_branch);
        assert_eq!(
            relative_dir,
            std::path::PathBuf::from(".exo/worktrees/pdv-snapshot-enums-gemini/")
        );

        let worktree_path = temp.path().join(&relative_dir);
        std::fs::create_dir_all(worktree_path.parent().unwrap()).unwrap();
        service
            .create_workspace(&worktree_path, &branch, &base)
            .unwrap();

        let resolved = service.get_workspace_bookmark(&worktree_path).unwrap();
        assert_eq!(resolved, Some(birth_branch));
    }

    /// Branch verification: after create_workspace, get_workspace_bookmark returns exact branch name.
    #[test]
    fn test_create_workspace_branch_verification() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());
        let worktree_path = temp.path().join("wt-verify");
        let branch = BranchName::try_from_str("test-verify-branch")
            .expect("literal validated string is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        service
            .create_workspace(&worktree_path, &branch, &base)
            .unwrap();

        let actual = service.get_workspace_bookmark(&worktree_path).unwrap();
        assert_eq!(actual, Some("test-verify-branch".to_string()));
    }

    /// Branch verification with dotted branch name (ExoMonad convention).
    #[test]
    fn test_create_workspace_branch_verification_dotted() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());
        let worktree_path = temp.path().join("wt-dotted");
        let branch_name = format!("{}.feat-a-gemini", default_branch);
        let branch = BranchName::try_from_str(branch_name.as_str())
            .expect("validated string input is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        service
            .create_workspace(&worktree_path, &branch, &base)
            .unwrap();

        let actual = service.get_workspace_bookmark(&worktree_path).unwrap();
        assert_eq!(actual, Some(branch_name));
    }

    /// Branch verification with deeply dotted branch name.
    #[test]
    fn test_create_workspace_branch_verification_deep() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());
        let worktree_path = temp.path().join("wt-deep");
        let branch_name = format!("{}.tl.sub.leaf-gemini", default_branch);
        let branch = BranchName::try_from_str(branch_name.as_str())
            .expect("validated string input is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        service
            .create_workspace(&worktree_path, &branch, &base)
            .unwrap();

        let actual = service.get_workspace_bookmark(&worktree_path).unwrap();
        assert_eq!(actual, Some(branch_name));
    }

    /// Resolution chain with agent-suffixed branch.
    #[test]
    fn test_file_pr_resolution_chain_agent_suffix() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());

        let birth_branch = format!("{}.fix-auth-gemini", default_branch);
        let branch = BranchName::try_from_str(birth_branch.as_str())
            .expect("validated string input is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        let relative_dir = crate::services::agent_control::resolve_working_dir(&birth_branch);
        assert_eq!(
            relative_dir,
            std::path::PathBuf::from(".exo/worktrees/fix-auth-gemini/")
        );

        let worktree_path = temp.path().join(&relative_dir);
        std::fs::create_dir_all(worktree_path.parent().unwrap()).unwrap();
        service
            .create_workspace(&worktree_path, &branch, &base)
            .unwrap();

        let resolved = service.get_workspace_bookmark(&worktree_path).unwrap();
        assert_eq!(resolved, Some(birth_branch));
    }

    /// Resolution chain with claude-suffixed branch.
    #[test]
    fn test_file_pr_resolution_chain_claude_suffix() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());

        let birth_branch = format!("{}.tl-auth-claude", default_branch);
        let branch = BranchName::try_from_str(birth_branch.as_str())
            .expect("validated string input is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        let relative_dir = crate::services::agent_control::resolve_working_dir(&birth_branch);
        let worktree_path = temp.path().join(&relative_dir);
        std::fs::create_dir_all(worktree_path.parent().unwrap()).unwrap();
        service
            .create_workspace(&worktree_path, &branch, &base)
            .unwrap();

        let resolved = service.get_workspace_bookmark(&worktree_path).unwrap();
        assert_eq!(resolved, Some(birth_branch));
    }

    /// Resolution chain with 4 levels deep.
    #[test]
    fn test_file_pr_resolution_chain_deep_4_levels() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());

        let birth_branch = format!("{}.tl.sub.leaf.worker-gemini", default_branch);
        let branch = BranchName::try_from_str(birth_branch.as_str())
            .expect("validated string input is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        let relative_dir = crate::services::agent_control::resolve_working_dir(&birth_branch);
        assert_eq!(
            relative_dir,
            std::path::PathBuf::from(".exo/worktrees/worker-gemini/")
        );

        let worktree_path = temp.path().join(&relative_dir);
        std::fs::create_dir_all(worktree_path.parent().unwrap()).unwrap();
        service
            .create_workspace(&worktree_path, &branch, &base)
            .unwrap();

        let resolved = service.get_workspace_bookmark(&worktree_path).unwrap();
        assert_eq!(resolved, Some(birth_branch));
    }

    /// Sibling collision: same slug from different parents → same worktree dir (known limitation).
    #[test]
    fn test_resolve_working_dir_sibling_collision() {
        let dir_a =
            crate::services::agent_control::resolve_working_dir("main.tl-a.my-feature-gemini");
        let dir_b =
            crate::services::agent_control::resolve_working_dir("main.tl-b.my-feature-gemini");
        assert_eq!(dir_a, dir_b, "Same slug = same dir (known limitation)");
    }

    /// resolve_working_dir for agent-suffixed branches.
    #[test]
    fn test_resolve_working_dir_agent_suffixed() {
        assert_eq!(
            crate::services::agent_control::resolve_working_dir("main.fix-auth-gemini"),
            std::path::PathBuf::from(".exo/worktrees/fix-auth-gemini/")
        );
    }

    /// resolve_working_dir for root branches.
    #[test]
    fn test_resolve_working_dir_root() {
        assert_eq!(
            crate::services::agent_control::resolve_working_dir("main"),
            std::path::PathBuf::from(".")
        );
    }

    /// Verify that two sibling agents with different birth branches resolve
    /// to different worktrees and get_workspace_bookmark returns the correct
    /// branch for each.
    #[test]
    fn test_file_pr_resolution_chain_sibling_isolation() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        let branch_a = format!("{}.feature-a-claude", default_branch);
        let branch_b = format!("{}.feature-b-claude", default_branch);

        let dir_a = temp
            .path()
            .join(crate::services::agent_control::resolve_working_dir(
                &branch_a,
            ));
        let dir_b = temp
            .path()
            .join(crate::services::agent_control::resolve_working_dir(
                &branch_b,
            ));

        std::fs::create_dir_all(dir_a.parent().unwrap()).unwrap();
        service
            .create_workspace(
                &dir_a,
                &BranchName::try_from_str(branch_a.as_str())
                    .expect("validated string input is non-empty"),
                &base,
            )
            .unwrap();
        service
            .create_workspace(
                &dir_b,
                &BranchName::try_from_str(branch_b.as_str())
                    .expect("validated string input is non-empty"),
                &base,
            )
            .unwrap();

        let resolved_a = service.get_workspace_bookmark(&dir_a).unwrap();
        let resolved_b = service.get_workspace_bookmark(&dir_b).unwrap();

        assert_eq!(resolved_a, Some(branch_a));
        assert_eq!(resolved_b, Some(branch_b));
        assert_ne!(resolved_a, resolved_b);
    }
}
