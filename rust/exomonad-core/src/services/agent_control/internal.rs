use super::*;

fn generate_opencode_agent_settings(
    agent_name: &str,
    role: &str,
    extra_mcp_servers: &HashMap<String, serde_json::Value>,
    role_context: &str,
    effort: Option<&str>,
) -> serde_json::Value {
    let mut mcp_servers = serde_json::Map::new();
    mcp_servers.insert(
        "exomonad".to_string(),
        serde_json::json!({
            "type": "local",
            "command": ["exomonad", "mcp-stdio", "--role", role, "--name", agent_name]
        }),
    );
    for (name, config) in extra_mcp_servers {
        mcp_servers.insert(name.clone(), config.clone());
    }

    let protocol = match role {
        "root" | "tl" => "",
        "worker" => super::spawn::OPENCODE_WORKER_INSTRUCTIONS,
        _ => super::spawn::OPENCODE_DEV_INSTRUCTIONS,
    };
    let instructions = serde_json::json!([format!("{protocol}\n\n{role_context}")]);

    let mut settings = serde_json::json!({
        "mcp": mcp_servers,
        "instructions": instructions,
        "plugin": ["./.exo/opencode-plugin"],
        "permission": "allow",
    });
    if let Some(effort) = effort.filter(|value| !value.is_empty()) {
        settings["agent"] = serde_json::json!({
            format!("exomonad-{role}"): {"reasoningEffort": effort}
        });
    }
    settings
}

async fn write_opencode_agent_plugin_files(dir: &Path) -> Result<()> {
    use crate::opencode_plugin::{OPENCODE_PLUGIN_PKG_JSON, OPENCODE_PLUGIN_TS};

    let plugin_dir = dir.join(".exo/opencode-plugin");
    fs::create_dir_all(&plugin_dir).await?;
    fs::write(plugin_dir.join("index.ts"), OPENCODE_PLUGIN_TS).await?;
    fs::write(plugin_dir.join("package.json"), OPENCODE_PLUGIN_PKG_JSON).await?;
    info!(path = %plugin_dir.display(), "Wrote OpenCode plugin files");
    Ok(())
}

impl<
        C: super::super::HasGitHubClient
            + super::super::HasTeamRegistry
            + super::super::HasAgentResolver
            + super::super::HasProjectDir
            + super::super::HasGitWorktreeService
            + 'static,
    > AgentControlService<C>
{
    pub(crate) fn runtime_role_context(&self, role: &crate::domain::Role) -> Result<String> {
        let context_path =
            resolve_role_context_path(self.project_dir(), self.wasm_name.as_str(), role.as_str())
                .ok_or_else(|| {
                anyhow!(
                    "Missing role context for {} at .exo/roles/{}/context/{}.md",
                    role,
                    self.wasm_name,
                    role
                )
            })?;
        let context = crate::services::agent_control::load_role_context(
            self.project_dir(),
            self.wasm_name.as_str(),
            role.as_str(),
        )
        .ok_or_else(|| {
            anyhow!(
                "Failed to read role context for {} from {}",
                role,
                context_path.display()
            )
        })?;
        Ok(context.replace("{{spawn_agent_type}}", self.spawn_agent_type.suffix()))
    }

    pub(crate) fn effort_for_role(&self, role: &str) -> Option<&str> {
        if role == "reviewer" {
            self.reviewer_effort()
        } else {
            self.spawn_agent_effort()
        }
    }

    pub(crate) fn resolve_tmux_session(&self) -> Result<String> {
        self.tmux_session
            .clone()
            .ok_or_else(|| anyhow!("No tmux session configured (call with_tmux_session)"))
    }

    /// Get the direct tmux IPC client, falling back to creating one from config or env.
    pub(crate) fn tmux(&self) -> Result<super::tmux_ipc::TmuxIpc> {
        if let Some(ref ipc) = self.tmux_ipc {
            return Ok(ipc.clone());
        }
        let session = self.resolve_tmux_session()?;
        Ok(super::tmux_ipc::TmuxIpc::new(&session))
    }

    /// Clean up an existing worktree (if present) and create a fresh one.
    ///
    /// Consolidates the idempotent cleanup + spawn_blocking + catch_unwind boilerplate
    /// shared across spawn_agent, spawn_subtree, and spawn_leaf_subtree.
    pub(crate) async fn create_worktree_checked(
        &self,
        worktree_path: &Path,
        branch_name: &BranchName,
        base_branch: &BranchName,
    ) -> Result<()> {
        self.create_worktree_checked_at(
            worktree_path,
            branch_name,
            base_branch.as_str(),
            false,
            false,
        )
        .await
    }

    /// Create a worktree from an exact revision while retaining the supplied branch name.
    pub(crate) async fn create_worktree_from_revision_checked(
        &self,
        worktree_path: &Path,
        branch_name: &BranchName,
        revision: &str,
    ) -> Result<()> {
        if revision.trim().is_empty() {
            return Err(anyhow!("worktree start revision is empty"));
        }
        self.create_worktree_checked_at(worktree_path, branch_name, revision, true, false)
            .await
    }

    /// Reattach a worktree to an already-created branch during a retry.
    pub(crate) async fn create_worktree_from_existing_branch_checked(
        &self,
        worktree_path: &Path,
        branch_name: &BranchName,
    ) -> Result<()> {
        self.create_worktree_checked_at(
            worktree_path,
            branch_name,
            branch_name.as_str(),
            false,
            true,
        )
        .await
    }

    async fn create_worktree_checked_at(
        &self,
        worktree_path: &Path,
        branch_name: &BranchName,
        base_ref: &str,
        from_revision: bool,
        existing_branch: bool,
    ) -> Result<()> {
        if worktree_path.exists() {
            info!(path = %worktree_path.display(), "Removing existing workspace for idempotency");
            let git_wt = self.git_wt().clone();
            let path = worktree_path.to_path_buf();
            match tokio::task::spawn_blocking(move || git_wt.remove_workspace(&path)).await {
                Err(join_err) => {
                    warn!(error = %join_err, "Join error while removing existing workspace (non-fatal)");
                }
                Ok(Err(e)) => {
                    warn!(error = %e, "Failed to remove existing workspace (non-fatal)");
                }
                Ok(Ok(_)) => {}
            }
        }

        info!(
            base_ref,
            branch_name = %branch_name,
            worktree_path = %worktree_path.display(),
            "Creating git worktree"
        );

        let git_wt = self.git_wt().clone();
        let path = worktree_path.to_path_buf();
        let bookmark = branch_name.clone();
        let base_ref = base_ref.to_string();
        let result = tokio::task::spawn_blocking(move || {
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                if existing_branch {
                    git_wt.create_workspace_from_existing_branch(&path, &bookmark)
                } else if from_revision {
                    git_wt.create_workspace_from_revision(&path, &bookmark, &base_ref)
                } else {
                    let base = BranchName::try_from_str(&base_ref)
                        .expect("validated branch name is non-empty");
                    git_wt.create_workspace(&path, &bookmark, &base)
                }
            }))
        })
        .await
        .context("tokio task join error while creating git worktree")?;

        match result {
            Ok(Ok(())) => {}
            Ok(Err(e)) => {
                return Err(anyhow::Error::from(EffectError::from(e)))
                    .context("Failed to create git worktree");
            }
            Err(panic_val) => {
                let msg = panic_val
                    .downcast_ref::<String>()
                    .map(|s| s.as_str())
                    .or_else(|| panic_val.downcast_ref::<&str>().copied())
                    .unwrap_or("unknown panic");
                return Err(anyhow!("git worktree creation panicked: {}", msg));
            }
        }

        // Verify the worktree is on the expected branch
        let git_wt = self.git_wt().clone();
        let verify_path = worktree_path.to_path_buf();
        let expected = branch_name.to_string();
        let actual =
            tokio::task::spawn_blocking(move || git_wt.get_workspace_bookmark(&verify_path))
                .await
                .context("spawn_blocking failed during branch verification")?
                .map_err(|e| anyhow!("Failed to verify worktree branch: {}", e))?;

        if actual.as_deref() != Some(&expected) {
            return Err(anyhow!(
                "Worktree branch mismatch: expected '{}', got {:?}",
                expected,
                actual
            ));
        }

        Ok(())
    }

    /// Build the common env vars shared by all spawn functions.
    ///
    /// `session_id` is the agent's birth-branch: for worktree agents this is the child's
    /// branch name; for inline workers it's the parent's birth-branch (they share context).
    pub(crate) fn common_spawn_env(
        &self,
        agent_name: &AgentName,
        session_id: &BranchName,
        role: &crate::domain::Role,
    ) -> HashMap<String, String> {
        let mut env_vars = HashMap::new();
        env_vars.insert("EXOMONAD_AGENT_ID".to_string(), agent_name.to_string());
        env_vars.insert("EXOMONAD_SESSION_ID".to_string(), session_id.to_string());
        env_vars.insert("EXOMONAD_ROLE".to_string(), role.as_str().to_string());
        env_vars.insert(
            "EXOMONAD_SPAWN_AGENT_TYPE".to_string(),
            self.spawn_agent_type.suffix().to_string(),
        );
        if let Some(ref session) = self.tmux_session {
            env_vars.insert("EXOMONAD_TMUX_SESSION".to_string(), session.clone());
        }
        if let Ok(value) = std::env::var("EXOMONAD_MAILBOX_PROTOCOL_AVAILABLE") {
            if !value.is_empty() {
                env_vars.insert("EXOMONAD_MAILBOX_PROTOCOL_AVAILABLE".to_string(), value);
            }
        }

        // CHAINLINK_DB anchors every spawned agent to the project-root chainlink DB so a
        // dev-leaf inside its git worktree (which contains no `.chainlink/`) still resolves
        // issues against the canonical tracker. The directory form is canonical (chainlink
        // CLI accepts either, but the directory is the resource and `issues.db` is an
        // artifact inside it). init.rs uses the same directory form when seeding the tmux
        // session env as a fallback for processes that bypass this HashMap.
        //
        // Propagation contract: this HashMap flows into `build_agent_command` which renders
        // entries as `KEY=value` shell-prefix tokens (see line ~343) ahead of the agent
        // command. tmux executes that string, the agent process inherits the env, and every
        // subsequent subprocess — MCP servers, hook scripts, direct `chainlink` calls —
        // inherits it transitively. The same path covers windows (TLs, dev-leaves, sub-TLs)
        // and panes (workers), so adding an entry here reaches all five agent runtimes
        // (Claude, Codex, OpenCode, worker) uniformly.
        env_vars.insert(
            "CHAINLINK_DB".to_string(),
            self.ctx
                .project_dir()
                .join(".chainlink")
                .display()
                .to_string(),
        );

        // Propagate CODEX_HOME to every spawned codex pane. install_codex_hook_trust
        // seeds [hooks.state] entries in `$CODEX_HOME/config.toml`; without this
        // pass-through, spawned codex agents fall back to ~/.codex and see the hooks
        // as untrusted, firing "3 hooks need review" (chainlink #259). The init.rs
        // tmux set-environment for CODEX_HOME covers the root TL pane but does not
        // reliably reach panes created later by spawn_leaf — this shell-prefix entry
        // closes that gap explicitly on the production code path. Harmless for non-
        // codex agents (they ignore an unfamiliar env var).
        if let Ok(codex_home) = std::env::var("CODEX_HOME") {
            if !codex_home.is_empty() {
                env_vars.insert("CODEX_HOME".to_string(), codex_home);
            }
        }

        for var in [
            "FORGEJO_HOST",
            "GH_HOST",
            "FORGEJO_TOKEN",
            "GH_TOKEN",
            "FORGEJO_REVIEWER_TOKEN",
            "FORGEJO_URL",
            "FORGEJO_OWNER",
            "FORGEJO_REPO",
            "REPO",
        ] {
            if let Ok(value) = std::env::var(var) {
                if !value.is_empty() {
                    env_vars.insert(var.to_string(), value);
                }
            }
        }

        if let Some(config_env) = &self.forgejo_spawn_env {
            config_env.apply_to(&mut env_vars);
        }

        if role.as_str() == "reviewer" {
            if let Some(reviewer_token) = env_vars.get("FORGEJO_REVIEWER_TOKEN").cloned() {
                env_vars.insert("FORGEJO_TOKEN".to_string(), reviewer_token.clone());
                env_vars.insert("GH_TOKEN".to_string(), reviewer_token);
            }
        }

        // Propagate swarm run_id and parent agent identity for OTel resource attributes
        if let Ok(v) = std::env::var("EXOMONAD_SWARM_RUN_ID") {
            env_vars.insert("EXOMONAD_SWARM_RUN_ID".to_string(), v);
        }
        env_vars.insert(
            "EXOMONAD_PARENT_AGENT".to_string(),
            self.effective_birth_branch(None).to_string(),
        );

        // Propagate W3C traceparent for cross-agent trace correlation
        {
            use tracing_opentelemetry::OpenTelemetrySpanExt;
            let cx = tracing::Span::current().context();
            let mut injector = std::collections::HashMap::new();
            opentelemetry::global::get_text_map_propagator(|propagator| {
                propagator.inject_context(&cx, &mut injector);
            });
            if let Some(traceparent) = injector.get("traceparent") {
                env_vars.insert("TRACEPARENT".to_string(), traceparent.clone());
            }
        }

        // Route Claude CLI calls through OpenRouter when configured.
        // ANTHROPIC_AUTH_TOKEN + empty ANTHROPIC_API_KEY tells Claude Code to use the token
        // against ANTHROPIC_BASE_URL (OpenRouter's Anthropic-compatible endpoint).
        if let Some(ref api_key) = self.openrouter_api_key {
            env_vars.insert(
                "ANTHROPIC_BASE_URL".to_string(),
                "https://openrouter.ai/api".to_string(),
            );
            env_vars.insert("ANTHROPIC_AUTH_TOKEN".to_string(), api_key.clone());
            env_vars.insert("ANTHROPIC_API_KEY".to_string(), String::new());
            // OpenCode uses OPENROUTER_API_KEY directly
            env_vars.insert("OPENROUTER_API_KEY".to_string(), api_key.clone());
        }

        env_vars
    }

    /// Emit an agent:started event if tmux_session is configured.
    pub(crate) fn emit_agent_started(&self, agent_name: &AgentName) -> Result<()> {
        if let Some(ref session) = self.tmux_session {
            let agent_id = crate::ui_protocol::AgentId::try_from(agent_name.to_string())
                .map_err(|e| anyhow!("Invalid agent_id: {}", e))?;
            let event = crate::ui_protocol::AgentEvent::AgentStarted {
                agent_id,
                timestamp: tmux_events::now_iso8601(),
            };
            if let Err(e) = tmux_events::emit_event(session, &event) {
                warn!("Failed to emit agent:started event: {}", e);
            }
        }
        Ok(())
    }

    /// Copy a role context file with template interpolation.
    ///
    /// Replaces `{{spawn_agent_type}}` with the configured spawn agent type suffix.
    /// Falls back to raw copy if the source is not valid UTF-8.
    pub(crate) async fn copy_role_context_with_interpolation(
        src: &std::path::Path,
        dest: &std::path::Path,
        spawn_type: &str,
    ) -> std::io::Result<()> {
        match tokio::fs::read_to_string(src).await {
            Ok(content) => {
                let interpolated = content.replace("{{spawn_agent_type}}", spawn_type);
                tokio::fs::write(dest, interpolated).await
            }
            Err(_) => {
                tokio::fs::copy(src, dest).await?;
                Ok(())
            }
        }
    }

    pub(crate) async fn new_tmux_window(
        &self,
        name: &str,
        cwd: &Path,
        agent_type: AgentType,
        prompt: Option<&str>,
        env_vars: HashMap<String, String>,
    ) -> Result<super::tmux_ipc::WindowId> {
        self.new_tmux_window_inner(
            name, cwd, agent_type, prompt, env_vars, None, None, None, None, None,
        )
        .await
    }

    /// Build the full shell command string for an agent.
    /// Handles: agent CLI + prompt/flags → env var prefix → nix develop wrapping.
    /// Used by both `new_tmux_window_inner` and `new_tmux_pane`.
    ///
    /// `prompt_file` is an absolute path to a file containing the prompt text.
    /// The prompt is read at runtime via `$(cat ...)` to avoid shell quoting issues
    /// with arbitrary prompt content (apostrophes, backticks, $(), etc.).
    #[allow(dead_code, clippy::too_many_arguments)]
    pub(crate) fn build_agent_command(
        agent_type: AgentType,
        prompt_file: Option<&Path>,
        fork_session_id: Option<&str>,
        env_vars: &HashMap<String, String>,
        cwd: &Path,
        claude_flags: Option<&ClaudeSpawnFlags>,
        yolo: bool,
        model: Option<&str>,
    ) -> String {
        Self::build_agent_command_with_effort(
            agent_type,
            prompt_file,
            fork_session_id,
            env_vars,
            cwd,
            claude_flags,
            yolo,
            model,
            None,
            super::InvocationMode::Interactive,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn build_agent_command_with_effort(
        agent_type: AgentType,
        prompt_file: Option<&Path>,
        fork_session_id: Option<&str>,
        env_vars: &HashMap<String, String>,
        cwd: &Path,
        claude_flags: Option<&ClaudeSpawnFlags>,
        _yolo: bool,
        model: Option<&str>,
        effort: Option<&str>,
        mode: super::InvocationMode,
    ) -> String {
        let cmd = agent_type.command();

        // Build permission flags for Claude agents
        let perms_flags = match agent_type {
            AgentType::Claude => {
                let mut flags = String::new();
                let mode = claude_flags.and_then(|f| f.permission_mode.as_ref());
                match mode {
                    Some(m) => {
                        flags.push_str(" --permission-mode ");
                        flags.push_str(m.as_str());
                    }
                    None => flags.push_str(" --dangerously-skip-permissions"),
                }
                if let Some(f) = claude_flags {
                    for tool in &f.allowed_tools {
                        flags.push_str(" --allowedTools ");
                        flags.push_str(&shell_escape::escape(tool.into()));
                    }
                    for tool in &f.disallowed_tools {
                        flags.push_str(" --disallowedTools ");
                        flags.push_str(&shell_escape::escape(tool.into()));
                    }
                }
                flags
            }
            AgentType::Codex => String::new(),
            AgentType::OpenCode => String::new(),
            AgentType::Shoal | AgentType::Process => String::new(),
        };

        let model_flag = model
            .map(|m| format!(" --model {}", shell_escape::escape(m.into())))
            .unwrap_or_default();
        let effort_flag = if agent_type == AgentType::Claude {
            effort
                .map(|level| format!(" --effort {}", shell_escape::escape(level.into())))
                .unwrap_or_default()
        } else {
            String::new()
        };
        let variant_flag = effort
            .map(|level| format!(" --variant {}", shell_escape::escape(level.into())))
            .unwrap_or_default();
        let one_shot_flag = if mode == super::InvocationMode::OneShot {
            agent_type.prompt_flag()
        } else {
            ""
        };
        let one_shot_flag = if one_shot_flag.is_empty() {
            String::new()
        } else {
            format!(" {}", one_shot_flag)
        };

        let agent_command = match (prompt_file, fork_session_id) {
            (Some(pf), Some(session_id)) => {
                let escaped_session = Self::escape_for_shell_command(session_id);
                let escaped_path = Self::escape_for_shell_command(&pf.display().to_string());
                match agent_type {
                    AgentType::Codex => Self::build_codex_command_for_agent(
                        agent_type.command(),
                        cwd,
                        Some(pf),
                        model,
                        effort,
                        Some(session_id),
                    ),
                    AgentType::OpenCode => {
                        format!(
                            "{} run --interactive{} --session {} --fork \"$(cat {})\"{}{}",
                            cmd,
                            perms_flags,
                            escaped_session,
                            escaped_path,
                            model_flag,
                            variant_flag
                        )
                    }
                    _ => {
                        format!(
                            "{}{}{}{}{} --resume {} --fork-session \"$(cat {})\"",
                            cmd,
                            one_shot_flag,
                            perms_flags,
                            model_flag,
                            effort_flag,
                            escaped_session,
                            escaped_path
                        )
                    }
                }
            }
            (Some(pf), None) => {
                let escaped_path = Self::escape_for_shell_command(&pf.display().to_string());
                match agent_type {
                    AgentType::Codex => Self::build_codex_command_for_agent(
                        agent_type.command(),
                        cwd,
                        Some(pf),
                        model,
                        effort,
                        None,
                    ),
                    AgentType::OpenCode => {
                        format!(
                            "{} run --interactive{} \"$(cat {})\"{}{}",
                            cmd, perms_flags, escaped_path, model_flag, variant_flag
                        )
                    }
                    _ => format!(
                        "{}{}{}{}{} \"$(cat {})\"",
                        cmd, one_shot_flag, perms_flags, model_flag, effort_flag, escaped_path
                    ),
                }
            }
            _ => match agent_type {
                AgentType::Codex => Self::build_codex_command_for_agent(
                    agent_type.command(),
                    cwd,
                    None,
                    model,
                    effort,
                    fork_session_id,
                ),
                _ => format!("{}{}{}{}", cmd, perms_flags, model_flag, effort_flag),
            },
        };

        // Shell-prefix every entry in env_vars (`KEY=value KEY2=value2 ...`) ahead of the
        // agent command. This is the single propagation point that delivers everything
        // common_spawn_env adds — EXOMONAD_*, CHAINLINK_DB, TRACEPARENT, model API keys — to
        // the spawned agent process and every subprocess it forks. Both new_tmux_window_inner
        // and new_tmux_pane (worker panes) call build_agent_command, so this covers all five
        // agent runtimes uniformly. If a future agent type needs out-of-band propagation
        // (e.g., a per-process config file), wire it next to this prefix, not instead of it.
        let env_prefix = env_vars
            .iter()
            .map(|(k, v)| format!("{}={}", k, shell_escape::escape(v.into())))
            .collect::<Vec<_>>()
            .join(" ");
        let full_command = if env_prefix.is_empty() {
            agent_command
        } else {
            format!("{} {}", env_prefix, agent_command)
        };

        let full_command =
            if let Some(exit_status_file) = env_vars.get("EXOMONAD_INVOCATION_EXIT_FILE") {
                let escaped_path = Self::escape_for_shell_command(exit_status_file);
                format!(
                    "{}; status=$?; printf '%s\\n' \"$status\" > {}; exit \"$status\"",
                    full_command, escaped_path
                )
            } else {
                full_command
            };

        // Wrap in nix develop shell if flake.nix exists in cwd
        if cwd.join("flake.nix").exists() {
            info!("Wrapping agent command in nix develop shell");
            let escaped = full_command.replace('\'', "'\\''");
            format!("nix develop -c sh -c '{}'", escaped)
        } else {
            full_command
        }
    }

    #[allow(dead_code)]
    pub(crate) fn build_codex_command(
        worktree_dir: &Path,
        prompt_file: Option<&Path>,
        model: Option<&str>,
        fork_session_id: Option<&str>,
    ) -> String {
        Self::build_codex_command_with_effort(
            worktree_dir,
            prompt_file,
            model,
            None,
            fork_session_id,
        )
    }

    pub(crate) fn build_codex_command_with_effort(
        worktree_dir: &Path,
        prompt_file: Option<&Path>,
        model: Option<&str>,
        effort: Option<&str>,
        fork_session_id: Option<&str>,
    ) -> String {
        Self::build_codex_command_for_agent(
            "codex",
            worktree_dir,
            prompt_file,
            model,
            effort,
            fork_session_id,
        )
    }

    fn build_codex_command_for_agent(
        command: &str,
        worktree_dir: &Path,
        prompt_file: Option<&Path>,
        model: Option<&str>,
        effort: Option<&str>,
        fork_session_id: Option<&str>,
    ) -> String {
        let escaped_dir = Self::escape_for_shell_command(&worktree_dir.display().to_string());
        let model_flag = model
            .map(|model| format!(" --model {}", shell_escape::escape(model.into())))
            .unwrap_or_default();
        let effort_flag = effort
            .map(|level| format!(" -c model_reasoning_effort=\"{}\"", level))
            .unwrap_or_default();

        match fork_session_id {
            Some(session_id) => format!(
                "{} fork {} --dangerously-bypass-approvals-and-sandbox --cd {}{}{}",
                command,
                Self::escape_for_shell_command(session_id),
                escaped_dir,
                model_flag,
                effort_flag
            ),
            None => {
                let prompt = prompt_file
                    .map(|path| {
                        format!(
                            " \"$(cat {})\"",
                            Self::escape_for_shell_command(&path.display().to_string())
                        )
                    })
                    .unwrap_or_default();
                format!(
                    "{} --dangerously-bypass-approvals-and-sandbox --cd {}{}{}{}",
                    command, escaped_dir, model_flag, effort_flag, prompt
                )
            }
        }
    }

    /// Write a prompt to a temp file and return the absolute path.
    /// Files are written to `.exo/tmp/` in the project directory.
    /// Uses UUID filenames to avoid races when multiple agents spawn concurrently.
    pub(crate) async fn write_prompt_file(
        project_dir: &Path,
        agent_name: &str,
        prompt: &str,
    ) -> Result<PathBuf> {
        let tmp_dir = project_dir.join(".exo/tmp");
        tokio::fs::create_dir_all(&tmp_dir)
            .await
            .context("Failed to create .exo/tmp/")?;
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let path = tmp_dir.join(format!("prompt-{}-{}.txt", ts, std::process::id()));
        tokio::fs::write(&path, prompt)
            .await
            .context("Failed to write prompt file")?;
        info!(path = %path.display(), agent = %agent_name, "Wrote prompt to temp file");
        Ok(path)
    }

    fn default_model_for_spawn(&self, agent_type: AgentType, role: Option<&str>) -> Option<&str> {
        if role == Some("reviewer") {
            None
        } else if matches!(agent_type, AgentType::OpenCode | AgentType::Codex) {
            self.spawn_agent_model()
        } else {
            None
        }
    }

    fn default_effort_for_spawn(&self, role: Option<&str>) -> Option<&str> {
        self.effort_for_role(role.unwrap_or("worker"))
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn new_tmux_window_inner(
        &self,
        name: &str,
        cwd: &Path,
        agent_type: AgentType,
        prompt: Option<&str>,
        env_vars: HashMap<String, String>,
        fork_session_id: Option<&str>,
        claude_flags: Option<&ClaudeSpawnFlags>,
        role: Option<&str>,
        model_override: Option<&str>,
        effort_override: Option<&str>,
    ) -> Result<super::tmux_ipc::WindowId> {
        info!(name, cwd = %cwd.display(), agent_type = ?agent_type, fork = fork_session_id.is_some(), "Creating tmux window");

        // Write prompt to file to avoid shell quoting issues
        let prompt_file = match prompt {
            Some(p) => Some(Self::write_prompt_file(self.project_dir(), name, p).await?),
            None => None,
        };

        let full_command = self.build_launch_command(
            agent_type,
            prompt_file.as_deref(),
            fork_session_id,
            &env_vars,
            cwd,
            claude_flags,
            role,
            model_override,
            effort_override,
        );
        self.new_tmux_window_with_command(name, cwd, &full_command)
            .await
    }

    /// Open a tmux window that runs the already-built launch command.
    pub(crate) async fn new_tmux_window_with_command(
        &self,
        name: &str,
        cwd: &Path,
        full_command: &str,
    ) -> Result<super::tmux_ipc::WindowId> {
        let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".to_string());
        let tmux = self.tmux()?;
        let window_name = name.to_string();
        let window_cwd = cwd.to_path_buf();
        tmux.new_window(&window_name, &window_cwd, &shell, full_command)
            .await
            .context("Failed to create tmux window")
    }

    /// Resolve a leaf's model and effort from its spawn options and build the
    /// launch command it will run. Kept separate so the launch-command boundary
    /// can be tested starting from `SpawnLeafOptions.model`.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn leaf_launch_command(
        &self,
        agent_type: AgentType,
        role: &str,
        options: &SpawnLeafOptions,
        prompt_file: Option<&Path>,
        env_vars: &HashMap<String, String>,
        cwd: &Path,
    ) -> String {
        let model = self.effective_model_for(agent_type, role, options.model.as_deref());
        let effort = self.effective_effort_for(role, None);
        self.build_launch_command(
            agent_type,
            prompt_file,
            None,
            env_vars,
            cwd,
            Some(&options.claude_flags),
            Some(role),
            model.as_deref(),
            effort.as_deref(),
        )
    }

    /// Build the shell command a spawned window will run, resolving the model
    /// and effort from the per-spawn override or the static per-role default.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn build_launch_command(
        &self,
        agent_type: AgentType,
        prompt_file: Option<&Path>,
        fork_session_id: Option<&str>,
        env_vars: &HashMap<String, String>,
        cwd: &Path,
        claude_flags: Option<&ClaudeSpawnFlags>,
        role: Option<&str>,
        model_override: Option<&str>,
        effort_override: Option<&str>,
    ) -> String {
        let model = model_override.or_else(|| self.default_model_for_spawn(agent_type, role));
        Self::build_agent_command_with_effort(
            agent_type,
            prompt_file,
            fork_session_id,
            env_vars,
            cwd,
            claude_flags,
            self.yolo,
            model,
            effort_override.or_else(|| self.default_effort_for_spawn(role)),
            super::InvocationMode::from_role(role),
        )
    }

    pub(crate) async fn get_tmux_windows(&self) -> Result<Vec<String>> {
        debug!("Querying tmux window names via direct IPC");
        let tmux = match self.tmux() {
            Ok(t) => t,
            Err(e) => {
                warn!("Failed to get tmux IPC client for list-windows: {}", e);
                return Ok(Vec::new());
            }
        };

        let result = timeout(TMUX_TIMEOUT, tmux.list_windows())
            .await
            .map_err(|_| {
                anyhow!(
                    "tmux list-windows timed out after {}s",
                    TMUX_TIMEOUT.as_secs()
                )
            })?;

        match result {
            Ok(windows) => Ok(windows.into_iter().map(|w| w.window_name).collect()),
            Err(e) => {
                warn!("tmux list-windows IPC failed, assuming no windows: {}", e);
                Ok(Vec::new())
            }
        }
    }

    /// Check if a tmux window with the given display name exists.
    pub(crate) async fn is_tmux_window_alive(&self, display_name: &str) -> bool {
        self.get_tmux_windows()
            .await
            .unwrap_or_default()
            .iter()
            .any(|window| window == display_name)
    }

    pub(crate) async fn close_tmux_window(&self, name: &str) -> Result<()> {
        info!(name, "Closing tmux window");

        let tmux = self.tmux()?;
        let window_name = name.to_string();

        let window_id = {
            let windows = tmux.list_windows().await?;
            windows
                .into_iter()
                .find(|w| w.window_name == window_name)
                .map(|w| w.window_id)
                .ok_or_else(|| anyhow!("Window not found: {}", window_name))?
        };

        let tmux = self.tmux()?;
        timeout(TMUX_TIMEOUT, tmux.kill_window(&window_id))
            .await
            .map_err(|_| {
                anyhow::Error::new(TimeoutError {
                    message: format!(
                        "tmux kill-window timed out after {}s",
                        TMUX_TIMEOUT.as_secs()
                    ),
                })
            })??;

        info!(name, "tmux kill-window successful");
        Ok(())
    }

    pub(crate) async fn verify_tmux_window_startup(
        &self,
        window_id: &super::tmux_ipc::WindowId,
    ) -> Result<()> {
        let tmux = self.tmux()?;
        let ready = timeout(
            super::INVOCATION_STARTUP_TIMEOUT,
            tmux.wait_for_window(window_id, super::INVOCATION_STARTUP_TIMEOUT),
        )
        .await
        .map_err(|_| {
            anyhow::Error::new(TimeoutError {
                message: format!(
                    "tmux startup readiness timed out after {}s",
                    super::INVOCATION_STARTUP_TIMEOUT.as_secs()
                ),
            })
        })??;
        if matches!(
            super::tmux_ipc::classify_window_startup(ready),
            super::tmux_ipc::WindowStartupStatus::ExitedBeforeReady
        ) {
            anyhow::bail!(
                "tmux window {} exited before startup readiness was confirmed",
                window_id
            );
        }
        Ok(())
    }

    pub(crate) async fn kill_tmux_window_id(
        &self,
        window_id: &super::tmux_ipc::WindowId,
    ) -> Result<()> {
        let tmux = self.tmux()?;
        timeout(TMUX_TIMEOUT, tmux.kill_window(window_id))
            .await
            .map_err(|_| {
                anyhow::Error::new(TimeoutError {
                    message: format!(
                        "tmux kill-window timed out after {}s",
                        TMUX_TIMEOUT.as_secs()
                    ),
                })
            })??;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn new_tmux_pane(
        &self,
        name: &str,
        cwd: &Path,
        agent_type: AgentType,
        prompt: Option<&str>,
        env_vars: HashMap<String, String>,
        parent_window_name: Option<&str>,
        claude_flags: Option<&ClaudeSpawnFlags>,
        model_override: Option<&str>,
    ) -> Result<super::tmux_ipc::PaneId> {
        info!(name, cwd = %cwd.display(), agent_type = ?agent_type, parent = ?parent_window_name, "Creating tmux pane");

        // Write prompt to file to avoid shell quoting issues
        let prompt_file = match prompt {
            Some(p) => Some(Self::write_prompt_file(self.project_dir(), name, p).await?),
            None => None,
        };

        let model = model_override.or_else(|| match agent_type {
            AgentType::OpenCode | AgentType::Codex => self.spawn_agent_model(),
            _ => None,
        });
        let full_command = Self::build_agent_command_with_effort(
            agent_type,
            prompt_file.as_deref(),
            None,
            &env_vars,
            cwd,
            claude_flags,
            self.yolo,
            model,
            self.effort_for_role("worker"),
            super::InvocationMode::OneShot,
        );
        let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".to_string());
        let tmux = self.tmux()?;

        // Find parent window ID by name
        let target_window = if let Some(wname) = parent_window_name {
            let wname = wname.to_string();
            let windows = tmux
                .list_windows()
                .await
                .context("Failed to list tmux windows")?;
            windows
                .iter()
                .find(|w| w.window_name == wname)
                .map(|w| w.window_id.clone())
                .ok_or_else(|| {
                    anyhow::anyhow!(
                        "No tmux window found matching '{}' — cannot create pane",
                        wname
                    )
                })?
        } else {
            // Default to first window if no name provided
            let windows = tmux
                .list_windows()
                .await
                .context("Failed to list tmux windows")?;
            windows
                .first()
                .map(|w| w.window_id.clone())
                .ok_or_else(|| {
                    anyhow!(
                        "No windows found in session {} — cannot create pane",
                        tmux.session_name()
                    )
                })?
        };

        let pane_cwd = cwd.to_path_buf();
        let pane_id = tmux
            .split_window(&target_window, &pane_cwd, &shell, &full_command)
            .await
            .context("Failed to create tmux pane")?;

        // Rebalance panes into a grid after each split to prevent
        // exponential height decay (60 → 29 → 14 → 6 → 2 → 1 lines).
        if let Err(e) = tmux
            .select_layout(&target_window, crate::domain::TmuxLayout::Tiled)
            .await
        {
            tracing::warn!(error = %e, "Failed to apply tiled layout (non-fatal)");
        }

        info!(name, pane_id = %pane_id, "Successfully created tmux pane");
        Ok(pane_id)
    }

    /// Write MCP config for the agent directory.
    ///
    /// Claude agents get `.mcp.json`.
    /// Codex agents get `.codex/config.toml`; shared hooks live in Codex user config.
    /// Uses stdio transport via `exomonad mcp-stdio`.
    pub(crate) async fn write_agent_mcp_config(
        &self,
        _effective_dir: &Path,
        agent_dir: &Path,
        agent_type: AgentType,
        role: &crate::domain::Role,
    ) -> Result<()> {
        let agent_name = agent_dir
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("unknown");

        let mcp_content = Self::generate_mcp_config(
            agent_name,
            agent_type,
            role.as_str(),
            &self.wasm_name,
            &self.extra_mcp_servers,
        );

        match agent_type {
            AgentType::Claude => {
                fs::write(agent_dir.join(".mcp.json"), mcp_content).await?;
                info!(agent_dir = %agent_dir.display(), role = %role.as_str(), "Wrote .mcp.json for Claude agent");
            }
            AgentType::Process => {} // No MCP config for process companions
            AgentType::Shoal => {
                let exo_dir = agent_dir.join(".exo");
                fs::create_dir_all(&exo_dir).await?;
                fs::write(exo_dir.join("mcp.json"), mcp_content).await?;
                info!(agent_dir = %agent_dir.display(), role = %role.as_str(), "Wrote .exo/mcp.json for Shoal agent");
            }
            AgentType::OpenCode => {
                let role_context = self.runtime_role_context(role)?;
                let opencode_config = generate_opencode_agent_settings(
                    agent_name,
                    role.as_str(),
                    &self.extra_mcp_servers,
                    &role_context,
                    self.effort_for_role(role.as_str()),
                );
                fs::write(
                    agent_dir.join("opencode.json"),
                    serde_json::to_string_pretty(&opencode_config)?,
                )
                .await?;
                write_opencode_agent_plugin_files(agent_dir).await?;
                info!(agent_dir = %agent_dir.display(), role = %role.as_str(), "Wrote opencode.json and plugin for OpenCode agent");
            }
            AgentType::Codex => {
                let model = if role.as_str() == "reviewer" {
                    self.reviewer_model.as_deref()
                } else {
                    self.spawn_agent_model()
                };
                self.write_codex_config_files(
                    agent_dir,
                    role,
                    &AgentName::try_from_str(agent_name)
                        .expect("validated string input is non-empty"),
                    model,
                    &self.extra_mcp_servers,
                )
                .await?;
            }
        }
        Ok(())
    }

    pub(crate) async fn write_codex_config_files(
        &self,
        dir: &Path,
        role: &crate::domain::Role,
        agent_name: &AgentName,
        model: Option<&str>,
        extra_mcp_servers: &HashMap<String, serde_json::Value>,
    ) -> Result<()> {
        let codex_dir = dir.join(".codex");
        fs::create_dir_all(&codex_dir).await?;

        let role_context = self.runtime_role_context(role)?;
        let instructions = match role.as_str() {
            "tl" | "root" => {
                format!("{role_context}\n\n{}", super::spawn::CODEX_TL_RUNTIME_NOTES)
            }
            "worker" => format!(
                "{}\n\n{role_context}",
                super::spawn::CODEX_WORKER_INSTRUCTIONS
            ),
            "reviewer" => {
                format!(
                    "{}\n\n{role_context}",
                    super::spawn::CODEX_REVIEWER_INSTRUCTIONS
                )
            }
            _ => format!("{}\n\n{role_context}", super::spawn::CODEX_DEV_INSTRUCTIONS),
        };
        let configured_effort = self.effort_for_role(role.as_str());
        let config = crate::codex_config::render_codex_config_with_effort(
            agent_name.as_str(),
            role.as_str(),
            &instructions,
            model,
            configured_effort,
            extra_mcp_servers,
            &crate::util::find_exomonad_binary(),
        );
        let codex_config_path = codex_dir.join("config.toml");
        fs::write(&codex_config_path, config).await?;

        if let Some(config_path) = crate::codex_config::codex_user_config_path() {
            crate::codex_config::trust_codex_project(&config_path, dir).with_context(|| {
                format!("Failed to trust Codex project in {}", config_path.display())
            })?;
            crate::codex_config::install_codex_hook_trust(&config_path, &codex_config_path)
                .with_context(|| {
                    format!("Failed to trust Codex hooks in {}", config_path.display())
                })?;
            info!(path = %config_path.display(), "Marked Codex agent worktree as trusted");
        } else {
            warn!("Could not determine Codex home; worktree may not be trusted automatically");
        }
        let legacy_hooks_path = codex_dir.join("hooks.json");
        if legacy_hooks_path.exists() {
            fs::remove_file(&legacy_hooks_path).await?;
        }
        info!(agent_dir = %dir.display(), role = %role.as_str(), "Wrote .codex/config.toml for Codex agent");
        Ok(())
    }

    /// Symlink server socket into worktree so agents find it without walk-up.
    pub(crate) async fn create_socket_symlink(&self, worktree_path: &Path) {
        let source = self.server_socket_source();
        let target_dir = worktree_path.join(".exo");
        let target = target_dir.join("server.sock");

        if let Err(e) = tokio::fs::create_dir_all(&target_dir).await {
            warn!(path = %target_dir.display(), error = %e, "Failed to create .exo/ in worktree");
            return;
        }

        // Ensure worktree .exo/ has a .gitignore so runtime artifacts don't cause
        // untracked file warnings (which force `git worktree remove --force`).
        let gitignore = target_dir.join(".gitignore");
        if !gitignore.exists() {
            if let Err(e) = tokio::fs::write(
                &gitignore,
                "# Runtime artifacts\nserver.sock\nserver.pid\ntmp/\n",
            )
            .await
            {
                tracing::warn!(path = %gitignore.display(), error = %e, "Failed to write .gitignore");
            }
        }

        if let Err(e) = tokio::fs::create_dir_all(target_dir.join("tmp")).await {
            warn!(
                path = %target_dir.join("tmp").display(),
                error = %e,
                "Failed to create .exo/tmp in worktree"
            );
        }

        if let Err(e) = tokio::fs::remove_file(&target).await {
            tracing::debug!(path = %target.display(), error = %e, "Could not remove old socket symlink");
        }

        match tokio::fs::symlink(&source, &target).await {
            Ok(()) => info!(
                source = %source.display(),
                target = %target.display(),
                "Symlinked server socket into worktree"
            ),
            Err(e) => warn!(
                source = %source.display(),
                target = %target.display(),
                error = %e,
                "Failed to symlink server socket"
            ),
        }
    }

    fn server_socket_source(&self) -> PathBuf {
        let project_dir = self.project_dir();
        let absolute_project_dir = project_dir.canonicalize().unwrap_or_else(|_| {
            if project_dir.is_absolute() {
                project_dir.to_path_buf()
            } else {
                std::env::current_dir()
                    .unwrap_or_else(|_| PathBuf::from("."))
                    .join(project_dir)
            }
        });
        absolute_project_dir.join(".exo/server.sock")
    }

    /// Resolve role context file with two-tier fallback: project-local > global.
    pub(crate) fn resolve_role_context(&self, role: &crate::domain::Role) -> Option<PathBuf> {
        resolve_role_context_path(self.project_dir(), &self.wasm_name, role.as_str())
    }

    /// Generate MCP configuration JSON for an agent using stdio transport.
    ///
    /// `extra_mcp_servers` are merged into the `mcpServers` object alongside the
    /// core exomonad entry, giving spawned agents access to the same extra servers
    /// (e.g. metacog, notebooklm) configured in the project's `config.toml`.
    pub(crate) fn generate_mcp_config(
        name: &str,
        agent_type: AgentType,
        role: &str,
        _wasm_name: &str,
        extra_mcp_servers: &HashMap<String, serde_json::Value>,
    ) -> String {
        match agent_type {
            AgentType::Claude => {
                let mut config = serde_json::json!({
                    "mcpServers": {
                        "exomonad": {
                            "type": "stdio",
                            "command": "exomonad",
                            "args": ["mcp-stdio", "--role", role, "--name", name]
                        }
                    }
                });
                if let Some(servers) = config["mcpServers"].as_object_mut() {
                    for (k, v) in extra_mcp_servers {
                        servers.insert(k.clone(), v.clone());
                    }
                }
                serde_json::to_string_pretty(&config).unwrap()
            }
            AgentType::Shoal => serde_json::to_string_pretty(&serde_json::json!({
                "command": "exomonad",
                "args": ["mcp-stdio", "--role", role, "--name", name]
            }))
            .unwrap(),
            AgentType::OpenCode => {
                let mut config = serde_json::json!({
                    "mcp": {
                        "exomonad": {
                            "type": "local",
                            "command": ["exomonad", "mcp-stdio", "--role", role, "--name", name]
                        }
                    }
                });
                if let Some(mcp) = config["mcp"].as_object_mut() {
                    for (k, v) in extra_mcp_servers {
                        mcp.insert(k.clone(), v.clone());
                    }
                }
                serde_json::to_string_pretty(&config).unwrap()
            }
            AgentType::Codex => String::new(),
            AgentType::Process => String::new(),
        }
    }

    /// Build the initial prompt for a spawned agent.
    pub(crate) fn build_initial_prompt(
        issue_id: &str,
        title: &str,
        body: &str,
        labels: &[String],
        issue_url: &str,
    ) -> String {
        let labels_str = if labels.is_empty() {
            "None".to_string()
        } else {
            labels
                .iter()
                .map(|l| format!("`{}`", l))
                .collect::<Vec<_>>()
                .join(", ")
        };

        format!(
            r###"# Issue #{issue_id}: {title}

**Issue URL:** {issue_url}
**Labels:** {labels_str}

## Description

{body}"###,
            issue_id = issue_id,
            title = title,
            issue_url = issue_url,
            labels_str = labels_str,
            body = body,
        )
    }

    /// Escape a string for safe use in shell command with single quotes.
    ///
    /// Wraps the string in single quotes and escapes any embedded single quotes.
    /// Used for fork_session_id (branch names). Prompts use file-based passing instead.
    ///
    /// Example: "user's issue" -> "'user'\''s issue'"
    pub(crate) fn escape_for_shell_command(s: &str) -> String {
        // Replace ' with '\'' (end quote, escaped quote, start quote)
        let escaped = s.replace('\'', r"'\''");
        format!("'{}'", escaped)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[allow(clippy::upper_case_acronyms)]
    type ACS = AgentControlService<crate::services::Services>;

    #[test]
    fn test_escape_for_shell_command_simple() {
        assert_eq!(
            ACS::escape_for_shell_command("hello world"),
            "'hello world'"
        );
    }

    #[test]
    fn test_escape_for_shell_command_with_quote() {
        // Standard shell escaping: end quote, escaped quote, start quote
        // 'user'\''s issue' = 'user' + \' + 's issue'
        assert_eq!(
            ACS::escape_for_shell_command("user's issue"),
            r"'user'\''s issue'"
        );
    }

    #[test]
    fn test_escape_for_shell_command_shell_chars() {
        let result = ACS::escape_for_shell_command("Test $VAR and `code`");
        assert!(result.contains("$VAR"));
        assert!(result.contains("`code`"));
        assert_eq!(result, "'Test $VAR and `code`'");
    }

    #[test]
    fn test_build_initial_prompt_format() {
        let prompt = ACS::build_initial_prompt(
            "123",
            "Fix the bug",
            "Description",
            &["bug".to_string(), "priority".to_string()],
            "https://github.com/owner/repo/issues/123",
        );

        assert!(prompt.contains("# Issue #123: Fix the bug"));
        assert!(prompt.contains("Description"));
        assert!(prompt.contains("https://github.com/owner/repo/issues/123"));
        assert!(prompt.contains("**Labels:** `bug`, `priority`"));
    }

    #[test]
    fn test_build_initial_prompt_no_labels() {
        let prompt = ACS::build_initial_prompt(
            "123",
            "Fix the bug",
            "Description",
            &[],
            "https://github.com/owner/repo/issues/123",
        );

        assert!(prompt.contains("**Labels:** None"));
    }

    #[test]
    fn test_claude_mcp_config_format() {
        let config = ACS::generate_mcp_config(
            "test-claude",
            AgentType::Claude,
            "tl",
            "devswarm",
            &HashMap::new(),
        );
        let parsed: serde_json::Value = serde_json::from_str(&config).unwrap();
        assert_eq!(parsed["mcpServers"]["exomonad"]["type"], "stdio");
        assert_eq!(parsed["mcpServers"]["exomonad"]["command"], "exomonad");
        let args = parsed["mcpServers"]["exomonad"]["args"].as_array().unwrap();
        assert_eq!(
            args,
            &["mcp-stdio", "--role", "tl", "--name", "test-claude"]
        );
    }

    #[test]
    fn test_opencode_worker_settings_use_worker_instructions() {
        let settings = ACS::generate_opencode_tl_settings("test-worker", "worker", &HashMap::new());
        let command = settings["mcp"]["exomonad"]["command"]
            .as_array()
            .expect("OpenCode MCP command must be an array");
        assert_eq!(command[2], "--role");
        assert_eq!(command[3], "worker");
        assert_eq!(command[4], "--name");
        assert_eq!(command[5], "test-worker");

        let instructions = settings["instructions"]
            .as_array()
            .expect("instructions must be an array")[0]
            .as_str()
            .expect("first instruction entry must be a string");

        assert!(instructions.contains("# ExoMonad Worker Agent Protocol"));
        assert!(instructions.contains("chainlink_session_work"));
        assert!(instructions.contains("chainlink_session_end"));
        assert!(instructions.contains("notify_parent"));
        assert!(instructions.contains("review-stuck"));
        assert!(instructions.contains("Never create Chainlink issues"));
        assert!(!instructions.contains("# ExoMonad Dev Agent Protocol"));
        assert!(!instructions.contains("file_pr"));
    }

    #[test]
    fn test_opencode_tl_settings_define_chainlink_review_ownership() {
        let context_path = Path::new("/tmp/sentinel-root.md");
        let settings = ACS::generate_opencode_root_settings_with_context(
            "test-tl",
            context_path,
            &HashMap::new(),
            None,
        );
        let instructions = settings["instructions"]
            .as_array()
            .expect("instructions must be an array")[0]
            .as_str()
            .expect("first instruction entry must be a string");

        assert_eq!(instructions, "/tmp/sentinel-root.md");
    }

    #[test]
    fn test_opencode_dev_settings_keep_dev_instructions() {
        let settings = ACS::generate_opencode_tl_settings("test-dev", "dev", &HashMap::new());
        let instructions = settings["instructions"]
            .as_array()
            .expect("instructions must be an array")[0]
            .as_str()
            .expect("first instruction entry must be a string");

        assert!(instructions.contains("# ExoMonad Dev Agent Protocol"));
        assert!(instructions.contains("file_pr"));
        assert!(instructions.contains("check_inbox"));
        assert!(!instructions.contains("# ExoMonad Worker Agent Protocol"));
        assert!(instructions.contains("chainlink_session_work"));
    }

    #[tokio::test]
    async fn test_write_agent_mcp_config_opencode_writes_full_config_and_plugin() {
        let dir = tempfile::tempdir().unwrap();
        let agent_dir = dir.path().join("test-opencode-dev");
        fs::create_dir_all(&agent_dir).await.unwrap();

        let service = ACS::new(std::sync::Arc::new(crate::services::Services::test()));
        service
            .write_agent_mcp_config(
                dir.path(),
                &agent_dir,
                AgentType::OpenCode,
                &crate::domain::Role::dev(),
            )
            .await
            .unwrap();

        let opencode_json = fs::read_to_string(agent_dir.join("opencode.json"))
            .await
            .unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&opencode_json).unwrap();
        assert_eq!(parsed["plugin"][0], "./.exo/opencode-plugin");
        assert!(parsed["instructions"][0]
            .as_str()
            .unwrap()
            .contains("# ExoMonad Dev Agent Protocol"));
        assert!(parsed["instructions"][0]
            .as_str()
            .unwrap()
            .contains("Call `check_inbox`"));
        assert!(agent_dir.join(".exo/opencode-plugin/index.ts").exists());
        assert!(agent_dir.join(".exo/opencode-plugin/package.json").exists());
    }

    #[tokio::test]
    async fn test_opencode_context_missing_fails_config_generation() {
        let temp_dir = tempfile::tempdir().unwrap();
        let project_dir = temp_dir.path().to_path_buf();
        let service = AgentControlService::new(test_services(project_dir.clone()));
        let agent_dir = project_dir.join("missing-context-agent");
        fs::create_dir_all(&agent_dir).await.unwrap();

        let error = service
            .write_agent_mcp_config(
                &project_dir,
                &agent_dir,
                AgentType::OpenCode,
                &crate::domain::Role::shoal(),
            )
            .await
            .expect_err("missing role context must fail OpenCode config generation");

        assert!(error.to_string().contains("Missing role context"));
    }

    fn test_services(project_dir: PathBuf) -> Arc<crate::services::Services> {
        let git_wt = Arc::new(crate::services::git_worktree::GitWorktreeService::new(
            project_dir.clone(),
        ));
        let mut services = crate::services::Services::test();
        services.project_dir = project_dir;
        services.git_wt = git_wt;
        Arc::new(services)
    }

    #[test]
    fn reviewer_spawn_defaults_do_not_inherit_worker_settings() {
        let service = AgentControlService::new(test_services(PathBuf::from(".")))
            .with_spawn_agent_model(Some("opencode-go/deepseek-v4-pro".to_string()))
            .with_spawn_agent_effort(Some("low".to_string()))
            .with_reviewer_effort(Some("high".to_string()));

        assert_eq!(
            service.default_model_for_spawn(AgentType::Codex, Some("reviewer")),
            None
        );
        assert_eq!(
            service.default_model_for_spawn(AgentType::OpenCode, Some("reviewer")),
            None
        );
        assert_eq!(
            service.default_model_for_spawn(AgentType::Codex, Some("dev")),
            Some("opencode-go/deepseek-v4-pro")
        );
        assert_eq!(
            service.default_effort_for_spawn(Some("reviewer")),
            Some("high")
        );
        assert_eq!(service.default_effort_for_spawn(Some("dev")), Some("low"));
    }

    #[test]
    fn leaf_launch_command_threads_spawn_leaf_options_model() {
        let service = AgentControlService::new(test_services(PathBuf::from(".")))
            .with_spawn_agent_model(Some("config-default".to_string()));
        let env = HashMap::new();

        let overridden = SpawnLeafOptions {
            task: "implement it".to_string(),
            branch_name: "leaf-a".to_string(),
            role: None,
            agent_type: AgentType::OpenCode,
            model: Some("override-model".to_string()),
            claude_flags: ClaudeSpawnFlags::default(),
            standalone_repo: false,
            allowed_dirs: Vec::new(),
            start_point: None,
            base_branch: None,
            expected_agent_name: None,
            invocation_pr_number: None,
            recovery_lineage: None,
        };
        let command = service.leaf_launch_command(
            AgentType::OpenCode,
            "dev",
            &overridden,
            None,
            &env,
            Path::new("/tmp/worktree"),
        );
        assert!(
            command.contains("--model override-model"),
            "override model missing from launch command: {command}"
        );

        let defaulted = SpawnLeafOptions {
            model: None,
            ..overridden
        };
        let default_command = service.leaf_launch_command(
            AgentType::OpenCode,
            "dev",
            &defaulted,
            None,
            &env,
            Path::new("/tmp/worktree"),
        );
        assert!(
            default_command.contains("--model config-default"),
            "static default model missing from launch command: {default_command}"
        );
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn test_codex_reviewer_config_uses_reviewer_instructions() {
        let temp_dir = tempfile::tempdir().unwrap();
        let project_dir = temp_dir.path().to_path_buf();
        let codex_home = project_dir.join("codex-home");
        std::env::set_var("CODEX_HOME", &codex_home);
        let services = test_services(project_dir.clone());
        let service = AgentControlService::new(services);
        let agent_dir = project_dir.join("reviewer-agent");

        service
            .write_codex_config_files(
                &agent_dir,
                &crate::domain::Role::reviewer(),
                &AgentName::try_from_str("reviewer-agent")
                    .expect("literal validated string is non-empty"),
                Some("gpt-5.2"),
                &HashMap::new(),
            )
            .await
            .unwrap();

        let config = tokio::fs::read_to_string(agent_dir.join(".codex/config.toml"))
            .await
            .unwrap();
        let parsed: toml::Value = toml::from_str(&config).expect("valid Codex config TOML");
        let instructions = parsed["developer_instructions"]
            .as_str()
            .expect("developer instructions are rendered");

        assert!(instructions.contains("# ExoMonad Reviewer Agent Protocol"));
        assert!(instructions.contains("approve_pr"));
        assert!(instructions.contains("request_changes"));
        assert!(instructions.contains("no network access"));
        assert!(instructions.contains("watcher reads Forgejo reviews"));
        assert!(instructions.contains("Call `check_inbox`"));
        assert!(!instructions.contains("notify_parent"));
        let lower = instructions.to_lowercase();
        assert!(!lower.contains("curl -") && !lower.contains("curl http"));
        assert!(!instructions.contains("# ExoMonad Dev Agent Protocol"));
        assert_eq!(parsed["model"].as_str(), Some("gpt-5.2"));
        assert!(codex_home.join("config.toml").exists());
        assert!(!agent_dir.join(".codex/hooks.json").exists());
        std::env::remove_var("CODEX_HOME");
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn test_codex_reviewer_config_preserves_luna_xhigh() {
        let temp_dir = tempfile::tempdir().unwrap();
        let project_dir = temp_dir.path().to_path_buf();
        let codex_home = project_dir.join("codex-home");
        std::env::set_var("CODEX_HOME", &codex_home);
        let services = test_services(project_dir.clone());
        let service = AgentControlService::new(services)
            .with_reviewer_model(Some("gpt-5.6-luna".to_string()))
            .with_reviewer_effort(Some("xhigh".to_string()));
        let agent_dir = project_dir.join("reviewer-luna-agent");

        service
            .write_agent_mcp_config(
                &project_dir,
                &agent_dir,
                AgentType::Codex,
                &crate::domain::Role::reviewer(),
            )
            .await
            .unwrap();

        let config = tokio::fs::read_to_string(agent_dir.join(".codex/config.toml"))
            .await
            .unwrap();
        let parsed: toml::Value = toml::from_str(&config).expect("valid Codex config TOML");
        assert_eq!(parsed["model"].as_str(), Some("gpt-5.6-luna"));
        assert_eq!(parsed["model_reasoning_effort"].as_str(), Some("xhigh"));
        std::env::remove_var("CODEX_HOME");
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn test_codex_worker_config_uses_worker_instructions() {
        let temp_dir = tempfile::tempdir().unwrap();
        let project_dir = temp_dir.path().to_path_buf();
        let codex_home = project_dir.join("codex-home");
        std::env::set_var("CODEX_HOME", &codex_home);
        let services = test_services(project_dir.clone());
        let service = AgentControlService::new(services);
        let agent_dir = project_dir.join("worker-agent");

        service
            .write_codex_config_files(
                &agent_dir,
                &crate::domain::Role::worker(),
                &AgentName::try_from_str("worker-agent")
                    .expect("literal validated string is non-empty"),
                None,
                &HashMap::new(),
            )
            .await
            .unwrap();

        let config = tokio::fs::read_to_string(agent_dir.join(".codex/config.toml"))
            .await
            .unwrap();
        let parsed: toml::Value = toml::from_str(&config).expect("valid Codex config TOML");
        let instructions = parsed["developer_instructions"]
            .as_str()
            .expect("developer instructions are rendered");

        assert!(instructions.contains("# ExoMonad Worker Agent Protocol"));
        assert!(instructions.contains("chainlink_session_work"));
        assert!(instructions.contains("chainlink_session_end"));
        assert!(instructions.contains("Call `check_inbox`"));
        assert!(!instructions.contains("chainlink_issue_close"));
        assert!(!instructions.contains("chainlink_agent_init"));
        assert!(!instructions.contains("# ExoMonad Dev Agent Protocol"));
        assert!(codex_home.join("config.toml").exists());
        assert!(!agent_dir.join(".codex/hooks.json").exists());
        std::env::remove_var("CODEX_HOME");
    }

    #[tokio::test]
    async fn test_create_socket_symlink() {
        let temp_dir = tempfile::tempdir().unwrap();
        let project_dir = temp_dir.path().to_path_buf();
        let exo_dir = project_dir.join(".exo");
        tokio::fs::create_dir_all(&exo_dir).await.unwrap();
        tokio::fs::write(exo_dir.join("server.sock"), "placeholder")
            .await
            .unwrap();

        let services = test_services(project_dir.clone());
        let service = AgentControlService::new(services);

        let worktree = temp_dir.path().join("child-wt");
        tokio::fs::create_dir_all(&worktree).await.unwrap();

        service.create_socket_symlink(&worktree).await;

        let link = worktree.join(".exo/server.sock");
        assert!(link.exists(), "Symlink should exist");
        let target = tokio::fs::read_link(&link).await.unwrap();
        assert!(
            target.is_absolute(),
            "socket symlink target should be absolute"
        );
        assert_eq!(target, project_dir.join(".exo/server.sock"));
        assert!(
            worktree.join(".exo/tmp").is_dir(),
            "file-indirect injection dir should exist"
        );
    }

    #[test]
    fn test_common_spawn_env_core_vars() {
        let services = test_services(PathBuf::from("."));
        let service = AgentControlService::new(services).with_birth_branch(
            BirthBranch::try_from_str("main.tl-auth")
                .expect("literal validated string is non-empty"),
        );

        let agent = AgentName::try_from_str("fix-oauth-codex")
            .expect("literal validated string is non-empty");
        let session_id = BranchName::try_from_str("main.tl-auth.fix-oauth-codex")
            .expect("literal validated string is non-empty");
        let role = crate::domain::Role::dev();

        let env = service.common_spawn_env(&agent, &session_id, &role);

        assert_eq!(env.get("EXOMONAD_AGENT_ID").unwrap(), "fix-oauth-codex");
        assert_eq!(
            env.get("EXOMONAD_SESSION_ID").unwrap(),
            "main.tl-auth.fix-oauth-codex"
        );
        assert_eq!(env.get("EXOMONAD_ROLE").unwrap(), "dev");
        assert_eq!(
            env.get("EXOMONAD_PARENT_AGENT").unwrap(),
            "main.tl-auth",
            "Parent agent should be the service's own birth branch"
        );
    }

    #[test]
    fn test_common_spawn_env_tmux_session() {
        let services = test_services(PathBuf::from("."));
        let service = AgentControlService::new(services)
            .with_birth_branch(
                BirthBranch::try_from_str("main").expect("literal validated string is non-empty"),
            )
            .with_tmux_session("exo-test-session".to_string());

        let agent =
            AgentName::try_from_str("worker-1").expect("literal validated string is non-empty");
        let session_id =
            BranchName::try_from_str("main").expect("literal validated string is non-empty");
        let role = crate::domain::Role::worker();

        let env = service.common_spawn_env(&agent, &session_id, &role);

        assert_eq!(
            env.get("EXOMONAD_TMUX_SESSION").unwrap(),
            "exo-test-session"
        );
    }

    #[test]
    fn test_common_spawn_env_no_tmux_session() {
        let services = test_services(PathBuf::from("."));
        let service = AgentControlService::new(services).with_birth_branch(
            BirthBranch::try_from_str("main").expect("literal validated string is non-empty"),
        );

        let agent =
            AgentName::try_from_str("worker-1").expect("literal validated string is non-empty");
        let session_id =
            BranchName::try_from_str("main").expect("literal validated string is non-empty");
        let role = crate::domain::Role::worker();

        let env = service.common_spawn_env(&agent, &session_id, &role);

        assert!(
            !env.contains_key("EXOMONAD_TMUX_SESSION"),
            "No tmux session should be set when not configured"
        );
    }

    #[test]
    fn test_common_spawn_env_chainlink_db_directory_form() {
        let project_dir = PathBuf::from("/tmp/exo-test-project");
        let services = test_services(project_dir.clone());
        let service = AgentControlService::new(services).with_birth_branch(
            BirthBranch::try_from_str("main").expect("literal validated string is non-empty"),
        );

        let agent =
            AgentName::try_from_str("leaf-1").expect("literal validated string is non-empty");
        let session_id =
            BranchName::try_from_str("main.leaf-1").expect("literal validated string is non-empty");
        let role = crate::domain::Role::dev();

        let env = service.common_spawn_env(&agent, &session_id, &role);

        let chainlink_db = env
            .get("CHAINLINK_DB")
            .expect("CHAINLINK_DB must be set so spawned agents resolve the canonical issues DB");

        assert_eq!(
            chainlink_db,
            &project_dir.join(".chainlink").display().to_string(),
            "CHAINLINK_DB must point at the project-root .chainlink directory"
        );
        assert!(
            !chainlink_db.ends_with("/issues.db"),
            "CHAINLINK_DB must use the directory form, not the file form — both are valid for the \
             chainlink CLI but the directory is the canonical resource and the project's single \
             source of truth across all spawn sites (init.rs uses the same form)"
        );
    }

    #[test]
    #[serial_test::serial]
    fn test_common_spawn_env_uses_reviewer_token_for_reviewer_role() {
        let services = test_services(PathBuf::from("."));
        let service = AgentControlService::new(services).with_birth_branch(
            BirthBranch::try_from_str("main").expect("literal validated string is non-empty"),
        );
        let agent = AgentName::try_from_str("review-pr-7-codex")
            .expect("literal validated string is non-empty");
        let session_id =
            BranchName::try_from_str("review-pr-7").expect("literal validated string is non-empty");

        std::env::set_var("FORGEJO_TOKEN", "author-token");
        std::env::set_var("GH_TOKEN", "author-token");
        std::env::set_var("FORGEJO_REVIEWER_TOKEN", "reviewer-token");
        let env = service.common_spawn_env(&agent, &session_id, &crate::domain::Role::reviewer());
        std::env::remove_var("FORGEJO_TOKEN");
        std::env::remove_var("GH_TOKEN");
        std::env::remove_var("FORGEJO_REVIEWER_TOKEN");

        assert_eq!(
            env.get("FORGEJO_TOKEN").map(String::as_str),
            Some("reviewer-token")
        );
        assert_eq!(
            env.get("GH_TOKEN").map(String::as_str),
            Some("reviewer-token")
        );
        assert_eq!(
            env.get("FORGEJO_REVIEWER_TOKEN").map(String::as_str),
            Some("reviewer-token")
        );
    }

    #[test]
    #[serial_test::serial]
    fn test_common_spawn_env_uses_config_forgejo_values_for_reviewer_role() {
        let services = test_services(PathBuf::from("."));
        let service = AgentControlService::new(services)
            .with_birth_branch(
                BirthBranch::try_from_str("main").expect("literal validated string is non-empty"),
            )
            .with_forgejo_spawn_env(
                ForgejoSpawnEnv::new(
                    Some("http://forgejo.local:3000".to_string()),
                    Some("config-author-token".to_string()),
                    Some("config-reviewer-token".to_string()),
                )
                .with_repo("config-owner", "config-repo"),
            );
        let agent = AgentName::try_from_str("review-pr-7-codex")
            .expect("literal validated string is non-empty");
        let session_id =
            BranchName::try_from_str("review-pr-7").expect("literal validated string is non-empty");

        std::env::set_var("FORGEJO_URL", "http://stale.local:3000");
        std::env::set_var("FORGEJO_TOKEN", "stale-author-token");
        std::env::set_var("GH_TOKEN", "stale-author-token");
        std::env::set_var("FORGEJO_REVIEWER_TOKEN", "stale-reviewer-token");
        let env = service.common_spawn_env(&agent, &session_id, &crate::domain::Role::reviewer());
        std::env::remove_var("FORGEJO_URL");
        std::env::remove_var("FORGEJO_TOKEN");
        std::env::remove_var("GH_TOKEN");
        std::env::remove_var("FORGEJO_REVIEWER_TOKEN");

        assert_eq!(
            env.get("FORGEJO_URL").map(String::as_str),
            Some("http://forgejo.local:3000")
        );
        assert_eq!(
            env.get("FORGEJO_HOST").map(String::as_str),
            Some("forgejo.local:3000")
        );
        assert_eq!(
            env.get("GH_HOST").map(String::as_str),
            Some("forgejo.local:3000")
        );
        assert_eq!(
            env.get("FORGEJO_REVIEWER_TOKEN").map(String::as_str),
            Some("config-reviewer-token")
        );
        assert_eq!(
            env.get("FORGEJO_TOKEN").map(String::as_str),
            Some("config-reviewer-token"),
            "reviewer role must use the reviewer token for Forgejo API writes"
        );
        assert_eq!(
            env.get("GH_TOKEN").map(String::as_str),
            Some("config-reviewer-token")
        );
        assert_eq!(
            env.get("FORGEJO_OWNER").map(String::as_str),
            Some("config-owner")
        );
        assert_eq!(
            env.get("FORGEJO_REPO").map(String::as_str),
            Some("config-repo")
        );
        assert_eq!(env.get("REPO").map(String::as_str), Some("config-repo"));
    }

    #[test]
    #[serial_test::serial]
    fn test_common_spawn_env_codex_home_propagated() {
        // Sibling to the CHAINLINK_DB test — confirms CODEX_HOME flows through the
        // shell-prefix env path so spawned codex agents see install_codex_hook_trust's
        // seeded [hooks.state] entries instead of falling back to ~/.codex.
        // #[serial] required because this test mutates CODEX_HOME, which other env-mutating
        // tests in this module also touch (test_codex_*_config_uses_*_instructions).
        let project_dir = PathBuf::from("/tmp/exo-test-project");
        let services = test_services(project_dir.clone());
        let service = AgentControlService::new(services).with_birth_branch(
            BirthBranch::try_from_str("main").expect("literal validated string is non-empty"),
        );

        let agent =
            AgentName::try_from_str("leaf-1").expect("literal validated string is non-empty");
        let session_id =
            BranchName::try_from_str("main.leaf-1").expect("literal validated string is non-empty");
        let role = crate::domain::Role::dev();

        std::env::set_var("CODEX_HOME", "/tmp/exo-test-codex-home");
        let env_set = service.common_spawn_env(&agent, &session_id, &role);
        std::env::remove_var("CODEX_HOME");
        let env_unset = service.common_spawn_env(&agent, &session_id, &role);

        assert_eq!(
            env_set.get("CODEX_HOME").map(String::as_str),
            Some("/tmp/exo-test-codex-home"),
            "CODEX_HOME must be propagated into the spawn env so spawned codex panes \
             see the same hook-trust DB that install_codex_hook_trust seeded \
             (chainlink #259)"
        );
        assert!(
            !env_unset.contains_key("CODEX_HOME"),
            "CODEX_HOME must NOT be present in the spawn env when unset in the parent \
             process — propagation is opt-in, not synthetic"
        );
    }

    // =========================================================================
    // build_agent_command — OpenCode tests
    // =========================================================================

    fn empty_env() -> HashMap<String, String> {
        HashMap::new()
    }

    #[test]
    fn test_build_agent_command_covers_all_coding_runtimes() {
        let prompt = Path::new("/tmp/test-prompt.txt");
        for agent_type in [
            AgentType::Claude,
            AgentType::Codex,
            AgentType::Shoal,
            AgentType::OpenCode,
        ] {
            let command = ACS::build_agent_command(
                agent_type,
                Some(prompt),
                None,
                &empty_env(),
                Path::new("/tmp/test"),
                None,
                false,
                None,
            );

            assert!(
                command.starts_with(agent_type.command()),
                "{agent_type:?} command must start with its configured harness: {command}"
            );
            assert!(
                command.contains("$(cat '/tmp/test-prompt.txt')"),
                "{agent_type:?} command must receive the assignment prompt: {command}"
            );
            assert!(
                !command.contains(" -p "),
                "{agent_type:?} must keep its interactive stdin path for live guidance: {command}"
            );
        }

        let opencode = ACS::build_agent_command(
            AgentType::OpenCode,
            Some(prompt),
            None,
            &empty_env(),
            Path::new("/tmp/test"),
            None,
            false,
            None,
        );
        assert!(opencode.contains("run --interactive"));
    }

    #[test]
    fn test_one_shot_claude_uses_print_flag_and_companions_do_not() {
        let prompt = Path::new("/tmp/test-prompt.txt");
        let one_shot = ACS::build_agent_command_with_effort(
            AgentType::Claude,
            Some(prompt),
            None,
            &empty_env(),
            Path::new("/tmp/test"),
            None,
            false,
            Some("sonnet"),
            Some("high"),
            super::InvocationMode::OneShot,
        );
        assert!(one_shot.starts_with("claude -p --dangerously-skip-permissions"));

        let companion = ACS::build_agent_command_with_effort(
            AgentType::Claude,
            Some(prompt),
            None,
            &empty_env(),
            Path::new("/tmp/test"),
            None,
            false,
            Some("sonnet"),
            Some("high"),
            super::InvocationMode::Interactive,
        );
        assert!(!companion.contains(" -p "));
    }

    #[test]
    fn test_one_shot_does_not_change_codex_or_opencode_commands() {
        let prompt = Path::new("/tmp/test-prompt.txt");
        for agent_type in [AgentType::Codex, AgentType::OpenCode] {
            let interactive = ACS::build_agent_command_with_effort(
                agent_type,
                Some(prompt),
                None,
                &empty_env(),
                Path::new("/tmp/test"),
                None,
                false,
                None,
                None,
                super::InvocationMode::Interactive,
            );
            let one_shot = ACS::build_agent_command_with_effort(
                agent_type,
                Some(prompt),
                None,
                &empty_env(),
                Path::new("/tmp/test"),
                None,
                false,
                None,
                None,
                super::InvocationMode::OneShot,
            );
            assert_eq!(
                interactive, one_shot,
                "{agent_type:?} launch must be unchanged"
            );
        }
    }

    #[test]
    fn test_build_agent_command_opencode_no_prompt() {
        let cmd = ACS::build_agent_command(
            AgentType::OpenCode,
            None,
            None,
            &empty_env(),
            Path::new("/tmp/test"),
            None,
            false,
            None,
        );
        assert_eq!(cmd, "opencode");
    }

    #[test]
    fn test_build_agent_command_records_exit_status_when_requested() {
        let mut env = HashMap::new();
        env.insert(
            "EXOMONAD_INVOCATION_EXIT_FILE".to_string(),
            "/tmp/exomonad-exit-code".to_string(),
        );
        let command = ACS::build_agent_command(
            AgentType::Codex,
            None,
            None,
            &env,
            Path::new("/tmp/test"),
            None,
            false,
            None,
        );

        assert!(command.contains("status=$?"));
        assert!(command.contains("/tmp/exomonad-exit-code"));
        assert!(command.ends_with("exit \"$status\""));
    }

    #[test]
    fn test_build_agent_command_opencode_with_prompt_no_model() {
        let prompt = Path::new("/tmp/test-prompt.txt");
        let cmd = ACS::build_agent_command(
            AgentType::OpenCode,
            Some(prompt),
            None,
            &empty_env(),
            Path::new("/tmp/test"),
            None,
            false,
            None,
        );
        assert_eq!(
            cmd,
            "opencode run --interactive \"$(cat '/tmp/test-prompt.txt')\""
        );
    }

    #[test]
    fn test_build_agent_command_opencode_with_prompt_and_model() {
        let prompt = Path::new("/tmp/test-prompt.txt");
        let cmd = ACS::build_agent_command(
            AgentType::OpenCode,
            Some(prompt),
            None,
            &empty_env(),
            Path::new("/tmp/test"),
            None,
            false,
            Some("anthropic/claude-sonnet-4-5"),
        );
        assert_eq!(
            cmd,
            "opencode run --interactive \"$(cat '/tmp/test-prompt.txt')\" --model anthropic/claude-sonnet-4-5"
        );
    }

    #[test]
    fn test_build_agent_command_opencode_fork_session_with_model() {
        let prompt = Path::new("/tmp/test-prompt.txt");
        let cmd = ACS::build_agent_command(
            AgentType::OpenCode,
            Some(prompt),
            Some("main.feature-a-opencode"),
            &empty_env(),
            Path::new("/tmp/test"),
            None,
            false,
            Some("anthropic/claude-haiku-4-5"),
        );
        assert_eq!(
            cmd,
            "opencode run --interactive --session 'main.feature-a-opencode' --fork \"$(cat '/tmp/test-prompt.txt')\" --model anthropic/claude-haiku-4-5"
        );
    }

    #[test]
    fn test_build_agent_command_opencode_model_shell_escaping() {
        let cmd = ACS::build_agent_command(
            AgentType::OpenCode,
            None,
            None,
            &empty_env(),
            Path::new("/tmp/test"),
            None,
            false,
            Some("anthropic/claude's-model"),
        );
        // Single quote in model name must be shell-escaped
        assert_eq!(cmd, "opencode --model 'anthropic/claude'\\''s-model'");
    }

    #[test]
    fn test_build_codex_command_fresh_with_prompt_and_model() {
        let cmd = ACS::build_codex_command(
            Path::new("/tmp/worktree"),
            Some(Path::new("/tmp/test-prompt.txt")),
            Some("gpt-5.2"),
            None,
        );

        assert_eq!(
            cmd,
            "codex --dangerously-bypass-approvals-and-sandbox --cd '/tmp/worktree' --model gpt-5.2 \"$(cat '/tmp/test-prompt.txt')\""
        );
    }

    #[test]
    fn test_build_codex_command_fork_with_model() {
        let cmd = ACS::build_codex_command(
            Path::new("/tmp/worktree"),
            Some(Path::new("/tmp/test-prompt.txt")),
            Some("gpt-5.2"),
            Some("session-123"),
        );

        assert_eq!(
            cmd,
            "codex fork 'session-123' --dangerously-bypass-approvals-and-sandbox --cd '/tmp/worktree' --model gpt-5.2"
        );
    }

    #[test]
    fn test_build_agent_command_claude_includes_effort() {
        let cmd = ACS::build_agent_command_with_effort(
            AgentType::Claude,
            Some(Path::new("/tmp/test-prompt.txt")),
            None,
            &empty_env(),
            Path::new("/tmp/worktree"),
            None,
            false,
            Some("sonnet"),
            Some("high"),
            super::InvocationMode::Interactive,
        );

        assert!(cmd.contains("--model sonnet --effort high"));
    }

    #[test]
    fn test_build_agent_command_opencode_includes_variant() {
        let cmd = ACS::build_agent_command_with_effort(
            AgentType::OpenCode,
            Some(Path::new("/tmp/test-prompt.txt")),
            None,
            &empty_env(),
            Path::new("/tmp/worktree"),
            None,
            false,
            Some("opencode-go/deepseek-v4-pro"),
            Some("high"),
            super::InvocationMode::Interactive,
        );

        assert!(cmd.ends_with("--model opencode-go/deepseek-v4-pro --variant high"));
    }

    #[test]
    fn test_build_codex_command_includes_effort() {
        let cmd = ACS::build_codex_command_with_effort(
            Path::new("/tmp/worktree"),
            Some(Path::new("/tmp/test-prompt.txt")),
            Some("gpt-5.2"),
            Some("xhigh"),
            None,
        );

        assert!(cmd.contains("--model gpt-5.2 -c model_reasoning_effort=\"xhigh\""));
    }

    #[test]
    fn test_opencode_effort_profile_is_role_specific() {
        let settings = ACS::generate_opencode_tl_settings_with_effort(
            "test-worker",
            "worker",
            &HashMap::new(),
            Some("high"),
        );

        assert_eq!(
            settings["agent"]["exomonad-worker"]["reasoningEffort"],
            "high"
        );
        assert!(settings["instructions"][0]
            .as_str()
            .is_some_and(|instructions| instructions.contains("# ExoMonad Worker Agent Protocol")));
    }

    #[test]
    fn test_build_agent_command_codex_includes_env_prefix() {
        let mut env = HashMap::new();
        env.insert(
            "EXOMONAD_AGENT_ID".to_string(),
            "worker-1-codex".to_string(),
        );

        let cmd = ACS::build_agent_command(
            AgentType::Codex,
            Some(Path::new("/tmp/test-prompt.txt")),
            None,
            &env,
            Path::new("/tmp/worktree"),
            None,
            false,
            None,
        );

        assert_eq!(
            cmd,
            "EXOMONAD_AGENT_ID=worker-1-codex codex --dangerously-bypass-approvals-and-sandbox --cd '/tmp/worktree' \"$(cat '/tmp/test-prompt.txt')\""
        );
    }
}
