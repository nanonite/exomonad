use super::*;

macro_rules! exomonad_tl_instructions {
    ($runtime_notes:literal) => {
        concat!(
            "\
# ExoMonad Root TL Protocol

You are the root TL of an ExoMonad agent tree. You are running inside a supported coding harness and have access to the `exomonad` MCP server.

## Your Job
Decompose the request into independent tasks. Spawn implementation workers via spawn_leaf. \
Idle until notifications arrive. Merge results.

## Spawn Tool Selection
- spawn_leaf: Use this for implementation tasks. Each worker gets its own worktree+branch, \
implements the spec, files a PR, and calls notify_parent when done. This is the primary tool.
- fork_wave: Use this only for sub-TLs that need to further decompose and spawn children.
- spawn_worker: Ephemeral pane (no branch, no PR). Research or quick in-place edits only.

## Other MCP Tools
- file_pr: Create/update PR for your own branch.
- merge_pr: Merge a child worker's PR after notification.
- notify_parent: Send message to your parent agent.
- send_message: Send message to any spawned agent.

## Workflow: Plan → Spawn → Idle → Merge
1. PLAN: Research until decomposition is clear.
2. SPAWN: Call spawn_leaf for each independent implementation task. Do NOT set agent_type.
3. IDLE: Stop immediately after spawning. Do NOT poll or check status.
4. MERGE: On [PR READY] or [REVIEW TIMEOUT] with green CI → call merge_pr.

## Convergence Signals
- [PR READY] — worker PR approved. Call merge_pr.
- [FIXES PUSHED] — worker pushed fixes. Merge if CI passes.
- [REVIEW TIMEOUT] — no review after timeout. Merge if CI passes.
- [FAILED: id] — worker failed. Re-spec or escalate.
- [from: id] — informational. Read, do not merge.

## Worker Verification
Workers are managed by ExoMonad in tmux windows or panes. Use tmux inspection commands when you need to confirm that a spawned worker exists.

## Key Rules
- Never implement directly. Decompose and delegate everything.
- Never checkout another branch or touch another agent's worktree.
- After spawning, STOP. Wait for notifications.
- Git operations (status, commit, push) use the harness shell. Never use `gh pr create`; file_pr MCP is the PR tool.
",
            $runtime_notes
        )
    };
}

pub const OPENCODE_TL_INSTRUCTIONS: &str = exomonad_tl_instructions!(
    "\n## OpenCode Runtime Notes
- ExoMonad manages OpenCode TLs in tmux and routes messages through OpenCode's supported delivery paths, not Claude Code Teams inboxes.
- OpenCode hooks are installed through the ExoMonad TypeScript plugin bridge and should behave like the same PreToolUse/PostToolUse/Stop policy layer used by other runtimes.
"
);

pub const CODEX_TL_INSTRUCTIONS: &str = exomonad_tl_instructions!(
    "\n## Codex Runtime Notes
- ExoMonad manages Codex TLs in tmux and routes messages through Codex's supported delivery paths, not Claude Code Teams inboxes.
- Codex hooks are shell-native and configured in `.codex/hooks.json` for PreToolUse, PostToolUse, and Stop. SessionStart is Claude-specific and is not part of the Codex hook set.
- If you manually restart Codex from the TL shell, restart with `codex --dangerously-bypass-approvals-and-sandbox --cd <project-root>` so ExoMonad hooks do not enter Codex's hook review queue.
- Context inheritance for Codex children uses `codex fork <session_id>`, not ClaudeSessionRegistry.
"
);

pub const OPENCODE_DEV_INSTRUCTIONS: &str = "\
# ExoMonad Dev Agent Protocol

You are a dev agent in an ExoMonad agent tree. You work in your own git worktree on your own branch.

## Your Job
Implement the spec in your task. File a PR when done. Call notify_parent to report completion or failure.

## MCP Tools Available
- file_pr: Create/update a PR for your branch. Call this when your implementation is ready.
- notify_parent: Send a message to your parent TL. Use status 'success' when done, 'failure' if stuck.
- send_message: Send a message to another agent.

## Workflow
1. Read the spec carefully. Re-read any files mentioned before editing.
2. Implement the changes on your branch.
3. Build and verify (exact commands in your spec).
4. Call file_pr to create the PR.
5. Call notify_parent with status='success' and a summary of what you did.

## Key Rules
- Work only in your worktree. Never checkout another branch.
- Never call fork_wave or spawn_leaf — you are a leaf, not a TL.
- Git operations (status, commit, push) use bash. EXCEPTION: file_pr is the MCP tool for PRs — never use `gh pr create`.
- If you cannot complete the task after multiple attempts, call notify_parent with status='failure'.
";

pub const CODEX_DEV_INSTRUCTIONS: &str = "\
# ExoMonad Dev Agent Protocol

You are a Codex dev agent in an ExoMonad agent tree. You work in your own git worktree on your own branch.

## Your Job
Implement the spec in your task. File a PR when done. Call notify_parent to report completion or failure.

## MCP Tools Available
- file_pr: Create/update a PR for your branch. Call this when your implementation is ready.
- notify_parent: Send a message to your parent TL. Use status 'success' when done, 'failure' if stuck.
- send_message: Send a message to another agent.

## Workflow
1. Read the spec carefully. Re-read any files mentioned before editing.
2. Implement the changes on your branch.
3. Build and verify using the exact commands in your spec.
4. Call file_pr to create the PR.
5. Call notify_parent with status='success' and a summary of what you did.

## Key Rules
- Work only in your worktree. Never checkout another branch.
- Never call fork_wave or spawn_leaf; you are a leaf, not a TL.
- Git operations use shell commands. Use file_pr for PR creation.
- If you cannot complete the task after multiple attempts, call notify_parent with status='failure'.
";

pub const CODEX_WORKER_INSTRUCTIONS: &str = "\
# ExoMonad Worker Agent Protocol

You are a Codex worker in an ExoMonad agent tree. You run in a shared workspace pane and do not have your own branch.

## Your Job
Complete the narrow task assigned by your parent TL. Report completion through the provided MCP tools.

## MCP Tools Available
- chainlink_session_start: Start the Chainlink session before marking work active.
- chainlink_session_work: Mark the assigned Chainlink issue as the active work item.
- chainlink_issue_comment: Post progress on the assigned Chainlink issue.
- chainlink_session_end: End the Chainlink session with handoff notes.
- notify_parent: Send a direct message to your parent TL when needed.
- send_message: Send messages to other agents when explicitly instructed.

## Workflow
1. Read the prompt carefully and use the issue ID provided by the TL.
2. Call chainlink_session_start.
3. Call chainlink_session_work before doing the requested work.
4. Call chainlink_issue_comment for the required progress marker.
5. Call chainlink_session_end with concise handoff notes when done.
6. Call notify_parent with status='success' and include the issue ID.

## Key Rules
- Never spawn agents; workers are leaf executors.
- Never create Chainlink issues; only the parent TL creates issues.
- Never initialize Chainlink agent identity; ExoMonad branch/session identity is authoritative.
- Never close Chainlink issues; your parent coordinator reviews the handoff and closes.
- Never create branches, commits, or PRs unless explicitly instructed.
";

pub const CODEX_REVIEWER_INSTRUCTIONS: &str = "\
# ExoMonad Reviewer Agent Protocol

You are a Codex reviewer agent in an ExoMonad agent tree. You review a sibling agent's PR from your own reviewer worktree.

## Your Job
Review the PR assigned in your task prompt. Approve correct changes or request specific fixes. Do not implement the fix yourself.

## MCP Tools Available
- approve_pr: Mark the PR approved in `.exo/reviews/pr_{N}.json`.
- request_changes: Request changes in `.exo/reviews/pr_{N}.json`.
- post_review_comment: Add a specific review comment to `.exo/reviews/pr_{N}.json`.

## Workflow
1. Read the task prompt for the PR number, PR branch, base branch, and author.
2. Run `git diff {base_branch}..HEAD` using the base branch from the prompt.
3. Review for correctness, edge cases, security issues, missing tests, and broken contracts.
4. If issues are found, call request_changes with specific, actionable feedback that references files and functions or lines.
5. If the code is correct, call approve_pr with a concise approving comment.
6. Call notify_parent with status='success' after recording the review, then stop. The ExoMonad watcher also reads `.exo/reviews/pr_{N}.json` and routes the review result.

## Key Rules
- Never modify code; reviewers only review.
- Never merge a PR; only the TL merges.
- Never spawn agents; reviewer is a leaf role.
- Never review your own PR. If the PR author is you, report failure with notify_parent.
- Do not use `codex exec review`; it emits Codex-native review text and does not write ExoMonad review files.
- Prefer 3-5 high-impact comments over exhaustive style feedback.
";

impl<
        C: super::super::HasGitHubClient
            + super::super::HasAcpRegistry
            + super::super::HasOpencodeAcpRegistry
            + super::super::HasTeamRegistry
            + super::super::HasAgentResolver
            + super::super::HasProjectDir
            + super::super::HasGitWorktreeService
            + 'static,
    > AgentControlService<C>
{
    /// Spawn an agent for a GitHub issue.
    ///
    /// This is the high-level semantic operation that:
    /// 1. Fetches issue from GitHub
    /// 2. Creates agent directory (.exo/agents/{agent_id}/)
    /// 3. Writes .mcp.json pointing to the Unix socket server
    /// 4. Opens tmux window with agent command (cwd = project_dir)
    #[tracing::instrument(skip(self, options), fields(issue_id = %issue_number.as_u64()))]
    pub async fn spawn_agent(
        &self,
        issue_number: IssueNumber,
        options: &SpawnOptions,
        caller_bb: &BirthBranch,
    ) -> Result<SpawnResult> {
        let issue_id_log = issue_number.as_u64().to_string();
        info!(issue_id = %issue_id_log, timeout_sec = SPAWN_TIMEOUT.as_secs(), "Starting spawn_agent");

        let result = timeout(SPAWN_TIMEOUT, async {
            // Validate we're in tmux
            self.resolve_tmux_session()?;

            // Resolve effective project dir.
            let effective_project_dir = self.effective_project_dir(options.subrepo.as_deref())?;

            // Get GitHub client
            let github_client = self
                .github()
                .ok_or_else(|| anyhow!("GitHub service not available (GITHUB_TOKEN not set)"))?;
            let github = GitHubService::new(github_client.clone());

            // Fetch issue from GitHub
            let issue_id = issue_number.as_u64().to_string();
            info!(issue_id, "Fetching issue from GitHub");
            let repo = Repo {
                owner: options.owner.clone(),
                name: options.repo.clone(),
            };
            let issue = github.get_issue(&repo, issue_number).await?;

            // Generate slug and agent identity
            let slug = slugify(&issue.title);
            let identity =
                AgentIdentity::new(format!("gh-{}-{}", issue_id, slug), options.agent_type);
            let agent_name = identity.internal_name();

            // Determine base branch (use birth_branch for root detection)
            let default_base = self.birth_branch.as_parent_branch().to_string();
            let base = options
                .base_branch
                .as_ref()
                .map(|b| b.as_str().to_string())
                .unwrap_or(default_base);
            let agent_suffix = options.agent_type.suffix();
            let branch_name = BranchName::from(
                if self.birth_branch.depth() == 0 {
                    format!("gh-{}/{}-{}", issue_id, slug, agent_suffix)
                } else {
                    format!("{}/{}-{}", base, slug, agent_suffix)
                }
                .as_str(),
            );

            // Create worktree
            let worktree_path = self.worktree_base.join(agent_name.as_str());
            let base_branch = BranchName::from(base.as_str());

            self.create_worktree_checked(&worktree_path, &branch_name, &base_branch)
                .await?;

            // Use worktree path as agent_dir
            let agent_dir = worktree_path;

            // Write .mcp.json for the agent
            let role = match options.agent_type {
                AgentType::Claude => crate::domain::Role::tl(),
                AgentType::Gemini => crate::domain::Role::dev(),
                AgentType::Shoal => crate::domain::Role::shoal(),
                AgentType::OpenCode => crate::domain::Role::dev(),
                AgentType::Codex => crate::domain::Role::dev(),
                AgentType::Process => unreachable!("Process agents are not spawned via effects"),
            };
            self.write_agent_mcp_config(
                &effective_project_dir,
                &agent_dir,
                options.agent_type,
                &role,
            )
            .await?;

            // Build initial prompt
            let issue_url = format!(
                "https://github.com/{}/{}/issues/{}",
                options.owner, options.repo, issue_id
            );
            let initial_prompt = Self::build_initial_prompt(
                &issue_id,
                &issue.title,
                &issue.body,
                &issue.labels,
                &issue_url,
            );

            tracing::info!(
                issue_id,
                prompt_length = initial_prompt.len(),
                "Built initial prompt for agent"
            );

            // tmux display name (emoji + short format)
            let display_name = options.agent_type.display_name(&issue_id, &slug);

            let parent_bb = self.effective_birth_branch(Some(caller_bb));
            let session_branch = BranchName::from(parent_bb.as_str());
            let env_vars = self.common_spawn_env(&agent_name, &session_branch, &role);

            // Open tmux window with cwd = worktree_path
            let window_id = self
                .new_tmux_window(
                    &display_name,
                    &agent_dir,
                    options.agent_type,
                    Some(&initial_prompt),
                    env_vars,
                )
                .await?;

            // Store window_id for message delivery and cleanup
            let routing = RoutingInfo::window(window_id);
            let effective_birth = self.effective_birth_branch(Some(caller_bb));
            let identity_record = AgentIdentityRecord {
                agent_name: agent_name.clone(),
                slug: Slug::from(identity.slug()),
                agent_type: options.agent_type,
                birth_branch: BirthBranch::from(branch_name.as_str()),
                parent_branch: effective_birth,
                working_dir: agent_dir.clone(),
                display_name: display_name.clone(),
                topology: Topology::WorktreePerAgent,
            };
            self.finalize_spawn(&agent_name, routing, Some(identity_record))
                .await?;

            self.emit_agent_started(&agent_name)?;

            Ok::<SpawnResult, anyhow::Error>(SpawnResult {
                agent_dir: agent_dir.clone(),
                agent_name,
                issue_title: issue.title,
                agent_type: options.agent_type,
            })
        })
        .await
        .map_err(|_| {
            let msg = format!("spawn_agent timed out after {}s", SPAWN_TIMEOUT.as_secs());
            warn!(issue_id = %issue_id_log, error = %msg, "spawn_agent timed out");
            anyhow::Error::new(TimeoutError { message: msg })
        })??;

        info!(issue_id = %issue_id_log, "spawn_agent completed successfully");
        Ok(result)
    }

    /// Spawn multiple agents.
    #[tracing::instrument(skip(self, options))]
    pub async fn spawn_agents(
        &self,
        issue_ids: &[String],
        options: &SpawnOptions,
        caller_bb: &BirthBranch,
    ) -> BatchSpawnResult {
        let mut result = BatchSpawnResult {
            spawned: Vec::new(),
            failed: Vec::new(),
        };

        for issue_id_str in issue_ids {
            // Parse issue ID
            match IssueNumber::try_from(issue_id_str.clone()) {
                Ok(issue_number) => {
                    match self.spawn_agent(issue_number, options, caller_bb).await {
                        Ok(spawn_result) => result.spawned.push(spawn_result),
                        Err(e) => {
                            warn!(issue_id = issue_id_str, error = %e, "Failed to spawn agent");
                            result.failed.push((issue_id_str.clone(), e.to_string()));
                        }
                    }
                }
                Err(e) => {
                    warn!(issue_id = issue_id_str, error = %e, "Invalid issue number");
                    result.failed.push((issue_id_str.clone(), e.to_string()));
                }
            }
        }

        result
    }

    /// Spawn a named teammate with a direct prompt.
    ///
    /// Idempotent on teammate name: if already running, returns existing info.
    /// If config entry exists but tmux window is dead, cleans stale entry and respawns.
    /// No per-agent directories or MCP configs — agents share the repo's config.
    /// State lives in Teams config.json + tmux window only.
    #[tracing::instrument(skip(self, options), fields(name = %options.name.as_str()))]
    pub async fn spawn_gemini_teammate(
        &self,
        options: &SpawnGeminiTeammateOptions,
        caller_bb: &BirthBranch,
    ) -> Result<SpawnResult> {
        info!(name = %options.name, timeout_sec = SPAWN_TIMEOUT.as_secs(), "Starting spawn_gemini_teammate");

        let result = timeout(SPAWN_TIMEOUT, async {
            self.resolve_tmux_session()?;

            let effective_project_dir = self.effective_project_dir(options.subrepo.as_deref())?;

            // Sanitize name and construct typed identity
            let identity = AgentIdentity::new(slugify(options.name.as_str()), options.agent_type);
            let agent_name = identity.internal_name();
            let display_name = identity.display_name();

            // Idempotency check: if tmux window is alive, return existing info
            let tab_alive = self.is_tmux_window_alive(&display_name).await;

            info!(
                name = %options.name,
                agent_name = %agent_name,
                tab_alive,
                "Idempotency check"
            );

            if tab_alive {
                info!(name = %options.name, "Teammate already running, returning existing");
                return Ok(SpawnResult {
                    agent_dir: PathBuf::new(),
                    agent_name,
                    issue_title: options.name.to_string(),
                    agent_type: options.agent_type,
                });
            }

            // Determine base branch
            let base_branch = if let Some(ref b) = options.base_branch {
                BranchName::from(b.as_str())
            } else {
                // Default to current branch
                let current_branch_output = Command::new("git")
                    .args(["rev-parse", "--abbrev-ref", "HEAD"])
                    .current_dir(&effective_project_dir)
                    .output()
                    .await
                    .context("Failed to get current branch")?;
                let branch_str = String::from_utf8_lossy(&current_branch_output.stdout)
                    .trim()
                    .to_string();
                BranchName::from(branch_str.as_str())
            };

            // Use '.' separator to avoid directory/file conflicts in git refs
            // and avoid ambiguity with '-' word separators in slugs.
            // Branch includes type suffix so rsplit_once('.') yields the AgentName directly.
            let branch_name = BranchName::from(format!("{}.{}", base_branch, agent_name).as_str());
            let worktree_path = self.worktree_base.join(agent_name.as_str());

            self.create_worktree_checked(&worktree_path, &branch_name, &base_branch)
                .await?;

            let role = crate::domain::Role::dev();
            let mut env_vars = self.common_spawn_env(&agent_name, &branch_name, &role);

            // Write per-agent MCP config into the worktree
            self.write_agent_mcp_config(
                &effective_project_dir,
                &worktree_path,
                options.agent_type,
                &role,
            )
            .await?;

            // For Gemini agents, point at worktree settings via env var and pre-trust folder
            if options.agent_type == AgentType::Gemini {
                let settings_path = worktree_path.join(".gemini").join("settings.json");
                env_vars.insert(
                    "GEMINI_CLI_SYSTEM_SETTINGS_PATH".to_string(),
                    settings_path.to_string_lossy().to_string(),
                );
                Self::gemini_trust_folder(&worktree_path).await;
            }

            let window_id = self
                .new_tmux_window(
                    &display_name,
                    &worktree_path,
                    options.agent_type,
                    Some(&options.prompt),
                    env_vars,
                )
                .await?;

            // Store window_id for message delivery and cleanup
            let routing = RoutingInfo::window(window_id);
            let effective_birth = self.effective_birth_branch(Some(caller_bb));
            let child_birth = effective_birth.child(agent_name.as_str());
            let identity_record = AgentIdentityRecord {
                agent_name: agent_name.clone(),
                slug: Slug::from(identity.slug()),
                agent_type: options.agent_type,
                birth_branch: child_birth,
                parent_branch: effective_birth,
                working_dir: worktree_path.clone(),
                display_name: display_name.clone(),
                topology: Topology::WorktreePerAgent,
            };
            self.finalize_spawn(&agent_name, routing, Some(identity_record))
                .await?;

            self.emit_agent_started(&agent_name)?;

            Ok::<SpawnResult, anyhow::Error>(SpawnResult {
                agent_dir: PathBuf::new(),
                agent_name,
                issue_title: options.name.to_string(),
                agent_type: options.agent_type,
            })
        })
        .await
        .map_err(|_| {
            let msg = format!(
                "spawn_gemini_teammate timed out after {}s",
                SPAWN_TIMEOUT.as_secs()
            );
            warn!(name = %options.name, error = %msg, "spawn_gemini_teammate timed out");
            anyhow::Error::new(TimeoutError { message: msg })
        })??;

        info!(name = %options.name, "spawn_gemini_teammate completed successfully");
        Ok(result)
    }

    /// Generate settings.json content for a Gemini worker.
    ///
    /// Constructs the JSON configuration including MCP server connection and lifecycle hooks.
    /// Note: Gemini hooks must be PascalCase (e.g. AfterAgent).
    ///
    /// `context_path` is an optional absolute path to the role context file.
    /// Using an absolute path ensures workers spawned from worktrees can find the context.
    pub(crate) fn generate_gemini_worker_settings(
        agent_name: &str,
        context_path: Option<&Path>,
        extra_mcp_servers: &HashMap<String, serde_json::Value>,
    ) -> serde_json::Value {
        let mut context_files = vec![serde_json::Value::String("GEMINI.md".to_string())];
        if let Some(path) = context_path {
            context_files.push(serde_json::Value::String(
                path.to_string_lossy().to_string(),
            ));
        }
        let mut settings = serde_json::json!({
            "mcpServers": {
                "exomonad": {
                    "type": "stdio",
                    "command": "exomonad",
                    "args": ["mcp-stdio", "--role", "worker", "--name", agent_name]
                }
            },
            "context": {
                "fileName": context_files
            },
            "hooks": {
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
            }
        });
        if let Some(servers) = settings["mcpServers"].as_object_mut() {
            for (k, v) in extra_mcp_servers {
                servers.insert(k.clone(), v.clone());
            }
        }
        settings
    }

    /// Generate opencode.json content for an OpenCode agent.
    ///
    /// Constructs the JSON configuration including MCP server connection, role instructions,
    /// and plugin registration pointing at `.exo/opencode-plugin`. The plugin package files
    /// must be written separately via `write_opencode_plugin_files`.
    pub fn generate_opencode_tl_settings(
        agent_name: &str,
        role: &str,
        extra_mcp_servers: &HashMap<String, serde_json::Value>,
    ) -> serde_json::Value {
        let mut mcp_servers = serde_json::Map::new();
        mcp_servers.insert(
            "exomonad".to_string(),
            serde_json::json!({
                "type": "local",
                "command": ["exomonad", "mcp-stdio", "--role", role, "--name", agent_name]
            }),
        );
        for (k, v) in extra_mcp_servers {
            mcp_servers.insert(k.clone(), v.clone());
        }

        // instructions must be an array per OpenCode's schema.
        // Only inject TL instructions for root/tl roles; dev/worker roles get no override.
        let instructions = match role {
            "root" | "tl" => serde_json::json!([OPENCODE_TL_INSTRUCTIONS]),
            _ => serde_json::json!([OPENCODE_DEV_INSTRUCTIONS]),
        };
        serde_json::json!({
            "mcp": mcp_servers,
            "instructions": instructions,
            "plugin": ["./.exo/opencode-plugin"],
        })
    }

    /// Write the exomonad OpenCode plugin package to `<dir>/.exo/opencode-plugin/`.
    ///
    /// Creates `index.ts` (the TypeScript bridge) and `package.json`. The plugin
    /// is referenced by `opencode.json` via `"plugin": ["./.exo/opencode-plugin"]`.
    pub async fn write_opencode_plugin_files(dir: &Path) -> Result<()> {
        use crate::opencode_plugin::{OPENCODE_PLUGIN_PKG_JSON, OPENCODE_PLUGIN_TS};
        let plugin_dir = dir.join(".exo/opencode-plugin");
        fs::create_dir_all(&plugin_dir).await?;
        fs::write(plugin_dir.join("index.ts"), OPENCODE_PLUGIN_TS).await?;
        fs::write(plugin_dir.join("package.json"), OPENCODE_PLUGIN_PKG_JSON).await?;
        info!(path = %plugin_dir.display(), "Wrote OpenCode plugin files");
        Ok(())
    }

    /// Spawn a worker agent in the current worktree (no branch/worktree).
    #[instrument(skip_all, fields(name = %options.name, agent_type = %options.agent_type.suffix()))]
    pub async fn spawn_worker(
        &self,
        options: &SpawnWorkerOptions,
        ctx: &crate::effects::EffectContext,
    ) -> Result<SpawnResult> {
        let agent_type = options.agent_type;
        info!(name = %options.name, agent_type = agent_type.suffix(), timeout_sec = SPAWN_TIMEOUT.as_secs(), "Starting spawn_worker");

        let result = timeout(SPAWN_TIMEOUT, async {
            self.resolve_tmux_session()?;

            // Sanitize name and construct typed identity
            let identity = AgentIdentity::new(slugify(options.name.as_str()), agent_type);
            let agent_name = identity.internal_name();
            let display_name = identity.display_name();

            // Idempotency: check if agent config dir already exists (workers are panes, not tabs)
            let agent_config_dir = self.project_dir()
                .join(".exo")
                .join("agents")
                .join(agent_name.as_str());
            let settings_path = agent_config_dir.join("settings.json");
            if settings_path.exists() {
                // Check tmux pane liveness — settings.json can outlive the pane
                let pane_alive = match RoutingInfo::read_from_dir(&agent_config_dir).await {
                    Ok(routing) => match routing.pane_id {
                        Some(ref pane_id) => self.tmux()?.pane_exists(pane_id).await.unwrap_or(false),
                        None => false,
                    },
                    Err(_) => false,
                };
                if pane_alive {
                    info!(name = %options.name, "Worker pane still alive, returning existing");
                    return Ok(SpawnResult {
                        agent_dir: PathBuf::new(),
                        agent_name,
                        issue_title: options.name.to_string(),
                        agent_type,
                    });
                }
                // Stale: pane is dead but config dir remains. Clean up and respawn.
                info!(name = %options.name, path = %agent_config_dir.display(), "Stale worker detected (pane dead), cleaning up and respawning");
                if let Err(e) = fs::remove_dir_all(&agent_config_dir).await {
                    warn!(name = %options.name, error = %e, "Failed to clean up stale worker config dir");
                }
            }

            let role = crate::domain::Role::worker();
            let parent_bb = self.effective_birth_branch(Some(&ctx.birth_branch));
            let session_branch = BranchName::from(parent_bb.as_str());
            let mut env_vars = self.common_spawn_env(&agent_name, &session_branch, &role);

            fs::create_dir_all(&agent_config_dir).await?;

            // Legacy .birth_branch file for serve.rs fallback resolution.
            // identity.json (written via finalize_spawn) is the canonical source,
            // but keep this for backward compatibility with older server instances.
            let parent_bb = self.effective_birth_branch(Some(&ctx.birth_branch));
            fs::write(agent_config_dir.join(".birth_branch"), parent_bb.as_str()).await?;

            match agent_type {
                AgentType::Gemini => {
                    let context_path = self.resolve_role_context(&role);
                    let settings = Self::generate_gemini_worker_settings(agent_name.as_str(), context_path.as_deref(), &self.extra_mcp_servers);
                    fs::write(&settings_path, serde_json::to_string_pretty(&settings)?).await?;
                    info!(path = %settings_path.display(), agent_name = %agent_name, "Wrote worker Gemini settings to agent config dir");
                    env_vars.insert(
                        "GEMINI_CLI_SYSTEM_SETTINGS_PATH".to_string(),
                        settings_path.to_string_lossy().to_string(),
                    );
                    let caller_worktree_for_trust = self.project_dir().join(&ctx.working_dir);
                    Self::gemini_trust_folder(&caller_worktree_for_trust).await;
                }
                AgentType::OpenCode => {
                    // Write worker-specific opencode.json so the worker gets
                    // its own role/name (not the caller's root config, which
                    // lacks notify_parent and other worker tools).
                    let worker_config = Self::generate_opencode_tl_settings(
                        agent_name.as_str(),
                        "worker",
                        &self.extra_mcp_servers,
                    );
                    let opencode_json_path = agent_config_dir.join("opencode.json");
                    fs::write(&opencode_json_path, serde_json::to_string_pretty(&worker_config)?).await?;
                    Self::write_opencode_plugin_files(&agent_config_dir).await?;
                    info!(path = %opencode_json_path.display(), agent_name = %agent_name, "Wrote worker opencode.json and plugin to agent config dir");
                }
                AgentType::Codex => {
                    self.write_codex_config_files(
                        &agent_config_dir,
                        &role,
                        &agent_name,
                        self.spawn_agent_model(),
                        &self.extra_mcp_servers,
                    )
                    .await?;
                    info!(path = %agent_config_dir.join(".codex/config.toml").display(), agent_name = %agent_name, "Wrote worker Codex config to agent config dir");
                }
                _ => {}
            }

            // Resolve caller's context (tab and worktree) from its context.
            let caller_tab = resolve_own_tab_name(ctx);
            let caller_worktree = ctx.working_dir.clone();
            let absolute_worktree = self.project_dir().join(&caller_worktree);

            // Config-discovered runtimes run in their agent config dir so they
            // receive the worker role/name instead of inheriting the caller's
            // project config.
            let worker_cwd = match agent_type {
                AgentType::OpenCode | AgentType::Codex => agent_config_dir.clone(),
                _ => absolute_worktree.clone(),
            };

            // Workers are panes in the parent's tab — pane_id is the stable identifier.
            // Prompt goes through a temp file to avoid shell quoting issues.
            let pane_id = self.new_tmux_pane(
                &display_name,
                &worker_cwd,
                agent_type,
                Some(&options.prompt),
                env_vars,
                Some(&caller_tab),
                Some(&options.claude_flags),
            )
            .await?;

            // Store pane_id for message delivery and cleanup
            let routing = RoutingInfo::pane(pane_id, &caller_tab);
            let parent_bb = self.effective_birth_branch(Some(&ctx.birth_branch));
            let identity_record = AgentIdentityRecord {
                agent_name: agent_name.clone(),
                slug: Slug::from(identity.slug()),
                agent_type,
                birth_branch: parent_bb.clone(),
                parent_branch: parent_bb,
                working_dir: ctx.working_dir.clone(),
                display_name: display_name.clone(),
                topology: Topology::SharedDir,
            };
            self.finalize_spawn(&agent_name, routing, Some(identity_record))
                .await?;

            self.emit_agent_started(&agent_name)?;

            Ok::<SpawnResult, anyhow::Error>(SpawnResult {
                agent_dir: PathBuf::new(),
                agent_name,
                issue_title: options.name.to_string(),
                agent_type,
            })
        })
        .await
        .map_err(|_| {
            let msg = format!("spawn_worker timed out after {}s", SPAWN_TIMEOUT.as_secs());
            warn!(name = %options.name, error = %msg, "spawn_worker timed out");
            anyhow::Error::new(TimeoutError { message: msg })
        })??;

        info!(name = %options.name, "spawn_worker completed successfully");
        Ok(result)
    }

    /// Spawn a subtree agent (Claude-only) in a new git worktree.
    #[instrument(skip_all, fields(slug = %options.branch_name, agent_type = "claude"))]
    pub async fn spawn_subtree(
        &self,
        options: &SpawnSubtreeOptions,
        caller_bb: &BirthBranch,
    ) -> Result<SpawnResult> {
        info!(branch_name = %options.branch_name, timeout_sec = SPAWN_TIMEOUT.as_secs(), "Starting spawn_subtree");

        let result = timeout(SPAWN_TIMEOUT, async {
            self.resolve_tmux_session()?;

            let effective_birth = self.effective_birth_branch(Some(caller_bb));

            // Depth check using typed birth-branch.
            let depth = effective_birth.depth();

            if depth >= 3 {
                return Err(anyhow!("Subtree depth limit reached (max 3). Current birth-branch: {}, depth: {}", effective_birth, depth));
            }

            let effective_project_dir = self.project_dir();

            // Sanitize branch name and construct typed identity
            let agent_type = options.agent_type;
            let identity = AgentIdentity::new(slugify(&options.branch_name), agent_type);
            let agent_name = identity.internal_name();
            let display_name = identity.display_name();

            // Idempotency check: if tmux window is alive, return existing info
            let tab_alive = self.is_tmux_window_alive(&display_name).await;
            if tab_alive {
                info!(slug = %identity.slug(), "Subtree already running, returning existing");
                return Ok(SpawnResult {
                    agent_dir: self.worktree_base.join(agent_name.as_str()),
                    agent_name,
                    issue_title: options.branch_name.clone(),
                    agent_type,
                });
            }

            // Parent branch derived from typed birth-branch.
            let current_branch = BranchName::from(effective_birth.as_parent_branch());

            // Ensure a remote exists for local-only workflows
            ensure_remote_exists(effective_project_dir).await;

            // Push parent branch so child PRs can reference it as base
            ensure_branch_pushed(self.git_wt(), &current_branch, effective_project_dir).await;

            // Branch: {current_branch}.{agent_name} (suffixed for unified namespace)
            let child_birth = effective_birth.child(agent_name.as_str());
            let branch_name = child_birth.to_string();

            // Path resolution: working_dir overrides the default worktree location.
            // standalone_repo: git init (fresh .git boundary) instead of git worktree add.
            // These are orthogonal: working_dir controls WHERE, standalone_repo controls HOW.
            let (worktree_path, is_custom_dir) = if let Some(ref custom_dir) = options.working_dir {
                (custom_dir.clone(), true)
            } else {
                (self.worktree_base.join(agent_name.as_str()), false)
            };

            if options.standalone_repo {
                self.init_standalone_repo(&worktree_path).await?;
                if !options.allowed_dirs.is_empty() {
                    self.copy_allowed_dirs(&worktree_path, &options.allowed_dirs).await?;
                }
            } else if !is_custom_dir {
                let branch = BranchName::from(branch_name.as_str());
                self.create_worktree_checked(&worktree_path, &branch, &current_branch).await?;
            }

            self.create_socket_symlink(&worktree_path).await;

            let default_tl = crate::domain::Role::tl();
            let role = options.role.as_ref().unwrap_or(&default_tl);

            // Copy role context into worktree.
            // Must be a copy, not a symlink — symlinks escape the worktree boundary
            // and cause Claude Code to discover parent context files.
            if let Some(context_src) = self.resolve_role_context(role) {
                let spawn_type = self.spawn_agent_type.suffix();
                match agent_type {
                    AgentType::Claude => {
                        let rules_dir = worktree_path.join(".claude/rules");
                        let _ = fs::create_dir_all(&rules_dir).await;
                        let dest = rules_dir.join("exomonad_role.md");
                        let _ = fs::remove_file(&dest).await;
                        match Self::copy_role_context_with_interpolation(&context_src, &dest, spawn_type).await {
                            Ok(_) => info!(role = %role, src = %context_src.display(), dest = %dest.display(), "Copied role context into worktree"),
                            Err(e) => warn!(role = %role, error = %e, "Failed to copy role context (non-fatal)"),
                        }
                    }
                    AgentType::Gemini => {
                        let dest_dir = worktree_path.join(format!(".exo/roles/{}/context", self.wasm_name));
                        let _ = fs::create_dir_all(&dest_dir).await;
                        let dest = dest_dir.join(format!("{}.md", role));
                        let _ = fs::remove_file(&dest).await;
                        match fs::copy(&context_src, &dest).await {
                            Ok(_) => info!(role = %role, src = %context_src.display(), dest = %dest.display(), "Copied role context into Gemini worktree"),
                            Err(e) => warn!(role = %role, error = %e, "Failed to copy Gemini role context (non-fatal)"),
                        }
                    }
                    AgentType::OpenCode => {
                        let rules_dir = worktree_path.join(".claude/rules");
                        let _ = fs::create_dir_all(&rules_dir).await;
                        let dest = rules_dir.join("exomonad_role.md");
                        let _ = fs::remove_file(&dest).await;
                        match Self::copy_role_context_with_interpolation(&context_src, &dest, spawn_type).await {
                            Ok(_) => info!(role = %role, src = %context_src.display(), dest = %dest.display(), "Copied role context into OpenCode worktree"),
                            Err(e) => warn!(role = %role, error = %e, "Failed to copy OpenCode role context (non-fatal)"),
                        }
                    }
                    AgentType::Codex => {
                        let dest_dir = worktree_path.join(".codex");
                        let _ = fs::create_dir_all(&dest_dir).await;
                        let dest = dest_dir.join("exomonad_role.md");
                        let _ = fs::remove_file(&dest).await;
                        match Self::copy_role_context_with_interpolation(&context_src, &dest, spawn_type).await {
                            Ok(_) => info!(role = %role, src = %context_src.display(), dest = %dest.display(), "Copied role context into Codex worktree"),
                            Err(e) => warn!(role = %role, error = %e, "Failed to copy Codex role context (non-fatal)"),
                        }
                    }
                    AgentType::Shoal | AgentType::Process => {}
                }
            }

            let session_branch = BranchName::from(branch_name.as_str());
            let mut env_vars = self.common_spawn_env(&agent_name, &session_branch, role);

            // Write agent MCP config
            self.write_agent_mcp_config(effective_project_dir, &worktree_path, agent_type, role)
                .await?;

            match agent_type {
                AgentType::Claude => {
                    // Enable Claude Code Agent Teams for native inter-agent messaging
                    env_vars.insert(
                        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS".to_string(),
                        "1".to_string(),
                    );

                    // Write .claude/settings.local.json with hooks (SessionStart registers UUID for --fork-session)
                    let binary_path = crate::util::find_exomonad_binary();
                    crate::hooks::HookConfig::write_persistent(&worktree_path, &binary_path, options.permissions.as_ref(), Some(self.project_dir()))
                        .map_err(|e| anyhow!("Failed to write hook config in worktree: {}", e))?;
                    info!(worktree = %worktree_path.display(), "Wrote hook configuration for spawned Claude agent");

                    // Symlink Claude project dir so child can discover parent's sessions for --fork-session.
                    // Claude Code encodes paths via [^a-zA-Z0-9] → '-' (lossy regex replacement).
                    // Without this symlink, --resume --fork-session fails with "no conversation ID found".
                    {
                        let claude_projects_dir = dirs::home_dir()
                            .unwrap_or_default()
                            .join(".claude")
                            .join("projects");
                        let encode_path = |p: &Path| -> String {
                            p.to_string_lossy()
                                .chars()
                                .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
                                .collect()
                        };
                        let canonical_project_dir = self.project_dir().canonicalize().unwrap_or_else(|_| self.project_dir().to_path_buf());
                        let parent_encoded = encode_path(&canonical_project_dir);
                        let worktree_encoded = encode_path(&worktree_path);
                        let parent_project = claude_projects_dir.join(&parent_encoded);
                        let child_project = claude_projects_dir.join(&worktree_encoded);
                        if parent_project.exists() && !child_project.exists() {
                            match std::os::unix::fs::symlink(&parent_project, &child_project) {
                                Ok(()) => info!(
                                    parent = %parent_encoded,
                                    child = %worktree_encoded,
                                    "Symlinked Claude project dir for session inheritance"
                                ),
                                Err(e) => warn!(
                                    parent = %parent_encoded,
                                    child = %worktree_encoded,
                                    error = %e,
                                    "Failed to symlink Claude project dir (fork-session may not work)"
                                ),
                            }
                        }
                    }
                }
                AgentType::OpenCode => {
                    let opencode_config = Self::generate_opencode_tl_settings(
                        agent_name.as_str(),
                        role.as_str(),
                        &self.extra_mcp_servers,
                    );
                    fs::write(
                        worktree_path.join("opencode.json"),
                        serde_json::to_string_pretty(&opencode_config)?,
                    ).await?;
                    Self::write_opencode_plugin_files(&worktree_path).await?;
                    info!(worktree = %worktree_path.display(), "Wrote opencode.json and plugin for OpenCode TL agent");
                }
                _ => {}
            }

            // Build task prompt with worktree context warning
            let mut task_with_context = format!(
                "You are now in worktree {} on branch {}. All file paths from your inherited context are STALE — use relative paths only and re-read files before editing.\n\n{}",
                worktree_path.display(), branch_name, options.task
            );

            if options.standalone_repo && !options.allowed_dirs.is_empty() {
                task_with_context.push_str("\n\nShared technical dependencies are available as read-only reference in `.exo/context/`. Do not modify files in this directory.");
            }

            // Inject workspace-level agent.md if present
            let agent_md_path = self.project_dir().join("agent.md");
            if agent_md_path.exists() {
                if let Ok(content) = tokio::fs::read_to_string(&agent_md_path).await {
                    task_with_context.push_str("\n\n---\n\n# Workspace Context (from agent.md)\n\n");
                    task_with_context.push_str(&content);
                }
            }

            // Open tmux window with cwd = worktree_path
            let routing = match agent_type {
                AgentType::OpenCode => {
                    // OpenCode workers run in a tmux window like Claude workers.
                    // `build_agent_command` generates `opencode run "$(cat '<prompt_file>')"`.
                    // MCP is configured via opencode.json in the worktree.
                    // Messages are delivered via tmux STDIN injection (same as all other agents).
                    let window_id = self.new_tmux_window_inner(
                        &display_name,
                        &worktree_path,
                        agent_type,
                        Some(&task_with_context),
                        env_vars,
                        None, // no fork_session for OpenCode
                        None, // no claude_flags
                        options.model.as_deref(),
                    )
                    .await
                    .map_err(|e| {
                        warn!(name = %identity.slug(), error = %e, "tmux window creation failed, rolling back");
                        e
                    })?;
                    RoutingInfo::window(window_id)
                }
                AgentType::Codex => {
                    let fork_id = options.parent_session_id.as_ref().map(|id| id.as_str());
                    let window_id = self.new_tmux_window_inner(
                        &display_name,
                        &worktree_path,
                        agent_type,
                        Some(&task_with_context),
                        env_vars,
                        fork_id,
                        None,
                        options.model.as_deref(),
                    )
                    .await
                    .map_err(|e| {
                        warn!(name = %identity.slug(), error = %e, "tmux window creation failed, rolling back");
                        e
                    })?;
                    RoutingInfo::window(window_id)
                }
                AgentType::Claude => {
                    // Determine fork mode from parent_session_id
                    let fork_id = options.parent_session_id.as_ref().map(|id| id.as_str());
                    let window_id = self.new_tmux_window_inner(
                        &display_name,
                        &worktree_path,
                        agent_type,
                        Some(&task_with_context),
                        env_vars,
                        fork_id,
                        Some(&options.claude_flags),
                        options.model.as_deref(),
                    )
                    .await
                    .map_err(|e| {
                        warn!(name = %identity.slug(), error = %e, "tmux window creation failed, rolling back");
                        e
                    })?;
                    RoutingInfo::window(window_id)
                }
                _ => {
                    let window_id = self.new_tmux_window(
                        &display_name,
                        &worktree_path,
                        agent_type,
                        Some(&task_with_context),
                        env_vars,
                    )
                    .await
                    .map_err(|e| {
                        warn!(name = %identity.slug(), error = %e, "tmux window creation failed, rolling back");
                        e
                    })?;
                    RoutingInfo::window(window_id)
                }
            };
            let identity_record = AgentIdentityRecord {
                agent_name: agent_name.clone(),
                slug: Slug::from(identity.slug()),
                agent_type,
                birth_branch: child_birth,
                parent_branch: effective_birth,
                working_dir: worktree_path.clone(),
                display_name: display_name.clone(),
                topology: Topology::WorktreePerAgent,
            };
            self.finalize_spawn(&agent_name, routing, Some(identity_record))
                .await?;

            Ok::<SpawnResult, anyhow::Error>(SpawnResult {
                agent_dir: worktree_path.clone(),
                agent_name,
                issue_title: options.branch_name.clone(),
                agent_type,
            })
        })
        .await
        .map_err(|_| {
            let msg = format!("spawn_subtree timed out after {}s", SPAWN_TIMEOUT.as_secs());
            warn!(branch_name = %options.branch_name, error = %msg, "spawn_subtree timed out");
            anyhow::Error::new(TimeoutError { message: msg })
        })??;

        info!(branch_name = %options.branch_name, "spawn_subtree completed successfully");
        Ok(result)
    }

    /// Spawn a Gemini leaf agent in a new git worktree.
    #[instrument(skip_all, fields(slug = %options.branch_name, agent_type = "gemini"))]
    pub async fn spawn_leaf_subtree(
        &self,
        options: &SpawnLeafOptions,
        caller_bb: &BirthBranch,
    ) -> Result<SpawnResult> {
        info!(branch_name = %options.branch_name, timeout_sec = SPAWN_TIMEOUT.as_secs(), "Starting spawn_leaf_subtree");

        let result = timeout(SPAWN_TIMEOUT, async {
            self.resolve_tmux_session()?;

            // No depth check for leaf nodes.

            let effective_birth = self.effective_birth_branch(Some(caller_bb));
            let effective_project_dir = self.project_dir();

            // Parent branch derived from typed birth-branch.
            let current_branch = BranchName::from(effective_birth.as_parent_branch());

            // Sanitize branch name and construct typed identity
            let agent_type = options.agent_type;
            let identity = AgentIdentity::new(slugify(&options.branch_name), agent_type);
            let agent_name = identity.internal_name();
            let display_name = identity.display_name();

            // Idempotency check
            let tab_alive = self.is_tmux_window_alive(&display_name).await;
            if tab_alive {
                info!(slug = %identity.slug(), "Leaf subtree already running, returning existing");
                return Ok(SpawnResult {
                    agent_dir: self.worktree_base.join(agent_name.as_str()),
                    agent_name,
                    issue_title: options.branch_name.clone(),
                    agent_type,
                });
            }

            // Ensure a remote exists for local-only workflows
            ensure_remote_exists(effective_project_dir).await;

            // Push parent branch so child PRs can reference it as base
            ensure_branch_pushed(self.git_wt(), &current_branch, effective_project_dir).await;

            let child_birth = effective_birth.child(agent_name.as_str());
            let branch_name = BranchName::from(child_birth.to_string().as_str());

            let worktree_path = self.worktree_base.join(agent_name.as_str());
            let mut remove_worktree_on_spawn_failure = false;

            if options.standalone_repo {
                self.init_standalone_repo(&worktree_path).await?;
                remove_worktree_on_spawn_failure = true;
                if !options.allowed_dirs.is_empty() {
                    self.copy_allowed_dirs(&worktree_path, &options.allowed_dirs).await?;
                }
            } else if worktree_path.exists() {
                if !worktree_path.is_dir() {
                    return Err(anyhow!(
                        "Existing leaf worktree path is not a directory: {}",
                        worktree_path.display()
                    ));
                }
                info!(
                    worktree_path = %worktree_path.display(),
                    branch_name = %branch_name,
                    "Reusing existing leaf worktree"
                );
            } else {
                self.create_worktree_checked(&worktree_path, &branch_name, &current_branch).await?;
                remove_worktree_on_spawn_failure = true;
            }

            self.create_socket_symlink(&worktree_path).await;

            let default_dev = crate::domain::Role::dev();
            let role = options.role.as_ref().unwrap_or(&default_dev);
            let mut env_vars = self.common_spawn_env(&agent_name, &branch_name, role);
            self.write_agent_mcp_config(effective_project_dir, &worktree_path, agent_type, role)
                .await?;

            // Set GEMINI_CLI_SYSTEM_SETTINGS_PATH and pre-trust folder
            let settings_path = worktree_path.join(".gemini").join("settings.json");
            env_vars.insert(
                "GEMINI_CLI_SYSTEM_SETTINGS_PATH".to_string(),
                settings_path.to_string_lossy().to_string(),
            );
            Self::gemini_trust_folder(&worktree_path).await;

            let mut task = options.task.clone();
            if options.standalone_repo && !options.allowed_dirs.is_empty() {
                task.push_str("\n\nShared technical dependencies are available as read-only reference in `.exo/context/`. Do not modify files in this directory.");
            }

            // Open tmux window (not pane)
            // Task already includes leaf completion protocol — rendered by Haskell Prompt builder.
            let agent_config_dir = self.project_dir().join(".exo").join("agents").join(agent_name.as_str());
            let window_id = match self.new_tmux_window(
                &display_name,
                &worktree_path,
                agent_type,
                Some(&task),
                env_vars,
            )
            .await {
                Ok(wid) => wid,
                Err(e) => {
                    warn!(name = %identity.slug(), error = %e, "tmux window creation failed, rolling back");
                    let _ = fs::remove_dir_all(&agent_config_dir).await;
                    if remove_worktree_on_spawn_failure && worktree_path.exists() {
                        let git_wt = self.git_wt().clone();
                        let path = worktree_path.clone();
                        let _ = tokio::task::spawn_blocking(move || git_wt.remove_workspace(&path)).await;
                    }
                    return Err(e);
                }
            };

            // Store window_id for message delivery and cleanup
            let routing = RoutingInfo::window(window_id);
            let identity_record = AgentIdentityRecord {
                agent_name: agent_name.clone(),
                slug: Slug::from(identity.slug()),
                agent_type,
                birth_branch: child_birth,
                parent_branch: effective_birth,
                working_dir: worktree_path.clone(),
                display_name: display_name.clone(),
                topology: Topology::WorktreePerAgent,
            };
            self.finalize_spawn(&agent_name, routing, Some(identity_record))
                .await?;

            Ok::<SpawnResult, anyhow::Error>(SpawnResult {
                agent_dir: worktree_path.clone(),
                agent_name,
                issue_title: options.branch_name.clone(),
                agent_type,
            })
        })
        .await
        .map_err(|_| {
            let msg = format!("spawn_leaf_subtree timed out after {}s", SPAWN_TIMEOUT.as_secs());
            warn!(branch_name = %options.branch_name, error = %msg, "spawn_leaf_subtree timed out");
            anyhow::Error::new(TimeoutError { message: msg })
        })??;

        info!(branch_name = %options.branch_name, "spawn_leaf_subtree completed successfully");
        Ok(result)
    }

    /// Spawn a reviewer agent for a sibling PR.
    ///
    /// Creates a tmux window with `role=reviewer` working from the project root.
    /// The reviewer examines `git diff base..{pr_branch}`, writes review comments
    /// to `.exo/reviews/pr_{N}.json`, and approves or requests changes.
    ///
    /// Use this when the TL receives `[PR READY]` from a child agent.
    #[instrument(skip_all, fields(pr_number = pr_entry.number))]
    pub async fn spawn_reviewer_subtree(
        &self,
        pr_entry: &crate::services::file_pr_local::PrEntry,
        caller_bb: &BirthBranch,
    ) -> Result<SpawnResult> {
        let context_section = if self.reviewer_context.is_empty() {
            String::new()
        } else {
            format!(
                "\n\nRead first:\n{}",
                self.reviewer_context
                    .iter()
                    .map(|p| format!("- {p}"))
                    .collect::<Vec<_>>()
                    .join("\n")
            )
        };
        let task = format!(
            "Review PR #{}: {}\n\nBranch: {}\nBase: {}\nAuthor: {}{}",
            pr_entry.number,
            pr_entry.title,
            pr_entry.head_branch,
            pr_entry.base_branch,
            pr_entry.author_agent,
            context_section,
        );
        let branch_name = format!("review-pr-{}", pr_entry.number);

        // Compute the reviewer's own identity and path — same derivation spawn_subtree uses
        // internally so the MCP config agent_name matches the directory name.
        let agent_type = self.reviewer_agent_type;
        let identity = AgentIdentity::new(slugify(&branch_name), agent_type);
        let reviewer_path = self.worktree_base.join(identity.internal_name().as_str());

        // Create a detached-HEAD worktree at the PR branch tip unless it already exists.
        // Detached so we don't compete with the worker's branch; the reviewer never commits.
        // This prevents clobbering the worker's opencode.json/MCP config while both run.
        if !reviewer_path.exists() {
            let git_wt = self.git_wt().clone();
            let path = reviewer_path.clone();
            let at_ref = pr_entry.head_branch.clone();
            let name = identity.internal_name().to_string();
            tokio::task::spawn_blocking(move || {
                git_wt.create_workspace_detached(&path, &at_ref, &name)
            })
            .await
            .context("tokio join error creating reviewer worktree")?
            .context("Failed to create reviewer worktree")?;
        }

        let options = SpawnSubtreeOptions {
            task,
            branch_name,
            parent_session_id: None,
            role: Some(crate::domain::Role::reviewer()),
            agent_type,
            claude_flags: ClaudeSpawnFlags::default(),
            working_dir: Some(reviewer_path),
            permissions: Some(AgentPermissions {
                allow: vec![],
                deny: vec![
                    "fork_wave".into(),
                    "spawn_leaf".into(),
                    "spawn_worker".into(),
                    "merge_pr".into(),
                    "file_pr".into(),
                ],
                default_mode: None,
            }),
            standalone_repo: false,
            allowed_dirs: vec![],
            model: self.reviewer_model.clone(),
        };

        self.spawn_subtree(&options, caller_bb).await
    }
}

#[async_trait::async_trait]
impl<
        C: crate::services::HasGitHubClient
            + crate::services::HasAcpRegistry
            + crate::services::HasOpencodeAcpRegistry
            + crate::services::HasTeamRegistry
            + crate::services::HasAgentResolver
            + crate::services::HasProjectDir
            + crate::services::HasGitWorktreeService
            + 'static,
    > crate::services::ReviewerSpawner for AgentControlService<C>
{
    async fn spawn_reviewer_for_pr(
        &self,
        pr: &crate::services::file_pr_local::PrEntry,
    ) -> anyhow::Result<()> {
        // Guard: if the author's worktree is gone, this PR is stale from a previous
        // session (crash or --recreate). Skip — no agent will respond to feedback.
        let worktree_path = self.worktree_base.join(&pr.author_agent);
        if !worktree_path.exists() {
            tracing::warn!(
                pr_number = pr.number,
                agent = %pr.author_agent,
                path = %worktree_path.display(),
                "Skipping reviewer spawn — author worktree absent (stale PR from previous session)"
            );
            return Ok(());
        }
        let caller_bb = BirthBranch::from(pr.base_branch.as_str());
        self.spawn_reviewer_subtree(pr, &caller_bb).await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_copy_allowed_dirs_validation() {
        let temp_dir = tempfile::tempdir().unwrap();
        let project_dir = temp_dir.path().to_path_buf();

        // Setup source dirs
        let shared_context = project_dir.join("shared-context");
        fs::create_dir_all(&shared_context).await.unwrap();
        fs::write(shared_context.join("ref.txt"), "context data")
            .await
            .unwrap();

        let agent_wt = project_dir.join("agent-wt");
        fs::create_dir_all(&agent_wt).await.unwrap();

        let git_wt = Arc::new(crate::services::git_worktree::GitWorktreeService::new(
            project_dir.clone(),
        ));
        let mut services = crate::services::Services::test();
        services.project_dir = project_dir.clone();
        services.git_wt = git_wt;
        let service = AgentControlService::new(Arc::new(services));

        // Test valid copy
        service
            .copy_allowed_dirs(&agent_wt, &["shared-context".to_string()])
            .await
            .unwrap();
        assert!(agent_wt
            .join(".exo/context/shared-context/ref.txt")
            .exists());

        // Test invalid paths (should skip but not fail)
        service
            .copy_allowed_dirs(
                &agent_wt,
                &["/absolute".to_string(), "../outside".to_string()],
            )
            .await
            .unwrap();
        assert!(!agent_wt.join(".exo/context/absolute").exists());
        assert!(!agent_wt.join(".exo/context/outside").exists());
    }

    #[test]
    fn test_claude_project_path_encoding() {
        // Claude Code encodes paths via [^a-zA-Z0-9] → '-'
        // Verified against actual ~/.claude/projects/ directory names.
        let encode = |s: &str| -> String {
            s.chars()
                .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
                .collect()
        };

        // Basic path
        assert_eq!(
            encode("/home/inanna/dev/exomonad"),
            "-home-inanna-dev-exomonad"
        );
        // Worktree path (dots and hyphens in segments)
        assert_eq!(
            encode("/home/inanna/dev/exomonad/.exo/worktrees/fork-session"),
            "-home-inanna-dev-exomonad--exo-worktrees-fork-session"
        );
        // Hidden dir (leading dot → double dash after parent separator)
        assert_eq!(
            encode("/home/inanna/.config/home-manager"),
            "-home-inanna--config-home-manager"
        );
        // Deep nested path with hyphens
        assert_eq!(
            encode("/home/inanna/dev/aegis-binder-diagnostic-framework"),
            "-home-inanna-dev-aegis-binder-diagnostic-framework"
        );
        // Path with spaces
        assert_eq!(
            encode("/home/user/My Projects/app"),
            "-home-user-My-Projects-app"
        );
    }
}
