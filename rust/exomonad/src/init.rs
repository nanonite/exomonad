use crate::uds_client;
use anyhow::{Context, Result};
use exomonad::config::Config;
use exomonad_core::services::AgentType;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tracing::{debug, info, warn};

/// Read chainlink-tl.md from the project directory, strip YAML frontmatter,
/// and return the content. Returns None if the file cannot be read (non-fatal).
fn read_chainlink_tl_protocol(cwd: &Path) -> Option<String> {
    let path = cwd.join(".exo/roles/devswarm/context/chainlink-tl.md");
    let content = std::fs::read_to_string(&path).ok()?;
    let stripped = if content.starts_with("---") {
        if let Some(end) = content[3..].find("---") {
            content[3 + end + 3..].trim().to_string()
        } else {
            content
        }
    } else {
        content
    };
    if stripped.is_empty() {
        None
    } else {
        Some(stripped)
    }
}

fn watcher_dashboard_command(cwd: &Path) -> Result<String> {
    let watcher_log_dir = cwd.join(".exo/logs");
    let watcher_log_path = watcher_log_dir.join("watcher.log");
    std::fs::create_dir_all(&watcher_log_dir).with_context(|| {
        format!(
            "failed to create watcher log directory {}",
            watcher_log_dir.display()
        )
    })?;
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&watcher_log_path)
        .with_context(|| format!("failed to create {}", watcher_log_path.display()))?;
    Ok("exomonad watch".to_string())
}

const WATCHER_WINDOW_NAME: &str = "Watcher";

fn has_watcher_dashboard_window<'a>(window_names: impl IntoIterator<Item = &'a str>) -> bool {
    window_names
        .into_iter()
        .any(|name| name == WATCHER_WINDOW_NAME)
}

async fn ensure_watcher_dashboard_window(
    ipc: &exomonad_core::services::tmux_ipc::TmuxIpc,
    cwd: &Path,
    shell: &str,
) {
    let windows = match ipc.list_windows().await {
        Ok(windows) => windows,
        Err(e) => {
            warn!(error = %e, "Failed to list tmux windows before checking Watcher dashboard (non-fatal)");
            return;
        }
    };

    if has_watcher_dashboard_window(windows.iter().map(|window| window.window_name.as_str())) {
        debug!("Watcher dashboard window already exists");
        return;
    }

    match watcher_dashboard_command(cwd) {
        Ok(watcher_cmd) => match ipc
            .new_window(WATCHER_WINDOW_NAME, cwd, shell, &watcher_cmd)
            .await
        {
            Ok(watcher_win) => info!(window = %watcher_win, "Watcher dashboard window created"),
            Err(e) => warn!(error = %e, "Failed to create Watcher dashboard window (non-fatal)"),
        },
        Err(e) => warn!(error = %e, "Failed to prepare Watcher dashboard window (non-fatal)"),
    }
}

fn forgejo_host_from_url(input: &str) -> Option<String> {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return None;
    }
    let no_scheme = trimmed
        .strip_prefix("http://")
        .or_else(|| trimmed.strip_prefix("https://"))
        .unwrap_or(trimmed);
    let host = no_scheme.split('/').next().unwrap_or_default().trim();
    if host.is_empty() {
        None
    } else {
        Some(host.to_string())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct RemoteRepoParts {
    host: String,
    owner: String,
    repo: String,
    has_http_auth: bool,
}

fn configure_forgejo_remote(cwd: &Path, forgejo_url: &str, forgejo_token: &str) -> Result<()> {
    let output = std::process::Command::new("git")
        .current_dir(cwd)
        .args(["remote", "get-url", "origin"])
        .output()
        .context("failed to run git remote get-url origin")?;
    if !output.status.success() {
        warn!("No origin remote found; skipping Forgejo remote token auth setup");
        return Ok(());
    }

    let old_url = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let Some(new_url) = forgejo_token_remote_url(&old_url, forgejo_url, forgejo_token) else {
        return Ok(());
    };
    let status = std::process::Command::new("git")
        .current_dir(cwd)
        .args(["remote", "set-url", "origin", &new_url])
        .status()
        .context("failed to run git remote set-url origin")?;
    if !status.success() {
        anyhow::bail!("git remote set-url origin exited with {status}");
    }

    info!(
        old_url = %redact_remote_token(&old_url, forgejo_token),
        new_url = %redact_remote_token(&new_url, forgejo_token),
        "Configured git remote to use Forgejo HTTP token auth"
    );
    Ok(())
}

fn forgejo_token_remote_url(
    remote_url: &str,
    forgejo_url: &str,
    forgejo_token: &str,
) -> Option<String> {
    let forgejo_token = forgejo_token.trim();
    if forgejo_token.is_empty() {
        debug!("Skipping Forgejo remote token auth setup without a forgejo_token");
        return None;
    }

    let remote = parse_remote_repo_parts(remote_url)?;
    let forgejo_host_raw = forgejo_host_from_url(forgejo_url)?;
    let forgejo_host = host_without_port(&forgejo_host_raw);
    if remote.host != forgejo_host {
        debug!(
            remote_host = %remote.host,
            forgejo_host,
            "Skipping Forgejo remote token auth setup for non-Forgejo origin"
        );
        return None;
    }
    if remote_url.contains(forgejo_token) || remote.has_http_auth {
        debug!("Forgejo remote already has HTTP auth; skipping remote rewrite");
        return None;
    }

    tokenized_forgejo_url(forgejo_url, forgejo_token, &remote.owner, &remote.repo)
}

// Token is embedded in the URL and visible in local git config.
// Acceptable for local Forgejo instances. Do not use for public hosts.
fn tokenized_forgejo_url(
    forgejo_url: &str,
    forgejo_token: &str,
    owner: &str,
    repo: &str,
) -> Option<String> {
    let base = forgejo_url.trim().trim_end_matches('/');
    let (scheme, rest) = base.split_once("://")?;
    Some(format!(
        "{scheme}://forgejo_pat:{forgejo_token}@{rest}/{owner}/{repo}.git"
    ))
}

fn parse_remote_repo_parts(remote_url: &str) -> Option<RemoteRepoParts> {
    let trimmed = remote_url.trim();
    if let Some(rest) = trimmed.strip_prefix("git@") {
        let (host, path) = rest.split_once(':')?;
        return remote_parts(host, path, false);
    }
    if let Some(rest) = trimmed.strip_prefix("ssh://") {
        let rest = rest.split_once('@').map(|(_, path)| path).unwrap_or(rest);
        let (host, path) = rest.split_once('/')?;
        return remote_parts(host, path, false);
    }
    let (_, rest) = trimmed.split_once("://")?;
    let (authority, path) = rest.split_once('/')?;
    let has_http_auth = authority.contains('@');
    let host = authority
        .rsplit_once('@')
        .map(|(_, host)| host)
        .unwrap_or(authority);
    remote_parts(host, path, has_http_auth)
}

fn remote_parts(host: &str, path: &str, has_http_auth: bool) -> Option<RemoteRepoParts> {
    let cleaned = path
        .trim_start_matches('/')
        .strip_suffix(".git")
        .unwrap_or(path);
    let mut segments = cleaned.split('/').filter(|segment| !segment.is_empty());
    let repo = segments.next_back()?.to_string();
    let owner = segments.next_back()?.to_string();
    Some(RemoteRepoParts {
        host: host_without_port(host).to_string(),
        owner,
        repo,
        has_http_auth,
    })
}

fn host_without_port(host: &str) -> &str {
    host.split(':').next().unwrap_or(host)
}

fn redact_remote_token(url: &str, token: &str) -> String {
    if token.is_empty() {
        url.to_string()
    } else {
        url.replace(token, "<token>")
    }
}

fn check_fj_cli_configuration(cwd: &Path) {
    if !exomonad_core::services::ForgejoClient::fj_binary_in_path() {
        warn!(
            "[Forgejo] Not configured - forgejo_url/forgejo_token are absent and fj was not found in PATH"
        );
        return;
    }

    info!(
        "[Forgejo] fj found in PATH; exomonad serve will use the fj CLI backend when HTTP config is absent"
    );
    match std::process::Command::new("fj")
        .args(["auth", "status"])
        .current_dir(cwd)
        .status()
    {
        Ok(status) if status.success() => {
            info!("[Forgejo] fj auth status succeeded");
        }
        Ok(status) => {
            warn!(
                status = %status,
                "[Forgejo] fj is in PATH but `fj auth status` failed; file_pr, watcher_pr_state, and spawn_reviewer may fail until fj is authenticated"
            );
        }
        Err(error) => {
            warn!(
                error = %error,
                "[Forgejo] failed to run `fj auth status`; file_pr, watcher_pr_state, and spawn_reviewer may fail until fj is authenticated"
            );
        }
    }
}

fn mailbox_protocol_available_for_config(config: &Config) -> bool {
    config.root_agent_type == AgentType::Claude && config.spawn_agent_type == AgentType::Claude
}

fn forgejo_env_vars(
    forgejo_url: &str,
    forgejo_token: &str,
    forgejo_reviewer_token: Option<&str>,
) -> Vec<(&'static str, String)> {
    let forgejo_token = forgejo_token.trim();
    let forgejo_reviewer_token = forgejo_reviewer_token
        .map(str::trim)
        .filter(|token| !token.is_empty());
    if forgejo_token.is_empty() && forgejo_reviewer_token.is_none() {
        return Vec::new();
    }

    let mut vars = Vec::new();
    if let Some(forgejo_host) = forgejo_host_from_url(forgejo_url) {
        vars.push(("FORGEJO_HOST", forgejo_host.clone()));
        vars.push(("GH_HOST", forgejo_host));
    }
    if !forgejo_token.is_empty() {
        vars.push(("FORGEJO_TOKEN", forgejo_token.to_string()));
        vars.push(("GH_TOKEN", forgejo_token.to_string()));
    }
    if let Some(token) = forgejo_reviewer_token {
        vars.push(("FORGEJO_REVIEWER_TOKEN", token.to_string()));
    }
    vars.push(("FORGEJO_URL", forgejo_url.to_string()));
    vars
}

fn current_time_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

fn extra_mcp_server_to_json(server: &crate::config::McpServerConfig) -> Result<Value> {
    Ok(match server {
        crate::config::McpServerConfig::Http { url, headers } => {
            let mut entry = serde_json::json!({"type": "http", "url": url});
            if !headers.is_empty() {
                entry["headers"] = serde_json::to_value(headers)?;
            }
            entry
        }
        crate::config::McpServerConfig::Stdio { command, args } => {
            serde_json::json!({"type": "stdio", "command": command, "args": args})
        }
    })
}

fn exomonad_mcp_server(binary_path: &Path, role: &str, name: &str) -> Value {
    serde_json::json!({
        "type": "stdio",
        "command": binary_path.display().to_string(),
        "args": ["mcp-stdio", "--role", role, "--name", name]
    })
}

fn extra_mcp_servers_to_json(
    servers: &std::collections::HashMap<String, crate::config::McpServerConfig>,
) -> Result<std::collections::HashMap<String, Value>> {
    servers
        .iter()
        .map(|(name, server)| Ok((name.clone(), extra_mcp_server_to_json(server)?)))
        .collect()
}

fn write_codex_root_config(config: &Config, cwd: &Path) -> Result<()> {
    let codex_dir = cwd.join(".codex");
    std::fs::create_dir_all(&codex_dir)?;
    let extra_mcp_servers = extra_mcp_servers_to_json(&config.extra_mcp_servers)?;
    let codex_config = exomonad_core::codex_config::render_codex_config(
        "root",
        "root",
        exomonad_core::services::agent_control::CODEX_TL_INSTRUCTIONS,
        config.model.as_deref(),
        &extra_mcp_servers,
        &exomonad_core::find_exomonad_binary(),
    );
    let codex_config_path = codex_dir.join("config.toml");
    std::fs::write(&codex_config_path, codex_config)?;
    if let Some(config_path) = exomonad_core::codex_config::codex_user_config_path() {
        exomonad_core::codex_config::trust_codex_project(&config_path, cwd).with_context(|| {
            format!("Failed to trust Codex project in {}", config_path.display())
        })?;
        exomonad_core::codex_config::install_codex_hook_trust(&config_path, &codex_config_path)
            .with_context(|| format!("Failed to trust Codex hooks in {}", config_path.display()))?;
        info!(path = %config_path.display(), "Marked project as trusted in Codex user config");
    } else {
        warn!("Could not determine Codex home; project may not be trusted automatically");
    }
    let legacy_hooks_path = codex_dir.join("hooks.json");
    if legacy_hooks_path.exists() {
        std::fs::remove_file(legacy_hooks_path)?;
    }
    info!("Codex configuration written to .codex/");
    Ok(())
}

fn build_claude_root_command(model: Option<&str>, initial_prompt: Option<&str>) -> String {
    let model_flag = model
        .filter(|value| !value.is_empty())
        .map(|value| format!(" --model {}", shell_escape::escape(value.into())))
        .unwrap_or_default();

    let launch = initial_prompt
        .filter(|value| !value.is_empty())
        .map(|prompt| {
            format!(
                "claude --dangerously-skip-permissions{model_flag} {}",
                shell_escape::escape(prompt.into())
            )
        })
        .unwrap_or_else(|| format!("claude --dangerously-skip-permissions{model_flag}"));

    format!("{launch}; echo; echo [Claude Code exited]; exec bash -l")
}

fn build_codex_root_command(
    cwd: &Path,
    model: Option<&str>,
    initial_prompt: Option<&str>,
) -> String {
    let escaped_dir = shell_escape::escape(cwd.display().to_string().into());
    let model_flag = model
        .filter(|value| !value.is_empty())
        .map(|value| format!(" --model {}", shell_escape::escape(value.into())))
        .unwrap_or_default();
    let prompt = initial_prompt
        .filter(|value| !value.is_empty())
        .map(|value| format!(" {}", shell_escape::escape(value.into())))
        .unwrap_or_default();

    let command = format!(
        "codex --dangerously-bypass-approvals-and-sandbox --cd {}{}{}",
        escaped_dir, model_flag, prompt
    );
    let restart_hint = shell_escape::escape(
        format!(
            "[Codex exited - restart with: codex --dangerously-bypass-approvals-and-sandbox --cd {}{}]",
            escaped_dir, model_flag
        )
        .into(),
    );

    format!("{command}; echo; printf '%s\n' {restart_hint}; exec bash -l")
}

/// Reject `--tl-model` / `--worker-model` values that opencode doesn't recognise.
/// Caller must only invoke this when the model is `Some` and the agent type is OpenCode.
/// Validate a Claude model string against known aliases and the `claude-*` prefix convention.
///
/// Accepts short aliases ("sonnet", "opus", "haiku") and full model IDs ("claude-sonnet-4-6").
/// Rejects arbitrary strings that match neither pattern — catches typos before a window is opened.
fn validate_claude_model(model: &str) -> Result<()> {
    // Aliases from `claude --help --model`: "sonnet" or "opus"
    const KNOWN_ALIASES: &[&str] = &["sonnet", "opus"];
    let is_alias = KNOWN_ALIASES.contains(&model);
    let is_full_id = model.starts_with("claude-");
    if !is_alias && !is_full_id {
        anyhow::bail!(
            "Unknown Claude model `{model}`. Use a short alias ('sonnet', 'opus') \
             or a full model ID starting with 'claude-' (e.g. 'claude-sonnet-4-6')."
        );
    }
    Ok(())
}

async fn validate_opencode_model(model: &str) -> Result<()> {
    let out = tokio::process::Command::new("opencode")
        .args(["models"])
        .output()
        .await
        .context("Failed to run `opencode models` for validation")?;
    if !out.status.success() {
        anyhow::bail!(
            "`opencode models` exited {}: {}",
            out.status,
            String::from_utf8_lossy(&out.stderr)
        );
    }
    let text = std::str::from_utf8(&out.stdout)?;
    let known: std::collections::HashSet<&str> = text
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .collect();
    if !known.contains(model) {
        anyhow::bail!("Unknown opencode model `{model}`. Run `exomonad models` to see the list.");
    }
    Ok(())
}

fn validate_codex_model(model: &str) -> Result<()> {
    if !model.starts_with("gpt-") {
        anyhow::bail!(
            "Unknown Codex model `{model}`. Use a Codex/OpenAI model ID starting with `gpt-` \
             (for example `gpt-5.2-codex`)."
        );
    }
    Ok(())
}

fn validate_gemini_model(model: &str) -> Result<()> {
    if !model.starts_with("gemini-") {
        anyhow::bail!(
            "Unknown Gemini model `{model}`. Use a Gemini model ID starting with `gemini-` \
             (for example `gemini-2.5-pro`)."
        );
    }
    Ok(())
}

fn validate_opencode_model_owner(
    agent_type: AgentType,
    model: Option<&str>,
    model_field: &str,
    harness_field: &str,
) -> Result<()> {
    if agent_type == AgentType::OpenCode || model.is_none() {
        return Ok(());
    }

    let model = model.expect("checked above");
    anyhow::bail!(
        "{model_field} is set to `{model}`, but {harness_field} is `{}`. \
         OpenCode model fields only apply when the matching harness is `opencode`.",
        agent_type_str(agent_type)
    );
}

fn validate_reviewer_model_for_harness(agent_type: AgentType, model: Option<&str>) -> Result<()> {
    let Some(model) = model else {
        return Ok(());
    };

    match agent_type {
        AgentType::Claude => validate_claude_model(model),
        AgentType::Codex => validate_codex_model(model),
        AgentType::Gemini => validate_gemini_model(model),
        AgentType::OpenCode | AgentType::Shoal | AgentType::Process => Ok(()),
    }
}

/// Run the init command: create or attach to tmux session.
pub async fn run(
    session_override: Option<String>,
    recreate: bool,
    opencode_as_tl: bool,
    openrouter: bool,
    tl: Option<String>,
    worker: Option<String>,
    tl_model: Option<String>,
    worker_model: Option<String>,
    reviewer: Option<String>,
    reviewer_model: Option<String>,
    verbose: bool,
) -> Result<()> {
    use exomonad_core::services::tmux_ipc::TmuxIpc;
    use exomonad_core::services::{resolve_role_context_path, AgentType};
    use std::io::{IsTerminal, Write};
    let cwd = std::env::current_dir()?;
    let config_path = cwd.join(".exo/config.toml");
    if !config_path.exists() {
        anyhow::bail!("No exomonad project found. Run `exomonad new` first.");
    }

    // Resolve config
    let mut config = Config::discover()?;

    // CLI flags override config
    if opencode_as_tl {
        config.opencode_as_tl = true;
        config.root_agent_type = AgentType::OpenCode;
    }
    if let Some(ref tl_type) = tl {
        config.root_agent_type = parse_agent_type(tl_type)?;
    }
    if let Some(ref worker_type) = worker {
        config.spawn_agent_type = parse_agent_type(worker_type)?;
    }
    if let Some(m) = tl_model {
        if config.root_agent_type == AgentType::OpenCode {
            config.opencode.tl_model = Some(m);
        } else {
            config.model = Some(m);
        }
    }
    if let Some(m) = worker_model {
        if config.spawn_agent_type == AgentType::OpenCode {
            config.opencode.worker_model = Some(m);
        }
    }
    if let Some(ref reviewer_type) = reviewer {
        config.reviewer.agent_type = parse_agent_type(reviewer_type)?;
    }
    if let Some(m) = reviewer_model {
        config.reviewer.model = Some(m);
    }
    if openrouter {
        config.openrouter.enabled = true;
    }

    validate_opencode_model_owner(
        config.root_agent_type,
        config.opencode.tl_model.as_deref(),
        "[opencode].tl_model",
        "root_agent_type",
    )?;
    validate_opencode_model_owner(
        config.spawn_agent_type,
        config.opencode.worker_model.as_deref(),
        "[opencode].worker_model",
        "spawn_agent_type",
    )?;

    if config.root_agent_type == AgentType::OpenCode {
        if let Some(m) = config.opencode.tl_model.as_deref() {
            validate_opencode_model(m).await?;
        }
    }
    if config.spawn_agent_type == AgentType::OpenCode {
        if let Some(m) = config.opencode.worker_model.as_deref() {
            validate_opencode_model(m).await?;
        }
    }
    if config.reviewer.agent_type == AgentType::OpenCode {
        if let Some(m) = config.reviewer.model.as_deref() {
            validate_opencode_model(m).await?;
        }
    } else {
        validate_reviewer_model_for_harness(
            config.reviewer.agent_type,
            config.reviewer.model.as_deref(),
        )?;
    }

    // Check OTel endpoint reachability if configured
    if let Some(ref endpoint) = config.otlp_endpoint {
        if let Some(host_port) = endpoint
            .strip_prefix("http://")
            .or_else(|| endpoint.strip_prefix("https://"))
        {
            let hp = host_port.to_string();
            let reachable = tokio::task::spawn_blocking(move || {
                use std::net::ToSocketAddrs;
                match hp.to_socket_addrs() {
                    Ok(mut addrs) => {
                        if let Some(addr) = addrs.next() {
                            std::net::TcpStream::connect_timeout(
                                &addr,
                                std::time::Duration::from_secs(2),
                            )
                            .is_ok()
                        } else {
                            false
                        }
                    }
                    Err(_) => false,
                }
            })
            .await
            .unwrap_or(false);

            if reachable {
                info!(endpoint = %endpoint, "OTel endpoint reachable");
            } else if config.yolo || !std::io::stdin().is_terminal() {
                warn!(
                    endpoint = %endpoint,
                    "OTel endpoint unreachable — proceeding without tracing (YOLO or headless)"
                );
            } else {
                eprint!(
                    "OTel endpoint {} unreachable — continue without tracing? [y/N] ",
                    endpoint
                );
                std::io::stderr().flush().ok();
                let input = tokio::task::spawn_blocking(|| {
                    let mut buf = String::new();
                    std::io::stdin().read_line(&mut buf).ok();
                    buf
                })
                .await
                .unwrap_or_default();
                if !input.trim().eq_ignore_ascii_case("y") {
                    anyhow::bail!(
                        "OTel endpoint unreachable. Start it with:\n  docker compose -f ~/.exo/otel/docker-compose.yml up -d"
                    );
                }
            }
        }
    }

    let session = session_override.unwrap_or(config.tmux_session.clone());
    let session_alive = TmuxIpc::has_session(&session).await?;
    if should_attach_existing_session(recreate, session_alive) {
        let ipc = TmuxIpc::new(&session);
        let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".to_string());
        ensure_watcher_dashboard_window(&ipc, &cwd, &shell).await;
        report_orphaned_agent_windows(&session, &cwd).await;
        info!(session = %session, "Attaching to existing session");
        return TmuxIpc::attach_session(&session).await;
    }

    let reset_count = refresh_agent_session_timestamps(&cwd)?;
    if reset_count > 0 {
        info!(
            agents = reset_count,
            "Reset orphan reconciler session timers for existing agents"
        );
    }

    // Auto-build or copy WASM if it doesn't exist yet
    let wasm_filename = format!("wasm-guest-{}.wasm", config.wasm_name);
    let wasm_path = config.wasm_dir.join(&wasm_filename);
    let roles_dir = cwd.join(".exo/roles");
    let has_roles = roles_dir.is_dir();

    if !wasm_path.exists() {
        if has_roles {
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
    } else if !has_roles {
        // Refresh stale WASM from global install if it's newer
        if let Ok(home) = std::env::var("HOME") {
            let global_wasm = PathBuf::from(home).join(".exo/wasm").join(&wasm_filename);
            if global_wasm.exists() {
                let local_mtime = std::fs::metadata(&wasm_path).and_then(|m| m.modified());
                let global_mtime = std::fs::metadata(&global_wasm).and_then(|m| m.modified());

                match (local_mtime, global_mtime) {
                    (Ok(local), Ok(global)) if global > local => {
                        info!(
                            src = %global_wasm.display(),
                            dst = %wasm_path.display(),
                            local_mtime = ?local,
                            global_mtime = ?global,
                            "Refreshing project WASM from global install (global is newer)"
                        );
                        std::fs::copy(&global_wasm, &wasm_path)?;
                    }
                    (Err(e), _) | (_, Err(e)) => {
                        debug!(error = %e, "Failed to compare WASM mtimes, skipping refresh");
                    }
                    _ => {}
                }
            }
        }
    }

    // Write root agent birth branch so fork_wave resolves the correct parent prefix.
    // Without this, BirthBranch::root() falls back to `git branch --show-current` in the
    // server process CWD, which may differ from the TL's actual branch.
    {
        let root_agent_dir = cwd.join(".exo/agents/root");
        std::fs::create_dir_all(&root_agent_dir)?;
        let current_branch = std::process::Command::new("git")
            .args(["branch", "--show-current"])
            .current_dir(&cwd)
            .output()
            .ok()
            .filter(|o| o.status.success())
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
            .unwrap_or_else(|| "main".to_string());
        std::fs::write(root_agent_dir.join(".birth_branch"), &current_branch)?;
        info!(branch = %current_branch, "Wrote root agent birth branch");
    }

    if let (Some(forgejo_url), Some(forgejo_token)) = (
        config.forgejo_url.as_deref(),
        config.forgejo_token.as_deref(),
    ) {
        if let Err(e) = configure_forgejo_remote(&cwd, forgejo_url, forgejo_token) {
            warn!(error = %e, "Failed to auto-configure Forgejo remote URL (non-fatal)");
        }
    } else if config.forgejo_url.is_none() && config.forgejo_token.is_none() {
        check_fj_cli_configuration(&cwd);
    }

    // Write root runtime configuration.
    let binary_path = exomonad_core::find_exomonad_binary();
    match config.root_agent_type {
        AgentType::OpenCode => {
            use exomonad_core::services::agent_control::AgentControlService;
            use exomonad_core::services::Services;
            let extra_mcp_servers = std::collections::HashMap::new();
            let opencode_config = AgentControlService::<Services>::generate_opencode_tl_settings(
                "root",
                "root",
                &extra_mcp_servers,
            );
            let opencode_dir = cwd.join(".exo/agents/root");
            std::fs::create_dir_all(&opencode_dir)?;
            std::fs::write(
                opencode_dir.join("opencode.json"),
                serde_json::to_string_pretty(&opencode_config)?,
            )?;
            AgentControlService::<Services>::write_opencode_plugin_files(&opencode_dir)
                .await
                .context("Failed to write OpenCode plugin files to .exo/agents/root")?;
            info!("OpenCode configuration written to .exo/agents/root/");
        }
        AgentType::Codex => {
            write_codex_root_config(&config, &cwd).context("Failed to write Codex root config")?;
        }
        _ => {
            exomonad_core::hooks::HookConfig::write_persistent(&cwd, &binary_path, None, None)
                .context("Failed to write hook configuration")?;
            info!("Hook configuration written to .claude/settings.local.json");
        }
    }

    // Copy Claude rules template if available and not already present
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

    // Symlink role context for root agent
    {
        let context_source = resolve_role_context_path(&cwd, &config.wasm_name, "root");
        if let Some(src) = context_source {
            let rules_dir = match config.root_agent_type {
                AgentType::Codex => cwd.join(".codex"),
                _ => cwd.join(".claude/rules"),
            };
            std::fs::create_dir_all(&rules_dir)?;
            let link = rules_dir.join("exomonad_role.md");
            let _ = std::fs::remove_file(&link); // idempotent
                                                 // Compute relative path from the role dir to the source
            let relative = pathdiff::diff_paths(&src, &rules_dir).unwrap_or(src.clone());
            match std::os::unix::fs::symlink(&relative, &link) {
                Ok(()) => {
                    info!(src = %src.display(), link = %link.display(), "Symlinked role context for root")
                }
                Err(e) => warn!(error = %e, "Failed to symlink role context (non-fatal)"),
            }
        }
    }

    // Write Gemini MCP configuration and pre-trust folder if root agent is Gemini
    if config.root_agent_type == AgentType::Gemini {
        let gemini_dir = cwd.join(".gemini");
        std::fs::create_dir_all(&gemini_dir)?;
        let settings_path = gemini_dir.join("settings.json");

        let mut mcp_servers = serde_json::Map::new();
        mcp_servers.insert(
            "exomonad".to_string(),
            exomonad_mcp_server(&binary_path, "root", "root"),
        );
        for (name, server) in &config.extra_mcp_servers {
            let entry = match server {
                exomonad::config::McpServerConfig::Http { url, .. } => {
                    serde_json::json!({ "httpUrl": url })
                }
                exomonad::config::McpServerConfig::Stdio { command, args } => {
                    serde_json::json!({"type": "stdio", "command": command, "args": args})
                }
            };
            mcp_servers.insert(name.clone(), entry);
        }

        let settings = serde_json::json!({
            "mcpServers": mcp_servers,
            "hooks": gemini_hooks()
        });
        std::fs::write(&settings_path, serde_json::to_string_pretty(&settings)?)?;
        info!("Gemini MCP configuration written to .gemini/settings.json");

        // Pre-trust CWD to prevent Gemini's interactive "Trust this folder?" dialog
        exomonad_core::services::agent_control::AgentControlService::<
            exomonad_core::services::Services,
        >::gemini_trust_folder(&cwd)
        .await;
    }

    // Validate tmux is available
    let tmux_check = std::process::Command::new("tmux").arg("-V").output();
    match tmux_check {
        Ok(output) if output.status.success() => {
            let version = String::from_utf8_lossy(&output.stdout);
            info!("tmux version: {}", version.trim());
        }
        Ok(output) => {
            anyhow::bail!(
                "tmux -V failed (status {}). Is tmux installed correctly?",
                output.status
            );
        }
        Err(e) => {
            anyhow::bail!(
                "tmux not found: {}. Install tmux before running exomonad init.",
                e
            );
        }
    }

    if recreate {
        // Kill the running server process before tearing down the session
        let pid_path = cwd.join(".exo/server.pid");
        if let Ok(content) = std::fs::read_to_string(&pid_path) {
            if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&content) {
                if let Some(pid) = parsed.get("pid").and_then(|v| v.as_u64()) {
                    use nix::sys::signal;
                    use nix::unistd::Pid;
                    let pid = Pid::from_raw(pid as i32);
                    if signal::kill(pid, None).is_ok() {
                        info!(pid = pid.as_raw(), "Stopping server");
                        let _ = signal::kill(pid, signal::Signal::SIGTERM);
                        for _ in 0..10 {
                            if signal::kill(pid, None).is_err() {
                                break;
                            }
                            tokio::time::sleep(Duration::from_millis(200)).await;
                        }
                    }
                }
            }
        }
        // Clean up server socket and pid unconditionally — old server is dead or dying.
        let sock = cwd.join(".exo/server.sock");
        let _ = std::fs::remove_file(&sock);
        let _ = std::fs::remove_file(&pid_path);
        info!("Cleaned up server socket and pid");

        if session_alive {
            info!(session = %session, "Deleting session (--recreate)");
            TmuxIpc::kill_session(&session).await?;
        }
    }

    // Create fresh session
    info!(session = %session, "Creating session");

    // 1. Write .mcp.json (for Claude Code discovery)
    if config.root_agent_type == AgentType::Claude {
        let mut mcp_servers = serde_json::Map::new();
        mcp_servers.insert(
            "exomonad".to_string(),
            exomonad_mcp_server(&binary_path, "root", "root"),
        );

        // Add extra MCP servers from config
        for (name, server) in &config.extra_mcp_servers {
            let entry = match server {
                exomonad::config::McpServerConfig::Http { url, headers } => {
                    let mut e = serde_json::json!({"type": "http", "url": url});
                    if !headers.is_empty() {
                        e["headers"] = serde_json::to_value(headers)?;
                    }
                    e
                }
                exomonad::config::McpServerConfig::Stdio { command, args } => {
                    serde_json::json!({"type": "stdio", "command": command, "args": args})
                }
            };
            mcp_servers.insert(name.clone(), entry);
        }

        let mcp_json = serde_json::json!({ "mcpServers": mcp_servers });
        std::fs::write(
            cwd.join(".mcp.json"),
            serde_json::to_string_pretty(&mcp_json)?,
        )?;
        info!("Wrote .mcp.json with {} MCP server(s)", mcp_servers.len());
    }

    // 2. Create session in background
    let server_window_id = TmuxIpc::new_session(&session, &cwd).await?;

    // Verify session
    if !TmuxIpc::has_session(&session).await? {
        anyhow::bail!(
            "tmux session '{}' was created but is not responding.",
            session
        );
    }

    if let Some(forgejo_url) = config.forgejo_url.as_deref() {
        for (var, value) in forgejo_env_vars(
            forgejo_url,
            config.forgejo_token.as_deref().unwrap_or(""),
            config.forgejo_reviewer_token.as_deref(),
        ) {
            std::env::set_var(var, &value);
            let _ = std::process::Command::new("tmux")
                .args(["set-environment", "-t", &session, var, &value])
                .status();
        }
    }

    let mailbox_protocol_available = if mailbox_protocol_available_for_config(&config) {
        "1"
    } else {
        "0"
    };
    std::env::set_var(
        "EXOMONAD_MAILBOX_PROTOCOL_AVAILABLE",
        mailbox_protocol_available,
    );
    let _ = std::process::Command::new("tmux")
        .args([
            "set-environment",
            "-t",
            &session,
            "EXOMONAD_MAILBOX_PROTOCOL_AVAILABLE",
            mailbox_protocol_available,
        ])
        .status();

    // Set EXOMONAD_TMUX_SESSION
    let env_output = std::process::Command::new("tmux")
        .args([
            "set-environment",
            "-t",
            &session,
            "EXOMONAD_TMUX_SESSION",
            &session,
        ])
        .output()
        .context("Failed to set EXOMONAD_TMUX_SESSION in tmux session")?;
    if !env_output.status.success() {
        warn!(
            "tmux set-environment failed: {}",
            String::from_utf8_lossy(&env_output.stderr)
        );
    }

    // Anchor chainlink to the root workspace DB so worktree windows don't create their own.
    // Use the directory form (no /issues.db suffix) to match every spawn-site propagation —
    // build_spawn_env in services/agent_control/internal.rs is the canonical form, this is the
    // tmux-level fallback for any process that does not go through build_spawn_env.
    let chainlink_db = cwd.join(".chainlink");
    let _ = std::process::Command::new("tmux")
        .args([
            "set-environment",
            "-t",
            &session,
            "CHAINLINK_DB",
            chainlink_db.to_str().unwrap_or_default(),
        ])
        .status();

    // Propagate CODEX_HOME into the tmux session env so Codex panes see the
    // same hook-trust DB that init's install_codex_hook_trust just seeded.
    // Without this, when tmux server is already running from another session
    // (e.g., a parallel workspace), the new session attaches to that server
    // and inherits the server's captured env — NOT the env exported by the
    // shell that ran `exomonad init`. Codex then falls back to ~/.codex and
    // sees the hooks as untrusted, firing "3 hooks need review". The e2e
    // tests/e2e/reviewer-convergence-loop hit this reliably (chainlink #253).
    if let Ok(codex_home) = std::env::var("CODEX_HOME") {
        if !codex_home.is_empty() {
            let _ = std::process::Command::new("tmux")
                .args(["set-environment", "-t", &session, "CODEX_HOME", &codex_home])
                .status();
        }
    }

    // Set EXOMONAD_ROLE=root so hook CLI passes &role=root to server
    let role_output = std::process::Command::new("tmux")
        .args(["set-environment", "-t", &session, "EXOMONAD_ROLE", "root"])
        .output()
        .context("Failed to set EXOMONAD_ROLE in tmux session")?;
    if !role_output.status.success() {
        warn!(
            "tmux set-environment EXOMONAD_ROLE failed: {}",
            String::from_utf8_lossy(&role_output.stderr)
        );
    }

    // Propagate verbose trace flags session-wide so spawned worktrees inherit them
    if verbose {
        for (var, val) in [
            ("EXOMONAD_VERBOSE", "1"),
            ("EXOMONAD_HOOK_TRACE", "1"),
            ("EXOMONAD_CHAINLINK_TRACE", "1"),
        ] {
            let _ = std::process::Command::new("tmux")
                .args(["set-environment", "-t", &session, var, val])
                .status();
        }
        info!("Verbose mode enabled: EXOMONAD_VERBOSE=1 EXOMONAD_HOOK_TRACE=1 EXOMONAD_CHAINLINK_TRACE=1 set in session environment");
    }

    // Set terminal window title to project/session name
    let _ = std::process::Command::new("tmux")
        .args(["set-option", "-t", &session, "set-titles", "on"])
        .output();
    let _ = std::process::Command::new("tmux")
        .args([
            "set-option",
            "-t",
            &session,
            "set-titles-string",
            "#{session_name}:#{window_name}",
        ])
        .output();

    // 3. Setup windows
    let ipc = TmuxIpc::new(&session);
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".to_string());

    let server_target = server_window_id;
    let rename_status = std::process::Command::new("tmux")
        .args(["rename-window", "-t", server_target.as_str(), "Server"])
        .status()
        .context("Failed to rename server window")?;
    if !rename_status.success() {
        warn!("tmux rename-window failed with status {}", rename_status);
    }
    // Set env vars via tmux set-environment so they're inherited cleanly
    // (avoids inlining secrets in send-keys command strings / terminal scrollback)
    for var in ["FORGEJO_TOKEN", "FORGEJO_API_URL"] {
        if let Ok(val) = std::env::var(var) {
            let _ = std::process::Command::new("tmux")
                .args(["set-environment", "-t", &session, var, &val])
                .status();
        }
    }

    // OpenRouter: propagate LLM routing env vars to all windows in this session.
    if config.openrouter.enabled {
        if let Some(ref api_key) = config.openrouter.resolved_api_key() {
            for (var, val) in [
                ("ANTHROPIC_BASE_URL", "https://openrouter.ai/api"),
                ("ANTHROPIC_AUTH_TOKEN", api_key.as_str()),
                ("ANTHROPIC_API_KEY", ""),
            ] {
                let _ = std::process::Command::new("tmux")
                    .args(["set-environment", "-t", &session, var, val])
                    .status();
            }
            info!("OpenRouter routing enabled: session env vars injected");
        } else {
            warn!("openrouter.enabled = true but no API key found (set openrouter.api_key or OPENROUTER_API_KEY)");
        }
    }

    let model_env = {
        let mut parts = Vec::new();
        if let Some(m) = &config.opencode.tl_model {
            parts.push(format!("EXOMONAD_TL_MODEL={}", m));
        }
        if let Some(m) = &config.opencode.worker_model {
            parts.push(format!("EXOMONAD_WORKER_MODEL={}", m));
        }
        if parts.is_empty() {
            String::new()
        } else {
            format!(" {}", parts.join(" "))
        }
    };
    let verbose_prefix = if verbose {
        "RUST_LOG=info EXOMONAD_HOOK_TRACE=1 EXOMONAD_CHAINLINK_TRACE=1 "
    } else {
        ""
    };
    let serve_cmd = format!(
        "{}EXOMONAD_TMUX_SESSION={} EXOMONAD_ROOT_AGENT_TYPE={} EXOMONAD_SPAWN_AGENT_TYPE={} EXOMONAD_REVIEWER_AGENT_TYPE={}{} exomonad serve",
        verbose_prefix,
        &session,
        agent_type_str(config.root_agent_type),
        agent_type_str(config.spawn_agent_type),
        agent_type_str(config.reviewer.agent_type),
        model_env,
    );
    let send_status = std::process::Command::new("tmux")
        .args([
            "send-keys",
            "-t",
            server_target.as_str(),
            &serve_cmd,
            "Enter",
        ])
        .status()
        .context("Failed to send server start command to tmux")?;
    if !send_status.success() {
        anyhow::bail!(
            "Failed to start server in tmux (send-keys exited with {})",
            send_status
        );
    }

    // Create "TL" window
    // OpenCode TL stays on the current branch (same as Claude TL) — workers fork off
    // it via fork_wave and file PRs back. A separate worktree branch for the TL would
    // break the parent-branch PR topology.
    if config.root_agent_type == AgentType::OpenCode {
        use exomonad_core::services::agent_control::AgentControlService;
        use exomonad_core::services::Services;
        let extra_mcp = extra_mcp_servers_to_json(&config.extra_mcp_servers)?;
        // Write opencode.json to repo root so the TL window discovers it via CWD.
        let opencode_config = AgentControlService::<Services>::generate_opencode_tl_settings(
            "root", "root", &extra_mcp,
        );
        std::fs::write(
            cwd.join("opencode.json"),
            serde_json::to_string_pretty(&opencode_config)?,
        )?;
        AgentControlService::<Services>::write_opencode_plugin_files(&cwd)
            .await
            .context("Failed to write OpenCode plugin files to repo root")?;
        info!("Wrote opencode.json and plugin to repo root for OpenCode TL");
    }
    let tl_cwd = cwd.clone();

    let base_command = if let Some(ref cmd) = config.root_command {
        cmd.clone()
    } else {
        let model_flag = config
            .model
            .as_ref()
            .map(|m| format!(" --model {}", m))
            .unwrap_or_default();
        let opencode_model_flag = config
            .opencode
            .tl_model
            .as_deref()
            .map(|m| format!(" --model {}", shell_escape::escape(m.into())))
            .unwrap_or_default();
        match (config.root_agent_type, config.initial_prompt.as_deref()) {
            (AgentType::Claude, prompt) => {
                build_claude_root_command(config.model.as_deref(), prompt)
            }
            (AgentType::Gemini, Some(prompt)) => {
                let yolo_flag = if config.yolo { " --yolo" } else { "" };
                format!(
                    "gemini{model_flag}{yolo_flag} --prompt-interactive '{}'",
                    prompt.replace('\'', "'\\''")
                )
            }
            (AgentType::Gemini, None) => {
                let yolo_flag = if config.yolo { " --yolo" } else { "" };
                format!("gemini{model_flag}{yolo_flag}")
            }
            (AgentType::Shoal, Some(prompt)) => format!(
                "shoal-agent --exo root --prompt '{}'",
                prompt.replace('\'', "'\\''")
            ),
            (AgentType::Shoal, None) => "shoal-agent --exo root".to_string(),
            (AgentType::OpenCode, Some(prompt)) => {
                let yolo = if config.yolo {
                    " --dangerously-skip-permissions"
                } else {
                    ""
                };
                let chainlink_protocol = read_chainlink_tl_protocol(&cwd);
                let augmented = match chainlink_protocol {
                    Some(ref protocol) => format!("{}\n\n---\n\n{}", protocol, prompt),
                    None => prompt.to_string(),
                };
                format!(
                    "opencode run{yolo}{opencode_model_flag} '{}'",
                    augmented.replace('\'', "'\\''")
                )
            }
            (AgentType::OpenCode, None) => {
                let yolo = if config.yolo {
                    " --dangerously-skip-permissions"
                } else {
                    ""
                };
                format!("opencode{opencode_model_flag}{yolo}")
            }
            (AgentType::Codex, prompt) => {
                build_codex_root_command(&cwd, config.model.as_deref(), prompt)
            }
            (AgentType::Process, _) => {
                unreachable!("Process is for companions only, not root agent")
            }
        }
    };

    let tl_command = match config.shell_command {
        Some(sc) => format!("{} -c \"{}\"", sc, base_command.replace('"', "\\\"")),
        None => base_command,
    };

    let _ = ipc.new_window("TL", &tl_cwd, &shell, &tl_command).await?;

    // 4. Poll for server socket
    wait_for_server_socket(&cwd).await?;

    ensure_watcher_dashboard_window(&ipc, &cwd, &shell).await;

    // 5. Spawn companion agents
    let companions_to_spawn: Vec<&crate::config::CompanionConfig> =
        config.companions.iter().collect();

    for companion in companions_to_spawn {
        // Validate companion name (alphanumeric, hyphens, underscores only)
        if !companion
            .name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        {
            anyhow::bail!(
                "Invalid companion name '{}': must contain only [A-Za-z0-9_-]",
                companion.name
            );
        }

        // Resolve agent_type: explicit or default to Claude with warning
        let agent_type = match companion.agent_type {
            Some(t) => t,
            None => {
                warn!(
                    name = %companion.name,
                    "Companion '{}' missing agent_type, defaulting to claude. Add agent_type = \"claude\" to silence this warning.",
                    companion.name
                );
                AgentType::Claude
            }
        };

        // Process companions: plain command in a tmux window, no agent infrastructure
        if agent_type == AgentType::Process {
            let companion_cmd = &companion.command;
            info!(
                name = %companion.name,
                cmd = %companion_cmd,
                "Spawning companion process"
            );
            let window_id = ipc
                .new_window(&companion.name, &cwd, &shell, companion_cmd)
                .await?;
            info!(
                name = %companion.name,
                window = %window_id.as_str(),
                cmd = %companion_cmd,
                "Companion process spawned"
            );
            continue;
        }

        info!(name = %companion.name, role = %companion.role, agent_type = ?agent_type, "Spawning companion agent");

        // Create agent identity directory
        let agent_dir = cwd.join(".exo/agents").join(&companion.name);
        std::fs::create_dir_all(&agent_dir)?;

        // Write birth_branch identity
        std::fs::write(agent_dir.join(".birth_branch"), &companion.name)?;

        // Determine CWD for the companion window
        let companion_cwd = if agent_type == AgentType::Claude {
            // Claude companions get their own git worktree for isolated .mcp.json discovery
            let worktree_path = cwd.join(".exo/companions").join(&companion.name);
            let branch_name = format!("companion/{}", companion.name);

            if !worktree_path.exists() {
                // Ensure HEAD exists — worktree creation needs a valid ref
                let head_valid = std::process::Command::new("git")
                    .args(["rev-parse", "--verify", "HEAD"])
                    .current_dir(&cwd)
                    .output()
                    .map(|o| o.status.success())
                    .unwrap_or(false);

                if !head_valid {
                    info!("No commits in repo, creating initial commit for worktree support");
                    let _ = std::process::Command::new("git")
                        .args(["commit", "--allow-empty", "-m", "initial commit"])
                        .current_dir(&cwd)
                        .output();
                }

                // Create worktree (reuse branch if it already exists)
                let branch_exists = std::process::Command::new("git")
                    .args(["rev-parse", "--verify", &branch_name])
                    .current_dir(&cwd)
                    .output()
                    .map(|o| o.status.success())
                    .unwrap_or(false);

                std::fs::create_dir_all(cwd.join(".exo/companions"))?;

                let worktree_result = if branch_exists {
                    std::process::Command::new("git")
                        .args(["worktree", "add"])
                        .arg(&worktree_path)
                        .arg(&branch_name)
                        .current_dir(&cwd)
                        .output()
                } else {
                    std::process::Command::new("git")
                        .args(["worktree", "add", "-b", &branch_name])
                        .arg(&worktree_path)
                        .arg("HEAD")
                        .current_dir(&cwd)
                        .output()
                };

                match worktree_result {
                    Ok(output) if output.status.success() => {
                        info!(
                            name = %companion.name,
                            path = %worktree_path.display(),
                            branch = %branch_name,
                            "Created companion worktree"
                        );
                    }
                    Ok(output) => {
                        anyhow::bail!(
                            "Failed to create worktree for companion '{}': {}",
                            companion.name,
                            String::from_utf8_lossy(&output.stderr)
                        );
                    }
                    Err(e) => {
                        anyhow::bail!(
                            "Failed to run git worktree add for companion '{}': {}",
                            companion.name,
                            e
                        );
                    }
                }
            } else {
                info!(
                    name = %companion.name,
                    path = %worktree_path.display(),
                    "Reusing existing companion worktree"
                );
            }

            // Write .mcp.json to worktree root — Claude discovers via CWD
            let mut companion_mcp_servers = serde_json::Map::new();
            companion_mcp_servers.insert(
                "exomonad".to_string(),
                exomonad_mcp_server(&binary_path, &companion.role, &companion.name),
            );
            // Include extra MCP servers from config
            for (name, server) in &config.extra_mcp_servers {
                let entry = match server {
                    exomonad::config::McpServerConfig::Http { url, headers } => {
                        let mut e = serde_json::json!({"type": "http", "url": url});
                        if !headers.is_empty() {
                            e["headers"] = serde_json::to_value(headers)?;
                        }
                        e
                    }
                    exomonad::config::McpServerConfig::Stdio { command, args } => {
                        serde_json::json!({"type": "stdio", "command": command, "args": args})
                    }
                };
                companion_mcp_servers.insert(name.clone(), entry);
            }
            let companion_mcp_json = serde_json::json!({ "mcpServers": companion_mcp_servers });
            std::fs::write(
                worktree_path.join(".mcp.json"),
                serde_json::to_string_pretty(&companion_mcp_json)?,
            )?;

            // Write .claude/settings.local.json to worktree root (hooks)
            exomonad_core::hooks::HookConfig::write_persistent(
                &worktree_path,
                &binary_path,
                None,
                Some(&cwd),
            )
            .context("Failed to write companion hook configuration")?;

            // Copy role context into companion's rules dir.
            // Must be a copy, not a symlink — symlinks escape the worktree boundary
            // and cause Claude Code to discover parent context files.
            {
                let context_source =
                    resolve_role_context_path(&cwd, &config.wasm_name, &companion.role);
                if let Some(src) = context_source {
                    let rules_dir = worktree_path.join(".claude/rules");
                    let _ = std::fs::create_dir_all(&rules_dir);
                    let dest = rules_dir.join("exomonad_role.md");
                    let _ = std::fs::remove_file(&dest); // idempotent
                    match std::fs::copy(&src, &dest) {
                        Ok(_) => {
                            info!(name = %companion.name, src = %src.display(), dest = %dest.display(), "Copied role context for companion")
                        }
                        Err(e) => {
                            warn!(name = %companion.name, error = %e, "Failed to copy role context (non-fatal)")
                        }
                    }
                }
            }

            // Symlink server socket into worktree's .exo/
            let worktree_exo = worktree_path.join(".exo");
            std::fs::create_dir_all(&worktree_exo)?;
            let socket_target = worktree_exo.join("server.sock");
            let _ = std::fs::remove_file(&socket_target);
            let socket_source = cwd.join(".exo/server.sock");
            std::os::unix::fs::symlink(&socket_source, &socket_target)?;
            info!(
                source = %socket_source.display(),
                target = %socket_target.display(),
                created_at_ms = current_time_millis(),
                "Symlinked server socket into companion worktree"
            );

            worktree_path
        } else {
            // Gemini/Shoal companions use project root CWD
            let companion_mcp = serde_json::json!({
                "mcpServers": {
                    "exomonad": exomonad_mcp_server(&binary_path, &companion.role, &companion.name)
                }
            });

            match agent_type {
                AgentType::Gemini => {
                    let settings = serde_json::json!({
                        "mcpServers": companion_mcp["mcpServers"],
                        "hooks": gemini_hooks()
                    });
                    std::fs::write(
                        agent_dir.join("settings.json"),
                        serde_json::to_string_pretty(&settings)?,
                    )?;
                }
                AgentType::Shoal => {}
                AgentType::OpenCode => {
                    let opencode_config = serde_json::json!({
                        "mcp": {
                            "exomonad": {
                                "type": "local",
                                "command": ["exomonad", "mcp-stdio", "--role", &companion.role, "--name", &companion.name]
                            }
                        }
                    });
                    std::fs::write(
                        agent_dir.join("opencode.json"),
                        serde_json::to_string_pretty(&opencode_config)?,
                    )?;
                }
                AgentType::Codex => {}
                AgentType::Claude | AgentType::Process => unreachable!(),
            }

            cwd.clone()
        };

        // Build command per agent type.
        // Prefix with identity env vars so hook CLI resolves the correct agent.
        let escaped_task = companion.task.as_deref().map(|t| t.replace('\'', "'\\''"));
        let model_flag = companion
            .model
            .as_ref()
            .map(|m| format!(" --model {}", m))
            .unwrap_or_default();
        let env_prefix = format!(
            "EXOMONAD_AGENT_ID={} EXOMONAD_ROLE={} ",
            companion.name, companion.role
        );
        let companion_cmd = match agent_type {
            AgentType::Claude => {
                // Pure CWD discovery — no --mcp-config, no --strict-mcp-config
                let task_part = match &escaped_task {
                    Some(t) => format!(" '{}'", t),
                    None => String::new(),
                };
                format!(
                    "{env_prefix}{}{model_flag}{task_part}; echo; echo '[{} exited]'; exec bash -l",
                    companion.command, companion.name
                )
            }
            AgentType::Gemini => {
                let settings = agent_dir.join("settings.json");
                let yolo_flag = if config.yolo { " --yolo" } else { "" };
                let task_part = match &escaped_task {
                    Some(t) => format!(" '{}'", t),
                    None => String::new(),
                };
                // Pre-trust CWD for Gemini
                exomonad_core::services::agent_control::AgentControlService::<
                    exomonad_core::services::Services,
                >::gemini_trust_folder(&companion_cwd)
                .await;
                format!(
                    "{env_prefix}GEMINI_CLI_SYSTEM_SETTINGS_PATH={} {}{model_flag}{yolo_flag}{}",
                    settings.display(),
                    companion.command,
                    task_part
                )
            }
            AgentType::Shoal => {
                let task_part = match &escaped_task {
                    Some(t) => format!(" '{}'", t),
                    None => String::new(),
                };
                format!("{env_prefix}{}{}", companion.command, task_part)
            }
            AgentType::OpenCode => {
                let yolo = if config.yolo {
                    " --dangerously-skip-permissions"
                } else {
                    ""
                };
                let model_flag = companion
                    .model
                    .as_deref()
                    .map(|m| format!(" --model {}", shell_escape::escape(m.into())))
                    .unwrap_or_default();
                let task_part = match &escaped_task {
                    Some(t) => format!(" '{}'", t),
                    None => String::new(),
                };
                format!("{env_prefix}opencode run{yolo}{model_flag}{task_part}")
            }
            AgentType::Codex => format!(
                "{env_prefix}echo 'Codex companion startup is not implemented yet'; exec bash -l"
            ),
            AgentType::Process => unreachable!("Process companions handled above"),
        };
        let window_id = ipc
            .new_window(&companion.name, &companion_cwd, &shell, &companion_cmd)
            .await?;

        // Write routing.json with window_id
        let routing = serde_json::json!({
            "window_id": window_id.as_str()
        });
        std::fs::write(
            agent_dir.join("routing.json"),
            serde_json::to_string_pretty(&routing)?,
        )?;

        info!(name = %companion.name, window = %window_id.as_str(), "Companion agent spawned");
    }

    // 6. Attach
    info!(session = %session, "Attaching to session");
    TmuxIpc::attach_session(&session).await
}

fn should_attach_existing_session(recreate: bool, session_alive: bool) -> bool {
    session_alive && !recreate
}

/// Refresh orphan timeout baselines for agents that predate this `exomonad init` session.
fn refresh_agent_session_timestamps(cwd: &Path) -> Result<usize> {
    let agents_dir = cwd.join(".exo/agents");
    if !agents_dir.is_dir() {
        return Ok(0);
    }

    let now_secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
        .to_string();
    let mut updated = 0;

    for entry in std::fs::read_dir(&agents_dir)? {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }
        if entry.file_name().to_string_lossy() == "root" {
            continue;
        }

        std::fs::write(entry.path().join("spawned_at"), &now_secs)?;
        updated += 1;
    }

    Ok(updated)
}

async fn report_orphaned_agent_windows(session: &str, cwd: &Path) {
    let output = std::process::Command::new("tmux")
        .args([
            "list-windows",
            "-t",
            session,
            "-F",
            "#{window_name}\t#{pane_current_command}",
        ])
        .output();

    let output = match output {
        Ok(o) if o.status.success() => o,
        Ok(o) => {
            warn!(
                session,
                stderr = %String::from_utf8_lossy(&o.stderr),
                "Could not list tmux windows for orphan report"
            );
            return;
        }
        Err(error) => {
            warn!(session, error = %error, "tmux list-windows failed for orphan report");
            return;
        }
    };

    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();

    let mut rows: Vec<String> = Vec::new();
    for line in String::from_utf8_lossy(&output.stdout).lines() {
        let mut parts = line.split('\t');
        let window_name = match parts.next() {
            Some(name) => name.trim(),
            None => continue,
        };
        let pane_cmd = parts.next().unwrap_or("").trim();

        if window_name.is_empty() || window_name == "Server" || window_name == "TL" {
            continue;
        }

        let is_shell_prompt = matches!(pane_cmd, "bash" | "zsh" | "fish" | "sh");
        if !is_shell_prompt {
            continue;
        }

        let agent_dir = cwd.join(".exo/agents").join(window_name);
        if !agent_dir.exists() {
            continue;
        }

        let issue = std::fs::read_to_string(agent_dir.join("active_issue"))
            .ok()
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| "(none)".to_string());

        let age = std::fs::read_to_string(agent_dir.join("spawned_at"))
            .ok()
            .and_then(|v| v.trim().parse::<u64>().ok())
            .map(|spawned| format!("{}m", now.saturating_sub(spawned) / 60))
            .unwrap_or_else(|| "unknown".to_string());

        rows.push(format!("- {window_name}: issue={issue}, age={age}"));
    }

    if rows.is_empty() {
        return;
    }

    warn!(
        session,
        count = rows.len(),
        "Orphaned agent windows detected (not auto-killed)"
    );
    for row in rows {
        warn!(session, "{}", row);
    }
}

pub fn ensure_gitignore(project_dir: &Path) -> Result<()> {
    let gitignore_path = project_dir.join(".gitignore");
    let content = if gitignore_path.exists() {
        std::fs::read_to_string(&gitignore_path)?
    } else {
        String::new()
    };

    let has_line = |line: &str| content.lines().any(|l| l.trim() == line);
    let needed: Vec<&str> = [
        ".exo/*",
        "!.exo/config.toml",
        "!.exo/roles/",
        "!.exo/lib/",
        "!.exo/rules/",
        ".codex/",
        ".claude/settings.local.json",
        ".opencode/",
        "opencode.json",
        ".chainlink/",
    ]
    .into_iter()
    .filter(|line| !has_line(line))
    .collect();

    if needed.is_empty() {
        return Ok(());
    }

    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&gitignore_path)?;
    use std::io::Write;
    if !content.is_empty() && !content.ends_with('\n') {
        writeln!(file)?;
    }
    if !has_line(".exo/*") {
        writeln!(
            file,
            "# ExoMonad - track config and source, ignore runtime artifacts"
        )?;
    }
    for line in &needed {
        writeln!(file, "{}", line)?;
    }
    Ok(())
}

pub async fn wait_for_server_socket(project_dir: &Path) -> Result<()> {
    let socket_path = project_dir.join(".exo/server.sock");
    let start = Instant::now();
    let timeout_dur = Duration::from_secs(30);

    while start.elapsed() < timeout_dur {
        if socket_path.exists() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }

    if !socket_path.exists() {
        anyhow::bail!(
            "Server socket not found at {} after 30s.",
            socket_path.display()
        );
    }

    let client = uds_client::ServerClient::new(socket_path.to_path_buf());
    for _ in 0..5 {
        if client.is_healthy().await {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }

    anyhow::bail!("Server socket exists but health check failed.")
}

/// Parse agent type from CLI string (e.g., "opencode", "claude", "gemini").
fn parse_agent_type(s: &str) -> Result<AgentType> {
    match s.to_lowercase().as_str() {
        "claude" | "claude-code" => Ok(AgentType::Claude),
        "gemini" => Ok(AgentType::Gemini),
        "opencode" | "opencode-cli" => Ok(AgentType::OpenCode),
        "codex" => Ok(AgentType::Codex),
        "shoal" => Ok(AgentType::Shoal),
        _ => anyhow::bail!(
            "Unknown agent type: {}. Valid values: claude, gemini, opencode, codex, shoal",
            s
        ),
    }
}

fn agent_type_str(t: AgentType) -> &'static str {
    match t {
        AgentType::Claude => "claude",
        AgentType::Gemini => "gemini",
        AgentType::OpenCode => "opencode",
        AgentType::Codex => "codex",
        AgentType::Shoal => "shoal",
        AgentType::Process => "process",
    }
}

/// Gemini CLI hooks for BeforeTool, BeforeModel, AfterModel, and AfterAgent.
/// Matches the hooks generated by `generate_gemini_worker_settings` in spawn.rs.
fn gemini_hooks() -> serde_json::Value {
    serde_json::json!({
        "BeforeTool": [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": "exomonad hook before-tool --runtime gemini"
                    }
                ]
            }
        ],
        "BeforeModel": [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": "exomonad hook before-model --runtime gemini"
                    }
                ]
            }
        ],
        "AfterModel": [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": "exomonad hook after-model --runtime gemini"
                    }
                ]
            }
        ],
        "AfterAgent": [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": "exomonad hook worker-exit --runtime gemini"
                    }
                ]
            }
        ]
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn watcher_dashboard_command_creates_log_file() {
        let tmp = tempfile::tempdir().unwrap();
        let command = watcher_dashboard_command(tmp.path()).unwrap();
        let log_path = tmp.path().join(".exo/logs/watcher.log");

        assert!(log_path.exists());
        assert_eq!(command, "exomonad watch");
    }

    #[test]
    fn watcher_dashboard_window_detection_uses_window_name() {
        assert!(has_watcher_dashboard_window(["Server", "Watcher", "TL"]));
        assert!(!has_watcher_dashboard_window(["Server", "TL"]));
    }

    #[test]
    fn forgejo_token_remote_url_rewrites_matching_ssh_origin() {
        let url = forgejo_token_remote_url(
            "git@localhost:exomonad/nemotron-port.git",
            "http://localhost:3000",
            "token-123",
        )
        .unwrap();

        assert_eq!(
            url,
            "http://forgejo_pat:token-123@localhost:3000/exomonad/nemotron-port.git"
        );
    }

    #[test]
    fn forgejo_token_remote_url_ignores_empty_token() {
        assert!(forgejo_token_remote_url(
            "git@localhost:exomonad/nemotron-port.git",
            "http://localhost:3000",
            "  ",
        )
        .is_none());
    }

    #[test]
    fn forgejo_token_remote_url_ignores_different_origin_host() {
        assert!(forgejo_token_remote_url(
            "git@github.com:nanonite/exomonad.git",
            "http://localhost:3000",
            "token-123",
        )
        .is_none());
    }

    #[test]
    fn forgejo_token_remote_url_is_idempotent_with_existing_auth() {
        assert!(forgejo_token_remote_url(
            "http://forgejo_pat:token-123@localhost:3000/exomonad/nemotron-port.git",
            "http://localhost:3000",
            "token-123",
        )
        .is_none());
    }

    #[test]
    fn parse_remote_repo_parts_uses_last_two_path_segments() {
        let parts =
            parse_remote_repo_parts("git@forge.example:repositories/owner/exomonad.git").unwrap();

        assert_eq!(parts.host, "forge.example");
        assert_eq!(parts.owner, "owner");
        assert_eq!(parts.repo, "exomonad");
    }

    #[test]
    fn init_attaches_existing_session_without_recreate() {
        assert!(should_attach_existing_session(false, true));
    }

    #[test]
    fn init_does_not_attach_when_recreate_requested() {
        assert!(!should_attach_existing_session(true, true));
    }

    #[test]
    fn init_does_not_attach_missing_session() {
        assert!(!should_attach_existing_session(false, false));
    }

    #[test]
    fn forgejo_env_vars_include_forgejo_and_gh_auth() {
        let vars = forgejo_env_vars("http://localhost:3000", "token-123", Some("reviewer-456"));

        assert!(vars.contains(&("FORGEJO_HOST", "localhost:3000".to_string())));
        assert!(vars.contains(&("GH_HOST", "localhost:3000".to_string())));
        assert!(vars.contains(&("FORGEJO_TOKEN", "token-123".to_string())));
        assert!(vars.contains(&("GH_TOKEN", "token-123".to_string())));
        assert!(vars.contains(&("FORGEJO_REVIEWER_TOKEN", "reviewer-456".to_string())));
        assert!(vars.contains(&("FORGEJO_URL", "http://localhost:3000".to_string())));
    }

    #[test]
    fn forgejo_env_vars_ignore_empty_tokens() {
        assert!(forgejo_env_vars("http://localhost:3000", "  ", None).is_empty());
    }

    #[test]
    fn refresh_agent_session_timestamps_skips_root_and_updates_agents() {
        let dir = tempfile::tempdir().unwrap();
        let agents = dir.path().join(".exo/agents");
        let root = agents.join("root");
        let leaf = agents.join("issue-1-leaf-codex");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&leaf).unwrap();
        std::fs::write(root.join("spawned_at"), "1").unwrap();
        std::fs::write(leaf.join("spawned_at"), "1").unwrap();

        let before = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let updated = refresh_agent_session_timestamps(dir.path()).unwrap();
        let after = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();

        assert_eq!(updated, 1);
        assert_eq!(
            std::fs::read_to_string(root.join("spawned_at")).unwrap(),
            "1"
        );
        let leaf_spawned_at = std::fs::read_to_string(leaf.join("spawned_at"))
            .unwrap()
            .parse::<u64>()
            .unwrap();
        assert!((before..=after).contains(&leaf_spawned_at));
    }

    #[test]
    fn ensure_gitignore_writes_runtime_scaffold_paths_on_fresh_repo() {
        let dir = tempfile::tempdir().unwrap();

        ensure_gitignore(dir.path()).unwrap();
        let content = std::fs::read_to_string(dir.path().join(".gitignore")).unwrap();

        for expected in [
            ".exo/*",
            "!.exo/config.toml",
            "!.exo/roles/",
            "!.exo/lib/",
            "!.exo/rules/",
            ".codex/",
            ".claude/settings.local.json",
            ".opencode/",
            "opencode.json",
            ".chainlink/",
        ] {
            assert!(
                content.lines().any(|line| line.trim() == expected),
                "missing gitignore entry: {expected}"
            );
        }
    }

    #[test]
    fn ensure_gitignore_only_appends_missing_runtime_scaffold_paths() {
        let dir = tempfile::tempdir().unwrap();
        let gitignore = dir.path().join(".gitignore");
        std::fs::write(&gitignore, "target/\n.exo/*\n.codex/\n").unwrap();

        ensure_gitignore(dir.path()).unwrap();
        let once = std::fs::read_to_string(&gitignore).unwrap();
        ensure_gitignore(dir.path()).unwrap();
        let twice = std::fs::read_to_string(&gitignore).unwrap();

        assert_eq!(once, twice);
        assert_eq!(
            once.lines().filter(|line| line.trim() == ".exo/*").count(),
            1
        );
        assert_eq!(
            once.lines().filter(|line| line.trim() == ".codex/").count(),
            1
        );
        assert!(once.lines().any(|line| line.trim() == ".opencode/"));
        assert!(once.lines().any(|line| line.trim() == "opencode.json"));
    }

    // ── validate_claude_model tests ───────────────────────────────────────
    // Aliases sourced from `claude --help`: 'sonnet' or 'opus'
    // Full IDs accepted via "claude-" prefix.

    #[test]
    fn test_validate_claude_model_aliases() {
        assert!(validate_claude_model("sonnet").is_ok());
        assert!(validate_claude_model("opus").is_ok());
    }

    #[test]
    fn test_validate_claude_model_full_ids() {
        assert!(validate_claude_model("claude-haiku-4-5-20251001").is_ok());
        assert!(validate_claude_model("claude-sonnet-4-6").is_ok());
        assert!(validate_claude_model("claude-opus-4-7").is_ok());
    }

    #[test]
    fn test_validate_claude_model_rejects_invalid() {
        assert!(validate_claude_model("gpt-4o").is_err());
        assert!(validate_claude_model("anthropic/claude-haiku").is_err());
        assert!(validate_claude_model("").is_err());
        assert!(validate_claude_model("haiku").is_err());
        assert!(validate_claude_model("haiku-model").is_err());
    }

    #[test]
    fn test_validate_codex_model_rejects_non_codex_prefixes() {
        assert!(validate_codex_model("gpt-5.2-codex").is_ok());
        assert!(validate_codex_model("opencode-go/deepseek-v4-flash").is_err());
        assert!(validate_codex_model("claude-sonnet-4-6").is_err());
    }

    #[test]
    fn test_validate_gemini_model_rejects_non_gemini_prefixes() {
        assert!(validate_gemini_model("gemini-2.5-pro").is_ok());
        assert!(validate_gemini_model("gpt-5.2-codex").is_err());
        assert!(validate_gemini_model("opencode-go/deepseek-v4-flash").is_err());
    }

    #[test]
    fn test_opencode_tl_model_requires_opencode_root_harness() {
        let error = validate_opencode_model_owner(
            AgentType::Claude,
            Some("opencode-go/deepseek-v4-flash"),
            "[opencode].tl_model",
            "root_agent_type",
        )
        .unwrap_err()
        .to_string();

        assert!(error.contains("[opencode].tl_model"));
        assert!(error.contains("root_agent_type is `claude`"));
    }

    #[test]
    fn test_opencode_worker_model_requires_opencode_worker_harness() {
        let error = validate_opencode_model_owner(
            AgentType::Codex,
            Some("opencode-go/deepseek-v4-flash"),
            "[opencode].worker_model",
            "spawn_agent_type",
        )
        .unwrap_err()
        .to_string();

        assert!(error.contains("[opencode].worker_model"));
        assert!(error.contains("spawn_agent_type is `codex`"));
    }

    #[test]
    fn test_opencode_model_owner_allows_matching_harness() {
        assert!(validate_opencode_model_owner(
            AgentType::OpenCode,
            Some("opencode-go/deepseek-v4-flash"),
            "[opencode].worker_model",
            "spawn_agent_type",
        )
        .is_ok());
    }

    #[test]
    fn exomonad_mcp_server_uses_resolved_binary_path() {
        let server = exomonad_mcp_server(Path::new("/tmp/bin/exomonad"), "worker", "agent-1");

        assert_eq!(
            server.get("command").and_then(Value::as_str),
            Some("/tmp/bin/exomonad")
        );
        assert_eq!(
            server.get("args"),
            Some(&serde_json::json!([
                "mcp-stdio",
                "--role",
                "worker",
                "--name",
                "agent-1"
            ]))
        );
    }

    #[test]
    fn claude_root_command_uses_initial_prompt() {
        let command = build_claude_root_command(Some("sonnet"), Some("Spawn the worker"));

        assert_eq!(
            command,
            "claude --dangerously-skip-permissions --model sonnet 'Spawn the worker'; echo; echo [Claude Code exited]; exec bash -l"
        );
        assert!(!command.contains(" -c"));
        assert!(command.ends_with("exec bash -l"));
    }

    #[test]
    fn claude_root_command_without_prompt_starts_fresh_session() {
        let command = build_claude_root_command(Some("sonnet"), None);

        assert_eq!(
            command,
            "claude --dangerously-skip-permissions --model sonnet; echo; echo [Claude Code exited]; exec bash -l"
        );
        assert!(!command.contains(" -c"));
    }

    #[test]
    fn codex_root_command_launches_codex_tl() {
        let command = build_codex_root_command(
            Path::new("/tmp/exomonad repo"),
            Some("gpt-5.2"),
            Some("Plan the next wave"),
        );

        assert_eq!(
            command,
            "codex --dangerously-bypass-approvals-and-sandbox --cd '/tmp/exomonad repo' --model gpt-5.2 'Plan the next wave'; echo; printf '%s\n' '[Codex exited - restart with: codex --dangerously-bypass-approvals-and-sandbox --cd '\\''/tmp/exomonad repo'\\'' --model gpt-5.2]'; exec bash -l"
        );
        assert!(!command.contains("not implemented"));
        assert!(command.ends_with("exec bash -l"));
    }
}
