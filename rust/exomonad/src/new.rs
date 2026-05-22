use anyhow::{anyhow, Context, Result};
use exomonad::config::Config;
use serde::{Deserialize, Serialize};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::Duration;
use tracing::{info, warn};

const DEFAULT_TANGLED_KNOT_URL: &str = "http://localhost:5555";
const DEFAULT_TANGLED_SPINDLE_URL: &str = "ws://localhost:6555";
const DEFAULT_TANGLED_APPVIEW_URL: &str = "http://localhost:3000";
const DEFAULT_TANGLED_SPINDLE_DB: &str = "spindle.db";

#[derive(Clone, Copy)]
enum ProjectLanguage {
    Rust,
    Haskell,
    Python,
    Node,
    Generic,
}

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
    let tangled_integration = discover_tangled_integration(&cwd).await;
    std::fs::write(&config_path, config_content(tangled_integration.as_ref()))?;

    let prs_path = cwd.join(".exo/prs.json");
    if !prs_path.exists() {
        std::fs::write(&prs_path, "{\"prs\":{},\"next_number\":1}\n")?;
        info!("Created .exo/prs.json (empty PR registry)");
    }

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

    if let Err(error) = scaffold_tangled_workflow(&cwd) {
        warn!(
            error = %error,
            "Failed to scaffold .github/workflows/ci.yml; continuing"
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

#[derive(Debug, Clone)]
struct TangledNewIntegration {
    knot_url: String,
    spindle_url: String,
    appview_url: String,
    owner_did: String,
    knot_container: String,
    spindle_db: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct RepoCreateRequest {
    rkey: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    name: Option<String>,
    default_branch: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    source: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RepoCreateResponse {
    repo_did: Option<String>,
}

fn config_content(tangled: Option<&TangledNewIntegration>) -> String {
    let tangled_block = match tangled {
        Some(tangled) => format!(
            "# Tangled CI integration (auto-detected by exomonad new)\n\
             tangled_knot_url = {}\n\
             tangled_spindle_url = {}\n\
             tangled_appview_url = {}\n\
             tangled_owner_did = {}\n\
             tangled_knot_container = {}\n\
             tangled_spindle_db = {}\n",
            toml_string(&tangled.knot_url),
            toml_string(&tangled.spindle_url),
            toml_string(&tangled.appview_url),
            toml_string(&tangled.owner_did),
            toml_string(&tangled.knot_container),
            toml_string(&tangled.spindle_db),
        ),
        None => "# Tangled CI integration (auto-detected when a local knot is reachable)\n\
                 # tangled_knot_url = \"http://localhost:5555\"\n\
                 # tangled_spindle_url = \"ws://localhost:6555\"\n\
                 # tangled_appview_url = \"http://localhost:3000\"\n\
                 # tangled_owner_did = \"did:plc:yourDID\"\n\
                 # tangled_knot_container = \"tangled-knot-knot-1\"\n\
                 # tangled_spindle_db = \"spindle.db\"\n"
            .to_string(),
    };

    format!(
        "# ExoMonad project config\n\
         # All fields are optional — see CLAUDE.md for full reference.\n\
         \n\
         # default_role = \"tl\"\n\
         # tmux_session = \"my-project\"\n\
         # model = \"sonnet\"\n\
         \n\
         {tangled_block}
         # Forgejo CI integration
         # forgejo_url = \"http://localhost:3000\"
         # forgejo_token = \"your_forgejo_token\"
         # forgejo_webhook_secret = \"your_webhook_secret\"
         # forgejo_ssh_port = 2222
"
    )
}

fn toml_string(value: &str) -> String {
    serde_json::to_string(value).expect("serializing a string cannot fail")
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

async fn discover_tangled_integration(project_dir: &Path) -> Option<TangledNewIntegration> {
    let knot_url = std::env::var("EXOMONAD_TANGLED_KNOT_URL")
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| DEFAULT_TANGLED_KNOT_URL.to_string());
    let knot_url = normalize_http_url(&knot_url);
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .ok()?;

    if let Err(error) = probe_tangled_knot(&client, &knot_url).await {
        warn!(
            url = %knot_url,
            error = %error,
            "Local Tangled knot probe failed; leaving config template commented"
        );
        return None;
    }

    let knot_container = match discover_tangled_knot_container() {
        Some(container) => container,
        None => {
            warn!("Local Tangled knot is reachable but no knot Docker container was discovered; leaving config template commented");
            return None;
        }
    };
    let owner_did = match discover_knot_container_env(&knot_container, "KNOT_SERVER_OWNER") {
        Some(owner) => owner,
        None => {
            warn!(
                container = %knot_container,
                "Local Tangled knot container has no KNOT_SERVER_OWNER; leaving config template commented"
            );
            return None;
        }
    };
    let knot_hostname = discover_knot_container_env(&knot_container, "KNOT_SERVER_HOSTNAME")
        .map(|value| crate::init::normalize_knot_hostname(&value))
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| crate::init::normalize_knot_hostname(&knot_url));

    let repo_name = crate::init::tangled_repo_name(project_dir);
    let repo_did = crate::init::tangled_dev_repo_did(&knot_hostname, &repo_name);
    if let Err(error) = register_tangled_repo(&client, &knot_url, &repo_name).await {
        warn!(
            repo_name,
            repo_did,
            error = %error,
            "Tangled repo.create failed; exomonad init will still use local container registration"
        );
    }

    Some(TangledNewIntegration {
        knot_url,
        spindle_url: std::env::var("EXOMONAD_TANGLED_SPINDLE_URL")
            .ok()
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| DEFAULT_TANGLED_SPINDLE_URL.to_string()),
        appview_url: std::env::var("EXOMONAD_TANGLED_APPVIEW_URL")
            .ok()
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| DEFAULT_TANGLED_APPVIEW_URL.to_string()),
        owner_did,
        knot_container: knot_container.clone(),
        spindle_db: DEFAULT_TANGLED_SPINDLE_DB.to_string(),
    })
}

async fn probe_tangled_knot(client: &reqwest::Client, knot_url: &str) -> Result<()> {
    client
        .get(knot_url)
        .send()
        .await
        .map(|_| ())
        .with_context(|| format!("failed to reach {knot_url}"))
}

async fn register_tangled_repo(
    client: &reqwest::Client,
    knot_url: &str,
    repo_name: &str,
) -> Result<Option<String>> {
    let mut request = client
        .post(xrpc_url(knot_url, "sh.tangled.repo.create"))
        .json(&repo_create_request(repo_name));
    if let Some(token) = tangled_service_auth_token() {
        request = request.bearer_auth(token);
    }

    let response = request.send().await.context("repo.create request failed")?;
    if !response.status().is_success() {
        return Err(anyhow!("repo.create returned {}", response.status()));
    }

    let body = response
        .bytes()
        .await
        .context("failed to read repo.create response")?;
    if body.is_empty() {
        return Ok(None);
    }

    serde_json::from_slice::<RepoCreateResponse>(&body)
        .map(|body| body.repo_did)
        .context("failed to decode repo.create response")
}

fn tangled_service_auth_token() -> Option<String> {
    std::env::var("EXOMONAD_TANGLED_SERVICE_AUTH")
        .ok()
        .map(|token| token.trim().to_string())
        .filter(|token| !token.is_empty())
}

fn repo_create_request(repo_name: &str) -> RepoCreateRequest {
    RepoCreateRequest {
        rkey: repo_name.to_string(),
        name: Some(repo_name.to_string()),
        default_branch: "main".to_string(),
        source: None,
    }
}

fn discover_tangled_knot_container() -> Option<String> {
    if let Ok(container) = std::env::var("EXOMONAD_TANGLED_KNOT_CONTAINER") {
        if !container.is_empty() {
            return Some(container);
        }
    }

    let output = std::process::Command::new("docker")
        .args(["ps", "--format", "{{.Names}}"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let names = String::from_utf8_lossy(&output.stdout);
    names
        .lines()
        .find(|name| name.trim() == "tangled-knot-knot-1")
        .or_else(|| names.lines().find(|name| name.contains("knot")))
        .map(|name| name.trim().to_string())
}

fn discover_knot_container_env(container: &str, key: &str) -> Option<String> {
    let script = format!("printf '%s' \"${{{key}:-}}\"");
    let output = std::process::Command::new("docker")
        .args(["exec", container, "sh", "-c", &script])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let value = String::from_utf8_lossy(&output.stdout).trim().to_string();
    (!value.is_empty()).then_some(value)
}

fn normalize_http_url(raw: &str) -> String {
    let trimmed = raw.trim().trim_end_matches('/');
    if trimmed.starts_with("http://") || trimmed.starts_with("https://") {
        trimmed.to_string()
    } else {
        format!("http://{trimmed}")
    }
}

fn xrpc_url(base: &str, method: &str) -> String {
    format!("{}/xrpc/{method}", base.trim_end_matches('/'))
}

pub(crate) fn scaffold_tangled_workflow(project_dir: &Path) -> std::io::Result<()> {
    let workflow_path = project_dir.join(".github/workflows/ci.yml");
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

    let content = tangled_workflow_content(detect_language(project_dir)?);
    let mut temp = tempfile::NamedTempFile::new_in(parent)?;
    temp.write_all(content.as_bytes())?;
    temp.flush()?;
    temp.persist(&workflow_path)
        .map(|_| ())
        .map_err(|e| e.error)?;
    info!(
        path = %workflow_path.display(),
        "Created GitHub Actions CI workflow scaffold"
    );
    Ok(())
}

fn detect_language(project_dir: &Path) -> std::io::Result<ProjectLanguage> {
    if project_dir.join("Cargo.toml").exists() {
        return Ok(ProjectLanguage::Rust);
    }
    if project_dir.join("cabal.project").exists() || has_root_cabal_file(project_dir)? {
        return Ok(ProjectLanguage::Haskell);
    }
    if project_dir.join("pyproject.toml").exists() || project_dir.join("setup.py").exists() {
        return Ok(ProjectLanguage::Python);
    }
    if project_dir.join("package.json").exists() {
        return Ok(ProjectLanguage::Node);
    }
    Ok(ProjectLanguage::Generic)
}

fn has_root_cabal_file(project_dir: &Path) -> std::io::Result<bool> {
    for entry in std::fs::read_dir(project_dir)? {
        let entry = entry?;
        if entry.file_type()?.is_file()
            && entry
                .path()
                .extension()
                .is_some_and(|extension| extension == "cabal")
        {
            return Ok(true);
        }
    }
    Ok(false)
}

fn tangled_workflow_content(language: ProjectLanguage) -> String {
    match language {
        ProjectLanguage::Rust => r#"name: CI

on:
  push:
  pull_request:

jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@stable
      - name: Build
        run: cargo build --workspace
      - name: Test
        run: cargo test --workspace
"#
        .to_string(),
        ProjectLanguage::Python => r#"name: CI

on:
  push:
  pull_request:

jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.x'
      - name: Install
        run: |
          python -m pip install --upgrade pip
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
      - name: Test
        run: |
          if [ -f pyproject.toml ]; then python -m pytest || true; fi
"#
        .to_string(),
        ProjectLanguage::Node => r#"name: CI

on:
  push:
  pull_request:

jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - name: Install
        run: npm ci || npm install
      - name: Test
        run: npm test --if-present
"#
        .to_string(),
        _ => r#"name: CI

on:
  push:
  pull_request:

jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: customize-ci
        run: |
          # TODO: Replace this placeholder with workspace-specific build and test commands.
          echo "Add CI commands"
"#
        .to_string(),
    }
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

    const REQUIRED_FRAMING: &str = "engine: nixery\nwhen:\n  - event: [push, manual]\n    branch: [\"*\"]\n  - event: [pull_request]\n    branch: [main]\nclone:\n  depth: 1\n  submodules: false\n";

    #[test]
    fn scaffolds_rust_tangled_workflow() {
        assert_language_scaffold(&[("Cargo.toml", "[package]\n")], &["rustup", "pkg-config"]);
    }

    #[test]
    fn scaffolds_haskell_tangled_workflow_from_cabal_project() {
        assert_language_scaffold(
            &[("cabal.project", "packages: .\n")],
            &["ghc", "cabal-install", "pkg-config"],
        );
    }

    #[test]
    fn scaffolds_haskell_tangled_workflow_from_root_cabal_file() {
        assert_language_scaffold(
            &[("example.cabal", "cabal-version: 3.0\n")],
            &["ghc", "cabal-install", "pkg-config"],
        );
    }

    #[test]
    fn scaffolds_python_tangled_workflow() {
        assert_language_scaffold(&[("pyproject.toml", "[project]\n")], &["python3"]);
    }

    #[test]
    fn scaffolds_node_tangled_workflow() {
        assert_language_scaffold(&[("package.json", "{}\n")], &["nodejs"]);
    }

    #[test]
    fn scaffolds_generic_tangled_workflow() {
        let content = assert_language_scaffold(&[], &["TODO: Add the nixpkgs"]);
        assert!(content.contains("nixpkgs: [] # TODO"));
    }

    #[test]
    fn scaffold_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        scaffold_tangled_workflow(dir.path()).unwrap();
        let workflow_path = dir.path().join(".github/workflows/ci.yml");
        std::fs::write(&workflow_path, "custom: true\n").unwrap();

        scaffold_tangled_workflow(dir.path()).unwrap();

        assert_eq!(
            std::fs::read_to_string(workflow_path).unwrap(),
            "custom: true\n"
        );
    }

    #[test]
    fn ignores_language_markers_below_workspace_root() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("vendor")).unwrap();
        std::fs::write(dir.path().join("vendor/Cargo.toml"), "[package]\n").unwrap();

        scaffold_tangled_workflow(dir.path()).unwrap();

        let content = read_workflow(dir.path());
        assert!(content.contains("TODO: Add the nixpkgs"));
        assert!(!content.contains("rustup"));
    }

    #[test]
    fn config_template_leaves_tangled_fields_commented_without_detection() {
        let content = config_content(None);

        assert!(content.contains("# tangled_knot_url = \"http://localhost:5555\""));
        assert!(content.contains("# tangled_owner_did = \"did:plc:yourDID\""));
        assert!(!content.contains("\ntangled_owner_did ="));
    }

    #[test]
    fn config_template_writes_active_tangled_fields_when_detected() {
        let content = config_content(Some(&TangledNewIntegration {
            knot_url: "http://localhost:5555".to_string(),
            spindle_url: "ws://localhost:6555".to_string(),
            appview_url: "http://localhost:3000".to_string(),
            owner_did: "did:plc:owner".to_string(),
            knot_container: "tangled-knot-knot-1".to_string(),
            spindle_db: "spindle.db".to_string(),
        }));

        assert!(content.contains("tangled_knot_url = \"http://localhost:5555\""));
        assert!(content.contains("tangled_spindle_url = \"ws://localhost:6555\""));
        assert!(content.contains("tangled_appview_url = \"http://localhost:3000\""));
        assert!(content.contains("tangled_owner_did = \"did:plc:owner\""));
        assert!(content.contains("tangled_knot_container = \"tangled-knot-knot-1\""));
        assert!(content.contains("tangled_spindle_db = \"spindle.db\""));
        toml::from_str::<toml::Value>(&content).unwrap();
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

    #[test]
    fn repo_create_request_matches_current_tangled_lexicon() {
        let request = repo_create_request("example");
        let json = serde_json::to_value(request).unwrap();

        assert_eq!(json["rkey"], "example");
        assert_eq!(json["name"], "example");
        assert_eq!(json["defaultBranch"], "main");
        assert!(json.get("repoDid").is_none());
        assert!(json.get("source").is_none());
    }

    #[test]
    fn service_auth_env_ignores_empty_tokens() {
        std::env::set_var("EXOMONAD_TANGLED_SERVICE_AUTH", "  ");
        assert!(tangled_service_auth_token().is_none());
        std::env::remove_var("EXOMONAD_TANGLED_SERVICE_AUTH");
    }

    #[test]
    fn normalizes_tangled_urls() {
        assert_eq!(
            normalize_http_url("localhost:5555/"),
            "http://localhost:5555"
        );
        assert_eq!(
            xrpc_url("http://localhost:5555/", "sh.tangled.repo.create"),
            "http://localhost:5555/xrpc/sh.tangled.repo.create"
        );
    }

    fn assert_language_scaffold(markers: &[(&str, &str)], expected: &[&str]) -> String {
        let dir = tempfile::tempdir().unwrap();
        for (path, content) in markers {
            std::fs::write(dir.path().join(path), content).unwrap();
        }

        scaffold_tangled_workflow(dir.path()).unwrap();

        let content = read_workflow(dir.path());
        assert!(content.contains(REQUIRED_FRAMING));
        assert!(content.contains("actions/checkout@v4") || content.contains("customize-ci"));
        assert!(
            content.contains("TODO")
                || content.contains("cargo test")
                || content.contains("npm test")
                || content.contains("pytest")
        );
        serde_yaml::from_str::<serde_yaml::Value>(&content).unwrap();
        for needle in expected {
            assert!(content.contains(needle), "missing {needle} in:\n{content}");
        }
        content
    }

    fn read_workflow(project_dir: &Path) -> String {
        let path = project_dir.join(".github/workflows/ci.yml");
        assert!(path.exists());
        std::fs::read_to_string(path).unwrap()
    }
}
