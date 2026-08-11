use crate::uds_client;
use anyhow::{Context, Result};
use exomonad::config::{
    Config, EffortLevel, ResolvedEffort, REVIEWER_EFFORT_ENV, REVIEWER_MAX_ROUNDS_ENV,
    REVIEWER_MODEL_ENV,
};
use exomonad_core::services::AgentType;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tracing::{debug, info, warn};

fn read_root_tl_protocol(cwd: &Path, wasm_name: &str) -> Option<String> {
    exomonad_core::services::agent_control::load_role_context(cwd, wasm_name, "root")
}

fn codex_root_instructions(cwd: &Path, wasm_name: &str) -> String {
    read_root_tl_protocol(cwd, wasm_name)
        .map(|protocol| {
            format!(
                "{protocol}\n\n{}",
                exomonad_core::services::agent_control::CODEX_TL_RUNTIME_NOTES
            )
        })
        .unwrap_or_else(|| {
            exomonad_core::services::agent_control::CODEX_TL_RUNTIME_NOTES.to_string()
        })
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

fn redact_init_argv(args: impl IntoIterator<Item = String>) -> Vec<String> {
    let mut redact_next = false;
    args.into_iter()
        .map(|arg| {
            if redact_next {
                redact_next = false;
                return "<redacted>".to_string();
            }
            let lower = arg.to_ascii_lowercase();
            let sensitive = ["token", "secret", "password", "api-key", "api_key"]
                .iter()
                .any(|needle| lower.contains(needle));
            if sensitive && !arg.contains('=') {
                redact_next = true;
            }
            if sensitive {
                match arg.split_once('=') {
                    Some((flag, _)) => format!("{flag}=<redacted>"),
                    None => arg,
                }
            } else {
                arg
            }
        })
        .collect()
}

fn append_init_invocation_log(cwd: &Path, config: &Config, argv: &[String]) -> Result<()> {
    let log_dir = cwd.join(".exo/logs");
    std::fs::create_dir_all(&log_dir)
        .with_context(|| format!("failed to create init log directory {}", log_dir.display()))?;
    let payload = serde_json::json!({
        "timestamp_ms": current_time_millis(),
        "argv": argv,
        "session": config.tmux_session.as_str(),
        "resolved": {
            "root_agent_type": agent_type_str(config.root_agent_type),
            "spawn_agent_type": agent_type_str(config.spawn_agent_type),
            "reviewer_agent_type": agent_type_str(config.reviewer.agent_type),
            "root_model": config.model.as_deref(),
            "opencode_tl_model": config.opencode.tl_model.as_deref(),
            "opencode_worker_model": config.opencode.worker_model.as_deref(),
            "reviewer_model": config.reviewer.model.as_deref(),
            "tl_effort": config.tl_effort_level,
            "worker_effort": config.worker_effort_level,
            "reviewer_effort": config.reviewer_effort_level,
        }
    });
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir.join("init.jsonl"))?;
    use std::io::Write as _;
    writeln!(file, "{}", serde_json::to_string(&payload)?)?;
    Ok(())
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

/// Git config key holding an explicit remote-name override. Mirrors
/// `exomonad_core::services::repo`'s `REMOTE_OVERRIDE_KEY` — kept as a
/// literal here since this module doesn't depend on that private const.
const GIT_REMOTE_OVERRIDE_KEY: &str = "exomonad.remote";

/// Resolve which git remote exomonad's PR/CI operations should use: the
/// `exomonad.remote` git config override if set, else `"origin"`.
fn resolve_git_remote(cwd: &Path) -> String {
    let output = std::process::Command::new("git")
        .current_dir(cwd)
        .args(["config", "--get", GIT_REMOTE_OVERRIDE_KEY])
        .output();
    match output {
        Ok(o) if o.status.success() => {
            let value = String::from_utf8_lossy(&o.stdout).trim().to_string();
            if value.is_empty() {
                "origin".to_string()
            } else {
                value
            }
        }
        _ => "origin".to_string(),
    }
}

/// Validate `remote` names an existing git remote, then persist it as the
/// `exomonad.remote` git config override (`git config --local`). Used by
/// `exomonad init --set-git-remote <name>` to pin which remote PR/CI
/// operations use when a repo has multiple remotes (e.g. a GitHub `origin`
/// alongside a Forgejo remote) — worktrees share the main repo's
/// `.git/config`, so this applies to every spawned agent automatically.
fn set_git_remote_override(cwd: &Path, remote: &str) -> Result<()> {
    let output = std::process::Command::new("git")
        .current_dir(cwd)
        .arg("remote")
        .output()
        .context("failed to run git remote")?;
    if !output.status.success() {
        anyhow::bail!("git remote exited with {}", output.status);
    }
    let remotes: Vec<String> = String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(str::trim)
        .filter(|r| !r.is_empty())
        .map(str::to_string)
        .collect();
    if !remotes.iter().any(|r| r == remote) {
        anyhow::bail!(
            "--set-git-remote {remote}: no such git remote configured (found: {}). \
             Add it first with `git remote add {remote} <url>`.",
            remotes.join(", ")
        );
    }

    let status = std::process::Command::new("git")
        .current_dir(cwd)
        .args(["config", "--local", GIT_REMOTE_OVERRIDE_KEY, remote])
        .status()
        .with_context(|| format!("failed to run git config --local {GIT_REMOTE_OVERRIDE_KEY}"))?;
    if !status.success() {
        anyhow::bail!("git config --local {GIT_REMOTE_OVERRIDE_KEY} {remote} exited with {status}");
    }

    info!(remote, "Configured exomonad.remote git config override");
    Ok(())
}

fn configure_forgejo_remote(
    cwd: &Path,
    forgejo_url: &str,
    forgejo_token: &str,
    remote: &str,
) -> Result<()> {
    let output = std::process::Command::new("git")
        .current_dir(cwd)
        .args(["remote", "get-url", remote])
        .output()
        .with_context(|| format!("failed to run git remote get-url {remote}"))?;
    if !output.status.success() {
        warn!(
            remote,
            "No such remote found; skipping Forgejo remote token auth setup"
        );
        return Ok(());
    }

    let old_url = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let Some(new_url) = forgejo_token_remote_url(&old_url, forgejo_url, forgejo_token) else {
        return Ok(());
    };
    let status = std::process::Command::new("git")
        .current_dir(cwd)
        .args(["remote", "set-url", remote, &new_url])
        .status()
        .with_context(|| format!("failed to run git remote set-url {remote}"))?;
    if !status.success() {
        anyhow::bail!("git remote set-url {remote} exited with {status}");
    }

    info!(
        remote,
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

fn write_codex_companion_config(
    config: &Config,
    dir: &Path,
    name: &str,
    role: &str,
    model: Option<&str>,
) -> Result<()> {
    let codex_dir = dir.join(".codex");
    std::fs::create_dir_all(&codex_dir)?;
    let root_instructions;
    let instructions = match role {
        "tl" | "root" => {
            root_instructions = codex_root_instructions(dir, &config.wasm_name);
            &root_instructions
        }
        "worker" => exomonad_core::services::agent_control::CODEX_WORKER_INSTRUCTIONS,
        "reviewer" => exomonad_core::services::agent_control::CODEX_REVIEWER_INSTRUCTIONS,
        _ => exomonad_core::services::agent_control::CODEX_DEV_INSTRUCTIONS,
    };
    let extra_mcp_servers = extra_mcp_servers_to_json(&config.extra_mcp_servers)?;
    let configured_effort = config.worker_effort_level.level.to_string();
    let rendered = exomonad_core::codex_config::render_codex_config_with_effort(
        name,
        role,
        instructions,
        model,
        Some(&configured_effort),
        &extra_mcp_servers,
        &exomonad_core::find_exomonad_binary(),
    );
    std::fs::write(codex_dir.join("config.toml"), rendered)?;
    Ok(())
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

fn parse_opencode_model_catalog(
    text: &str,
) -> std::collections::HashMap<String, std::collections::BTreeSet<String>> {
    let mut catalog = std::collections::HashMap::new();
    let mut json = String::new();
    let mut label = None;

    for line in text.lines() {
        if json.is_empty() {
            let trimmed = line.trim();
            if trimmed.starts_with('{') {
                json.push_str(trimmed);
            } else if !trimmed.is_empty() {
                label = Some(trimmed.to_string());
            }
            continue;
        }

        json.push('\n');
        json.push_str(line);
        let Ok(value) = serde_json::from_str::<Value>(&json) else {
            continue;
        };
        let Some(object) = value.as_object() else {
            json.clear();
            label = None;
            continue;
        };
        let id = object
            .get("id")
            .and_then(Value::as_str)
            .or(label.as_deref());
        let provider = object.get("providerID").and_then(Value::as_str);
        let variants: std::collections::BTreeSet<String> = object
            .get("variants")
            .and_then(Value::as_object)
            .map(|variants| variants.keys().cloned().collect())
            .unwrap_or_default();
        if let Some(id) = id {
            catalog.insert(id.to_string(), variants.clone());
            if let Some(provider) = provider {
                catalog.insert(format!("{provider}/{id}"), variants);
            }
        }
        json.clear();
        label = None;
    }

    catalog
}

async fn validate_opencode_model(model: &str, effort: Option<&str>) -> Result<()> {
    let out = tokio::process::Command::new("opencode")
        .args(["models", "--verbose"])
        .output()
        .await
        .context("Failed to run `opencode models --verbose` for validation")?;
    if !out.status.success() {
        anyhow::bail!(
            "`opencode models --verbose` exited {}: {}",
            out.status,
            String::from_utf8_lossy(&out.stderr)
        );
    }
    let text = std::str::from_utf8(&out.stdout)?;
    let catalog = parse_opencode_model_catalog(text);
    let Some(variants) = catalog.get(model) else {
        anyhow::bail!("Unknown opencode model `{model}`. Run `exomonad models` to see the list.");
    };
    if let Some(effort) = effort.filter(|value| !value.is_empty()) {
        if !variants.contains(effort) {
            let supported = if variants.is_empty() {
                "none".to_string()
            } else {
                variants.iter().cloned().collect::<Vec<_>>().join(", ")
            };
            anyhow::bail!(
                "Unsupported OpenCode effort `{effort}` for model `{model}`. Supported variants: {supported}. Correct with the role-specific effort flag."
            );
        }
    }
    Ok(())
}

fn validate_codex_model_name(model: &str) -> Result<()> {
    if !model.starts_with("gpt-") {
        anyhow::bail!(
            "Unknown Codex model `{model}`. Use a Codex/OpenAI model ID starting with `gpt-` \
             (for example `gpt-5.2-codex`)."
        );
    }
    Ok(())
}

async fn validate_codex_model(model: &str, effort: Option<&str>) -> Result<()> {
    validate_codex_model_name(model)?;
    let out = tokio::process::Command::new("codex")
        .args(["debug", "models"])
        .output()
        .await
        .context("Failed to run `codex debug models` for validation")?;
    if !out.status.success() {
        anyhow::bail!(
            "`codex debug models` exited {}: {}",
            out.status,
            String::from_utf8_lossy(&out.stderr)
        );
    }
    let catalog: Value =
        serde_json::from_slice(&out.stdout).context("Codex model catalog was not valid JSON")?;
    let models = catalog
        .get("models")
        .and_then(Value::as_array)
        .context("Codex model catalog did not contain a models array")?;
    let Some(model_record) = models
        .iter()
        .find(|record| record.get("slug").and_then(Value::as_str) == Some(model))
    else {
        anyhow::bail!("Unknown Codex model `{model}`. Run `codex debug models` to see the list.");
    };
    if let Some(effort) = effort.filter(|value| !value.is_empty()) {
        let supported = model_record
            .get("supported_reasoning_levels")
            .and_then(Value::as_array)
            .map(|levels| {
                levels
                    .iter()
                    .filter_map(|level| level.get("effort").and_then(Value::as_str))
                    .map(str::to_string)
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        if !supported.iter().any(|level| level == effort) {
            let supported = if supported.is_empty() {
                "none".to_string()
            } else {
                supported.join(", ")
            };
            anyhow::bail!(
                "Unsupported Codex effort `{effort}` for model `{model}`. Supported reasoning levels: {supported}. Correct with the role-specific effort flag."
            );
        }
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

async fn validate_reviewer_model_for_harness(
    agent_type: AgentType,
    model: Option<&str>,
    effort: Option<&str>,
) -> Result<()> {
    let Some(model) = model else {
        return Ok(());
    };

    match agent_type {
        AgentType::Claude => validate_claude_model(model),
        AgentType::Codex => validate_codex_model(model, effort).await,
        AgentType::OpenCode => validate_opencode_model(model, effort).await,
        AgentType::Shoal | AgentType::Process => Ok(()),
    }
}

fn reviewer_max_rounds_tmux_args(session: &str, value: Option<u32>) -> Vec<String> {
    let mut args = vec![
        "set-environment".to_string(),
        "-t".to_string(),
        session.to_string(),
    ];
    match value {
        Some(rounds) => {
            args.push(REVIEWER_MAX_ROUNDS_ENV.to_string());
            args.push(rounds.to_string());
        }
        None => {
            args.push("-u".to_string());
            args.push(REVIEWER_MAX_ROUNDS_ENV.to_string());
        }
    }
    args
}

fn set_reviewer_max_rounds_environment(session: &str, value: Option<u32>) -> Result<()> {
    let args = reviewer_max_rounds_tmux_args(session, value);
    let output = std::process::Command::new("tmux")
        .args(&args)
        .output()
        .context("Failed to propagate reviewer round limit to tmux session")?;
    if !output.status.success() {
        anyhow::bail!(
            "tmux set-environment failed for {}: {}",
            REVIEWER_MAX_ROUNDS_ENV,
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(())
}

fn agent_configuration_environment(config: &Config) -> String {
    let mut parts = Vec::new();
    if let Some(model) = &config.opencode.tl_model {
        parts.push(format!(
            "EXOMONAD_TL_MODEL={}",
            shell_escape::escape(model.clone().into())
        ));
    }
    if let Some(model) = &config.opencode.worker_model {
        parts.push(format!(
            "EXOMONAD_WORKER_MODEL={}",
            shell_escape::escape(model.clone().into())
        ));
    }
    if let Some(model) = &config.reviewer.model {
        parts.push(format!(
            "{}={}",
            REVIEWER_MODEL_ENV,
            shell_escape::escape(model.clone().into())
        ));
    }
    parts.push(format!(
        "{}={}",
        REVIEWER_EFFORT_ENV,
        shell_escape::escape(config.reviewer_effort_level.level.to_string().into())
    ));
    format!(" {}", parts.join(" "))
}

fn tl_loop_package_root(cwd: &Path) -> Result<PathBuf> {
    let local = cwd.join("tl_loop");
    if local.join("__main__.py").is_file() {
        return Ok(cwd.to_path_buf());
    }
    if let Ok(home) = std::env::var("HOME") {
        let installed = PathBuf::from(home).join(".exo");
        if installed.join("tl_loop/__main__.py").is_file() {
            return Ok(installed);
        }
    }
    anyhow::bail!(
        "programmatic TL package not found; run `just install-all` in the exomonad repository"
    )
}

fn write_tl_loop_plan(cwd: &Path, initial_prompt: Option<&str>) -> Result<()> {
    let Some(prompt) = initial_prompt
        .map(str::trim)
        .filter(|value| !value.is_empty())
    else {
        return Ok(());
    };
    let value = serde_json::from_str::<Value>(prompt)
        .context("initial_prompt must be a JSON WorkPlan document for the programmatic TL")?;
    if !value.is_object() {
        anyhow::bail!("initial_prompt must be a JSON object containing the TL WorkPlan");
    }
    let plan_path = cwd.join(".exo/tl-loop/plan.json");
    if plan_path.exists() {
        return Ok(());
    }
    if let Some(parent) = plan_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(plan_path, serde_json::to_string_pretty(&value)?)?;
    info!("Wrote structured TL plan from initial_prompt");
    Ok(())
}

fn write_tl_loop_identity(cwd: &Path, branch: &str) -> Result<()> {
    let agent_dir = cwd.join(".exo/agents/root");
    std::fs::create_dir_all(&agent_dir)?;
    let identity = serde_json::json!({
        "agent_name": "root",
        "slug": "root",
        "agent_type": "codex",
        "birth_branch": branch,
        "parent_branch": branch,
        "working_dir": ".",
        "display_name": "TL loop",
        "topology": "shared_dir",
        "model": null,
        "effort": null,
        "ledger_owned": true,
    });
    std::fs::write(
        agent_dir.join("identity.json"),
        serde_json::to_string_pretty(&identity)?,
    )?;
    Ok(())
}

fn tl_loop_command(cwd: &Path, package_root: &Path) -> String {
    let package = shell_escape::escape(package_root.display().to_string().into());
    let project = shell_escape::escape(cwd.display().to_string().into());
    let plan = shell_escape::escape(
        cwd.join(".exo/tl-loop/plan.json")
            .display()
            .to_string()
            .into(),
    );
    format!(
        "EXOMONAD_AGENT_ID=root EXOMONAD_ROLE=tl PYTHONPATH={package} python3 -m tl_loop run --project-root {project} --plan {plan} --run-id root --wait-for-plan"
    )
}

/// Run the init command: create or attach to tmux session.
// The CLI exposes these independent initialization options as separate flags.
#[allow(clippy::too_many_arguments)]
pub async fn run(
    session_override: Option<String>,
    recreate: bool,
    openrouter: bool,
    worker: Option<String>,
    worker_model: Option<String>,
    worker_effort_level: Option<EffortLevel>,
    reviewer_effort_level: Option<EffortLevel>,
    reviewer: Option<String>,
    reviewer_model: Option<String>,
    reviewer_max_rounds: Option<u32>,
    verbose: bool,
    set_git_remote: Option<String>,
    reset_inbox: bool,
    import_legacy: Vec<PathBuf>,
    import_legacy_dry_run: bool,
) -> Result<()> {
    use exomonad_core::services::tmux_ipc::TmuxIpc;
    use exomonad_core::services::{resolve_role_context_path, AgentType, InboxStore};
    use std::io::{IsTerminal, Write};
    let cwd = std::env::current_dir()?;
    if reviewer_max_rounds == Some(0) {
        anyhow::bail!("--reviewer-max-rounds must be at least 1, got 0");
    }
    let config_path = cwd.join(".exo/config.toml");
    if !config_path.exists() {
        anyhow::bail!("No exomonad project found. Run `exomonad new` first.");
    }

    if let Some(ref remote_name) = set_git_remote {
        set_git_remote_override(&cwd, remote_name)?;
    }

    if reset_inbox {
        InboxStore::open(&cwd)?.clear_all()?;
        info!("cleared inbox messages and metadata");
    }

    // Resolve config
    let mut config = Config::discover()?;
    let tl_loop_root = tl_loop_package_root(&cwd)?;
    write_tl_loop_plan(&cwd, config.initial_prompt.as_deref())?;

    if !import_legacy.is_empty() {
        crate::logs::run(
            &cwd,
            import_legacy,
            "auto".to_string(),
            import_legacy_dry_run,
            false,
        )?;
        info!(
            dry_run = import_legacy_dry_run,
            "explicit legacy observability import completed before init"
        );
    }

    // CLI flags override config
    if let Some(ref worker_type) = worker {
        config.spawn_agent_type = parse_agent_type(worker_type)?;
    }
    if let Some(m) = worker_model {
        if config.spawn_agent_type == AgentType::OpenCode {
            config.opencode.worker_model = Some(m);
        }
    }
    if let Some(level) = worker_effort_level {
        config.worker_effort_level = ResolvedEffort::from_cli(level);
    }
    if let Some(level) = reviewer_effort_level {
        config.reviewer_effort_level = ResolvedEffort::from_cli(level);
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
        config.spawn_agent_type,
        config.opencode.worker_model.as_deref(),
        "[opencode].worker_model",
        "spawn_agent_type",
    )?;

    let tl_effort = config.tl_effort_level.level.to_string();
    let worker_effort = config.worker_effort_level.level.to_string();
    let reviewer_effort = config.reviewer_effort_level.level.to_string();
    log_ignored_effort("tl", config.root_agent_type, &tl_effort);
    log_ignored_effort("worker", config.spawn_agent_type, &worker_effort);
    log_ignored_effort("reviewer", config.reviewer.agent_type, &reviewer_effort);
    if config.spawn_agent_type == AgentType::OpenCode {
        if let Some(m) = config.opencode.worker_model.as_deref() {
            validate_opencode_model(m, Some(&worker_effort)).await?;
        }
    } else if config.spawn_agent_type == AgentType::Codex {
        if let Some(m) = config.opencode.worker_model.as_deref() {
            validate_codex_model(m, Some(&worker_effort)).await?;
        }
    }
    validate_reviewer_model_for_harness(
        config.reviewer.agent_type,
        config.reviewer.model.as_deref(),
        Some(&reviewer_effort),
    )
    .await?;

    let root_model = None::<&str>;
    let worker_model = if config.spawn_agent_type == AgentType::OpenCode {
        config.opencode.worker_model.as_deref()
    } else {
        None
    };
    let init_argv = redact_init_argv(std::env::args().collect::<Vec<_>>());
    if let Err(e) = append_init_invocation_log(&cwd, &config, &init_argv) {
        warn!(error = %e, "Failed to append init invocation log");
    } else {
        info!(
            root_agent_type = agent_type_str(config.root_agent_type),
            spawn_agent_type = agent_type_str(config.spawn_agent_type),
            reviewer_agent_type = agent_type_str(config.reviewer.agent_type),
            root_model = ?root_model,
            worker_model = ?worker_model,
            reviewer_model = ?config.reviewer.model,
            tl_effort = %config.tl_effort_level.level,
            tl_effort_source = %config.tl_effort_level.source,
            worker_effort = %config.worker_effort_level.level,
            worker_effort_source = %config.worker_effort_level.source,
            reviewer_effort = %config.reviewer_effort_level.level,
            reviewer_effort_source = %config.reviewer_effort_level.source,
            "Resolved exomonad init agent configuration"
        );
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
    let session_transition = if recreate {
        exomonad_core::services::SessionTransition::Recreate
    } else if session_alive {
        exomonad_core::services::SessionTransition::Attach
    } else {
        exomonad_core::services::SessionTransition::Fresh
    };
    exomonad_core::services::transition_session(&cwd, session_transition, "exomonad init")?;
    if should_attach_existing_session(recreate, session_alive) {
        if reviewer_max_rounds.is_some() {
            anyhow::bail!(
                "--reviewer-max-rounds applies when starting a server; use --recreate to restart the existing session"
            );
        }
        if reset_inbox {
            warn!(
                session = %session,
                "--reset-inbox cleared the inbox while an existing session was alive; attaching without restarting it"
            );
        }
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
        write_tl_loop_identity(&cwd, &current_branch)?;
        std::fs::write(root_agent_dir.join(".birth_branch"), &current_branch)?;
        info!(branch = %current_branch, "Wrote root agent birth branch");
    }

    if let (Some(forgejo_url), Some(forgejo_token)) = (
        config.forgejo_url.as_deref(),
        config.forgejo_token.as_deref(),
    ) {
        let git_remote = resolve_git_remote(&cwd);
        if let Err(e) = configure_forgejo_remote(&cwd, forgejo_url, forgejo_token, &git_remote) {
            warn!(error = %e, "Failed to auto-configure Forgejo remote URL (non-fatal)");
        }
    } else if config.forgejo_url.is_none() && config.forgejo_token.is_none() {
        check_fj_cli_configuration(&cwd);
    }

    // Hooks remain available to Claude workers and companions; the root TL is
    // the Python controller below and never launches an interactive harness.
    let binary_path = exomonad_core::find_exomonad_binary();
    exomonad_core::hooks::HookConfig::write_persistent(&cwd, &binary_path, None, None)
        .context("Failed to write hook configuration")?;
    info!("Hook configuration written to .claude/settings.local.json");

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
    let mut mcp_servers = serde_json::Map::new();
    mcp_servers.insert(
        "exomonad".to_string(),
        exomonad_mcp_server(&binary_path, "tl", "root"),
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

    // 2. Create session in background
    let server_window_id = TmuxIpc::new_session(&session, &cwd).await?;

    // Verify session
    if !TmuxIpc::has_session(&session).await? {
        anyhow::bail!(
            "tmux session '{}' was created but is not responding.",
            session
        );
    }

    set_reviewer_max_rounds_environment(&session, reviewer_max_rounds)?;

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

    let model_env = agent_configuration_environment(&config);
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

    // The human-facing TL window runs one coordinator: the Python controller.
    // Root harness settings and root_command are intentionally ignored.
    let tl_cwd = cwd.clone();
    let base_command = tl_loop_command(&cwd, &tl_loop_root);

    let tl_command = match config.shell_command {
        Some(ref sc) => format!("{} -c \"{}\"", sc, base_command.replace('"', "\\\"")),
        None => base_command,
    };

    let _ = ipc.new_window("TL", &tl_cwd, &shell, &tl_command).await?;

    // 4. Poll for server socket
    wait_for_server_socket(&cwd).await?;
    report_observability_health(&cwd);

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
        } else if agent_type == AgentType::OpenCode {
            use exomonad_core::services::agent_control::AgentControlService;
            use exomonad_core::services::Services;
            let exo_dir = agent_dir.join(".exo");
            std::fs::create_dir_all(&exo_dir)?;
            let socket_target = exo_dir.join("server.sock");
            let _ = std::fs::remove_file(&socket_target);
            std::os::unix::fs::symlink(cwd.join(".exo/server.sock"), &socket_target)?;
            let extra_mcp = extra_mcp_servers_to_json(&config.extra_mcp_servers)?;
            let effort = config.worker_effort_level.level.to_string();
            let opencode_config =
                AgentControlService::<Services>::generate_opencode_tl_settings_with_effort(
                    &companion.name,
                    &companion.role,
                    &extra_mcp,
                    Some(&effort),
                );
            std::fs::write(
                agent_dir.join("opencode.json"),
                serde_json::to_string_pretty(&opencode_config)?,
            )?;
            AgentControlService::<Services>::write_opencode_plugin_files(&agent_dir)
                .await
                .context("Failed to write companion OpenCode plugin files")?;
            agent_dir.clone()
        } else if agent_type == AgentType::Codex {
            let exo_dir = agent_dir.join(".exo");
            std::fs::create_dir_all(&exo_dir)?;
            let socket_target = exo_dir.join("server.sock");
            let _ = std::fs::remove_file(&socket_target);
            std::os::unix::fs::symlink(cwd.join(".exo/server.sock"), &socket_target)?;
            write_codex_companion_config(
                &config,
                &agent_dir,
                &companion.name,
                &companion.role,
                companion.model.as_deref(),
            )?;
            agent_dir.clone()
        } else {
            // Shoal companions use the project root CWD.
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
        let worker_effort_flag = format!(
            " --effort {}",
            shell_escape::escape(config.worker_effort_level.level.to_string().into())
        );
        let worker_variant_flag = format!(
            " --variant {}",
            shell_escape::escape(config.worker_effort_level.level.to_string().into())
        );
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
                    "{env_prefix}{}{model_flag}{worker_effort_flag}{task_part}; echo; echo '[{} exited]'; exec bash -l",
                    companion.command, companion.name
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
                if escaped_task.is_some() {
                    format!(
                        "{env_prefix}opencode run{yolo}{model_flag}{worker_variant_flag}{task_part}"
                    )
                } else {
                    format!(
                        "{env_prefix}opencode{yolo}{model_flag} --agent exomonad-{}",
                        companion.role
                    )
                }
            }
            AgentType::Codex => {
                let task_part = match &escaped_task {
                    Some(task) => format!(" '{}'", task),
                    None => String::new(),
                };
                let configured_effort = config.worker_effort_level.level.to_string();
                let effort = configured_effort.as_str();
                let codex_model_flag = companion
                    .model
                    .as_deref()
                    .map(|model| format!(" --model {}", shell_escape::escape(model.into())))
                    .unwrap_or_default();
                let effort_flag = format!(" -c model_reasoning_effort=\"{}\"", effort);
                format!(
                    "{env_prefix}{} --dangerously-bypass-approvals-and-sandbox --cd {}{codex_model_flag}{effort_flag}{task_part}",
                    agent_type.command(),
                    shell_escape::escape(companion_cwd.display().to_string().into())
                )
            }
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

fn report_observability_health(project_dir: &Path) {
    match exomonad_core::services::read_sink_health(project_dir) {
        Ok(Some(health)) => info!(
            status = %health.measurement_status,
            accepted_events = health.accepted_event_count,
            rejected_events = health.rejected_event_count,
            write_failures = health.write_failure_count,
            last_successful_seq = ?health.last_successful_seq,
            "structured observability startup health"
        ),
        Ok(None) => warn!(
            "structured observability startup health is unknown; sink-health.json is not available yet"
        ),
        Err(error) => warn!(%error, "could not read structured observability startup health"),
    }
}

/// Parse agent type from CLI string.
fn parse_agent_type(s: &str) -> Result<AgentType> {
    let value = s.to_lowercase();
    if value == ["ge", "mini"].concat() {
        anyhow::bail!(
            "{}",
            exomonad_core::services::agent_control::AGENT_TYPE_DEPRECATION_MESSAGE
        );
    }
    match value.as_str() {
        "claude" | "claude-code" => Ok(AgentType::Claude),
        "opencode" | "opencode-cli" => Ok(AgentType::OpenCode),
        "codex" => Ok(AgentType::Codex),
        "shoal" => Ok(AgentType::Shoal),
        _ => anyhow::bail!(
            "Unknown agent type: {}. Valid values: claude, opencode, codex, shoal",
            s
        ),
    }
}

fn agent_type_str(t: AgentType) -> &'static str {
    match t {
        AgentType::Claude => "claude",
        AgentType::OpenCode => "opencode",
        AgentType::Codex => "codex",
        AgentType::Shoal => "shoal",
        AgentType::Process => "process",
    }
}

fn log_ignored_effort(role: &str, agent_type: AgentType, effort: &str) {
    if matches!(agent_type, AgentType::Shoal) {
        info!(
            role,
            harness = agent_type_str(agent_type),
            effort,
            "Configured effort is ignored because this harness has no stable effort interface"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use exomonad_test_support::{
        assert_fixture_git_root, init_fixture_git_repository, run_fixture_git_command,
    };

    #[test]
    fn codex_protocol_delivery_is_prompt_independent() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join(".exo/roles/sentinel/context");
        std::fs::create_dir_all(&path).unwrap();
        std::fs::write(path.join("root.md"), "SENTINEL ROOT PROTOCOL").unwrap();

        for initial_prompt in [None, Some("task-only initial prompt")] {
            let instructions = codex_root_instructions(tmp.path(), "sentinel");
            assert!(instructions.contains("SENTINEL ROOT PROTOCOL"));
            assert!(instructions.contains("Codex Runtime Notes"));
            if let Some(prompt) = initial_prompt {
                assert!(!instructions.contains(prompt));
            }
        }
    }

    #[test]
    fn reviewer_max_rounds_tmux_args_set_or_clear_session_override() {
        assert_eq!(
            reviewer_max_rounds_tmux_args("demo", Some(7)),
            vec![
                "set-environment",
                "-t",
                "demo",
                REVIEWER_MAX_ROUNDS_ENV,
                "7"
            ]
        );
        assert_eq!(
            reviewer_max_rounds_tmux_args("demo", None),
            vec![
                "set-environment",
                "-t",
                "demo",
                "-u",
                REVIEWER_MAX_ROUNDS_ENV
            ]
        );
    }

    #[test]
    fn watcher_dashboard_command_creates_log_file() {
        let tmp = tempfile::tempdir().unwrap();
        let command = watcher_dashboard_command(tmp.path()).unwrap();
        let log_path = tmp.path().join(".exo/logs/watcher.log");

        assert!(log_path.exists());
        assert_eq!(command, "exomonad watch");
    }

    #[test]
    fn redact_init_argv_redacts_sensitive_flag_values() {
        let redacted = redact_init_argv(vec![
            "exomonad".to_string(),
            "init".to_string(),
            "--worker".to_string(),
            "opencode".to_string(),
            "--api-key".to_string(),
            "secret-value".to_string(),
            "--token=abc123".to_string(),
        ]);

        assert_eq!(
            redacted,
            vec![
                "exomonad".to_string(),
                "init".to_string(),
                "--worker".to_string(),
                "opencode".to_string(),
                "--api-key".to_string(),
                "<redacted>".to_string(),
                "--token=<redacted>".to_string(),
            ]
        );
    }

    #[test]
    fn append_init_invocation_log_records_resolved_worker_type() {
        let tmp = tempfile::tempdir().unwrap();
        let mut config = Config {
            tmux_session: "RustDebuggerRepo".to_string(),
            root_agent_type: AgentType::Claude,
            spawn_agent_type: AgentType::OpenCode,
            model: Some("sonnet".to_string()),
            ..Config::default()
        };
        config.reviewer.agent_type = AgentType::Codex;
        config.opencode.worker_model = Some("opencode-go/deepseek-v4-pro".to_string());

        append_init_invocation_log(
            tmp.path(),
            &config,
            &[
                "exomonad".to_string(),
                "init".to_string(),
                "--worker".to_string(),
                "opencode".to_string(),
            ],
        )
        .unwrap();

        let log = std::fs::read_to_string(tmp.path().join(".exo/logs/init.jsonl")).unwrap();
        let value: serde_json::Value = serde_json::from_str(log.trim()).unwrap();

        assert_eq!(value["resolved"]["root_agent_type"], "claude");
        assert_eq!(value["resolved"]["spawn_agent_type"], "opencode");
        assert_eq!(value["resolved"]["reviewer_agent_type"], "codex");
        assert_eq!(
            value["resolved"]["opencode_worker_model"],
            "opencode-go/deepseek-v4-pro"
        );
        assert_eq!(value["argv"][2], "--worker");
    }

    #[test]
    fn agent_configuration_environment_propagates_reviewer_settings() {
        let mut config = Config::default();
        config.opencode.tl_model = Some("opencode/tl model".to_string());
        config.opencode.worker_model = Some("opencode/worker".to_string());
        config.reviewer.model = Some("openai/reviewer model".to_string());
        config.reviewer_effort_level = ResolvedEffort::from_cli(EffortLevel::XHigh);

        let environment = agent_configuration_environment(&config);

        assert!(environment.contains("EXOMONAD_TL_MODEL='opencode/tl model'"));
        assert!(environment.contains("EXOMONAD_WORKER_MODEL=opencode/worker"));
        assert!(environment.contains("EXOMONAD_REVIEWER_MODEL='openai/reviewer model'"));
        assert!(environment.contains("EXOMONAD_REVIEWER_EFFORT_LEVEL=xhigh"));

        config.reviewer.model = None;
        let default_environment = agent_configuration_environment(&config);
        assert!(!default_environment.contains(REVIEWER_MODEL_ENV));
        assert!(default_environment.contains("EXOMONAD_REVIEWER_EFFORT_LEVEL=xhigh"));
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

    fn init_temp_git_repo(remotes: &[(&str, &str)]) -> tempfile::TempDir {
        let tmp = tempfile::tempdir().unwrap();
        init_fixture_git_repository(tmp.path()).unwrap();
        for (name, url) in remotes {
            run_fixture_git_command(tmp.path(), &["remote", "add", name, url]).unwrap();
        }
        tmp
    }

    #[test]
    fn resolve_git_remote_defaults_to_origin_when_unset() {
        let tmp = init_temp_git_repo(&[("origin", "https://github.com/nanonite/repo.git")]);
        assert_eq!(resolve_git_remote(tmp.path()), "origin");
    }

    #[test]
    fn set_git_remote_override_rejects_nonexistent_remote() {
        let tmp = init_temp_git_repo(&[("origin", "https://github.com/nanonite/repo.git")]);
        let err = set_git_remote_override(tmp.path(), "forgejo")
            .expect_err("remote does not exist yet")
            .to_string();
        assert!(err.contains("no such git remote"), "{err}");
    }

    #[test]
    fn set_git_remote_override_persists_and_resolves() {
        let tmp = init_temp_git_repo(&[
            ("origin", "https://github.com/nanonite/repo.git"),
            ("forgejo", "http://localhost:3000/goya/repo.git"),
        ]);
        set_git_remote_override(tmp.path(), "forgejo").unwrap();
        assert_eq!(resolve_git_remote(tmp.path()), "forgejo");
    }

    #[test]
    fn configure_forgejo_remote_targets_configured_remote_not_origin() {
        let tmp = init_temp_git_repo(&[
            ("origin", "git@github.com:nanonite/repo.git"),
            ("forgejo", "git@localhost:goya/repo.git"),
        ]);

        configure_forgejo_remote(tmp.path(), "http://localhost:3000", "token-123", "forgejo")
            .unwrap();

        assert_fixture_git_root(tmp.path()).unwrap();
        let forgejo_url =
            run_fixture_git_command(tmp.path(), &["remote", "get-url", "forgejo"]).unwrap();
        let forgejo_url = String::from_utf8_lossy(&forgejo_url.stdout)
            .trim()
            .to_string();
        assert_eq!(
            forgejo_url,
            "http://forgejo_pat:token-123@localhost:3000/goya/repo.git"
        );

        let origin_url =
            run_fixture_git_command(tmp.path(), &["remote", "get-url", "origin"]).unwrap();
        let origin_url = String::from_utf8_lossy(&origin_url.stdout)
            .trim()
            .to_string();
        assert_eq!(
            origin_url, "git@github.com:nanonite/repo.git",
            "origin must be untouched when the configured remote is 'forgejo'"
        );
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
        assert!(validate_codex_model_name("gpt-5.2-codex").is_ok());
        assert!(validate_codex_model_name("opencode-go/deepseek-v4-flash").is_err());
        assert!(validate_codex_model_name("claude-sonnet-4-6").is_err());
    }

    #[tokio::test]
    async fn reviewer_validation_rejects_cross_harness_model() {
        let error = validate_reviewer_model_for_harness(
            AgentType::Codex,
            Some("opencode-go/deepseek-v4-pro"),
            Some("high"),
        )
        .await
        .unwrap_err();

        assert!(error.to_string().contains("Codex model"));
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
    fn tl_loop_command_uses_programmatic_controller() {
        let command = tl_loop_command(Path::new("/tmp/repo"), Path::new("/tmp/exo"));
        assert!(command.contains("EXOMONAD_ROLE=tl"));
        assert!(command.contains("PYTHONPATH=/tmp/exo"));
        assert!(command.contains("python3 -m tl_loop run"));
        assert!(command.contains("--wait-for-plan"));
    }

    #[test]
    fn structured_initial_prompt_writes_plan_and_rejects_legacy_text() {
        let tmp = tempfile::tempdir().unwrap();
        write_tl_loop_plan(tmp.path(), Some(r#"{"plan":{"leaves":[]}}"#)).unwrap();
        let plan = std::fs::read_to_string(tmp.path().join(".exo/tl-loop/plan.json")).unwrap();
        assert!(plan.contains("\"plan\""));

        let invalid = tempfile::tempdir().unwrap();
        let error = write_tl_loop_plan(invalid.path(), Some("interactive TL prompt")).unwrap_err();
        assert!(error
            .to_string()
            .contains("initial_prompt must be a JSON WorkPlan"));
    }
    #[test]
    fn parses_opencode_verbose_catalog_variants() {
        let catalog = parse_opencode_model_catalog(
            "opencode-go/deepseek-v4-pro\n{\n  \"id\": \"deepseek-v4-pro\",\n  \"providerID\": \"opencode-go\",\n  \"variants\": {\n    \"high\": {},\n    \"max\": {}\n  }\n}\n",
        );

        assert!(catalog["opencode-go/deepseek-v4-pro"].contains("high"));
        assert!(catalog["opencode-go/deepseek-v4-pro"].contains("max"));
        assert!(!catalog["opencode-go/deepseek-v4-pro"].contains("medium"));
    }
}
