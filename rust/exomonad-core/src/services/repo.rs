use crate::domain::{GithubOwner, GithubRepo};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::Path;
use tokio::process::Command;

const DEFAULT_REMOTE: &str = "origin";

/// Shared repository information.
#[derive(Debug, Serialize, Deserialize, PartialEq, Clone)]
pub struct RepoInfo {
    /// Repository owner (e.g., "anthropics").
    pub owner: GithubOwner,
    /// Repository name (e.g., "exomonad").
    pub repo: GithubRepo,
}

/// Get repository owner and name from git remote.
///
/// This function detects the configured git remote and parses the owner and repo
/// from the resulting URL (supporting both HTTPS and SSH formats).
pub async fn get_repo_info<P: AsRef<Path>>(working_dir: P) -> Result<RepoInfo> {
    let working_dir = working_dir.as_ref();
    let remote = detect_first_remote(working_dir).await?;
    let url = get_remote_url(working_dir, &remote).await?;

    let (owner, repo) = parse_github_url(&url)
        .ok_or_else(|| anyhow::anyhow!("Failed to parse GitHub URL: {}", url))?;

    Ok(RepoInfo { owner, repo })
}

async fn detect_first_remote(working_dir: &Path) -> Result<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(working_dir)
        .arg("remote")
        .output()
        .await
        .context("Failed to execute git remote")?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        anyhow::bail!("Failed to list git remotes: {}", stderr.trim());
    }

    let remotes = String::from_utf8_lossy(&output.stdout);
    select_remote(&remotes).context("No git remotes configured")
}

async fn get_remote_url(working_dir: &Path, remote: &str) -> Result<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(working_dir)
        .args(["remote", "get-url", remote])
        .output()
        .await
        .with_context(|| format!("Failed to execute git remote get-url {remote}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        anyhow::bail!("Failed to get remote URL for {remote}: {}", stderr.trim());
    }

    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

fn select_remote(output: &str) -> Option<String> {
    let mut first_remote = None;

    for remote in output
        .lines()
        .map(str::trim)
        .filter(|remote| !remote.is_empty())
    {
        if remote == DEFAULT_REMOTE {
            return Some(DEFAULT_REMOTE.to_string());
        }

        first_remote.get_or_insert_with(|| remote.to_string());
    }

    first_remote
}

/// Parse a GitHub URL (HTTPS or SSH) into (owner, repo) tuple.
pub fn parse_github_url(url: &str) -> Option<(GithubOwner, GithubRepo)> {
    // Normalize SSH-style GitHub remotes to HTTPS-style.
    let normalized = url.replace("git@github.com:", "https://github.com/");

    // Only strip a trailing `.git` suffix; do not remove interior ".git" substrings
    // which may legitimately appear in owner or repo names.
    let cleaned = normalized.strip_suffix(".git").unwrap_or(&normalized);

    let parts: Vec<&str> = cleaned.split('/').collect();

    match parts.as_slice() {
        [.., owner, repo] if !owner.is_empty() && !repo.is_empty() => Some((
            GithubOwner::try_from_str(*owner).expect("validated string input is non-empty"),
            GithubRepo::try_from_str(*repo).expect("validated string input is non-empty"),
        )),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_select_remote_prefers_origin() {
        assert_eq!(
            select_remote("forgejo\norigin\n"),
            Some("origin".to_string())
        );
    }

    #[test]
    fn test_select_remote_falls_back_to_first_remote() {
        assert_eq!(
            select_remote("forgejo\nupstream\n"),
            Some("forgejo".to_string())
        );
    }

    #[test]
    fn test_parse_github_url_https() {
        let (owner, repo) = parse_github_url("https://github.com/anthropics/exomonad").unwrap();
        assert_eq!(owner.as_str(), "anthropics");
        assert_eq!(repo.as_str(), "exomonad");
    }

    #[test]
    fn test_parse_github_url_ssh() {
        let (owner, repo) = parse_github_url("git@github.com:anthropics/exomonad.git").unwrap();
        assert_eq!(owner.as_str(), "anthropics");
        assert_eq!(repo.as_str(), "exomonad");
    }

    #[test]
    fn test_parse_github_url_with_git_suffix() {
        let (owner, repo) = parse_github_url("https://github.com/anthropics/exomonad.git").unwrap();
        assert_eq!(owner.as_str(), "anthropics");
        assert_eq!(repo.as_str(), "exomonad");
    }

    #[test]
    fn test_parse_github_url_invalid() {
        assert!(parse_github_url("not-a-url").is_none());
        assert!(parse_github_url("").is_none());
    }
}
