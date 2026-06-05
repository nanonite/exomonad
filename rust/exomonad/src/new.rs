use anyhow::{anyhow, Context, Result};
use exomonad::config::Config;
use serde::Deserialize;
use std::io::Write;
use std::path::{Path, PathBuf};
use tracing::{info, warn};

/// Initialize a new exomonad project in the current directory.
/// Creates .exo/config.toml, .gitignore entries, copies WASM, and rules template.
pub async fn run(_name: Option<String>) -> Result<()> {
    let cwd = std::env::current_dir()?;
    let config_path = cwd.join(".exo/config.toml");

    if config_path.exists() {
        anyhow::bail!("ExoMonad project already exists (found .exo/config.toml)");
    }

    info!("Initializing new ExoMonad project");
    std::fs::create_dir_all(cwd.join(".exo"))?;
    std::fs::write(&config_path, config_content())?;

    let policy_path = cwd.join(".exo/review-policy.toml");
    if !policy_path.exists() {
        std::fs::write(
            &policy_path,
            "# Review policy for the worktree event watcher and merge gate.
# All fields are optional — defaults shown below.

min_review_rounds = 1
reviewer_max_rounds = 2
reviewer_max_wait_seconds = 1200
review_freshness_window_secs = 1200
external_review_threshold = 300
external_review_paths = [\"proto/**\", \"rust/exomonad-core/src/handlers/**\"]
reviewer_max_rate_limit_retries = 2
require_second_reviewer_complexity = false
complexity_line_threshold = 500

[ci]
gate = \"auto\"
",
        )?;
        info!("Created .exo/review-policy.toml (default review policy)");
    }

    if let Err(error) = scaffold_forgejo_workflow(&cwd) {
        warn!(
            error = %error,
            "Failed to scaffold .forgejo/workflows/ci.yml; continuing"
        );
    }

    // Add gitignore entries
    crate::init::ensure_gitignore(&cwd)?;

    // Resolve config
    let config = Config::discover()?;
    if let (Some(forgejo_url), Some(forgejo_token)) = (
        config.forgejo_url.as_deref(),
        config.forgejo_token.as_deref(),
    ) {
        if let Err(error) = register_forgejo_repo(
            &cwd,
            forgejo_url,
            forgejo_token,
            config.forgejo_webhook_secret.as_deref(),
            config.forgejo_ssh_port,
        )
        .await
        {
            warn!(error = %error, "Forgejo repo registration skipped");
        }
    }

    // Copy WASM if it doesn't exist yet (same logic as init.rs)
    let wasm_filename = format!("wasm-guest-{}.wasm", config.wasm_name);
    let wasm_path = config.wasm_dir.join(&wasm_filename);
    if !wasm_path.exists() {
        let roles_dir = cwd.join(".exo/roles");
        if roles_dir.is_dir() {
            info!(path = %wasm_path.display(), "WASM not found, building...");
            exomonad::recompile::run_recompile(
                &config.wasm_name,
                &cwd,
                config.flake_ref.as_deref(),
            )
            .await?;
        } else if let Ok(home) = std::env::var("HOME") {
            let home = PathBuf::from(home);
            // Fall back to globally installed WASM from ~/.exo/wasm/
            let global_wasm = home.join(".exo/wasm").join(&wasm_filename);
            if global_wasm.exists() {
                info!(
                    src = %global_wasm.display(),
                    dst = %wasm_path.display(),
                    "Copying WASM from global install"
                );
                std::fs::create_dir_all(&config.wasm_dir)?;
                std::fs::copy(&global_wasm, &wasm_path)?;
            } else {
                warn!(
                    path = %wasm_path.display(),
                    "No WASM found locally or at ~/.exo/wasm/. Run 'just install-all' in the exomonad repo, or copy roles: cp -r /path/to/exomonad/.exo/roles .exo/roles"
                );
            }
        } else {
            warn!(
                path = %wasm_path.display(),
                "No WASM found locally or at ~/.exo/wasm/. Run 'just install-all' in the exomonad repo, or copy roles: cp -r /path/to/exomonad/.exo/roles .exo/roles"
            );
        }
    }

    // Write hook configuration
    let binary_path = exomonad_core::find_exomonad_binary();
    exomonad_core::hooks::HookConfig::write_persistent(&cwd, &binary_path, None, None)
        .context("Failed to write hook configuration")?;
    info!("Hook configuration written to .claude/settings.local.json");

    // Copy Claude rules template if available and not already present (same logic as init.rs)
    {
        let rules_dest = cwd.join(".claude/rules/exomonad.md");
        if !rules_dest.exists() {
            // Resolution: project-local .exo/rules/ → global ~/.exo/rules/
            let local_template = cwd.join(".exo/rules/exomonad.md");
            let global_template = std::env::var("HOME")
                .ok()
                .map(|h| PathBuf::from(h).join(".exo/rules/exomonad.md"));

            let source = if local_template.exists() {
                Some(local_template)
            } else {
                global_template.filter(|p| p.exists())
            };

            if let Some(src) = source {
                std::fs::create_dir_all(cwd.join(".claude/rules"))?;
                std::fs::copy(&src, &rules_dest)?;
                info!(
                    src = %src.display(),
                    "Copied Claude rules to .claude/rules/exomonad.md"
                );
            }
        }
    }

    info!("Project initialized. Run `exomonad init` to start a session.");
    Ok(())
}

fn config_content() -> String {
    r#"# ExoMonad project config
# All fields are optional - see CLAUDE.md for full reference.

# Forgejo integration
# forgejo_url = "http://localhost:3000"
# forgejo_token = "..."
# forgejo_reviewer_token = "..."  # must belong to a different Forgejo user than forgejo_token
"#
    .to_string()
}

fn forgejo_ssh_remote_url(
    forgejo_url: &str,
    configured_port: Option<u16>,
    owner: &str,
    repo_name: &str,
) -> Result<String> {
    let base = normalize_http_url(forgejo_url);
    let parsed = reqwest::Url::parse(&base)
        .with_context(|| format!("invalid forgejo_url for SSH remote: {base}"))?;
    let host = parsed
        .host_str()
        .ok_or_else(|| anyhow!("forgejo_url has no host: {base}"))?;
    let port = configured_port.unwrap_or_else(|| default_forgejo_ssh_port(host));
    let host = ssh_remote_host(host);
    Ok(format!("ssh://git@{host}:{port}/{owner}/{repo_name}.git"))
}

fn default_forgejo_ssh_port(host: &str) -> u16 {
    match host {
        "localhost" | "127.0.0.1" | "::1" => 2222,
        _ => 22,
    }
}

fn ssh_remote_host(host: &str) -> String {
    if host.contains(':') && !host.starts_with('[') {
        format!("[{host}]")
    } else {
        host.to_string()
    }
}

fn normalize_http_url(raw: &str) -> String {
    let trimmed = raw.trim().trim_end_matches('/');
    if trimmed.starts_with("http://") || trimmed.starts_with("https://") {
        trimmed.to_string()
    } else {
        format!("http://{trimmed}")
    }
}

pub(crate) fn scaffold_forgejo_workflow(project_dir: &Path) -> std::io::Result<()> {
    let workflow_path = project_dir.join(".forgejo/workflows/ci.yml");
    if workflow_path.exists() {
        info!(
            path = %workflow_path.display(),
            "CI workflow already present, leaving untouched"
        );
        return Ok(());
    }

    let parent = workflow_path.parent().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!("Path has no parent: {}", workflow_path.display()),
        )
    })?;
    std::fs::create_dir_all(parent)?;

    let content = forgejo_workflow_content();
    let mut temp = tempfile::NamedTempFile::new_in(parent)?;
    temp.write_all(content.as_bytes())?;
    temp.flush()?;
    temp.persist(&workflow_path)
        .map(|_| ())
        .map_err(|e| e.error)?;
    info!(
        path = %workflow_path.display(),
        "Created Forgejo Actions CI workflow scaffold"
    );
    Ok(())
}

fn forgejo_workflow_content() -> &'static str {
    r#"name: CI

# =====================================================================
# EXOMONAD GENERATED PLACEHOLDER
# Replace this scaffold with the build, test, lint, and release checks
# your project needs before relying on Forgejo/Gitea Actions gating.
# =====================================================================

on: push

jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        run: |
          git clone --depth 1 "${{ github.server_url }}/${{ github.repository }}.git" .
          git checkout "${{ github.sha }}"

      - name: Customize workflow
        run: |
          echo "Replace .forgejo/workflows/ci.yml with project-specific CI commands."
          echo "Add real build, test, lint, and release checks before merge gating."
"#
}

#[derive(Debug, Deserialize)]
struct ForgejoRepoOwner {
    login: String,
}

#[derive(Debug, Deserialize)]
struct ForgejoRepoResponse {
    owner: ForgejoRepoOwner,
}

#[derive(Debug, Deserialize)]
struct ForgejoUserResponse {
    login: String,
}

async fn register_forgejo_repo(
    project_dir: &Path,
    forgejo_url: &str,
    forgejo_token: &str,
    webhook_secret: Option<&str>,
    forgejo_ssh_port: Option<u16>,
) -> Result<()> {
    let client = reqwest::Client::new();
    let base = normalize_http_url(forgejo_url);
    let repo_name = project_dir
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("workspace")
        .to_string();

    let create_url = format!("{}/api/v1/user/repos", base.trim_end_matches('/'));
    let create_resp = client
        .post(&create_url)
        .bearer_auth(forgejo_token)
        .json(&serde_json::json!({"name": repo_name, "auto_init": false, "private": false}))
        .send()
        .await
        .context("forgejo repo create request failed")?;

    let owner = if create_resp.status().is_success() {
        let created: ForgejoRepoResponse = create_resp
            .json()
            .await
            .context("forgejo repo create decode failed")?;
        created.owner.login
    } else {
        let user_url = format!("{}/api/v1/user", base.trim_end_matches('/'));
        let user: ForgejoUserResponse = client
            .get(&user_url)
            .bearer_auth(forgejo_token)
            .send()
            .await
            .context("forgejo user lookup failed")?
            .error_for_status()
            .context("forgejo user lookup status error")?
            .json()
            .await
            .context("forgejo user decode failed")?;
        user.login
    };

    let remote_url = forgejo_ssh_remote_url(&base, forgejo_ssh_port, &owner, &repo_name)?;
    let _ = std::process::Command::new("git")
        .arg("remote")
        .arg("remove")
        .arg("forgejo")
        .current_dir(project_dir)
        .status();
    let _ = std::process::Command::new("git")
        .arg("remote")
        .arg("add")
        .arg("forgejo")
        .arg(&remote_url)
        .current_dir(project_dir)
        .status();

    let server_base = std::env::var("EXOMONAD_SERVER_URL")
        .unwrap_or_else(|_| "http://127.0.0.1:3001".to_string())
        .trim_end_matches('/')
        .to_string();
    let webhook_url = format!("{server_base}/ci");
    let hooks_url = format!(
        "{}/api/v1/repos/{}/{}/hooks",
        base.trim_end_matches('/'),
        owner,
        repo_name
    );
    let hook_payload = serde_json::json!({
        "type": "gitea",
        "active": true,
        "events": ["workflow_run", "check_run"],
        "config": {
            "url": webhook_url,
            "content_type": "json",
            "secret": webhook_secret.unwrap_or("")
        }
    });
    let _ = client
        .post(&hooks_url)
        .bearer_auth(forgejo_token)
        .json(&hook_payload)
        .send()
        .await;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scaffolds_forgejo_ci_placeholder_workflow() {
        let content = assert_forgejo_scaffold(&[]);

        assert!(content.contains("EXOMONAD GENERATED PLACEHOLDER"));
        assert!(content.contains("on: push"));
        assert!(content.contains("jobs:\n  ci:"));
        assert!(content.contains("git clone --depth 1"));
        assert!(content.contains("${{ github.server_url }}/${{ github.repository }}.git"));
        assert!(!content.contains("actions/checkout"));
        assert!(content.contains("Customize workflow"));
        assert!(content.contains("Replace .forgejo/workflows/ci.yml"));
        assert!(!content.contains("TODO"));
    }

    #[test]
    fn scaffold_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        scaffold_forgejo_workflow(dir.path()).unwrap();
        let workflow_path = dir.path().join(".forgejo/workflows/ci.yml");
        std::fs::write(&workflow_path, "custom: true\n").unwrap();

        scaffold_forgejo_workflow(dir.path()).unwrap();

        assert_eq!(
            std::fs::read_to_string(workflow_path).unwrap(),
            "custom: true\n"
        );
    }

    #[test]
    fn scaffold_uses_forgejo_workflow_path() {
        let dir = tempfile::tempdir().unwrap();
        scaffold_forgejo_workflow(dir.path()).unwrap();

        assert!(dir.path().join(".forgejo/workflows/ci.yml").exists());
        assert!(!dir.path().join(".gitea/workflows/ci.yml").exists());
        assert!(!dir.path().join(".github/workflows/ci.yml").exists());
    }

    #[test]
    fn forgejo_ssh_remote_url_defaults_localhost_to_dev_ssh_port() {
        assert_eq!(
            forgejo_ssh_remote_url("http://localhost:3000", None, "exomonad", "demo").unwrap(),
            "ssh://git@localhost:2222/exomonad/demo.git"
        );
    }

    #[test]
    fn forgejo_ssh_remote_url_uses_configured_port() {
        assert_eq!(
            forgejo_ssh_remote_url("https://forge.example", Some(2223), "exomonad", "demo")
                .unwrap(),
            "ssh://git@forge.example:2223/exomonad/demo.git"
        );
    }

    fn assert_forgejo_scaffold(markers: &[(&str, &str)]) -> String {
        let dir = tempfile::tempdir().unwrap();
        for (path, content) in markers {
            std::fs::write(dir.path().join(path), content).unwrap();
        }

        scaffold_forgejo_workflow(dir.path()).unwrap();

        let content = read_workflow(dir.path());
        serde_yaml::from_str::<serde_yaml::Value>(&content).unwrap();
        content
    }

    fn read_workflow(project_dir: &Path) -> String {
        let path = project_dir.join(".forgejo/workflows/ci.yml");
        assert!(path.exists());
        std::fs::read_to_string(path).unwrap()
    }
}
