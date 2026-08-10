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

#[derive(Debug)]
struct ExistingWorktree {
    path: PathBuf,
    branch: Option<String>,
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

fn push_remote_command(
    remote: &str,
    branch: &BranchName,
    forgejo_token: Option<&str>,
) -> std::process::Command {
    let mut command = headless_git_command();
    if let Some(token) = forgejo_token
        .map(str::trim)
        .filter(|token| !token.is_empty())
    {
        command
            .arg("-c")
            .arg(format!("http.extraheader=Authorization: token {token}"));
    }
    command.args(["push", remote, branch.as_str()]);
    command
}

/// Read the `exomonad.remote` git config override, if set. Mirrors
/// `services::repo::configured_remote`, but synchronous — this module
/// shells out via `std::process::Command`, not tokio.
fn configured_remote(workspace_path: &Path) -> Option<String> {
    let output = headless_git_command()
        .args(["config", "--get", "exomonad.remote"])
        .current_dir(workspace_path)
        .output()
        .ok()?;

    if !output.status.success() {
        return None;
    }

    let value = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if value.is_empty() {
        None
    } else {
        Some(value)
    }
}

fn last_branch_segment(branch: &str) -> Option<&str> {
    branch
        .rsplit('.')
        .next()
        .filter(|segment| !segment.is_empty())
}

impl GitWorktreeService {
    pub fn new(project_dir: PathBuf) -> Self {
        Self { project_dir }
    }

    fn prepare_repository_for_worktrees(&self) -> Result<(), WorktreeError> {
        self.validate_repository(&self.project_dir)?;
        let worktrees = self.list_worktrees()?;
        let main_path = worktrees
            .first()
            .map(|worktree| worktree.path.clone())
            .ok_or_else(|| WorktreeError::GitError {
                message: "refusing to create a worktree: repository has no main worktree"
                    .to_string(),
            })?;
        self.validate_repository(&main_path)?;

        let main_path = self.canonical_path(&main_path, "main worktree")?;
        let actual_root = self.git_path(&main_path, &["rev-parse", "--show-toplevel"])?;
        let actual_root = self.canonical_path(Path::new(&actual_root), "repository root")?;
        if actual_root != main_path {
            let configured_worktree = self
                .local_config_value(&main_path, "core.worktree")?
                .unwrap_or_else(|| "<unset>".to_string());
            return Err(WorktreeError::GitError {
                message: format!(
                    "refusing to enable worktreeConfig: core.worktree={configured_worktree:?} does not resolve to the main worktree"
                ),
            });
        }

        if !self
            .local_config_value(&main_path, "extensions.worktreeConfig")?
            .is_some_and(|value| value.eq_ignore_ascii_case("true"))
        {
            self.set_local_config(&main_path, "extensions.worktreeConfig", "true")?;
            info!(path = %main_path.display(), "Enabled git worktree-specific configuration");
        }

        self.migrate_worktree_identities(&main_path, &worktrees)
    }

    fn validate_repository(&self, path: &Path) -> Result<(), WorktreeError> {
        let bare = self.git_output(path, &["rev-parse", "--is-bare-repository"])?;
        if !bare.status.success() {
            return Err(WorktreeError::GitError {
                message: format!(
                    "failed to validate repository at {}: {}",
                    path.display(),
                    String::from_utf8_lossy(&bare.stderr).trim()
                ),
            });
        }

        let bare_value = String::from_utf8_lossy(&bare.stdout).trim().to_string();
        let configured_bare = self.local_config_value(path, "core.bare")?;
        if bare_value != "false"
            || configured_bare.as_deref().is_some_and(|value| {
                !matches!(
                    value.trim().to_ascii_lowercase().as_str(),
                    "false" | "0" | "no" | "off"
                )
            })
        {
            return Err(WorktreeError::GitError {
                message: format!(
                    "refusing to create a worktree: core.bare must be false (repository at {} reports {})",
                    path.display(),
                    configured_bare.unwrap_or(bare_value)
                ),
            });
        }

        let inside = self.git_output(path, &["rev-parse", "--is-inside-work-tree"])?;
        if !inside.status.success() || String::from_utf8_lossy(&inside.stdout).trim() != "true" {
            let configured_worktree = self
                .local_config_value(path, "core.worktree")?
                .unwrap_or_else(|| "<unset>".to_string());
            return Err(WorktreeError::GitError {
                message: format!(
                    "refusing to create a worktree: core.worktree={configured_worktree:?} does not describe a safe non-bare worktree"
                ),
            });
        }

        Ok(())
    }

    fn list_worktrees(&self) -> Result<Vec<ExistingWorktree>, WorktreeError> {
        let output = self.git_output(&self.project_dir, &["worktree", "list", "--porcelain"])?;
        if !output.status.success() {
            return Err(WorktreeError::GitError {
                message: format!(
                    "failed to list git worktrees: {}",
                    String::from_utf8_lossy(&output.stderr).trim()
                ),
            });
        }

        let mut worktrees = Vec::new();
        let mut current = None;
        for line in String::from_utf8_lossy(&output.stdout).lines() {
            if let Some(path) = line.strip_prefix("worktree ") {
                if let Some(worktree) = current.take() {
                    worktrees.push(worktree);
                }
                current = Some(ExistingWorktree {
                    path: PathBuf::from(path),
                    branch: None,
                });
            } else if let Some(branch) = line.strip_prefix("branch ") {
                if let Some(worktree) = current.as_mut() {
                    worktree.branch = Some(
                        branch
                            .strip_prefix("refs/heads/")
                            .unwrap_or(branch)
                            .to_string(),
                    );
                }
            }
        }
        if let Some(worktree) = current {
            worktrees.push(worktree);
        }

        if worktrees.is_empty() {
            return Err(WorktreeError::GitError {
                message: "failed to list git worktrees: no main worktree was reported".to_string(),
            });
        }
        Ok(worktrees)
    }

    fn migrate_worktree_identities(
        &self,
        main_path: &Path,
        worktrees: &[ExistingWorktree],
    ) -> Result<(), WorktreeError> {
        for worktree in worktrees {
            if !worktree.path.exists() {
                warn!(path = %worktree.path.display(), "Skipping missing git worktree during identity migration");
                continue;
            }

            let worktree_path = self.canonical_path(&worktree.path, "live worktree")?;
            if worktree_path == main_path {
                continue;
            }

            let identity_name = worktree
                .branch
                .as_deref()
                .and_then(last_branch_segment)
                .or_else(|| {
                    worktree
                        .path
                        .file_name()
                        .and_then(|name| name.to_str())
                        .filter(|name| !name.is_empty())
                });
            let Some(identity_name) = identity_name else {
                warn!(path = %worktree.path.display(), "Skipping git worktree without an identity-bearing branch or path");
                continue;
            };

            self.set_worktree_identity(&worktree.path, identity_name)?;
            info!(path = %worktree.path.display(), identity = %identity_name, "Migrated git worktree identity");
        }

        self.unset_clobbered_shared_identity(main_path)
    }

    fn unset_clobbered_shared_identity(&self, main_path: &Path) -> Result<(), WorktreeError> {
        for (key, pattern) in [
            ("user.name", r"^exomonad-.*$"),
            ("user.email", r"^.*@exomonad\.local$"),
        ] {
            let output = self.git_output(
                main_path,
                &["config", "--local", "--unset-all", key, pattern],
            )?;
            if output.status.success() {
                info!(key, "Removed legacy shared git identity configuration");
            } else if !matches!(output.status.code(), Some(1) | Some(5)) {
                return Err(WorktreeError::GitError {
                    message: format!(
                        "git config --local --unset-all {key} failed: {}",
                        String::from_utf8_lossy(&output.stderr).trim()
                    ),
                });
            }
        }
        Ok(())
    }

    fn set_worktree_identity(&self, path: &Path, identity_name: &str) -> Result<(), WorktreeError> {
        let git_user_name = format!("exomonad-{identity_name}");
        let git_user_email = format!("{identity_name}@exomonad.local");
        for (key, value) in [
            ("user.name", git_user_name.as_str()),
            ("user.email", git_user_email.as_str()),
        ] {
            let output = self.git_output(path, &["config", "--worktree", key, value])?;
            if !output.status.success() {
                let stderr = String::from_utf8_lossy(&output.stderr);
                error!(key, stderr = %stderr, "git config --worktree failed");
                return Err(WorktreeError::GitError {
                    message: format!("git config --worktree {key} failed: {}", stderr.trim()),
                });
            }
        }
        info!(path = %path.display(), user_name = %git_user_name, user_email = %git_user_email, "Set worktree git identity");
        Ok(())
    }

    fn local_config_value(&self, path: &Path, key: &str) -> Result<Option<String>, WorktreeError> {
        let output = self.git_output(path, &["config", "--local", "--get", key])?;
        if output.status.success() {
            return Ok(Some(
                String::from_utf8_lossy(&output.stdout).trim().to_string(),
            ));
        }
        if output.status.code() == Some(1) {
            return Ok(None);
        }
        Err(WorktreeError::GitError {
            message: format!(
                "git config --local --get {key} failed: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            ),
        })
    }

    fn set_local_config(&self, path: &Path, key: &str, value: &str) -> Result<(), WorktreeError> {
        let output = self.git_output(path, &["config", "--local", key, value])?;
        if output.status.success() {
            return Ok(());
        }
        Err(WorktreeError::GitError {
            message: format!(
                "git config --local {key} failed: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            ),
        })
    }

    fn git_path(&self, path: &Path, args: &[&str]) -> Result<String, WorktreeError> {
        let output = self.git_output(path, args)?;
        if !output.status.success() {
            return Err(WorktreeError::GitError {
                message: format!(
                    "git {} failed: {}",
                    args.join(" "),
                    String::from_utf8_lossy(&output.stderr).trim()
                ),
            });
        }
        Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
    }

    fn git_output(
        &self,
        path: &Path,
        args: &[&str],
    ) -> Result<std::process::Output, WorktreeError> {
        headless_git_command()
            .args(args)
            .current_dir(path)
            .output()
            .map_err(|error| WorktreeError::GitError {
                message: format!("failed to run git {}: {error}", args.join(" ")),
            })
    }

    fn canonical_path(&self, path: &Path, description: &str) -> Result<PathBuf, WorktreeError> {
        std::fs::canonicalize(path).map_err(|error| WorktreeError::GitError {
            message: format!(
                "failed to resolve {description} {}: {error}",
                path.display()
            ),
        })
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
        self.create_workspace_from_ref(path, branch, base.as_str(), true)
    }

    /// Create a new git worktree with a new branch based on an exact revision.
    ///
    /// This is used by replacement workflows where the old PR head must remain
    /// the starting point while the new PR targets the old PR's base branch.
    pub fn create_workspace_from_revision(
        &self,
        path: &Path,
        branch: &BranchName,
        revision: &str,
    ) -> Result<(), WorktreeError> {
        if revision.trim().is_empty() {
            return Err(WorktreeError::GitError {
                message: "worktree start revision is empty".to_string(),
            });
        }
        self.create_workspace_from_ref(path, branch, revision, true)
    }

    /// Attach a worktree to an existing local branch without recreating it.
    ///
    /// Replacement retries use this path after the first attempt has created
    /// the new branch but failed before the agent window was registered.
    pub fn create_workspace_from_existing_branch(
        &self,
        path: &Path,
        branch: &BranchName,
    ) -> Result<(), WorktreeError> {
        self.create_workspace_from_ref(path, branch, branch.as_str(), false)
    }

    /// Check whether a local branch exists without mutating the repository.
    pub fn branch_exists(&self, branch: &BranchName) -> Result<bool, WorktreeError> {
        let output = headless_git_command()
            .args(["show-ref", "--verify", "--quiet"])
            .arg(format!("refs/heads/{}", branch.as_str()))
            .current_dir(&self.project_dir)
            .output()
            .map_err(|e| WorktreeError::GitError {
                message: format!("Failed to inspect git branch: {}", e),
            })?;
        if output.status.success() {
            Ok(true)
        } else if output.status.code() == Some(1) {
            Ok(false)
        } else {
            Err(WorktreeError::GitError {
                message: format!(
                    "Failed to inspect branch {}: {}",
                    branch,
                    String::from_utf8_lossy(&output.stderr).trim()
                ),
            })
        }
    }

    fn create_workspace_from_ref(
        &self,
        path: &Path,
        branch: &BranchName,
        base_ref: &str,
        create_branch: bool,
    ) -> Result<(), WorktreeError> {
        self.prepare_repository_for_worktrees()?;
        info!(path = %path.display(), branch = %branch, base = %base_ref, "Creating git worktree");

        let mut command = headless_git_command();
        command.args(["worktree", "add"]);
        if create_branch {
            command.args(["-b", branch.as_str()]);
        }
        let output = command
            .arg(path)
            .arg(base_ref)
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
        self.set_worktree_identity(path, agent_name)
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
        self.prepare_repository_for_worktrees()?;
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

        self.set_worktree_identity(path, identity_name)?;
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
        self.push_bookmark_with_token(workspace_path, branch, None)
    }

    /// Push a branch to the configured remote (see `exomonad.remote` git
    /// config, defaulting to `origin`), using the Forgejo token for HTTP
    /// remote auth when provided.
    pub fn push_bookmark_with_token(
        &self,
        workspace_path: &Path,
        branch: &BranchName,
        forgejo_token: Option<&str>,
    ) -> Result<(), WorktreeError> {
        let remote = configured_remote(workspace_path).unwrap_or_else(|| "origin".to_string());
        self.push_to_remote_with_token(workspace_path, branch, &remote, forgejo_token)
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
        self.push_to_remote_with_token(workspace_path, branch, remote, None)
    }

    /// Push a branch to a named remote, using the Forgejo token for HTTP
    /// remote auth when provided.
    ///
    /// Equivalent to: `git push {remote} {branch}` (run in workspace_path)
    pub fn push_to_remote_with_token(
        &self,
        workspace_path: &Path,
        branch: &BranchName,
        remote: &str,
        forgejo_token: Option<&str>,
    ) -> Result<(), WorktreeError> {
        info!(branch = %branch, remote = %remote, path = %workspace_path.display(), "Pushing branch to remote");

        let output = push_remote_command(remote, branch, forgejo_token)
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
    use exomonad_test_support::{
        init_bare_fixture_git_repository, init_fixture_git_repository, run_fixture_git_command,
    };
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

    #[test]
    fn push_remote_command_injects_forgejo_token_extraheader() {
        let branch = BranchName::try_from_str("feature-auth")
            .expect("literal validated string is non-empty");
        let command = push_remote_command("origin", &branch, Some("secret-token"));
        let args = command
            .get_args()
            .map(|arg| arg.to_string_lossy().to_string())
            .collect::<Vec<_>>();

        assert_eq!(
            args,
            vec![
                "-c",
                "http.extraheader=Authorization: token secret-token",
                "push",
                "origin",
                "feature-auth",
            ]
        );
    }

    #[test]
    fn push_remote_command_injects_forgejo_token_for_named_remote() {
        let branch = BranchName::try_from_str("feature-auth")
            .expect("literal validated string is non-empty");
        let command = push_remote_command("forgejo", &branch, Some("secret-token"));
        let args = command
            .get_args()
            .map(|arg| arg.to_string_lossy().to_string())
            .collect::<Vec<_>>();

        assert_eq!(
            args,
            vec![
                "-c",
                "http.extraheader=Authorization: token secret-token",
                "push",
                "forgejo",
                "feature-auth",
            ]
        );
    }

    #[test]
    fn push_remote_command_omits_empty_forgejo_token_extraheader() {
        let branch = BranchName::try_from_str("feature-auth")
            .expect("literal validated string is non-empty");
        let command = push_remote_command("origin", &branch, Some("   "));
        let args = command
            .get_args()
            .map(|arg| arg.to_string_lossy().to_string())
            .collect::<Vec<_>>();

        assert_eq!(args, vec!["push", "origin", "feature-auth"]);
    }

    #[test]
    fn configured_remote_returns_none_when_unset() {
        let temp = TempDir::new().expect("failed to create temp dir");
        init_fixture_git_repository(temp.path()).expect("git init failed");

        assert_eq!(configured_remote(temp.path()), None);
    }

    #[test]
    fn configured_remote_returns_configured_value() {
        let temp = TempDir::new().expect("failed to create temp dir");
        init_fixture_git_repository(temp.path()).expect("git init failed");
        run_fixture_git_command(
            temp.path(),
            &["config", "--local", "exomonad.remote", "forgejo"],
        )
        .expect("git config failed");

        assert_eq!(configured_remote(temp.path()), Some("forgejo".to_string()));
    }

    fn init_test_repo() -> (TempDir, GitWorktreeService) {
        let temp = TempDir::new().expect("failed to create temp dir");
        let repo_dir = temp.path();

        init_fixture_git_repository(repo_dir).expect("failed to initialize test repository");
        run_fixture_git_command(repo_dir, &["config", "user.email", "test@example.com"])
            .expect("failed to configure test repository email");
        run_fixture_git_command(repo_dir, &["config", "user.name", "Test User"])
            .expect("failed to configure test repository name");
        run_fixture_git_command(
            repo_dir,
            &["commit", "--allow-empty", "-m", "Initial commit"],
        )
        .expect("failed to create test repository commit");

        let service = GitWorktreeService::new(repo_dir.to_path_buf());
        (temp, service)
    }

    fn get_default_branch(repo_dir: &std::path::Path) -> String {
        let output = run_fixture_git_command(repo_dir, &["branch", "--show-current"])
            .expect("failed to get default branch");
        String::from_utf8_lossy(&output.stdout).trim().to_string()
    }

    fn git_config_value(repo_dir: &std::path::Path, key: &str) -> Option<String> {
        let output = run_fixture_git_command(repo_dir, &["config", "--get", key]).ok()?;
        Some(String::from_utf8_lossy(&output.stdout).trim().to_string())
    }

    fn git_local_config_value(repo_dir: &std::path::Path, key: &str) -> Option<String> {
        let output =
            run_fixture_git_command(repo_dir, &["config", "--local", "--get", key]).ok()?;
        Some(String::from_utf8_lossy(&output.stdout).trim().to_string())
    }

    fn run_git(repo_dir: &std::path::Path, args: &[&str]) {
        run_fixture_git_command(repo_dir, args).unwrap_or_else(|error| {
            panic!("git {args:?} failed: {error}");
        });
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
    fn worktree_identities_are_isolated_and_shared_remote_is_preserved() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");
        run_git(
            temp.path(),
            &["config", "--local", "exomonad.remote", "forgejo"],
        );

        let branch_a = format!("{default_branch}.dev-one-codex");
        let path_a = temp.path().join("dev-one-codex");
        service
            .create_workspace(
                &path_a,
                &BranchName::try_from_str(branch_a.as_str())
                    .expect("validated string input is non-empty"),
                &base,
            )
            .unwrap();
        assert_eq!(
            git_config_value(&path_a, "user.name").as_deref(),
            Some("exomonad-dev-one-codex")
        );
        assert_eq!(configured_remote(&path_a).as_deref(), Some("forgejo"));

        let branch_b = format!("{default_branch}.dev-two-opencode");
        let path_b = temp.path().join("dev-two-opencode");
        service
            .create_workspace(
                &path_b,
                &BranchName::try_from_str(branch_b.as_str())
                    .expect("validated string input is non-empty"),
                &base,
            )
            .unwrap();

        assert_eq!(
            git_config_value(&path_a, "user.name").as_deref(),
            Some("exomonad-dev-one-codex")
        );
        assert_eq!(
            git_config_value(&path_b, "user.name").as_deref(),
            Some("exomonad-dev-two-opencode")
        );
        assert_eq!(configured_remote(&path_a).as_deref(), Some("forgejo"));
        assert_eq!(configured_remote(&path_b).as_deref(), Some("forgejo"));
        assert_eq!(
            git_config_value(temp.path(), "user.name").as_deref(),
            Some("Test User")
        );
        assert_eq!(
            git_config_value(temp.path(), "exomonad.remote").as_deref(),
            Some("forgejo")
        );
    }

    #[test]
    fn detached_reviewer_identity_is_distinct_from_dev_identity() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");
        let dev_branch = format!("{default_branch}.dev-leaf-codex");
        let dev_path = temp.path().join("dev-leaf-codex");
        service
            .create_workspace(
                &dev_path,
                &BranchName::try_from_str(dev_branch.as_str())
                    .expect("validated string input is non-empty"),
                &base,
            )
            .unwrap();

        let at_ref = String::from_utf8_lossy(
            &run_fixture_git_command(temp.path(), &["rev-parse", "HEAD"])
                .expect("failed to resolve reviewer revision")
                .stdout,
        )
        .trim()
        .to_string();
        let reviewer_path = temp.path().join("review-pr-609-claude");
        service
            .create_workspace_detached(&reviewer_path, &at_ref, "review-pr-609-claude")
            .unwrap();

        assert_eq!(
            git_config_value(&dev_path, "user.name").as_deref(),
            Some("exomonad-dev-leaf-codex")
        );
        assert_eq!(
            git_config_value(&reviewer_path, "user.name").as_deref(),
            Some("exomonad-review-pr-609-claude")
        );
        assert_ne!(
            git_config_value(&dev_path, "user.name"),
            git_config_value(&reviewer_path, "user.name")
        );
        assert_eq!(
            git_config_value(temp.path(), "user.name").as_deref(),
            Some("Test User")
        );
    }

    #[test]
    fn existing_worktrees_are_migrated_and_shared_legacy_identity_is_removed() {
        let (temp, service) = init_test_repo();
        let default_branch = get_default_branch(temp.path());
        let existing_branch = format!("{default_branch}.existing-dev-codex");
        let existing_path = temp.path().join("existing-dev-codex");
        run_git(
            temp.path(),
            &["config", "--local", "user.name", "exomonad-legacy-agent"],
        );
        run_git(
            temp.path(),
            &[
                "config",
                "--local",
                "user.email",
                "legacy-agent@exomonad.local",
            ],
        );
        let existing_path_string = existing_path.to_string_lossy().to_string();
        run_git(
            temp.path(),
            &[
                "worktree",
                "add",
                "-q",
                "-b",
                existing_branch.as_str(),
                existing_path_string.as_str(),
                default_branch.as_str(),
            ],
        );

        let new_branch = format!("{default_branch}.new-dev-opencode");
        let new_path = temp.path().join("new-dev-opencode");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");
        service
            .create_workspace(
                &new_path,
                &BranchName::try_from_str(new_branch.as_str())
                    .expect("validated string input is non-empty"),
                &base,
            )
            .unwrap();

        assert_eq!(
            git_config_value(&existing_path, "user.name").as_deref(),
            Some("exomonad-existing-dev-codex")
        );
        assert_eq!(
            git_config_value(&existing_path, "user.email").as_deref(),
            Some("existing-dev-codex@exomonad.local")
        );
        assert_eq!(
            git_local_config_value(temp.path(), "user.name"),
            None,
            "legacy shared user.name must be removed from the main config"
        );
        assert_eq!(
            git_local_config_value(temp.path(), "user.email"),
            None,
            "legacy shared user.email must be removed from the main config"
        );
        assert_eq!(
            git_config_value(temp.path(), "extensions.worktreeConfig").as_deref(),
            Some("true")
        );
    }

    #[test]
    fn worktree_creation_rejects_bare_or_unsafe_repositories() {
        let bare = TempDir::new().expect("failed to create bare repo directory");
        init_bare_fixture_git_repository(bare.path()).expect("bare git init failed");
        let bare_service = GitWorktreeService::new(bare.path().to_path_buf());
        let branch = BranchName::try_from_str("main.agent-codex")
            .expect("validated string input is non-empty");
        let base = BranchName::try_from_str("main").expect("validated string input is non-empty");
        let error = bare_service
            .create_workspace(&bare.path().join("worktree"), &branch, &base)
            .expect_err("bare repositories must not create linked worktrees");
        assert!(error.to_string().contains("core.bare must be false"));

        let (unsafe_repo, unsafe_service) = init_test_repo();
        let unsafe_default_branch = get_default_branch(unsafe_repo.path());
        run_git(
            unsafe_repo.path(),
            &[
                "config",
                "--local",
                "core.worktree",
                "/tmp/not-the-repository",
            ],
        );
        let error = unsafe_service
            .create_workspace(
                &unsafe_repo.path().join("worktree"),
                &branch,
                &BranchName::try_from_str(unsafe_default_branch.as_str())
                    .expect("validated string input is non-empty"),
            )
            .expect_err("unsafe core.worktree must reject worktree creation");
        assert!(error.to_string().contains("core.worktree"));
    }

    #[test]
    fn test_create_workspace_from_revision_preserves_source_head() {
        let (temp, service) = init_test_repo();
        std::fs::write(temp.path().join("source.txt"), "old PR head\n").unwrap();
        run_git(temp.path(), &["add", "source.txt"]);
        run_git(temp.path(), &["commit", "-m", "source head"]);
        let sha = String::from_utf8_lossy(
            &run_fixture_git_command(temp.path(), &["rev-parse", "HEAD"])
                .unwrap()
                .stdout,
        )
        .trim()
        .to_string();
        let branch = BranchName::try_from_str("main.replacement-codex")
            .expect("literal validated string is non-empty");
        let worktree_path = temp.path().join("replacement-codex");

        service
            .create_workspace_from_revision(&worktree_path, &branch, &sha)
            .unwrap();

        let head = String::from_utf8_lossy(
            &run_fixture_git_command(&worktree_path, &["rev-parse", "HEAD"])
                .unwrap()
                .stdout,
        )
        .trim()
        .to_string();
        assert_eq!(head, sha);
        assert_eq!(
            service.get_workspace_bookmark(&worktree_path).unwrap(),
            Some(branch.to_string())
        );
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

        let output =
            run_fixture_git_command(temp.path(), &["branch", "--list", "test-branch"]).unwrap();
        assert!(String::from_utf8_lossy(&output.stdout).contains("test-branch"));

        service.delete_bookmark(&branch).unwrap();

        let output =
            run_fixture_git_command(temp.path(), &["branch", "--list", "test-branch"]).unwrap();
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

        let result = service.push_to_remote(temp.path(), &branch, "forgejo");
        assert!(result.is_err());
    }

    #[test]
    fn test_push_to_remote_with_remote() {
        let bare = TempDir::new().expect("failed to create bare dir");
        let work = TempDir::new().expect("failed to create work dir");
        let work_dir = work.path();

        // Create a bare "remote"
        let run_bare = |args: &[&str]| {
            assert_eq!(args, &["init", "--bare"]);
            init_bare_fixture_git_repository(bare.path()).expect("bare git init failed");
        };
        run_bare(&["init", "--bare"]);

        // Create working repo with a forge remote pointing at the bare repo
        let run = |args: &[&str]| {
            run_git(work_dir, args);
        };
        init_fixture_git_repository(work_dir).expect("git init failed");
        run(&["config", "user.email", "test@example.com"]);
        run(&["config", "user.name", "Test User"]);
        run(&["commit", "--allow-empty", "-m", "Initial commit"]);
        run(&["remote", "add", "forgejo", bare.path().to_str().unwrap()]);

        let service = GitWorktreeService::new(work_dir.to_path_buf());
        let default_branch = get_default_branch(work_dir);
        let branch = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        let result = service.push_to_remote(work_dir, &branch, "forgejo");
        assert!(result.is_ok(), "push_to_remote failed: {:?}", result);
    }

    #[test]
    fn test_push_bookmark_with_token_honors_exomonad_remote_override() {
        let bare = TempDir::new().expect("failed to create bare dir");
        let work = TempDir::new().expect("failed to create work dir");
        let work_dir = work.path();

        let run_bare = |args: &[&str]| {
            assert_eq!(args, &["init", "--bare"]);
            init_bare_fixture_git_repository(bare.path()).expect("bare git init failed");
        };
        run_bare(&["init", "--bare"]);

        let run = |args: &[&str]| {
            run_git(work_dir, args);
        };
        init_fixture_git_repository(work_dir).expect("git init failed");
        run(&["config", "user.email", "test@example.com"]);
        run(&["config", "user.name", "Test User"]);
        run(&["commit", "--allow-empty", "-m", "Initial commit"]);
        // "origin" intentionally left unset — only a non-default remote
        // named "forgejo" exists, plus the exomonad.remote override
        // pointing push_bookmark_with_token at it instead of "origin".
        run(&["remote", "add", "forgejo", bare.path().to_str().unwrap()]);
        run(&["config", "--local", "exomonad.remote", "forgejo"]);

        let service = GitWorktreeService::new(work_dir.to_path_buf());
        let default_branch = get_default_branch(work_dir);
        let branch = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        let result = service.push_bookmark_with_token(work_dir, &branch, None);
        assert!(
            result.is_ok(),
            "push_bookmark_with_token failed to honor exomonad.remote: {:?}",
            result
        );
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
        let birth_branch = format!("{}.remove-option-mcp-codex", default_branch);
        let branch = BranchName::try_from_str(birth_branch.as_str())
            .expect("validated string input is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        // Step 1: resolve_working_dir (same logic as EffectContext construction)
        let relative_dir = crate::services::agent_control::resolve_working_dir(&birth_branch);
        assert_eq!(
            relative_dir,
            std::path::PathBuf::from(".exo/worktrees/remove-option-mcp-codex/")
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
            "{}.tui-port-2-claude.pdv-snapshot-enums-codex",
            default_branch
        );
        let branch = BranchName::try_from_str(birth_branch.as_str())
            .expect("validated string input is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        let relative_dir = crate::services::agent_control::resolve_working_dir(&birth_branch);
        assert_eq!(
            relative_dir,
            std::path::PathBuf::from(".exo/worktrees/pdv-snapshot-enums-codex/")
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
        let branch_name = format!("{}.feat-a-codex", default_branch);
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
        let branch_name = format!("{}.tl.sub.leaf-codex", default_branch);
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

        let birth_branch = format!("{}.fix-auth-codex", default_branch);
        let branch = BranchName::try_from_str(birth_branch.as_str())
            .expect("validated string input is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        let relative_dir = crate::services::agent_control::resolve_working_dir(&birth_branch);
        assert_eq!(
            relative_dir,
            std::path::PathBuf::from(".exo/worktrees/fix-auth-codex/")
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

        let birth_branch = format!("{}.tl.sub.leaf.worker-codex", default_branch);
        let branch = BranchName::try_from_str(birth_branch.as_str())
            .expect("validated string input is non-empty");
        let base = BranchName::try_from_str(default_branch.as_str())
            .expect("validated string input is non-empty");

        let relative_dir = crate::services::agent_control::resolve_working_dir(&birth_branch);
        assert_eq!(
            relative_dir,
            std::path::PathBuf::from(".exo/worktrees/worker-codex/")
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
            crate::services::agent_control::resolve_working_dir("main.tl-a.my-feature-codex");
        let dir_b =
            crate::services::agent_control::resolve_working_dir("main.tl-b.my-feature-codex");
        assert_eq!(dir_a, dir_b, "Same slug = same dir (known limitation)");
    }

    /// resolve_working_dir for agent-suffixed branches.
    #[test]
    fn test_resolve_working_dir_agent_suffixed() {
        assert_eq!(
            crate::services::agent_control::resolve_working_dir("main.fix-auth-codex"),
            std::path::PathBuf::from(".exo/worktrees/fix-auth-codex/")
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
