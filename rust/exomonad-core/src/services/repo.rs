use crate::domain::{GithubOwner, GithubRepo};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::Path;
use tokio::process::Command;

const DEFAULT_REMOTE: &str = "origin";

/// Git config key holding an explicit remote-name override, set by
/// `exomonad init --set-git-remote <name>` via `git config --local`. Lets a
/// project pin which remote exomonad's PR/CI operations use when multiple
/// remotes are configured (e.g. a GitHub `origin` alongside a Forgejo
/// remote) — without it, the wrong remote's owner/repo can leak into calls
/// meant for a different backend.
const REMOTE_OVERRIDE_KEY: &str = "exomonad.remote";
const REPOSITORY_CREDENTIAL_PARAMETERS: &[&str] = &[
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "oauth_token",
    "password",
    "passwd",
    "private_token",
    "sig",
    "signature",
    "token",
];

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
/// If `exomonad.remote` is set in git config (local or global), that remote
/// is used directly. Otherwise, this function detects the configured git
/// remote (preferring `origin`) and parses the owner and repo from the
/// resulting URL (supporting both HTTPS and SSH formats).
pub async fn get_repo_info<P: AsRef<Path>>(working_dir: P) -> Result<RepoInfo> {
    let working_dir = working_dir.as_ref();
    let remote = match configured_remote(working_dir).await? {
        Some(remote) => remote,
        None => detect_first_remote(working_dir).await?,
    };
    let url = get_remote_url(working_dir, &remote).await?;

    let (owner, repo) = parse_github_url(&url)?;

    Ok(RepoInfo { owner, repo })
}

/// Repository routing metadata used by cleanup safety checks.
///
/// This is not the durable TL run identity. Runnable runs store only the
/// canonical remote URL returned by [`get_repository_remote_url`]; branch and
/// cleanup routing details remain separate from that identity.
#[derive(Debug, Serialize, Deserialize, PartialEq, Clone)]
pub struct RepositoryIdentity {
    pub owner: GithubOwner,
    pub repo: GithubRepo,
    /// The remote's default branch (e.g. "main"), read from the locally
    /// recorded `refs/remotes/<remote>/HEAD` symbolic ref. Never guessed.
    pub base_branch: String,
    /// Remote hostname (e.g. "forge.example.com").
    pub forge_host: String,
    /// The remote URL with any embedded userinfo credentials stripped.
    pub remote_url: String,
    /// The resolved remote name (e.g. "origin", "forgejo").
    pub remote_name: String,
}

/// Resolve the selected remote as the sole durable run repository identity.
///
/// Unlike [`get_repository_identity`], this does not parse API coordinates or
/// require a locally recorded remote default branch. Those facts are not part
/// of repository identity and the configured integration branch is separate.
pub async fn get_repository_remote_url<P: AsRef<Path>>(working_dir: P) -> Result<String> {
    let working_dir = working_dir.as_ref();
    let remote = match configured_remote(working_dir).await? {
        Some(remote) => remote,
        None => detect_first_remote(working_dir).await?,
    };
    let url = get_remote_url(working_dir, &remote).await?;
    canonical_repository_url(&url)
}

/// Canonicalize one supported repository address without retaining credentials.
///
/// Supported inputs are HTTP(S), `ssh://`, and relative scp-like SSH remotes.
/// Host/scheme casing is normalized, transport users and recognized
/// credential-only query/fragment parameters are removed, and explicit ports
/// and repository path spelling are preserved. Unknown query/fragment
/// parameters are rejected because they may route to a different repository.
/// Error messages never include the supplied URL.
pub fn canonical_repository_url(input: &str) -> Result<String> {
    let value = input.trim();
    if value.is_empty() || value.chars().any(char::is_whitespace) {
        anyhow::bail!("repository remote URL is missing or malformed");
    }
    if let Some((scheme, rest)) = value.split_once("://") {
        return canonical_url_with_scheme(scheme, rest);
    }
    canonical_scp_remote(value)
}

fn canonical_url_with_scheme(scheme: &str, rest: &str) -> Result<String> {
    let scheme = scheme.to_ascii_lowercase();
    if !matches!(scheme.as_str(), "http" | "https" | "ssh") {
        anyhow::bail!("repository remote URL uses an unsupported transport");
    }
    let authority_end = rest
        .find('/')
        .context("repository remote URL is malformed")?;
    let raw_authority = &rest[..authority_end];
    let suffix = &rest[authority_end..];
    let (path, query, fragment) = split_url_suffix(suffix)?;
    validate_removable_parameters(query, "query")?;
    validate_removable_parameters(fragment, "fragment")?;
    let authority = canonical_authority(raw_authority)?;
    let path = canonical_repository_path(path)?;
    Ok(format!("{scheme}://{authority}{path}"))
}

fn split_url_suffix(suffix: &str) -> Result<(&str, Option<&str>, Option<&str>)> {
    let fragment_start = suffix.find('#');
    let before_fragment = fragment_start.map_or(suffix, |index| &suffix[..index]);
    let fragment = fragment_start.map(|index| &suffix[index + 1..]);
    let query_start = before_fragment.find('?');
    let path = query_start.map_or(before_fragment, |index| &before_fragment[..index]);
    let query = query_start.map(|index| &before_fragment[index + 1..]);
    if query == Some("") || fragment == Some("") {
        anyhow::bail!("repository remote URL has an ambiguous empty query or fragment");
    }
    Ok((path, query, fragment))
}

fn canonical_scp_remote(value: &str) -> Result<String> {
    let user_separator = value.find('@');
    if user_separator == Some(0) {
        anyhow::bail!("repository remote URL is malformed or unsupported");
    }
    let host_start = user_separator.map_or(0, |index| index + 1);
    let (authority, path) = if value[host_start..].starts_with('[') {
        let close = value[host_start..]
            .find(']')
            .context("repository remote URL is malformed or unsupported")?;
        let separator = host_start + close + 1;
        if value.as_bytes().get(separator) != Some(&b':') {
            anyhow::bail!("repository remote URL is malformed or unsupported");
        }
        (&value[..separator], &value[separator + 1..])
    } else {
        value
            .split_once(':')
            .context("repository remote URL is malformed or unsupported")?
    };
    if path.is_empty() || path.starts_with('/') || path.contains('?') || path.contains('#') {
        anyhow::bail!("absolute or ambiguous scp-like repository paths are unsupported");
    }
    if authority.matches('@').count() > 1 || authority.starts_with('@') {
        anyhow::bail!("repository remote URL is malformed or unsupported");
    }
    let authority = canonical_authority(authority)?;
    let path = canonical_repository_path(&format!("/{path}"))?;
    Ok(format!("ssh://{authority}{path}"))
}

fn canonical_authority(value: &str) -> Result<String> {
    let authority = value.rsplit('@').next().unwrap_or_default();
    if authority.is_empty() {
        anyhow::bail!("repository remote URL is malformed");
    }
    let (host, port) = if authority.starts_with('[') {
        let close = authority
            .find(']')
            .context("repository remote URL has an invalid host")?;
        let host = &authority[..=close];
        let suffix = &authority[close + 1..];
        if !suffix.is_empty() && !suffix.starts_with(':') {
            anyhow::bail!("repository remote URL is malformed");
        }
        let port = if suffix.is_empty() {
            None
        } else {
            Some(&suffix[1..])
        };
        (canonical_host(host)?, port)
    } else {
        if authority.matches(':').count() > 1 {
            anyhow::bail!("IPv6 repository hosts must use brackets");
        }
        let (host, port) = authority
            .split_once(':')
            .map_or((authority, None), |(host, port)| (host, Some(port)));
        (canonical_host(host)?, port)
    };
    match port {
        None => Ok(host),
        Some(port)
            if !port.is_empty()
                && port.bytes().all(|byte| byte.is_ascii_digit())
                && port.parse::<u16>().is_ok_and(|value| value > 0) =>
        {
            Ok(format!("{host}:{port}"))
        }
        Some(_) => anyhow::bail!("repository remote URL has an invalid port"),
    }
}

fn canonical_host(host: &str) -> Result<String> {
    if host.starts_with('[') && host.ends_with(']') {
        let address = host[1..host.len() - 1]
            .parse::<std::net::Ipv6Addr>()
            .context("repository remote URL has an invalid host")?;
        return Ok(format!("[{address}]"));
    }
    if host.is_empty() || !host.is_ascii() || host.ends_with('.') {
        anyhow::bail!("repository remote URL has an invalid host");
    }
    for label in host.split('.') {
        if label.is_empty()
            || label.len() > 63
            || label.starts_with('-')
            || label.ends_with('-')
            || !label
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        {
            anyhow::bail!("repository remote URL has an invalid host");
        }
    }
    Ok(host.to_ascii_lowercase())
}

fn canonical_repository_path(path: &str) -> Result<String> {
    if !path.starts_with('/') || path.len() < 2 {
        anyhow::bail!("repository remote URL has no repository path");
    }
    if !path.is_ascii()
        || path.chars().any(|character| {
            character.is_whitespace() || character.is_control() || character == '\\'
        })
    {
        anyhow::bail!("repository remote URL has a malformed path");
    }
    let bytes = path.as_bytes();
    let mut normalized = String::with_capacity(path.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' {
            if index + 2 >= bytes.len()
                || !bytes[index + 1].is_ascii_hexdigit()
                || !bytes[index + 2].is_ascii_hexdigit()
            {
                anyhow::bail!("repository remote URL has a malformed path");
            }
            normalized.push('%');
            normalized.push((bytes[index + 1] as char).to_ascii_uppercase());
            normalized.push((bytes[index + 2] as char).to_ascii_uppercase());
            index += 3;
        } else {
            normalized.push(bytes[index] as char);
            index += 1;
        }
    }
    for raw_segment in normalized.split('/') {
        let decoded = decode_percent_component(raw_segment)?;
        let segment = decoded.as_str();
        if matches!(segment, "." | "..") {
            anyhow::bail!("repository remote URL has an ambiguous path");
        }
        if REPOSITORY_CREDENTIAL_PARAMETERS.contains(&segment.to_ascii_lowercase().as_str()) {
            anyhow::bail!("repository remote URL path contains a credential marker");
        }
    }
    Ok(normalized)
}

fn validate_removable_parameters(value: Option<&str>, component: &str) -> Result<()> {
    let Some(value) = value else {
        return Ok(());
    };
    let mut found = false;
    for parameter in value.split('&') {
        let (key, _) = parameter
            .split_once('=')
            .with_context(|| format!("repository remote URL has a malformed {component}"))?;
        let decoded = decode_percent_component(key)?.replace('+', " ");
        if decoded.is_empty()
            || !REPOSITORY_CREDENTIAL_PARAMETERS.contains(&decoded.to_ascii_lowercase().as_str())
        {
            anyhow::bail!("repository remote URL has an ambiguous {component}");
        }
        found = true;
    }
    if !found {
        anyhow::bail!("repository remote URL has an ambiguous {component}");
    }
    Ok(())
}

fn decode_percent_component(value: &str) -> Result<String> {
    let bytes = value.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' {
            if index + 2 >= bytes.len() {
                anyhow::bail!("repository remote URL has a malformed escape");
            }
            let hex = std::str::from_utf8(&bytes[index + 1..index + 3])
                .context("repository remote URL has a malformed escape")?;
            let byte = u8::from_str_radix(hex, 16)
                .context("repository remote URL has a malformed escape")?;
            decoded.push(byte);
            index += 3;
        } else {
            decoded.push(bytes[index]);
            index += 1;
        }
    }
    String::from_utf8(decoded).context("repository remote URL has an invalid encoding")
}

/// Resolve the complete repository identity from the pinned or auto-detected
/// git remote, following the same remote-selection rules as
/// [`get_repo_info`]: honors `exomonad.remote` when set, otherwise prefers
/// `origin`. Fails closed — no field is inferred by convention when the
/// remote, its default branch, or its URL cannot be resolved unambiguously.
pub async fn get_repository_identity<P: AsRef<Path>>(working_dir: P) -> Result<RepositoryIdentity> {
    let working_dir = working_dir.as_ref();
    let remote = match configured_remote(working_dir).await? {
        Some(remote) => remote,
        None => detect_first_remote(working_dir).await?,
    };
    let url = get_remote_url(working_dir, &remote).await?;
    let (owner, repo) = parse_github_url(&url)?;
    let base_branch = get_remote_default_branch(working_dir, &remote).await?;
    let forge_host = parse_forge_host(&url)?;
    let remote_url = sanitize_remote_url(&url);

    Ok(RepositoryIdentity {
        owner,
        repo,
        base_branch,
        forge_host,
        remote_url,
        remote_name: remote,
    })
}

/// Read a remote's default branch from the locally recorded
/// `refs/remotes/<remote>/HEAD` symbolic ref. This is set by `git clone` and
/// by `git remote set-head`; it is never re-derived by convention (e.g.
/// assuming "main") when absent — that is exactly the kind of guess this
/// resolution must not make.
async fn get_remote_default_branch(working_dir: &Path, remote: &str) -> Result<String> {
    let target = format!("refs/remotes/{remote}/HEAD");
    let output = Command::new("git")
        .arg("-C")
        .arg(working_dir)
        .args(["symbolic-ref", "--short", &target])
        .output()
        .await
        .with_context(|| format!("Failed to execute git symbolic-ref {target}"))?;

    if !output.status.success() {
        anyhow::bail!(
            "Remote {remote:?}'s default branch is not recorded locally ({target} is unset); \
             run `git remote set-head {remote} --auto` (or `git remote set-head {remote} <branch>`) \
             to record it"
        );
    }

    let value = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let branch = value
        .strip_prefix(&format!("{remote}/"))
        .unwrap_or(&value)
        .to_string();
    if branch.is_empty() {
        anyhow::bail!("Remote {remote:?}'s recorded default branch ({target}) is empty");
    }
    Ok(branch)
}

/// Extract the hostname from an HTTP(S) or SSH (URL or scp-like) git remote.
fn parse_forge_host(url: &str) -> Result<String> {
    if let Ok(parsed) = url::Url::parse(url) {
        if let Some(host) = parsed.host_str() {
            return Ok(host.to_string());
        }
    }
    // scp-like syntax: user@host:owner/repo.git — url::Url cannot parse this
    // (no scheme), so fall back to a direct split on the same rule
    // normalize_remote_url already uses to recognize this form.
    if let Some((user_host, path)) = url.split_once(':') {
        if let Some((_, host)) = user_host.rsplit_once('@') {
            if !host.is_empty() && !path.is_empty() {
                return Ok(host.to_string());
            }
        }
    }
    anyhow::bail!("Failed to parse forge host from remote URL: {url}")
}

/// Strip embedded userinfo credentials (`user:token@`) from an HTTP(S)
/// remote URL. scp-like SSH remotes (`git@host:owner/repo.git`) carry no
/// parseable credential slot in this form — their `user@` names the
/// transport user, not a secret — so they are returned unchanged.
fn sanitize_remote_url(url: &str) -> String {
    if let Ok(mut parsed) = url::Url::parse(url) {
        if parsed.username().is_empty() && parsed.password().is_none() {
            return parsed.to_string();
        }
        if parsed.set_username("").is_ok() && parsed.set_password(None).is_ok() {
            return parsed.to_string();
        }
    }
    url.to_string()
}

/// Read the `exomonad.remote` git config override, if set.
///
/// `git config --get` exits 1 when the key is absent — that is not an
/// error, it just means no override is configured.
async fn configured_remote(working_dir: &Path) -> Result<Option<String>> {
    let output = Command::new("git")
        .arg("-C")
        .arg(working_dir)
        .args(["config", "--get", REMOTE_OVERRIDE_KEY])
        .output()
        .await
        .with_context(|| format!("Failed to execute git config --get {REMOTE_OVERRIDE_KEY}"))?;

    if !output.status.success() {
        return Ok(None);
    }

    let value = String::from_utf8_lossy(&output.stdout).trim().to_string();
    Ok(if value.is_empty() { None } else { Some(value) })
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

/// Parse a Forgejo/GitHub URL (HTTP(S) or SSH) into (owner, repo) tuple.
pub fn parse_github_url(url: &str) -> Result<(GithubOwner, GithubRepo)> {
    let normalized = normalize_remote_url(url)?;

    // Only strip a trailing `.git` suffix; do not remove interior ".git" substrings
    // which may legitimately appear in owner or repo names.
    let cleaned = normalized.strip_suffix(".git").unwrap_or(normalized);

    let parts: Vec<&str> = cleaned.split('/').collect();

    match parts.as_slice() {
        [.., owner, repo] if !owner.is_empty() && !repo.is_empty() => Ok((
            GithubOwner::try_from_str(owner)
                .with_context(|| format!("Invalid repository owner in remote URL: {url}"))?,
            GithubRepo::try_from_str(repo)
                .with_context(|| format!("Invalid repository name in remote URL: {url}"))?,
        )),
        _ => anyhow::bail!("Failed to parse Forgejo remote URL: {url}"),
    }
}

fn normalize_remote_url(url: &str) -> Result<&str> {
    let trimmed = url.trim();

    if trimmed.is_empty() {
        anyhow::bail!("Failed to parse Forgejo remote URL: remote URL is empty");
    }

    if let Some((_, path)) = trimmed.split_once("://") {
        return Ok(path);
    }

    if let Some((host, path)) = trimmed.split_once(':') {
        if host.contains('@') && !path.is_empty() {
            return Ok(path);
        }
    }

    if !trimmed.contains('@') {
        anyhow::bail!("Remote is a local path. Set your remote to the Forgejo HTTP URL: git remote set-url origin http://<forgejo>/owner/repo");
    }

    anyhow::bail!("Failed to parse Forgejo remote URL: {url}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use exomonad_test_support::{
        assert_fixture_git_root, init_fixture_git_repository, ScrubGitRepositoryEnv,
    };
    use serde::Deserialize;

    #[derive(Deserialize)]
    struct RepositoryUrlVector {
        name: String,
        input: String,
        canonical: Option<String>,
        error: Option<String>,
    }

    #[test]
    fn canonical_repository_url_matches_shared_python_vectors() {
        let vectors: Vec<RepositoryUrlVector> = serde_json::from_str(include_str!(
            "../../../../tl_loop/tests/fixtures/repository_identity_url_vectors.json"
        ))
        .expect("shared repository URL vectors are valid JSON");

        for vector in vectors {
            match (canonical_repository_url(&vector.input), vector.canonical, vector.error) {
                (Ok(actual), Some(expected), None) => {
                    assert_eq!(actual, expected, "{}", vector.name);
                    assert_eq!(canonical_repository_url(&actual).unwrap(), actual, "{}", vector.name);
                    assert!(!actual.contains("sample-secret"), "{}", vector.name);
                    assert!(!actual.contains("alice"), "{}", vector.name);
                }
                (Err(error), None, Some(expected)) => {
                    assert!(error.to_string().contains(&expected), "{}", vector.name);
                    assert!(!error.to_string().contains("sample-secret"), "{}", vector.name);
                }
                (result, canonical, error) => panic!(
                    "{}: vector outcome mismatch (result={result:?}, canonical={canonical:?}, error={error:?})",
                    vector.name
                ),
            }
        }
    }

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
    fn test_parse_github_url_forgejo_ssh() {
        let (owner, repo) = parse_github_url("git@localhost:anthropics/exomonad.git").unwrap();
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
        assert!(parse_github_url("git@localhost").is_err());
        assert!(parse_github_url("").is_err());
    }

    #[test]
    fn test_parse_github_url_rejects_local_path_with_fix() {
        let err = parse_github_url("/home/goya/bare/backrooms.git")
            .expect_err("local paths must not parse as owner/repo")
            .to_string();

        assert!(err.contains("Remote is a local path"));
        assert!(err.contains("git remote set-url origin http://<forgejo>/owner/repo"));
    }

    async fn init_repo_with_remotes(remotes: &[(&str, &str)]) -> tempfile::TempDir {
        let tmp = tempfile::tempdir().unwrap();
        init_fixture_git_repository(tmp.path()).unwrap();
        for (name, url) in remotes {
            assert_fixture_git_root(tmp.path()).unwrap();
            Command::new("git")
                .args(["remote", "add", name, url])
                .current_dir(tmp.path())
                .scrub_git_repository_env()
                .output()
                .await
                .unwrap();
        }
        tmp
    }

    #[tokio::test]
    async fn get_repo_info_auto_detects_origin_when_no_override_configured() {
        let tmp = init_repo_with_remotes(&[
            ("forgejo", "http://localhost:3000/goya/repo.git"),
            ("origin", "https://github.com/nanonite/repo.git"),
        ])
        .await;

        let info = get_repo_info(tmp.path()).await.unwrap();

        assert_eq!(info.owner.as_str(), "nanonite");
        assert_eq!(info.repo.as_str(), "repo");
    }

    #[tokio::test]
    async fn get_repo_info_honors_exomonad_remote_override() {
        let tmp = init_repo_with_remotes(&[
            ("origin", "https://github.com/nanonite/repo.git"),
            ("forgejo", "http://localhost:3000/goya/repo.git"),
        ])
        .await;
        assert_fixture_git_root(tmp.path()).unwrap();
        Command::new("git")
            .args(["config", "--local", "exomonad.remote", "forgejo"])
            .current_dir(tmp.path())
            .scrub_git_repository_env()
            .output()
            .await
            .unwrap();

        let info = get_repo_info(tmp.path()).await.unwrap();

        assert_eq!(info.owner.as_str(), "goya");
        assert_eq!(info.repo.as_str(), "repo");
    }

    #[tokio::test]
    async fn configured_remote_returns_none_when_unset() {
        let tmp =
            init_repo_with_remotes(&[("origin", "https://github.com/nanonite/repo.git")]).await;

        assert_eq!(configured_remote(tmp.path()).await.unwrap(), None);
    }

    #[tokio::test]
    async fn configured_remote_returns_configured_value() {
        let tmp =
            init_repo_with_remotes(&[("forgejo", "http://localhost:3000/goya/repo.git")]).await;
        assert_fixture_git_root(tmp.path()).unwrap();
        Command::new("git")
            .args(["config", "--local", "exomonad.remote", "forgejo"])
            .current_dir(tmp.path())
            .scrub_git_repository_env()
            .output()
            .await
            .unwrap();

        assert_eq!(
            configured_remote(tmp.path()).await.unwrap(),
            Some("forgejo".to_string())
        );
    }

    async fn set_remote_head(tmp: &Path, remote: &str, branch: &str) {
        Command::new("git")
            .args([
                "symbolic-ref",
                &format!("refs/remotes/{remote}/HEAD"),
                &format!("refs/remotes/{remote}/{branch}"),
            ])
            .current_dir(tmp)
            .scrub_git_repository_env()
            .output()
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn get_repository_identity_honors_exomonad_remote_pin() {
        let tmp = init_repo_with_remotes(&[
            ("origin", "https://github.com/nanonite/repo.git"),
            (
                "forgejo",
                "https://x-access-token:secret-token@forge.example.com/goya/repo.git",
            ),
        ])
        .await;
        assert_fixture_git_root(tmp.path()).unwrap();
        Command::new("git")
            .args(["config", "--local", "exomonad.remote", "forgejo"])
            .current_dir(tmp.path())
            .scrub_git_repository_env()
            .output()
            .await
            .unwrap();
        set_remote_head(tmp.path(), "forgejo", "main").await;

        let identity = get_repository_identity(tmp.path()).await.unwrap();

        assert_eq!(identity.owner.as_str(), "goya");
        assert_eq!(identity.repo.as_str(), "repo");
        assert_eq!(identity.base_branch, "main");
        assert_eq!(identity.remote_name, "forgejo");
        assert_eq!(identity.forge_host, "forge.example.com");
        assert_eq!(
            identity.remote_url,
            "https://forge.example.com/goya/repo.git"
        );
        assert!(!identity.remote_url.contains("secret-token"));
        assert!(!identity.remote_url.contains("x-access-token"));
    }

    #[tokio::test]
    async fn get_repository_identity_fails_closed_when_default_branch_is_not_recorded() {
        let tmp =
            init_repo_with_remotes(&[("origin", "https://github.com/nanonite/repo.git")]).await;
        // No `git remote set-head` was ever run: refs/remotes/origin/HEAD is
        // absent, so the default branch is genuinely ambiguous.

        let error = get_repository_identity(tmp.path())
            .await
            .expect_err("an unrecorded default branch must not be guessed")
            .to_string();

        assert!(error.contains("not recorded locally"));
        assert!(error.contains("origin"));
    }

    #[tokio::test]
    async fn get_repository_remote_url_does_not_require_remote_default_branch() {
        let tmp = init_repo_with_remotes(&[(
            "origin",
            "https://user:secret@Forge.Example.com:8443/Org/Repo.git",
        )])
        .await;

        let remote_url = get_repository_remote_url(tmp.path()).await.unwrap();

        assert_eq!(remote_url, "https://forge.example.com:8443/Org/Repo.git");
        assert!(!remote_url.contains("secret"));
        assert!(!remote_url.contains("user"));
    }

    #[test]
    fn sanitize_remote_url_strips_embedded_credentials() {
        let sanitized =
            sanitize_remote_url("https://x-access-token:secret-token@forge.example.com/o/r.git");
        assert_eq!(sanitized, "https://forge.example.com/o/r.git");
        assert!(!sanitized.contains("secret-token"));
    }

    #[test]
    fn sanitize_remote_url_leaves_credential_free_url_unchanged() {
        let sanitized = sanitize_remote_url("https://forge.example.com/o/r.git");
        assert_eq!(sanitized, "https://forge.example.com/o/r.git");
    }

    #[test]
    fn sanitize_remote_url_leaves_scp_like_ssh_url_unchanged() {
        let sanitized = sanitize_remote_url("git@forge.example.com:o/r.git");
        assert_eq!(sanitized, "git@forge.example.com:o/r.git");
    }

    #[test]
    fn parse_forge_host_from_https_url() {
        assert_eq!(
            parse_forge_host("https://x:y@forge.example.com/o/r.git").unwrap(),
            "forge.example.com"
        );
    }

    #[test]
    fn parse_forge_host_from_scp_like_ssh_url() {
        assert_eq!(
            parse_forge_host("git@forge.example.com:o/r.git").unwrap(),
            "forge.example.com"
        );
    }
}
