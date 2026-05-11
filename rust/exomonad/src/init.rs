use crate::uds_client;
use anyhow::{Context, Result};
use exomonad::config::Config;
use exomonad_core::services::AgentType;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
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
    if stripped.is_empty() { None } else { Some(stripped) }
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
        anyhow::bail!(
            "Unknown opencode model `{model}`. Run `exomonad models` to see the list."
        );
    }
    Ok(())
}

/// Run the init command: create or attach to tmux session.
pub async fn run(session_override: Option<String>, recreate: bool, opencode_as_tl: bool, openrouter: bool, tl: Option<String>, worker: Option<String>, tl_model: Option<String>, worker_model: Option<String>, reviewer: Option<String>, reviewer_model: Option<String>) -> Result<()> {
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
        config.opencode.tl_model = Some(m);
    }
    if let Some(m) = worker_model {
        config.opencode.worker_model = Some(m);
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
    match config.reviewer.agent_type {
        AgentType::OpenCode => {
            if let Some(m) = config.reviewer.model.as_deref() {
                validate_opencode_model(m).await?;
            }
        }
        AgentType::Claude => {
            if let Some(m) = config.reviewer.model.as_deref() {
                validate_claude_model(m)?;
            }
        }

        _ => {}
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
                    anyhow::bail!("OTel endpoint unreachable. Start it with:\n  docker compose -f ~/.exo/otel/docker-compose.yml up -d");
                }
            }
        }
    }

    let session = session_override.unwrap_or(config.tmux_session.clone());

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

    // Copy spindle binary from global install when Tangled is configured
    if config.tangled_owner_did.is_some() {
        let spindle_local = cwd.join(".exo/bin/spindle");
        if !spindle_local.exists() {
            if let Ok(home) = std::env::var("HOME") {
                let spindle_global = PathBuf::from(home).join(".exo/bin/spindle");
                if spindle_global.exists() {
                    std::fs::create_dir_all(spindle_local.parent().unwrap())?;
                    std::fs::copy(&spindle_global, &spindle_local)?;
                    #[cfg(unix)]
                    {
                        use std::os::unix::fs::PermissionsExt;
                        let mut perms = std::fs::metadata(&spindle_local)?.permissions();
                        perms.set_mode(0o755);
                        std::fs::set_permissions(&spindle_local, perms)?;
                    }
                    info!(
                        src = %spindle_global.display(),
                        dst = %spindle_local.display(),
                        "Copied spindle binary from global install"
                    );
                } else {
                    warn!(
                        "Tangled configured but spindle not found at ~/.exo/bin/spindle. \
                        Build it: cd tangled-core && go build -o ~/.exo/bin/spindle ./cmd/spindle"
                    );
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

    // Write hook configuration (SessionStart registers Claude UUID for --fork-session)
    // For OpenCode TL, write opencode.json MCP config instead of Claude hooks
    let binary_path = exomonad_core::find_exomonad_binary();
    if config.root_agent_type == AgentType::OpenCode {
        let opencode_config = serde_json::json!({
            "mcp": {
                "exomonad": {
                    "type": "local",
                    "command": ["exomonad", "mcp-stdio", "--role", "root", "--name", "root"]
                }
            },
            "instructions": [exomonad_core::services::agent_control::OPENCODE_TL_INSTRUCTIONS],
        });
        let opencode_dir = cwd.join(".exo/agents/root");
        std::fs::create_dir_all(&opencode_dir)?;
        std::fs::write(opencode_dir.join("opencode.json"), serde_json::to_string_pretty(&opencode_config)?)?;
        info!("OpenCode MCP configuration written to .exo/agents/root/opencode.json");
    } else {
        exomonad_core::hooks::HookConfig::write_persistent(&cwd, &binary_path, None, None)
            .context("Failed to write hook configuration")?;
        info!("Hook configuration written to .claude/settings.local.json");
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
            let rules_dir = cwd.join(".claude/rules");
            std::fs::create_dir_all(&rules_dir)?;
            let link = rules_dir.join("exomonad_role.md");
            let _ = std::fs::remove_file(&link); // idempotent
                                                 // Compute relative path from .claude/rules/ to the source
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
            serde_json::json!({
                "type": "stdio",
                "command": "exomonad",
                "args": ["mcp-stdio", "--role", "root", "--name", "root"]
            }),
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

    // Register repo with local Tangled knot (idempotent — no-op if already registered)
    register_tangled_repo(&cwd, &config).await;

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

    let session_alive = TmuxIpc::has_session(&session).await?;

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

        // Clear stale session state: PR registry and non-root agent entries.
        // These are per-session — the old agents are dead and their PRs will never
        // be updated, so carrying them forward causes spurious reviewer spawns.
        let prs_path = cwd.join(".exo/prs.json");
        if prs_path.exists() {
            let _ = std::fs::remove_file(&prs_path);
            info!("Cleared stale prs.json");
        }
        let agents_dir = cwd.join(".exo/agents");
        if let Ok(entries) = std::fs::read_dir(&agents_dir) {
            for entry in entries.flatten() {
                if entry.file_name() != "root" {
                    let _ = std::fs::remove_dir_all(entry.path());
                }
            }
            info!("Cleared stale agent entries (kept root)");
        }

        if session_alive {
            info!(session = %session, "Deleting session (--recreate)");
            TmuxIpc::kill_session(&session).await?;
        }
    } else if session_alive {
        // Attach to running session
        info!(session = %session, "Attaching to session");
        return TmuxIpc::attach_session(&session).await;
    }

    // Create fresh session
    info!(session = %session, "Creating session");

    // 1. Write .mcp.json (for Claude Code discovery)
    if config.root_agent_type == AgentType::Claude {
        let mut mcp_servers = serde_json::Map::new();
        mcp_servers.insert(
            "exomonad".to_string(),
            serde_json::json!({
                "type": "stdio",
                "command": "exomonad",
                "args": ["mcp-stdio", "--role", "root", "--name", "root"]
            }),
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

    // Anchor chainlink to the root workspace DB so worktree windows don't create their own
    let chainlink_db = cwd.join(".chainlink/issues.db");
    let _ = std::process::Command::new("tmux")
        .args([
            "set-environment",
            "-t",
            &session,
            "CHAINLINK_DB",
            chainlink_db.to_str().unwrap_or_default(),
        ])
        .status();

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
    for var in ["GITHUB_TOKEN", "GITHUB_API_URL"] {
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
    let serve_cmd = format!(
        "EXOMONAD_TMUX_SESSION={} EXOMONAD_ROOT_AGENT_TYPE={} EXOMONAD_SPAWN_AGENT_TYPE={} EXOMONAD_REVIEWER_AGENT_TYPE={}{} exomonad serve",
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
        // Write opencode.json to the repo root so the TL window finds it via CWD.
        let mut opencode_mcp_servers = serde_json::Map::new();
        opencode_mcp_servers.insert(
            "exomonad".to_string(),
            serde_json::json!({
                "type": "local",
                "command": ["exomonad", "mcp-stdio", "--role", "root", "--name", "root"]
            }),
        );
        for (name, server) in &config.extra_mcp_servers {
            let entry = match server {
                crate::config::McpServerConfig::Http { url, headers } => {
                    let mut e = serde_json::json!({"type": "http", "url": url});
                    if !headers.is_empty() {
                        e["headers"] = serde_json::to_value(headers)?;
                    }
                    e
                }
                crate::config::McpServerConfig::Stdio { command, args } => {
                    serde_json::json!({"type": "stdio", "command": command, "args": args})
                }
            };
            opencode_mcp_servers.insert(name.clone(), entry);
        }
        let opencode_config = serde_json::json!({ "mcp": opencode_mcp_servers });
        std::fs::write(
            cwd.join("opencode.json"),
            serde_json::to_string_pretty(&opencode_config)?,
        )?;
        info!("Wrote opencode.json to repo root for OpenCode TL");
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
            (AgentType::Claude, _) => format!("claude --dangerously-skip-permissions{model_flag} -c || claude --dangerously-skip-permissions{model_flag}; echo; echo [Claude Code exited]; exec bash -l"),
            (AgentType::Gemini, Some(prompt)) => {
                let yolo_flag = if config.yolo { " --yolo" } else { "" };
                format!("gemini{model_flag}{yolo_flag} --prompt-interactive '{}'", prompt.replace('\'', "'\\''"))
            }
            (AgentType::Gemini, None) => {
                let yolo_flag = if config.yolo { " --yolo" } else { "" };
                format!("gemini{model_flag}{yolo_flag}")
            }
            (AgentType::Shoal, Some(prompt)) => format!("shoal-agent --exo root --prompt '{}'", prompt.replace('\'', "'\\''")),
            (AgentType::Shoal, None) => "shoal-agent --exo root".to_string(),
            (AgentType::OpenCode, Some(prompt)) => {
                let yolo = if config.yolo { " --dangerously-skip-permissions" } else { "" };
                let chainlink_protocol = read_chainlink_tl_protocol(&cwd);
                let augmented = match chainlink_protocol {
                    Some(ref protocol) => format!("{}\n\n---\n\n{}", protocol, prompt),
                    None => prompt.to_string(),
                };
                format!("opencode run{yolo}{opencode_model_flag} '{}'", augmented.replace('\'', "'\\''"))
            }
            (AgentType::OpenCode, None) => {
                let yolo = if config.yolo { " --dangerously-skip-permissions" } else { "" };
                format!("opencode{opencode_model_flag}{yolo}")
            }
            (AgentType::Process, _) => unreachable!("Process is for companions only, not root agent"),
        }
    };

    let tl_command = match config.shell_command {
        Some(sc) => format!("{} -c \"{}\"", sc, base_command.replace('"', "\\\"")),
        None => base_command,
    };

    let _ = ipc.new_window("TL", &tl_cwd, &shell, &tl_command).await?;

    // 4. Poll for server socket
    wait_for_server_socket(&cwd).await?;

    // 5. Spawn companion agents
    // Auto-inject spindle as a process companion when Tangled is configured, unless the user
    // has already declared a companion named "spindle".
    let spindle_path = cwd.join(".exo/bin/spindle");
    let auto_spindle = build_spindle_companion(
        config.tangled_owner_did.as_deref(),
        config.tangled_knot_url.as_deref(),
        config.tangled_spindle_db.as_deref(),
        &spindle_path,
        &config.companions,
    );

    let companions_to_spawn: Vec<&crate::config::CompanionConfig> = auto_spindle
        .iter()
        .chain(config.companions.iter())
        .collect();

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
                serde_json::json!({
                    "type": "stdio",
                    "command": "exomonad",
                    "args": ["mcp-stdio", "--role", &companion.role, "--name", &companion.name]
                }),
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
                "Symlinked server socket into companion worktree"
            );

            worktree_path
        } else {
            // Gemini/Shoal companions use project root CWD
            let companion_mcp = serde_json::json!({
                "mcpServers": {
                    "exomonad": {
                        "type": "stdio",
                        "command": "exomonad",
                        "args": ["mcp-stdio", "--role", &companion.role, "--name", &companion.name]
                    }
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
                let yolo = if config.yolo { " --dangerously-skip-permissions" } else { "" };
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

/// Register the current workspace repo with the local Tangled knot and spindle.
///
/// Requires `tangled_knot_container`, `tangled_owner_did`, and `tangled_spindle_db` in config.
/// Derives the repo name from the `origin` git remote URL (last path segment, `.git` stripped),
/// falling back to the project directory name.
///
/// All steps are idempotent — safe to call on every `exomonad init`. Failures are warnings, not
/// errors: a missing Docker container or unreachable spindle DB must not prevent the session from
/// starting.
async fn register_tangled_repo(cwd: &Path, config: &exomonad::config::Config) {
    let (container, owner_did, spindle_db) = match (
        config.tangled_knot_container.as_deref(),
        config.tangled_owner_did.as_deref(),
        config.tangled_spindle_db.as_deref(),
    ) {
        (Some(c), Some(d), Some(s)) => (c, d, s),
        _ => {
            debug!("Tangled registration skipped: tangled_knot_container / tangled_owner_did / tangled_spindle_db not set");
            return;
        }
    };

    let knot_hostname = config
        .tangled_knot_url
        .as_deref()
        .unwrap_or("localhost:5555")
        .trim_start_matches("ws://")
        .trim_start_matches("wss://")
        .to_string();

    // Derive repo name from git remote origin URL, fall back to directory name.
    let repo_name = std::process::Command::new("git")
        .args(["remote", "get-url", "origin"])
        .current_dir(cwd)
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .and_then(|url| {
            url.trim_end_matches('/')
                .rsplit('/')
                .next()
                .map(|s| s.trim_end_matches(".git").to_string())
                .filter(|s| !s.is_empty())
        })
        .or_else(|| {
            cwd.file_name()
                .and_then(|n| n.to_str())
                .map(|s| s.to_string())
        })
        .unwrap_or_else(|| "repo".to_string());

    info!(
        container,
        owner_did,
        repo_name,
        "Registering repo with local Tangled knot"
    );

    // Step 1: create bare repo in knot container.
    let create_cmd = format!(
        "mkdir -p /home/git/repositories/owner/{name}.git && \
         git init --bare /home/git/repositories/owner/{name}.git 2>/dev/null || true && \
         chown -R git:git /home/git/repositories/owner/{name}.git",
        name = repo_name
    );
    let out = std::process::Command::new("docker")
        .args(["exec", container, "sh", "-c", &create_cmd])
        .output();
    match out {
        Ok(o) if o.status.success() => {
            info!(repo_name, "Knot: bare repo created/confirmed");
        }
        Ok(o) => {
            warn!(
                repo_name,
                stderr = %String::from_utf8_lossy(&o.stderr),
                "Knot: docker exec for bare repo returned non-zero (may already exist)"
            );
        }
        Err(e) => {
            warn!(error = %e, "Knot: docker exec failed — is Docker running?");
            return;
        }
    }

    // Step 2: create DID-based symlink for HTTP clone routing.
    let symlink_cmd = format!(
        "mkdir -p '/home/git/repositories/{did}' && \
         ln -sfn '../owner/{name}.git' '/home/git/repositories/{did}/{name}'",
        did = owner_did,
        name = repo_name
    );
    let out = std::process::Command::new("docker")
        .args(["exec", container, "sh", "-c", &symlink_cmd])
        .output();
    match out {
        Ok(o) if o.status.success() => {
            info!(owner_did, repo_name, "Knot: DID symlink created/confirmed");
        }
        Ok(o) => {
            warn!(
                stderr = %String::from_utf8_lossy(&o.stderr),
                "Knot: DID symlink docker exec returned non-zero"
            );
        }
        Err(e) => {
            warn!(error = %e, "Knot: docker exec for DID symlink failed");
        }
    }

    // Step 3: seed spindle.db repos table.
    let sql = format!(
        "INSERT OR IGNORE INTO repos (knot, owner, name) VALUES ('{knot}', '{did}', '{name}');",
        knot = knot_hostname,
        did = owner_did,
        name = repo_name
    );
    let out = std::process::Command::new("sqlite3")
        .args([spindle_db, &sql])
        .output();
    match out {
        Ok(o) if o.status.success() => {
            info!(
                spindle_db,
                repo_name, "Spindle: repo entry seeded"
            );
        }
        Ok(o) => {
            warn!(
                stderr = %String::from_utf8_lossy(&o.stderr),
                "Spindle: sqlite3 returned non-zero"
            );
        }
        Err(e) => {
            warn!(error = %e, "Spindle: sqlite3 failed — is sqlite3 installed?");
        }
    }

    // Step 4: set git remote 'tangled' (idempotent).
    let ssh_url = format!(
        "git@local-tangled:repositories/owner/{}.git",
        repo_name
    );
    // Remove stale remote first (ignore errors), then add fresh.
    let _ = std::process::Command::new("git")
        .args(["remote", "remove", "tangled"])
        .current_dir(cwd)
        .output();
    let out = std::process::Command::new("git")
        .args(["remote", "add", "tangled", &ssh_url])
        .current_dir(cwd)
        .output();
    match out {
        Ok(o) if o.status.success() => {
            info!(url = %ssh_url, "Set git remote 'tangled'");
        }
        Ok(o) => {
            warn!(
                stderr = %String::from_utf8_lossy(&o.stderr),
                "Failed to set git remote 'tangled'"
            );
        }
        Err(e) => {
            warn!(error = %e, "git remote add tangled failed");
        }
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
        "shoal" => Ok(AgentType::Shoal),
        _ => anyhow::bail!("Unknown agent type: {}. Valid values: claude, gemini, opencode, shoal", s),
    }
}

fn agent_type_str(t: AgentType) -> &'static str {
    match t {
        AgentType::Claude => "claude",
        AgentType::Gemini => "gemini",
        AgentType::OpenCode => "opencode",
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

/// Builds the auto-injected spindle process companion when Tangled is configured.
/// Returns None if:
/// - `owner_did` is not set
/// - a companion named "spindle" is already declared
/// - the spindle binary does not exist at `spindle_path`
fn build_spindle_companion(
    owner_did: Option<&str>,
    knot_url: Option<&str>,
    spindle_db: Option<&str>,
    spindle_path: &std::path::Path,
    existing_companions: &[crate::config::CompanionConfig],
) -> Option<crate::config::CompanionConfig> {
    let owner_did = owner_did?;

    if existing_companions.iter().any(|c| c.name == "spindle") {
        return None;
    }

    if !spindle_path.exists() {
        warn!(
            "tangled_owner_did is set but .exo/bin/spindle not found — \
            spindle will not be auto-started. \
            Copy the binary to ~/.exo/bin/spindle and re-run exomonad init."
        );
        return None;
    }

    let db = spindle_db.unwrap_or("spindle.db");
    let jetstream = knot_url
        .map(|u| {
            let ws = u
                .replacen("http://", "ws://", 1)
                .replacen("https://", "wss://", 1);
            format!("{}/events", ws.trim_end_matches('/'))
        })
        .unwrap_or_else(|| "ws://localhost:5555/events".to_string());

    let cmd = format!(
        "SPINDLE_SERVER_HOSTNAME=localhost \
         SPINDLE_SERVER_LISTEN_ADDR=0.0.0.0:6555 \
         SPINDLE_SERVER_DB_PATH={db} \
         SPINDLE_SERVER_OWNER={owner_did} \
         SPINDLE_SERVER_DEV=true \
         SPINDLE_SERVER_LOG_DIR=/tmp/spindle-logs \
         SPINDLE_SERVER_JETSTREAM_ENDPOINT={jetstream} \
         {spindle}",
        spindle = spindle_path.display()
    );

    info!(
        owner_did = %owner_did,
        jetstream = %jetstream,
        spindle_db = %db,
        "Auto-injecting spindle process companion"
    );

    Some(crate::config::CompanionConfig {
        name: "spindle".to_string(),
        role: String::new(),
        agent_type: Some(AgentType::Process),
        command: cmd,
        task: None,
        model: None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::CompanionConfig;
    use exomonad_core::services::AgentType;

    fn spindle_companion(name: &str) -> CompanionConfig {
        CompanionConfig {
            name: name.to_string(),
            role: String::new(),
            agent_type: Some(AgentType::Process),
            command: "spindle".to_string(),
            task: None,
            model: None,
        }
    }

    #[test]
    fn no_owner_did_returns_none() {
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join("spindle");
        std::fs::write(&bin, "").unwrap();
        assert!(build_spindle_companion(None, None, None, &bin, &[]).is_none());
    }

    #[test]
    fn missing_binary_returns_none() {
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join("spindle"); // does not exist
        assert!(build_spindle_companion(
            Some("did:plc:test"),
            None,
            None,
            &bin,
            &[]
        )
        .is_none());
    }

    #[test]
    fn already_declared_spindle_returns_none() {
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join("spindle");
        std::fs::write(&bin, "").unwrap();
        let companions = vec![spindle_companion("spindle")];
        assert!(build_spindle_companion(
            Some("did:plc:test"),
            None,
            None,
            &bin,
            &companions
        )
        .is_none());
    }

    #[test]
    fn injects_spindle_companion_with_defaults() {
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join("spindle");
        std::fs::write(&bin, "").unwrap();

        let companion = build_spindle_companion(
            Some("did:plc:test"),
            None,
            None,
            &bin,
            &[],
        )
        .unwrap();

        assert_eq!(companion.name, "spindle");
        assert_eq!(companion.agent_type, Some(AgentType::Process));
        assert!(companion.command.contains("SPINDLE_SERVER_OWNER=did:plc:test"));
        assert!(companion.command.contains("SPINDLE_SERVER_DB_PATH=spindle.db"));
        assert!(companion
            .command
            .contains("SPINDLE_SERVER_JETSTREAM_ENDPOINT=ws://localhost:5555/events"));
    }

    #[test]
    fn knot_url_converted_to_ws_jetstream() {
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join("spindle");
        std::fs::write(&bin, "").unwrap();

        let companion = build_spindle_companion(
            Some("did:plc:test"),
            Some("http://localhost:5555"),
            None,
            &bin,
            &[],
        )
        .unwrap();

        assert!(companion
            .command
            .contains("SPINDLE_SERVER_JETSTREAM_ENDPOINT=ws://localhost:5555/events"));
    }

    #[test]
    fn https_knot_url_converted_to_wss() {
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join("spindle");
        std::fs::write(&bin, "").unwrap();

        let companion = build_spindle_companion(
            Some("did:plc:test"),
            Some("https://knot.example.com"),
            None,
            &bin,
            &[],
        )
        .unwrap();

        assert!(companion
            .command
            .contains("SPINDLE_SERVER_JETSTREAM_ENDPOINT=wss://knot.example.com/events"));
    }

    #[test]
    fn custom_spindle_db_path_used() {
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join("spindle");
        std::fs::write(&bin, "").unwrap();

        let companion = build_spindle_companion(
            Some("did:plc:test"),
            None,
            Some("/data/spindle.db"),
            &bin,
            &[],
        )
        .unwrap();

        assert!(companion.command.contains("SPINDLE_SERVER_DB_PATH=/data/spindle.db"));
    }

    #[test]
    fn non_spindle_companion_does_not_suppress_injection() {
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join("spindle");
        std::fs::write(&bin, "").unwrap();
        let companions = vec![spindle_companion("mock-github")];

        let companion = build_spindle_companion(
            Some("did:plc:test"),
            None,
            None,
            &bin,
            &companions,
        );

        assert!(companion.is_some());
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
}
