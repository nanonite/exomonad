use super::*;

fn resume_spawn_lock() -> &'static tokio::sync::Mutex<()> {
    static LOCK: std::sync::OnceLock<tokio::sync::Mutex<()>> = std::sync::OnceLock::new();
    LOCK.get_or_init(|| tokio::sync::Mutex::new(()))
}

async fn persist_dispatch_intent(
    project_dir: &Path,
    agent_name: &AgentName,
    intent_id: Option<&str>,
) -> Result<()> {
    let Some(intent_id) = intent_id.map(str::trim).filter(|value| !value.is_empty()) else {
        return Ok(());
    };
    let agent_dir = project_dir.join(".exo/agents").join(agent_name.as_str());
    fs::create_dir_all(&agent_dir).await?;
    let temporary_path = agent_dir.join("dispatch_intent.tmp");
    fs::write(&temporary_path, intent_id).await?;
    fs::rename(temporary_path, agent_dir.join("dispatch_intent")).await?;
    Ok(())
}

fn parse_git_status_paths(stdout: &[u8]) -> Vec<String> {
    let records: Vec<&[u8]> = stdout
        .split(|byte| *byte == 0)
        .filter(|record| !record.is_empty())
        .collect();
    let mut paths = Vec::new();
    let mut index = 0;
    while index < records.len() {
        let record = records[index];
        if record.len() >= 4 {
            let status = &record[..2];
            paths.push(String::from_utf8_lossy(&record[3..]).into_owned());
            if status.iter().any(|byte| matches!(byte, b'R' | b'C')) {
                if let Some(previous_path) = records.get(index + 1) {
                    paths.push(String::from_utf8_lossy(previous_path).into_owned());
                    index += 1;
                }
            }
        }
        index += 1;
    }
    paths
}

fn is_tl_runtime_checkpoint(path: &str) -> bool {
    let normalized = path.strip_prefix("./").unwrap_or(path);
    normalized == ".exo/tl-loop" || normalized.starts_with(".exo/tl-loop/")
}

fn resolve_identity_working_dir(project_dir: &Path, working_dir: &Path) -> PathBuf {
    if working_dir.is_absolute() {
        working_dir.to_path_buf()
    } else {
        project_dir.join(working_dir)
    }
}

async fn find_existing_leaf_worktree_by_slug(
    worktree_base: &Path,
    slug: &str,
) -> Result<Option<(AgentIdentity, PathBuf)>> {
    let mut entries = match fs::read_dir(worktree_base).await {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };

    let mut candidates = Vec::new();
    while let Some(entry) = entries.next_entry().await? {
        let file_type = entry.file_type().await?;
        if !file_type.is_dir() {
            continue;
        }

        let name = entry.file_name().to_string_lossy().to_string();
        candidates.push((name, entry.path()));
    }

    candidates.sort_by(|left, right| left.0.cmp(&right.0));
    for (name, path) in candidates {
        let identity = AgentIdentity::from_internal_name(&name);
        if normalize_agent_slug(identity.slug()) == normalize_agent_slug(slug) {
            return Ok(Some((identity, path)));
        }
    }

    Ok(None)
}

fn reviewer_harness_denied_tools() -> Vec<String> {
    [
        "Edit",
        "Write",
        "MultiEdit",
        "NotebookEdit",
        "spawn_leaf",
        "spawn_worker",
        "merge_pr",
        "file_pr",
    ]
    .into_iter()
    .map(str::to_string)
    .collect()
}

async fn preflight_reviewer_hook_environment(project_dir: &Path) -> Result<()> {
    let binary_path = crate::util::find_exomonad_binary();
    if binary_path.components().count() > 1 && !binary_path.exists() {
        anyhow::bail!(
            "Reviewer hook preflight failed: exomonad hook binary not found at {}",
            binary_path.display()
        );
    }

    let socket_path = project_dir.join(".exo/server.sock");
    if !socket_path.exists() {
        anyhow::bail!(
            "Reviewer hook preflight failed: parent server socket missing at {}",
            socket_path.display()
        );
    }

    match tokio::time::timeout(
        std::time::Duration::from_secs(2),
        tokio::net::UnixStream::connect(&socket_path),
    )
    .await
    {
        Ok(Ok(_stream)) => Ok(()),
        Ok(Err(error)) => anyhow::bail!(
            "Reviewer hook preflight failed: parent server socket unreachable at {}: {}",
            socket_path.display(),
            error
        ),
        Err(_) => anyhow::bail!(
            "Reviewer hook preflight failed: timed out connecting to parent server socket at {}",
            socket_path.display()
        ),
    }
}

fn dirty_spawn_error(files: &[String]) -> anyhow::Error {
    let mut message = format!(
        "BLOCKED: cannot spawn agent into a dirty TL worktree. {} file(s) have uncommitted changes:",
        files.len()
    );
    for file in files {
        message.push_str("\n  ");
        message.push_str(file);
    }
    message.push_str("\nCommit the scaffold (per scaffold-fork-converge) or run `discard_worker_output` if throwaway, then retry. Workers spawn in-place and would inherit this state; dev-leaves fork from your branch HEAD and would not see your uncommitted work.");
    anyhow!(message)
}

async fn is_gitignored_path(worktree: &Path, file: &str) -> Result<bool> {
    let output = Command::new("git")
        .args(["check-ignore", "--no-index", "-q", "--", file])
        .current_dir(worktree)
        .output()
        .await
        .with_context(|| {
            format!(
                "failed to inspect gitignore rules in {}",
                worktree.display()
            )
        })?;

    match output.status.code() {
        Some(0) => Ok(true),
        Some(1) => Ok(false),
        _ => anyhow::bail!(
            "failed to inspect gitignore rules for {} in {}: {}",
            file,
            worktree.display(),
            String::from_utf8_lossy(&output.stderr).trim()
        ),
    }
}

async fn filter_gitignored_paths(worktree: &Path, files: Vec<String>) -> Result<Vec<String>> {
    let mut visible_files = Vec::new();
    for file in files {
        if !is_gitignored_path(worktree, &file).await? {
            visible_files.push(file);
        }
    }
    Ok(visible_files)
}

async fn ensure_clean_spawn_worktree(worktree: &Path) -> Result<()> {
    let output = Command::new("git")
        .args(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        .current_dir(worktree)
        .output()
        .await
        .with_context(|| format!("failed to inspect git status in {}", worktree.display()))?;

    if !output.status.success() {
        anyhow::bail!(
            "failed to inspect git status in {}: {}",
            worktree.display(),
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }

    let files = parse_git_status_paths(&output.stdout)
        .into_iter()
        .filter(|path| !is_tl_runtime_checkpoint(path))
        .collect();
    let files = filter_gitignored_paths(worktree, files).await?;
    if files.is_empty() {
        Ok(())
    } else {
        Err(dirty_spawn_error(&files))
    }
}

async fn verify_branch_head(
    project_dir: &Path,
    branch: &BranchName,
    expected_sha: &str,
) -> Result<()> {
    info!(branch = %branch, expected_sha, "Verifying resumed branch head");
    let revision = format!("{}^{{commit}}", branch.as_str());
    let output = Command::new("git")
        .args(["rev-parse", "--verify", revision.as_str()])
        .current_dir(project_dir)
        .output()
        .await
        .with_context(|| format!("failed to inspect head of branch {}", branch))?;
    if !output.status.success() {
        anyhow::bail!(
            "could not resolve head of resumed branch {}: {}",
            branch,
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    let actual_sha = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if actual_sha != expected_sha {
        anyhow::bail!(
            "resumed branch {} points at {}, expected {}",
            branch,
            actual_sha,
            expected_sha
        );
    }
    info!(branch = %branch, actual_sha, "Resumed branch head matches PR head SHA");
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ActiveWorker {
    name: String,
    age: String,
}

fn format_worker_age(duration: std::time::Duration) -> String {
    let seconds = duration.as_secs();
    if seconds < 60 {
        format!("{}s", seconds)
    } else if seconds < 60 * 60 {
        format!("{}m", seconds / 60)
    } else {
        format!("{}h", seconds / (60 * 60))
    }
}

fn active_worker_error(worker: &ActiveWorker) -> anyhow::Error {
    anyhow!(
        "BLOCKED: workers are sequential per CLAUDE.md and docs/decisions/agent-lifecycle-invariants.md. Active worker in this TL worktree: `{}` (spawned {} ago).\n\nOptions:\n1. Wait for the active worker's handoff before spawning the next.\n2. Use `spawn_leaf` for parallel work that warrants its own PR — dev-leaves have their own worktrees and don't share state.\n\nPer-worker attribution to allow parallel workers in one worktree is explicitly out of scope (see ADR § Out of Scope).",
        worker.name,
        worker.age
    )
}

pub(crate) const ROOT_CONTEXT_RELATIVE_PATH: &str = ".exo/roles/devswarm/context/root.md";

pub const CODEX_TL_RUNTIME_NOTES: &str = "\
## Codex Runtime Notes
- ExoMonad manages Codex TLs in tmux and routes messages through Codex's supported delivery paths, not Claude Code Teams inboxes.
- Codex hooks are shell-native and configured in `.codex/hooks.json` for PreToolUse, PostToolUse, and Stop. SessionStart is Claude-specific and is not part of the Codex hook set.
- If you manually restart Codex from the TL shell, restart with `codex --dangerously-bypass-approvals-and-sandbox --cd <project-root>` so ExoMonad hooks do not enter Codex's hook review queue.
- Context inheritance for Codex children uses `codex fork <session_id>`, not ClaudeSessionRegistry.
";

pub const OPENCODE_DEV_INSTRUCTIONS: &str = "\
# ExoMonad Dev Agent Protocol

You are a dev agent in an ExoMonad agent tree. You work in your own git worktree on your own branch.

## Your Job
Handle one assignment in this process: read the task, implement it, publish the authoritative PR result, and exit cleanly. One-shot means one assignment per process, not non-interactive execution.

While this invocation is alive, continue consuming durable inbox guidance delivered through the validated tmux target for this exact invocation. A stale target is rejected and never redirected to the root pane.

## MCP Tools Available
These names are MCP tools exposed inside your agent tool interface. They are not shell commands, are not on PATH, and must not be invoked with bash commands like `which file_pr` or `file_pr ...`.

- file_pr: Create/update a PR for your branch. Call this when your implementation is ready, and again after pushing review fixes.
- notify_parent: Send a message to your parent TL when context calls for direct handoff. Use status 'success' for completed handoffs and 'failure' if you are stuck and cannot proceed. Never use send_message with recipient 'parent'; 'parent' is a reserved alias resolved only by notify_parent.
- send_tmux_message: Send a message by injecting it into another agent tmux pane.
- send_mailbox_message: Send a message through Claude Teams inbox when mailbox support is available.
- check_inbox: Drain durable inbox guidance at the start of the assignment and after each major step. Unread mail piggybacks on MCP tool results and is authoritative TL direction.
- memory_append: Append a durable session-memory record through the host ledger.
- memory_list: List durable session-memory records for the current run with optional semantic filters.
- task_list: List tasks assigned to this agent.
- task_get: Read an assigned task.
- task_update: Update an assigned task.
- chainlink_session_start: Start the Chainlink session.
- chainlink_session_status: Read Chainlink session status.
- chainlink_issue_show: Read the assigned Chainlink issue.
- chainlink_issue_comment: Post progress on the assigned Chainlink issue.
- chainlink_subissue_create: Create a subissue when the task requires decomposition.
- chainlink_session_work: Mark the assigned Chainlink issue as active work.
- chainlink_session_end: End the Chainlink session with handoff notes.
- chainlink_subissue_close: Close a completed subissue.

## Workflow
1. Read the spec carefully. Re-read any files mentioned before editing.
2. Implement the changes on your branch.
3. Build and verify (exact commands in your spec).
4. Call file_pr to create the PR as the authoritative publication.
5. Call notify_parent with status='success' or status='failure' for the handoff, then exit. Do not wait for reviewer approval, CI, merge-ready, or merge.
6. If review guidance arrives before this invocation exits, consume it through the durable inbox and exact validated tmux target. If it arrives after exit, the TL uses `resume_pr` to start a fresh invocation in the same owner worktree, branch, and PR, with pending guidance visible at startup.
7. Use notify_parent with status='success' or status='failure' when direct handoff is
   appropriate for the context, including completion outside the normal review loop or being
   truly stuck after multiple attempts.

## Key Rules
- Work only in your worktree. Never checkout another branch.
- Never call spawn_leaf — you are a leaf, not a TL.
- NEVER merge PRs. Never call merge_pr, never run `gh pr merge`, never use any bash tool (ctx_execute, shell commands) to merge. Merging is exclusively the parent TL's responsibility.
- Git operations (status, commit, push) use bash. EXCEPTION: file_pr is the MCP tool for PRs — never use `gh pr create`.
- Do not create a new owner, branch, or stacked PR for review fixes.
";

pub const OPENCODE_WORKER_INSTRUCTIONS: &str = "\
# ExoMonad Worker Agent Protocol

You are an OpenCode worker in an ExoMonad agent tree. You run in a shared workspace pane and do not have your own branch.

## Your Job
Complete the narrow task assigned by your parent TL. Report completion through the provided MCP tools.

## MCP Tools Available
- chainlink_session_start: Start the Chainlink session before marking work active.
- chainlink_session_work: Mark the assigned Chainlink issue as the active work item.
- chainlink_issue_comment: Post progress on the assigned Chainlink issue.
- chainlink_session_end: End the Chainlink session with handoff notes.
- notify_parent: Send a direct message to your parent TL when needed. Never use send_message with recipient 'parent'; 'parent' is a reserved alias resolved only by notify_parent.
- send_tmux_message: Send messages to other agents through tmux when explicitly instructed.
- send_mailbox_message: Send messages through Claude Teams inbox when mailbox support is available.
- check_inbox: Drain durable inbox guidance at the start of the assignment and after each major step. Unread mail piggybacks on MCP tool results and is authoritative parent direction.
- memory_append: Append a durable session-memory record through the host ledger.
- memory_list: List durable session-memory records for the current run with optional semantic filters.
- task_list: List tasks assigned to this agent.
- task_get: Read an assigned task.
- task_update: Update an assigned task.
- chainlink_issue_show: Read the assigned Chainlink issue.

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
- A `review-stuck` signal is a human-clarification handoff; never create a replacement issue or respawn work for it.
- Never initialize Chainlink agent identity; ExoMonad branch/session identity is authoritative.
- Never close Chainlink issues; your parent coordinator reviews the handoff and closes.
- Never create branches, commits, or PRs unless explicitly instructed.
";

pub const CODEX_DEV_INSTRUCTIONS: &str = "\
# ExoMonad Dev Agent Protocol

You are a Codex dev agent in an ExoMonad agent tree. You work in your own git worktree on your own branch.

## Your Job
Handle one assignment in this process: read the task, implement it, publish the authoritative PR result, and exit cleanly. One-shot means one assignment per process, not non-interactive execution.

While this invocation is alive, continue consuming durable inbox guidance delivered through the validated tmux target for this exact invocation. A stale target is rejected and never redirected to the root pane.

## MCP Tools Available
- file_pr: Create/update a PR for your branch. Call this when your implementation is ready, and again after pushing review fixes.
- notify_parent: Send a message to your parent TL when context calls for direct handoff. Use status 'success' for completed handoffs and 'failure' if you are stuck and cannot proceed. Never use send_message with recipient 'parent'; 'parent' is a reserved alias resolved only by notify_parent.
- send_tmux_message: Send a message by injecting it into another agent tmux pane.
- send_mailbox_message: Send a message through Claude Teams inbox when mailbox support is available.
- check_inbox: Drain durable inbox guidance at the start of the assignment and after each major step. Unread mail piggybacks on MCP tool results and is authoritative TL direction.
- memory_append: Append a durable session-memory record through the host ledger.
- memory_list: List durable session-memory records for the current run with optional semantic filters.
- task_list: List tasks assigned to this agent.
- task_get: Read an assigned task.
- task_update: Update an assigned task.
- chainlink_session_start: Start the Chainlink session.
- chainlink_session_status: Read Chainlink session status.
- chainlink_issue_show: Read the assigned Chainlink issue.
- chainlink_issue_comment: Post progress on the assigned Chainlink issue.
- chainlink_subissue_create: Create a subissue when the task requires decomposition.
- chainlink_session_work: Mark the assigned Chainlink issue as active work.
- chainlink_session_end: End the Chainlink session with handoff notes.
- chainlink_subissue_close: Close a completed subissue.

## Workflow
1. Read the spec carefully. Re-read any files mentioned before editing.
2. Implement the changes on your branch.
3. Build and verify using the exact commands in your spec.
4. Call file_pr to create the PR as the authoritative publication.
5. Call notify_parent with status='success' or status='failure' for the handoff, then exit. Do not wait for reviewer approval, CI, merge-ready, or merge.
6. If review guidance arrives before this invocation exits, consume it through the durable inbox and exact validated tmux target. If it arrives after exit, the TL uses `resume_pr` to start a fresh invocation in the same owner worktree, branch, and PR, with pending guidance visible at startup.
7. Use notify_parent with status='success' or status='failure' when direct handoff is
   appropriate for the context, including completion outside the normal review loop or being
   truly stuck after multiple attempts.

## Key Rules
- Work only in your worktree. Never checkout another branch.
- Never call spawn_leaf; you are a leaf, not a TL.
- Git operations use shell commands. Use file_pr for PR creation.
- Do not create a new owner, branch, or stacked PR for review fixes.
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
- notify_parent: Send a direct message to your parent TL when needed. Never use send_message with recipient 'parent'; 'parent' is a reserved alias resolved only by notify_parent.
- send_tmux_message: Send messages to other agents through tmux when explicitly instructed.
- send_mailbox_message: Send messages through Claude Teams inbox when mailbox support is available.
- check_inbox: Drain durable inbox guidance at the start of the assignment and after each major step. Unread mail piggybacks on MCP tool results and is authoritative parent direction.
- memory_append: Append a durable session-memory record through the host ledger.
- memory_list: List durable session-memory records for the current run with optional semantic filters.
- task_list: List tasks assigned to this agent.
- task_get: Read an assigned task.
- task_update: Update an assigned task.
- chainlink_issue_show: Read the assigned Chainlink issue.

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
- A `review-stuck` signal is a human-clarification handoff; never create a replacement issue or respawn work for it.
- Never initialize Chainlink agent identity; ExoMonad branch/session identity is authoritative.
- Never close Chainlink issues; your parent coordinator reviews the handoff and closes.
- Never create branches, commits, or PRs unless explicitly instructed.
";

fn append_reviewer_metadata(
    body: &str,
    reviewer_agent: &str,
    reviewer_birth_branch: &str,
) -> String {
    let mut lines: Vec<&str> = body
        .lines()
        .filter(|line| {
            let trimmed = line.trim_start();
            !trimmed.starts_with("Reviewer-Agent:")
                && !trimmed.starts_with("Reviewer-Birth-Branch:")
        })
        .collect();
    while lines.last().is_some_and(|line| line.trim().is_empty()) {
        lines.pop();
    }
    format!(
        "{}
Reviewer-Agent: {}
Reviewer-Birth-Branch: {}",
        lines.join(
            "
"
        ),
        reviewer_agent,
        reviewer_birth_branch
    )
}

pub const CODEX_REVIEWER_INSTRUCTIONS: &str = "\
# ExoMonad Reviewer Agent Protocol

You are a Codex reviewer agent in an ExoMonad agent tree. You review a sibling agent's PR from your own reviewer worktree.

## Your Job
Review the PR assigned in your task prompt. Approve correct changes or request specific fixes. Do not implement the fix yourself.

This reviewer process handles one exact PR/SHA assignment. Submit one authoritative verdict or comment, then exit; never wait for CI, merge-ready, or merge. While the invocation is alive, durable inbox guidance may be injected only into its validated exact tmux target. A stale target is rejected and never redirected to the root pane.

## MCP Tools Available
- approve_pr: Submit an approved Forgejo PR review.
- request_changes: Submit a request-changes Forgejo PR review.
- post_review_comment: Submit a comment-only Forgejo PR review.
- check_inbox: Drain durable inbox guidance at the start of the review and after each major step. Unread mail piggybacks on MCP tool results and is authoritative TL direction.
- list_agents: Inspect the assigned agent tree and routing state.

## Workflow
1. Read the task prompt for the PR number, PR branch, base branch, and author.
2. Run `git diff {base_branch}..HEAD` using the base branch from the prompt.
3. Review for correctness, edge cases, security issues, missing tests, and broken contracts.
4. If issues are found, call request_changes with specific, actionable feedback that references files and functions or lines.
5. If the code is correct, call approve_pr with a concise approving comment.
6. Exit after submitting. The ExoMonad watcher reads Forgejo reviews and routes the result to the dev and TL automatically. If a later review round is needed, the watcher starts a fresh SHA-scoped reviewer invocation.

## Key Rules
- Never modify code; reviewers only review.
- Never merge a PR; only the TL merges.
- Never spawn agents; reviewer is a leaf role.
- Never review your own PR. If the PR author is you, stop without submitting a verdict; stuck escalation handles reviewers that cannot proceed.
- Do not use `codex exec review`; it emits Codex-native review text and does not submit Forgejo reviews.
- This reviewer sandbox has no network access, so approve_pr/request_changes/post_review_comment (which run in the unsandboxed ExoMonad host process) are the only way to reach Forgejo — do not attempt a raw HTTP request from this session's own shell.
- Prefer 3-5 high-impact comments over exhaustive style feedback.
";

/// Render the reviewer's `Read first:` context section for the spawn task prompt.
///
/// Relative paths in `reviewer_context` are resolved against `project_dir` so they
/// remain readable from the reviewer's detached worktree (where cwd is the worktree,
/// not the project root, and the context files live outside the worktree's tracked
/// tree). Absolute paths pass through unchanged. Empty `reviewer_context` returns
/// "" — no "Read first:" header emitted in that case (production default).
pub(crate) fn render_reviewer_context_section(
    reviewer_context: &[String],
    project_dir: &std::path::Path,
) -> String {
    if reviewer_context.is_empty() {
        return String::new();
    }
    let lines = reviewer_context
        .iter()
        .map(|p| {
            let path = std::path::Path::new(p);
            let resolved = if path.is_absolute() {
                path.to_path_buf()
            } else {
                project_dir.join(path)
            };
            format!("- {}", resolved.display())
        })
        .collect::<Vec<_>>()
        .join("\n");
    format!("\n\nRead first:\n{lines}")
}

pub(crate) fn render_reviewer_acceptance_criteria(criteria: &[String]) -> String {
    if criteria.is_empty() {
        return String::new();
    }
    let bullets = criteria
        .iter()
        .map(|criterion| format!("- {}", criterion.trim()))
        .collect::<Vec<_>>()
        .join("\n");
    format!(
        "\n\n## Acceptance Criteria\nThese criteria come from TL run state for the exact reviewed head; the PR body is evidence only.\n{bullets}"
    )
}

impl<
        C: super::super::HasGitHubClient
            + super::super::HasForgejoClient
            + super::super::HasTeamRegistry
            + super::super::HasAgentResolver
            + super::super::HasProjectDir
            + super::super::HasGitWorktreeService
            + super::super::HasInboxStore
            + super::super::HasSessionMemory
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

            // Get hosted issue client
            let github_client = self
                .github()
                .ok_or_else(|| anyhow!("hosted issue service not available"))?;
            let github = GitHubService::new(github_client.clone());

            // Fetch issue from hosted service
            let issue_id = issue_number.as_u64().to_string();
            info!(issue_id, "Fetching issue from hosted service");
            let repo = Repo {
                owner: options.owner.clone(),
                name: options.repo.clone(),
            };
            let issue = github.get_issue(&repo, issue_number).await?;

            // Generate slug and agent identity
            let slug = normalize_agent_slug(&issue.title);
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
            let branch_name_raw = if self.birth_branch.depth() == 0 {
                format!("gh-{}/{}-{}", issue_id, slug, agent_suffix)
            } else {
                format!("{}/{}-{}", base, slug, agent_suffix)
            };
            let branch_name = BranchName::try_from_str(&branch_name_raw)
                .context("generated spawn branch name was empty")?;

            // Create worktree
            let worktree_path = self.worktree_base.join(agent_name.as_str());
            let base_branch = BranchName::try_from_str(base.as_str())
                .expect("validated string input is non-empty");

            self.create_worktree_checked(&worktree_path, &branch_name, &base_branch)
                .await?;

            // Use worktree path as agent_dir
            let agent_dir = worktree_path;

            // Write .mcp.json for the agent
            let role = match options.agent_type {
                AgentType::Claude => crate::domain::Role::tl(),
                AgentType::Shoal => crate::domain::Role::shoal(),
                AgentType::OpenCode | AgentType::Codex => crate::domain::Role::dev(),
                AgentType::Process => unreachable!("Process agents are not spawned via effects"),
            };
            let model = self.effective_model_for(options.agent_type, role.as_str(), None);
            let effort = self.effective_effort_for(role.as_str(), None);
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
            let original_prompt = Self::build_initial_prompt(
                &issue_id,
                &issue.title,
                &issue.body,
                &issue.labels,
                &issue_url,
            );
            let continuation_prefix = crate::services::continuation::composer::child_spawn_prefix(
                self.ctx.as_ref(),
                self,
                caller_bb,
                &agent_name,
                i64::try_from(issue_number.as_u64())
                    .context("issue number exceeds continuation ledger range")?,
            )
            .await;
            let initial_prompt = crate::services::continuation::composer::prefix_task(
                continuation_prefix.as_deref(),
                &original_prompt,
            );

            tracing::info!(
                issue_id,
                prompt_length = initial_prompt.len(),
                prefix_length = continuation_prefix.as_ref().map_or(0, String::len),
                "Built initial prompt for agent"
            );

            // tmux display name (emoji + short format)
            let display_name = options.agent_type.display_name(&issue_id, &slug);

            let parent_bb = self.effective_birth_branch(Some(caller_bb));
            let session_branch = BranchName::try_from_str(parent_bb.as_str())
                .expect("validated string input is non-empty");
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
            let routing = RoutingInfo::window(window_id.clone());
            let effective_birth = self.effective_birth_branch(Some(caller_bb));
            let identity_record = AgentIdentityRecord {
                agent_name: agent_name.clone(),
                slug: Slug::try_from_str(identity.slug())
                    .context("generated agent slug was empty")?,
                agent_type: options.agent_type,
                birth_branch: BirthBranch::try_from_str(branch_name.as_str())
                    .expect("validated string input is non-empty"),
                parent_branch: effective_birth,
                working_dir: agent_dir.clone(),
                display_name: display_name.clone(),
                topology: Topology::WorktreePerAgent,
                model: model.clone(),
                effort: effort.clone(),
                ledger_owned: false,
                slice_id: None,
            };
            self.finalize_spawn(&agent_name, routing, Some(identity_record))
                .await?;

            self.emit_agent_started(&agent_name)?;

            Ok::<SpawnResult, anyhow::Error>(SpawnResult {
                agent_dir: agent_dir.clone(),
                branch_name: branch_name.to_string(),
                agent_name,
                issue_title: issue.title,
                agent_type: options.agent_type,
                pane_id: None,
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
        Self::generate_opencode_tl_settings_with_effort(agent_name, role, extra_mcp_servers, None)
    }

    pub fn generate_opencode_tl_settings_with_effort(
        agent_name: &str,
        role: &str,
        extra_mcp_servers: &HashMap<String, serde_json::Value>,
        effort: Option<&str>,
    ) -> serde_json::Value {
        Self::generate_opencode_settings(agent_name, role, extra_mcp_servers, effort, None, None)
    }

    pub fn generate_opencode_tl_settings_with_role_context(
        agent_name: &str,
        role: &str,
        extra_mcp_servers: &HashMap<String, serde_json::Value>,
        effort: Option<&str>,
        role_context: &str,
    ) -> serde_json::Value {
        Self::generate_opencode_settings(
            agent_name,
            role,
            extra_mcp_servers,
            effort,
            None,
            Some(role_context),
        )
    }

    /// Generate root OpenCode settings that load the canonical root protocol.
    pub fn generate_opencode_root_settings_with_context(
        agent_name: &str,
        context_path: &Path,
        extra_mcp_servers: &HashMap<String, serde_json::Value>,
        effort: Option<&str>,
    ) -> serde_json::Value {
        Self::generate_opencode_settings(
            agent_name,
            "root",
            extra_mcp_servers,
            effort,
            Some(context_path),
            None,
        )
    }

    fn generate_opencode_settings(
        agent_name: &str,
        role: &str,
        extra_mcp_servers: &HashMap<String, serde_json::Value>,
        effort: Option<&str>,
        context_path: Option<&Path>,
        role_context: Option<&str>,
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

        // instructions must be an array per OpenCode's schema. Root settings
        // contain a path so the harness reads the canonical protocol itself.
        let mut instructions = match context_path {
            Some(path) => vec![path.to_string_lossy().into_owned()],
            None => match role {
                "root" | "tl" => vec![ROOT_CONTEXT_RELATIVE_PATH.to_string()],
                "worker" => vec![OPENCODE_WORKER_INSTRUCTIONS.to_string()],
                _ => vec![OPENCODE_DEV_INSTRUCTIONS.to_string()],
            },
        };
        if let Some(role_context) = role_context {
            instructions.push(role_context.to_string());
        }
        let mut settings = serde_json::json!({
            "mcp": mcp_servers,
            "instructions": instructions,
            "plugin": ["./.exo/opencode-plugin"],
        });
        if let Some(effort) = effort.filter(|value| !value.is_empty()) {
            settings["agent"] = serde_json::json!({
                format!("exomonad-{role}"): {"reasoningEffort": effort}
            });
        }
        settings
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

    pub async fn write_opencode_git_stub(
        agent_config_dir: &Path,
        project_dir: &Path,
    ) -> Result<()> {
        let git_content = format!("gitdir: {}\n", project_dir.join(".git").display());
        fs::write(agent_config_dir.join(".git"), git_content).await?;
        Ok(())
    }

    async fn active_worker_for_parent_tab(
        &self,
        agents_dir: &Path,
        parent_tab: &str,
        current_agent_name: &AgentName,
    ) -> Result<Option<ActiveWorker>> {
        let Ok(mut entries) = fs::read_dir(agents_dir).await else {
            return Ok(None);
        };

        while let Some(entry) = entries.next_entry().await? {
            let file_type = entry.file_type().await?;
            if !file_type.is_dir() {
                continue;
            }

            let name = entry.file_name().to_string_lossy().to_string();
            if name == current_agent_name.as_str() {
                continue;
            }

            let agent_dir = entry.path();
            let Ok(routing) = RoutingInfo::read_from_dir(&agent_dir).await else {
                continue;
            };
            if routing.parent_tab.as_deref() != Some(parent_tab) {
                continue;
            }
            let worker_alive = if let Some(pane_id) = routing.pane_id.as_ref() {
                self.tmux()?.pane_exists(pane_id).await.unwrap_or(false)
            } else if let Some(window_id) = routing.window_id.as_ref() {
                self.tmux()?.window_exists(window_id).await.unwrap_or(false)
            } else {
                false
            };
            if !worker_alive {
                warn!(
                    worker = %name,
                    path = %agent_dir.display(),
                    "Removing stale active worker registration with no live tmux target"
                );
                if let Err(error) = fs::remove_dir_all(&agent_dir).await {
                    warn!(worker = %name, error = %error, "Failed to remove stale active worker registration");
                }
                continue;
            }

            let age = entry
                .metadata()
                .await
                .ok()
                .and_then(|metadata| metadata.modified().ok())
                .and_then(|modified| std::time::SystemTime::now().duration_since(modified).ok())
                .map(format_worker_age)
                .unwrap_or_else(|| "unknown time".to_string());
            return Ok(Some(ActiveWorker { name, age }));
        }

        Ok(None)
    }

    /// Spawn a worker agent in the current worktree (no branch/worktree).
    #[instrument(skip_all, fields(name = %options.name, agent_type = %options.agent_type.suffix()))]
    pub async fn spawn_worker(
        &self,
        options: &SpawnWorkerOptions,
        ctx: &crate::effects::EffectContext,
    ) -> Result<SpawnResult> {
        self.spawn_worker_with_intent(options, ctx, None).await
    }

    pub async fn spawn_worker_with_intent(
        &self,
        options: &SpawnWorkerOptions,
        ctx: &crate::effects::EffectContext,
        intent_id: Option<&str>,
    ) -> Result<SpawnResult> {
        let agent_type = options.agent_type;
        info!(name = %options.name, agent_type = agent_type.suffix(), timeout_sec = SPAWN_TIMEOUT.as_secs(), "Starting spawn_worker");

        let result = timeout(SPAWN_TIMEOUT, async {
            self.resolve_tmux_session()?;

            // Workers run in the caller's worktree and inherit any dirty state.
            let caller_tab = resolve_own_tab_name(ctx);
            let caller_worktree = ctx.working_dir.clone();
            let absolute_worktree = self.project_dir().join(&caller_worktree);
            ensure_clean_spawn_worktree(&absolute_worktree).await?;

            // Sanitize name and construct typed identity
            let identity = AgentIdentity::new(normalize_agent_slug(options.name.as_str()), agent_type);
            let agent_name = identity.internal_name();
            let display_name = identity.display_name();
            let agents_dir = self.project_dir().join(".exo").join("agents");

            if let Some(active_worker) = self
                .active_worker_for_parent_tab(&agents_dir, &caller_tab, &agent_name)
                .await?
            {
                return Err(active_worker_error(&active_worker));
            }

            // Idempotency: check if agent config dir already exists (workers are panes, not tabs)
            let agent_config_dir = agents_dir.join(agent_name.as_str());
            let routing_path = agent_config_dir.join("routing.json");
            if routing_path.exists() {
                // Check tmux pane liveness — routing.json can outlive the pane
                let existing_pane_id = match RoutingInfo::read_from_dir(&agent_config_dir).await {
                    Ok(routing) => match routing.pane_id {
                        Some(ref pane_id) if self.tmux()?.pane_exists(pane_id).await.unwrap_or(false) => {
                            Some(pane_id.as_str().to_string())
                        }
                        _ => None,
                    },
                    Err(_) => None,
                };
                if let Some(pane_id) = existing_pane_id {
                    info!(name = %options.name, "Worker pane still alive, returning existing");
                    return Ok(SpawnResult {
                        agent_dir: PathBuf::new(),
                        branch_name: String::new(),
                        agent_name,
                        issue_title: options.name.to_string(),
                        agent_type,
                        pane_id: Some(pane_id),
                    });
                }
                // Stale: pane is dead but config dir remains. Clean up and respawn.
                info!(name = %options.name, path = %agent_config_dir.display(), "Stale worker detected (pane dead), cleaning up and respawning");
                if let Err(e) = fs::remove_dir_all(&agent_config_dir).await {
                    warn!(name = %options.name, error = %e, "Failed to clean up stale worker config dir");
                }
            }

            persist_dispatch_intent(self.project_dir(), &agent_name, intent_id).await?;

            let role = crate::domain::Role::worker();
            let model = self.effective_model_for(agent_type, role.as_str(), options.model.as_deref());
            let effort = self.effective_effort_for(role.as_str(), None);
            let parent_bb = self.effective_birth_branch(Some(&ctx.birth_branch));
            let session_branch = BranchName::try_from_str(parent_bb.as_str()).expect("validated string input is non-empty");
            let mut env_vars = self.common_spawn_env(&agent_name, &session_branch, &role);
              env_vars.insert(
                  "GIT_AUTHOR_NAME".to_string(),
                  format!("exomonad-{}", agent_name.as_str()),
              );
              env_vars.insert(
                  "GIT_AUTHOR_EMAIL".to_string(),
                  format!("{}@exomonad.local", agent_name.as_str()),
              );

            fs::create_dir_all(&agent_config_dir).await?;

            // Legacy .birth_branch file for serve.rs fallback resolution.
            // identity.json (written via finalize_spawn) is the canonical source,
            // but keep this for backward compatibility with older server instances.
            let parent_bb = self.effective_birth_branch(Some(&ctx.birth_branch));
            fs::write(agent_config_dir.join(".birth_branch"), parent_bb.as_str()).await?;

            match agent_type {
                AgentType::OpenCode => {
                    // Write worker-specific opencode.json so the worker gets
                    // its own role/name (not the caller's root config, which
                    // lacks notify_parent and other worker tools).
                    let role_context = self.runtime_role_context(&role)?;
                    let worker_config = Self::generate_opencode_tl_settings_with_role_context(
                        agent_name.as_str(),
                        "worker",
                        &self.extra_mcp_servers,
                        None,
                        &role_context,
                    );
                    let opencode_json_path = agent_config_dir.join("opencode.json");
                    fs::write(&opencode_json_path, serde_json::to_string_pretty(&worker_config)?).await?;
                    Self::write_opencode_plugin_files(&agent_config_dir).await?;
                    Self::write_opencode_git_stub(&agent_config_dir, self.project_dir()).await?;
                    info!(path = %opencode_json_path.display(), agent_name = %agent_name, "Wrote worker opencode.json and plugin to agent config dir");
                }
                AgentType::Codex => {
                    self.write_codex_config_files(
                        &agent_config_dir,
                        &role,
                        &agent_name,
                        model.as_deref(),
                        &self.extra_mcp_servers,
                    )
                    .await?;
                    info!(path = %agent_config_dir.join(".codex/config.toml").display(), agent_name = %agent_name, "Wrote worker Codex config to agent config dir");
                }
                _ => {}
            }

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
                model.as_deref(),
            )
              .await?;
              let pane_id_string = pane_id.as_str().to_string();

              // Store pane_id for message delivery and cleanup
              let routing = RoutingInfo::pane(pane_id, &caller_tab);
            let parent_bb = self.effective_birth_branch(Some(&ctx.birth_branch));
            let identity_record = AgentIdentityRecord {
                agent_name: agent_name.clone(),
                slug: Slug::try_from_str(identity.slug())
                    .context("generated agent slug was empty")?,
                agent_type,
                birth_branch: parent_bb.clone(),
                parent_branch: parent_bb,
                working_dir: ctx.working_dir.clone(),
                display_name: display_name.clone(),
                topology: Topology::SharedDir,
                model: model.clone(),
                effort: effort.clone(),
                ledger_owned: false,
                slice_id: Some(options.name.to_string()),
            };
            self.finalize_spawn(&agent_name, routing, Some(identity_record))
                .await?;

            self.emit_agent_started(&agent_name)?;

            Ok::<SpawnResult, anyhow::Error>(SpawnResult {
                agent_dir: PathBuf::new(),
                branch_name: String::new(),
                agent_name,
                issue_title: options.name.to_string(),
                agent_type,
                pane_id: Some(pane_id_string),
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
            let identity = AgentIdentity::new(normalize_agent_slug(&options.branch_name), agent_type);
            let agent_name = identity.internal_name();
            let display_name = identity.display_name();
            let child_birth = effective_birth.child(agent_name.as_str());

            // Idempotency check: if tmux window is alive, return existing info
            let tab_alive = self.is_tmux_window_alive(&display_name).await;
            if tab_alive {
                info!(slug = %identity.slug(), "Subtree already running, returning existing");
                return Ok(SpawnResult {
                    agent_dir: self.worktree_base.join(agent_name.as_str()),
                    branch_name: child_birth.to_string(),
                    agent_name,
                    issue_title: options.branch_name.clone(),
                    agent_type,
                    pane_id: None,
                });
            }

            // Parent branch derived from typed birth-branch.
            let current_branch = BranchName::try_from_str(effective_birth.as_parent_branch())
                .context("effective birth branch was empty")?;

            // Ensure a remote exists for local-only workflows
            ensure_remote_exists(effective_project_dir).await;

            // Push parent branch so child PRs can reference it as base
            ensure_branch_pushed(self.git_wt(), &current_branch, effective_project_dir).await;

            // Branch: {current_branch}.{agent_name} (suffixed for unified namespace)
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
                let branch = BranchName::try_from_str(branch_name.as_str()).expect("validated string input is non-empty");
                self.create_worktree_checked(&worktree_path, &branch, &current_branch).await?;
            }

            self.create_socket_symlink(&worktree_path).await;

            let default_tl = crate::domain::Role::tl();
            let role = options.role.as_ref().unwrap_or(&default_tl);
            let model = self.effective_model_for(agent_type, role.as_str(), options.model.as_deref());
            let effort = self.effective_effort_for(role.as_str(), options.effort.as_deref());

            // Validate role context before spawning. Claude consumes a copied
            // file; OpenCode and Codex receive the same content inline in their
            // runtime instruction settings below.
            let context_src = self.resolve_role_context(role).ok_or_else(|| {
                anyhow!(
                    "Missing role context for {} at .exo/roles/{}/context/{}.md",
                    role,
                    self.wasm_name,
                    role
                )
            })?;
            let spawn_type = self.spawn_agent_type.suffix();
            match agent_type {
                AgentType::Claude => {
                    let rules_dir = worktree_path.join(".claude/rules");
                    fs::create_dir_all(&rules_dir).await?;
                    let dest = rules_dir.join("exomonad_role.md");
                    let _ = fs::remove_file(&dest).await;
                    Self::copy_role_context_with_interpolation(&context_src, &dest, spawn_type)
                        .await
                        .with_context(|| {
                            format!("Failed to copy Claude role context to {}", dest.display())
                        })?;
                    info!(role = %role, src = %context_src.display(), dest = %dest.display(), "Copied role context into worktree");
                }
                AgentType::OpenCode | AgentType::Codex | AgentType::Shoal | AgentType::Process => {}
            }

            let session_branch = BranchName::try_from_str(branch_name.as_str()).expect("validated string input is non-empty");
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
                    let role_context = self.runtime_role_context(role)?;
                    let opencode_config = Self::generate_opencode_tl_settings_with_role_context(
                        agent_name.as_str(),
                        role.as_str(),
                        &self.extra_mcp_servers,
                        None,
                        &role_context,
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
                        Some(role.as_str()),
                        model.as_deref(),
                        effort.as_deref(),
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
                        Some(role.as_str()),
                        model.as_deref(),
                        effort.as_deref(),
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
                        Some(role.as_str()),
                        model.as_deref(),
                        effort.as_deref(),
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
                slug: Slug::try_from_str(identity.slug())
                    .context("generated agent slug was empty")?,
                agent_type,
                birth_branch: child_birth,
                parent_branch: effective_birth,
                working_dir: worktree_path.clone(),
                display_name: display_name.clone(),
                topology: Topology::WorktreePerAgent,
                model: model.clone(),
                effort: effort.clone(),
                ledger_owned: false,
                slice_id: Some(options.branch_name.clone()),
            };
            let trigger = if options
                .role
                .as_ref()
                .is_some_and(|role| role.as_str() == "reviewer")
            {
                InvocationTrigger::Review
            } else {
                InvocationTrigger::Spawn
            };
            self.finalize_spawn_with_invocation(
                &agent_name,
                routing,
                Some(identity_record),
                InvocationMetadata {
                    runtime: agent_type,
                    trigger,
                    pr_number: options.invocation_pr_number,
                    head_sha: options.invocation_head_sha.clone(),
                    model: model.clone(),
                    effort: effort.clone(),
                },
            )
                .await?;

            Ok::<SpawnResult, anyhow::Error>(SpawnResult {
                agent_dir: worktree_path.clone(),
                branch_name: branch_name.clone(),
                agent_name,
                issue_title: options.branch_name.clone(),
                agent_type,
                pane_id: None,
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

    /// Spawn a leaf agent in a new git worktree.
    #[instrument(skip_all, fields(slug = %options.branch_name))]
    pub async fn spawn_leaf_subtree(
        &self,
        options: &SpawnLeafOptions,
        caller_bb: &BirthBranch,
    ) -> Result<SpawnResult> {
        self.spawn_leaf_subtree_with_intent(options, caller_bb, None)
            .await
    }

    pub async fn spawn_leaf_subtree_with_intent(
        &self,
        options: &SpawnLeafOptions,
        caller_bb: &BirthBranch,
        intent_id: Option<&str>,
    ) -> Result<SpawnResult> {
        info!(branch_name = %options.branch_name, timeout_sec = SPAWN_TIMEOUT.as_secs(), "Starting spawn_leaf_subtree");
        let _resume_spawn_guard = if options.expected_agent_name.is_some() {
            Some(resume_spawn_lock().lock().await)
        } else {
            None
        };

        let result = timeout(SPAWN_TIMEOUT, async {
            self.resolve_tmux_session()?;

            // No depth check for leaf nodes.

            let effective_birth = self.effective_birth_branch(Some(caller_bb));
            let effective_project_dir = self.project_dir();
            ensure_clean_spawn_worktree(effective_project_dir).await?;

            // Replacement leaves may start from an old PR head while their branch
            // hierarchy still targets the original PR base branch.
            let parent_branch = options
                .base_branch
                .as_deref()
                .unwrap_or(effective_birth.as_parent_branch());
            let current_branch = BranchName::try_from_str(parent_branch)
                .context("effective birth branch was empty")?;

            // Sanitize branch name and construct typed identity
            let slug = normalize_agent_slug(&options.branch_name);
            let slug_key = Slug::try_from_str(&slug).context("generated agent slug was empty")?;
            let mut agent_type = options.agent_type;
            let mut identity = AgentIdentity::new(slug.clone(), agent_type);
            let mut agent_name = identity.internal_name();
            let mut display_name = identity.display_name();
            let mut worktree_path = self.worktree_base.join(agent_name.as_str());
            let mut existing_identity_record = None;

            if let Some(expected_agent_name) = options.expected_agent_name.as_ref() {
                let expected_identity = AgentIdentity::from_internal_name(expected_agent_name.as_str());
                if normalize_agent_slug(expected_identity.slug()) != slug {
                    return Err(anyhow!(
                        "resolved resume identity {} does not match slug {}",
                        expected_agent_name,
                        slug
                    ));
                }
                identity = expected_identity;
                agent_type = identity.agent_type();
                agent_name = expected_agent_name.clone();
                display_name = identity.display_name();
                worktree_path = self.worktree_base.join(agent_name.as_str());
            } else if !options.standalone_repo && !worktree_path.exists() {
                let record = if let Some(record) =
                    self.agent_resolver().lookup_by_slug(&slug_key).await
                {
                    Some(record)
                } else {
                    self.agent_resolver()
                        .all()
                        .await
                        .into_iter()
                        .find(|record| normalize_agent_slug(record.slug.as_str()) == slug)
                };
                if let Some(record) = record {
                    if record.topology == Topology::WorktreePerAgent {
                        let record_worktree =
                            resolve_identity_working_dir(effective_project_dir, &record.working_dir);
                        if record_worktree.exists() {
                            agent_name = record.agent_name.clone();
                            agent_type = record.agent_type;
                            identity = AgentIdentity::from_internal_name(agent_name.as_str());
                            display_name = record.display_name.clone();
                            worktree_path = record_worktree;
                            existing_identity_record = Some(record);
                        }
                    }
                }

                if existing_identity_record.is_none() {
                    if let Some((found_identity, found_path)) =
                        find_existing_leaf_worktree_by_slug(&self.worktree_base, &slug).await?
                    {
                        identity = found_identity;
                        agent_type = identity.agent_type();
                        agent_name = identity.internal_name();
                        display_name = identity.display_name();
                        worktree_path = found_path;
                    }
                }
            }
            let prior_identity = self.agent_resolver().get(&agent_name).await;

            let existing_branch = if !options.standalone_repo && worktree_path.exists() {
                self.git_wt()
                    .get_workspace_bookmark(&worktree_path)
                    .context("failed to inspect existing leaf worktree branch")?
                    .map(|branch| BirthBranch::try_from_str(&branch))
                    .transpose()
                    .context("existing leaf worktree branch was invalid")?
            } else {
                None
            };
            let child_birth = if let Some(branch) = existing_branch.clone() {
                branch
            } else if let Some(record) = existing_identity_record.as_ref() {
                record.birth_branch.clone()
            } else if let Some(base_branch) = options.base_branch.as_deref() {
                BirthBranch::try_from_str(base_branch)
                    .context("replacement base branch was empty")?
                    .child(agent_name.as_str())
            } else {
                effective_birth.child(agent_name.as_str())
            };

            // Idempotency check: stable routing IDs take precedence over a
            // stale display-name scan when resuming an existing owner.
            let config_dir = self
                .project_dir()
                .join(".exo/agents")
                .join(agent_name.as_str());
            let tab_alive = match self.routing_liveness(&config_dir).await {
                Some(alive) => alive,
                None => self.is_tmux_window_alive(&display_name).await,
            };
            if tab_alive {
                if options.expected_agent_name.is_some() {
                    self.refresh_agent_activity(&agent_name).await?;
                }
                info!(slug = %identity.slug(), "Leaf subtree already running, returning existing");
                return Ok(SpawnResult {
                    agent_dir: worktree_path,
                    branch_name: child_birth.to_string(),
                    agent_name,
                    issue_title: options.branch_name.clone(),
                    agent_type,
                    pane_id: None,
                });
            }

            persist_dispatch_intent(self.project_dir(), &agent_name, intent_id).await?;

            // Ensure a remote exists for local-only workflows
            ensure_remote_exists(effective_project_dir).await;

            // Push parent branch so child PRs can reference it as base
            ensure_branch_pushed(self.git_wt(), &current_branch, effective_project_dir).await;

            let branch_name = BranchName::try_from_str(child_birth.to_string().as_str())
                .expect("validated string input is non-empty");
            let actual_branch_name = branch_name.to_string();

            if options.expected_agent_name.is_some() {
                if let Some(existing_branch) = existing_branch.as_ref() {
                    if existing_branch.as_str() != branch_name.as_str() {
                        return Err(anyhow!(
                            "existing resume worktree is on {}, expected {}",
                            existing_branch,
                            branch_name
                        ));
                    }
                    if let Some(expected_sha) = options.start_point.as_deref() {
                        verify_branch_head(effective_project_dir, &branch_name, expected_sha)
                            .await?;
                    }
                }
            }

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
                if options.start_point.is_some() {
                    let actual_branch = self
                        .git_wt()
                        .get_workspace_bookmark(&worktree_path)
                        .context("failed to inspect existing replacement worktree")?;
                    if actual_branch.as_deref() != Some(branch_name.as_str()) {
                        return Err(anyhow!(
                            "Existing replacement worktree is on {:?}, expected {}",
                            actual_branch,
                            branch_name
                        ));
                    }
                }
                info!(
                    worktree_path = %worktree_path.display(),
                    branch_name = %branch_name,
                    "Reusing existing leaf worktree"
                );
            } else {
                ensure_branch_fetched(effective_project_dir, &branch_name).await;
                if let Some(start_point) = options.start_point.as_deref() {
                    if self.git_wt().branch_exists(&branch_name)? {
                        if options.expected_agent_name.is_some() {
                            verify_branch_head(effective_project_dir, &branch_name, start_point)
                                .await?;
                        }
                        self.create_worktree_from_existing_branch_checked(
                            &worktree_path,
                            &branch_name,
                        )
                        .await?;
                    } else {
                        self.create_worktree_from_revision_checked(
                            &worktree_path,
                            &branch_name,
                            start_point,
                        )
                        .await?;
                    }
                } else {
                    self.create_worktree_checked(&worktree_path, &branch_name, &current_branch)
                        .await?;
                }
                remove_worktree_on_spawn_failure = true;
            }

            self.create_socket_symlink(&worktree_path).await;

            let default_dev = crate::domain::Role::dev();
            let role = options.role.as_ref().unwrap_or(&default_dev);
            let model = self.effective_model_for(agent_type, role.as_str(), None);
            let effort = self.effective_effort_for(role.as_str(), None);
            let identity_model = existing_identity_record
                .as_ref()
                .and_then(|record| record.model.clone())
                .or_else(|| prior_identity.as_ref().and_then(|record| record.model.clone()))
                .or_else(|| model.clone());
            let identity_effort = existing_identity_record
                .as_ref()
                .and_then(|record| record.effort.clone())
                .or_else(|| prior_identity.as_ref().and_then(|record| record.effort.clone()))
                .or_else(|| effort.clone());
            let mut env_vars = self.common_spawn_env(&agent_name, &branch_name, role);
            self.write_agent_mcp_config(effective_project_dir, &worktree_path, agent_type, role)
                .await?;

            let mut task = options.task.clone();

            // If an open PR already exists for this branch (re-spawn after worktree loss),
            // inject PR context so the leaf resumes instead of filing a new PR.
            if options.expected_agent_name.is_none() {
                if let Some(forgejo) = self.ctx.forgejo_client() {
                if let Ok(repo_info) =
                    crate::services::repo::get_repo_info(effective_project_dir).await
                {
                    if let Ok(Some(pr)) = forgejo
                        .find_open_pull_request(
                            &repo_info.owner,
                            &repo_info.repo,
                            &branch_name,
                        )
                        .await
                    {
                        let pr_number = pr.number;
                        let mut resume_context = format!(
                            "\n\nIMPORTANT: You are resuming work on an existing pull request, not starting fresh.\n\
                            Existing PR: #{} — {}\n\
                            Do NOT create a new pull request. Continue working on this branch.\n",
                            pr_number.as_u64(),
                            pr.title
                        );

                        // Fetch review comments so the leaf sees existing feedback
                        if let Ok(reviews) = forgejo
                            .list_pull_request_reviews(
                                &repo_info.owner,
                                &repo_info.repo,
                                pr_number,
                            )
                            .await
                        {
                            let mut review_bodies: Vec<String> = Vec::new();
                            for review in &reviews {
                                if !review.body.is_empty() {
                                    review_bodies.push(review.body.clone());
                                }
                                // Fetch inline review comments
                                if let Some(review_id) = review.id {
                                    if let Ok(comments) = forgejo
                                        .list_pull_request_review_comments(
                                            &repo_info.owner,
                                            &repo_info.repo,
                                            pr_number,
                                            review_id,
                                        )
                                        .await
                                    {
                                        for comment in &comments {
                                            let file_label =
                                                comment.path.as_deref().unwrap_or("unknown file");
                                            resume_context.push_str(&format!(
                                                "Review comment on {}: {}\n",
                                                file_label, comment.body
                                            ));
                                        }
                                    }
                                }
                            }
                            if !review_bodies.is_empty() {
                                resume_context.push_str("\nExisting review feedback:\n");
                                for body in &review_bodies {
                                    resume_context.push_str(body);
                                    resume_context.push('\n');
                                }
                            }
                        }

                        info!(
                            pr_number = pr_number.as_u64(),
                            branch = %branch_name,
                            "Injecting existing PR context into re-spawned leaf task"
                        );
                        task.push_str(&resume_context);
                    }
                }
            }
            }

            if options.standalone_repo && !options.allowed_dirs.is_empty() {
                task.push_str("\n\nShared technical dependencies are available as read-only reference in `.exo/context/`. Do not modify files in this directory.");
            }

            // Open tmux window (not pane)
            // Task already includes leaf completion protocol — rendered by Haskell Prompt builder.
            let agent_config_dir = self.project_dir().join(".exo").join("agents").join(agent_name.as_str());
            let agent_config_preexisting = agent_config_dir.exists();
            fs::create_dir_all(&agent_config_dir).await?;
            env_vars.insert(
                "EXOMONAD_INVOCATION_EXIT_FILE".to_string(),
                agent_config_dir.join("exit_code").display().to_string(),
            );
            let _ = fs::remove_file(agent_config_dir.join("exit_code")).await;
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
                    if !agent_config_preexisting {
                        let _ = fs::remove_dir_all(&agent_config_dir).await;
                    }
                    if remove_worktree_on_spawn_failure && worktree_path.exists() {
                        let git_wt = self.git_wt().clone();
                        let path = worktree_path.clone();
                        let _ = tokio::task::spawn_blocking(move || git_wt.remove_workspace(&path)).await;
                    }
                    return Err(e);
                }
            };

            if let Err(error) = self.verify_tmux_window_startup(&window_id).await {
                warn!(
                    name = %identity.slug(),
                    window = %window_id,
                    %error,
                    "tmux window exited before invocation startup readiness"
                );
                if let Err(cleanup_error) = self.kill_tmux_window_id(&window_id).await {
                    warn!(
                        window = %window_id,
                        error = %cleanup_error,
                        "Failed to clean up failed leaf tmux window"
                    );
                }
                if !agent_config_preexisting {
                    let _ = fs::remove_dir_all(&agent_config_dir).await;
                }
                if remove_worktree_on_spawn_failure && worktree_path.exists() {
                    let git_wt = self.git_wt().clone();
                    let path = worktree_path.clone();
                    let _ = tokio::task::spawn_blocking(move || git_wt.remove_workspace(&path)).await;
                }
                return Err(error);
            }

            // Store window_id for message delivery and cleanup
            let routing = RoutingInfo::window(window_id.clone());
            let expected_routing = routing.clone();
            let identity_record = AgentIdentityRecord {
                agent_name: agent_name.clone(),
                slug: Slug::try_from_str(identity.slug())
                    .context("generated agent slug was empty")?,
                agent_type,
                birth_branch: child_birth,
                parent_branch: effective_birth,
                working_dir: worktree_path.clone(),
                display_name: display_name.clone(),
                topology: Topology::WorktreePerAgent,
                model: identity_model.clone(),
                effort: identity_effort.clone(),
                ledger_owned: false,
                slice_id: Some(options.branch_name.clone()),
            };
            let trigger = if options.expected_agent_name.is_some() {
                InvocationTrigger::ResumePr
            } else {
                InvocationTrigger::Spawn
            };
            if let Err(error) = self
                .finalize_spawn_with_invocation(
                &agent_name,
                routing,
                Some(identity_record),
                InvocationMetadata {
                    runtime: agent_type,
                    trigger,
                    pr_number: options.invocation_pr_number,
                    head_sha: options.start_point.clone(),
                    model: model.clone(),
                    effort: effort.clone(),
                },
            )
                .await
            {
                warn!(
                    name = %identity.slug(),
                    window = %window_id,
                    %error,
                    "Failed to persist leaf invocation metadata"
                );
                if let Err(cleanup_error) = self.kill_tmux_window_id(&window_id).await {
                    warn!(
                        window = %window_id,
                        error = %cleanup_error,
                        "Failed to clean up leaf tmux window after metadata failure"
                    );
                }
                if !agent_config_preexisting {
                    let _ = fs::remove_dir_all(&agent_config_dir).await;
                }
                if remove_worktree_on_spawn_failure && worktree_path.exists() {
                    let git_wt = self.git_wt().clone();
                    let path = worktree_path.clone();
                    let _ = tokio::task::spawn_blocking(move || git_wt.remove_workspace(&path)).await;
                }
                return Err(error);
            }

            if let Err(error) = self.verify_tmux_window_startup(&window_id).await {
                warn!(
                    name = %identity.slug(),
                    window = %window_id,
                    %error,
                    "leaf invocation exited before spawn could report success"
                );
                match crate::services::agent_control::finish_invocation_and_tombstone(
                    &agent_config_dir,
                    &expected_routing,
                    InvocationStatus::Failed,
                    None,
                )
                .await
                {
                    Ok(InvocationFinishResult::IgnoredStale) => {
                        warn!(name = %identity.slug(), "Preserved newer invocation after startup failure")
                    }
                    Ok(InvocationFinishResult::Finished(_))
                    | Ok(InvocationFinishResult::Missing) => {}
                    Err(cleanup_error) => warn!(
                        name = %identity.slug(),
                        error = %cleanup_error,
                        "Failed to finish failed leaf invocation"
                    ),
                }
                if let Err(cleanup_error) = self.kill_tmux_window_id(&window_id).await {
                    warn!(
                        window = %window_id,
                        error = %cleanup_error,
                        "Failed to clean up leaf tmux window after startup failure"
                    );
                }
                return Err(error);
            }

            Ok::<SpawnResult, anyhow::Error>(SpawnResult {
                agent_dir: worktree_path.clone(),
                branch_name: actual_branch_name,
                agent_name,
                issue_title: options.branch_name.clone(),
                agent_type,
                pane_id: None,
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
    /// The reviewer examines `git diff base..{pr_branch}` and submits a
    /// Forgejo approval, request-changes review, or comment-only review.
    ///
    /// Use this when the TL receives `[PR READY]` from a child agent.
    #[instrument(skip_all, fields(pr_number = pr_entry.number))]
    pub async fn spawn_reviewer_subtree(
        &self,
        pr_entry: &crate::services::pr_registry::PrEntry,
        caller_bb: &BirthBranch,
    ) -> Result<SpawnResult> {
        let branch_name = format!("review-pr-{}", pr_entry.number);
        self.spawn_reviewer_subtree_with_criteria_named(pr_entry, caller_bb, &branch_name, &[])
            .await
    }

    pub async fn spawn_reviewer_subtree_with_criteria_named(
        &self,
        pr_entry: &crate::services::pr_registry::PrEntry,
        caller_bb: &BirthBranch,
        branch_name: &str,
        acceptance_criteria: &[String],
    ) -> Result<SpawnResult> {
        let context_section =
            render_reviewer_context_section(&self.reviewer_context, self.project_dir());
        let criteria_section = render_reviewer_acceptance_criteria(acceptance_criteria);
        let task = format!(
            "Review PR #{}: {}\n\nBranch: {}\nBase: {}\nAuthor: {}{}{}",
            pr_entry.number,
            pr_entry.title,
            pr_entry.head_branch,
            pr_entry.base_branch,
            pr_entry.author_agent,
            context_section,
            criteria_section,
        );

        // Compute the reviewer's own identity and path — same derivation spawn_subtree uses
        // internally so the MCP config agent_name matches the directory name.
        let agent_type = self.reviewer_agent_type;
        let identity = AgentIdentity::new(slugify(branch_name), agent_type);
        let reviewer_path = self.worktree_base.join(identity.internal_name().as_str());

        if agent_type == AgentType::Claude {
            preflight_reviewer_hook_environment(self.project_dir()).await?;
        }

        // Create a detached-HEAD worktree at the PR branch tip unless it already exists.
        // Detached so we don't compete with the worker's branch; the reviewer never commits.
        // This prevents clobbering the worker's opencode.json/MCP config while both run.
        let at_ref = pr_entry
            .last_head_sha
            .clone()
            .filter(|sha| !sha.trim().is_empty())
            .ok_or_else(|| {
                anyhow!(
                    "refusing reviewer spawn for PR #{} without a verified head SHA",
                    pr_entry.number
                )
            })?;
        if !reviewer_path.exists() {
            let git_wt = self.git_wt().clone();
            let path = reviewer_path.clone();
            let name = identity.internal_name().to_string();
            tokio::task::spawn_blocking(move || {
                git_wt.create_workspace_detached(&path, &at_ref, &name)
            })
            .await
            .context("tokio join error creating reviewer worktree")?
            .context("Failed to create reviewer worktree")?;
        } else {
            let current_head = Command::new("git")
                .args(["rev-parse", "HEAD"])
                .current_dir(&reviewer_path)
                .output()
                .await
                .context("failed to inspect reviewer worktree head")?;
            let current_head = String::from_utf8_lossy(&current_head.stdout)
                .trim()
                .to_string();
            if !current_head.eq_ignore_ascii_case(&at_ref) {
                let checkout = Command::new("git")
                    .args(["checkout", "--detach", "--force", &at_ref])
                    .current_dir(&reviewer_path)
                    .output()
                    .await
                    .context("failed to align reviewer worktree with published head")?;
                if !checkout.status.success() {
                    anyhow::bail!(
                        "reviewer worktree is at {current_head}, cannot checkout published head {at_ref}: {}",
                        String::from_utf8_lossy(&checkout.stderr).trim()
                    );
                }
            }
        }

        let options = SpawnSubtreeOptions {
            task,
            branch_name: branch_name.to_string(),
            parent_session_id: None,
            role: Some(crate::domain::Role::reviewer()),
            agent_type,
            claude_flags: ClaudeSpawnFlags::default(),
            working_dir: Some(reviewer_path),
            permissions: Some(AgentPermissions {
                allow: vec![],
                deny: reviewer_harness_denied_tools(),
                default_mode: None,
            }),
            standalone_repo: false,
            allowed_dirs: vec![],
            model: self.reviewer_model.clone(),
            effort: self.reviewer_effort().map(str::to_string),
            invocation_pr_number: Some(pr_entry.number),
            invocation_head_sha: pr_entry.last_head_sha.clone(),
        };

        let result = self.spawn_subtree(&options, caller_bb).await?;

        Ok(result)
    }

    pub async fn spawn_reviewer_for_recovery(
        &self,
        pr: &crate::services::pr_registry::PrEntry,
        caller_bb: &BirthBranch,
    ) -> Result<SpawnResult> {
        let reviewer_branch_name = format!("review-pr-{}", pr.number);
        self.spawn_reviewer_with_metadata_named(pr, caller_bb, &reviewer_branch_name, &[])
            .await
    }

    pub async fn spawn_reviewer_for_recovery_named(
        &self,
        pr: &crate::services::pr_registry::PrEntry,
        caller_bb: &BirthBranch,
        reviewer_branch_name: &str,
    ) -> Result<SpawnResult> {
        self.spawn_reviewer_with_metadata_named(pr, caller_bb, reviewer_branch_name, &[])
            .await
    }
    pub async fn spawn_reviewer_for_recovery_with_criteria_named(
        &self,
        pr: &crate::services::pr_registry::PrEntry,
        caller_bb: &BirthBranch,
        reviewer_branch_name: &str,
        acceptance_criteria: &[String],
    ) -> Result<SpawnResult> {
        self.spawn_reviewer_with_metadata_named(
            pr,
            caller_bb,
            reviewer_branch_name,
            acceptance_criteria,
        )
        .await
    }

    async fn spawn_reviewer_with_metadata_named(
        &self,
        pr: &crate::services::pr_registry::PrEntry,
        caller_bb: &BirthBranch,
        reviewer_branch_name: &str,
        acceptance_criteria: &[String],
    ) -> Result<SpawnResult> {
        let reviewer_identity =
            AgentIdentity::new(slugify(reviewer_branch_name), self.reviewer_agent_type);
        let reviewer_internal_name = reviewer_identity.internal_name().to_string();
        let reviewer_birth_branch = caller_bb.child(&reviewer_internal_name).to_string();
        let result = self
            .spawn_reviewer_subtree_with_criteria_named(
                pr,
                caller_bb,
                reviewer_branch_name,
                acceptance_criteria,
            )
            .await?;
        self.persist_reviewer_assignment(pr, &reviewer_internal_name, &reviewer_birth_branch)
            .await;
        Ok(result)
    }

    async fn persist_reviewer_assignment(
        &self,
        pr: &crate::services::pr_registry::PrEntry,
        reviewer_internal_name: &str,
        reviewer_birth_branch: &str,
    ) {
        let Some(forgejo) = self.ctx.forgejo_client() else {
            return;
        };
        match crate::services::repo::get_repo_info(self.project_dir()).await {
            Ok(repo_info) => {
                let base_branch = BranchName::try_from_str(pr.base_branch.as_str())
                    .expect("validated string input is non-empty");
                let body = append_reviewer_metadata(
                    &pr.body,
                    reviewer_internal_name,
                    reviewer_birth_branch,
                );
                if let Err(err) = forgejo
                    .update_pull_request(
                        &repo_info.owner,
                        &repo_info.repo,
                        crate::domain::PRNumber::new(pr.number),
                        &pr.title,
                        &body,
                        &base_branch,
                    )
                    .await
                {
                    tracing::warn!(
                        pr_number = pr.number,
                        error = %err,
                        "Failed to persist reviewer assignment to Forgejo PR body"
                    );
                }
            }
            Err(err) => tracing::warn!(
                pr_number = pr.number,
                error = %err,
                "Failed to resolve repository while persisting reviewer assignment"
            ),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use exomonad_test_support::{
        assert_fixture_git_root, init_fixture_git_repository, ScrubGitRepositoryEnv,
    };

    #[test]
    fn test_opencode_dev_instructions_clarify_mcp_tools_are_not_shell_commands() {
        assert!(OPENCODE_DEV_INSTRUCTIONS
            .contains("MCP tools exposed inside your agent tool interface"));
        assert!(OPENCODE_DEV_INSTRUCTIONS.contains("not shell commands"));
        assert!(OPENCODE_DEV_INSTRUCTIONS.contains("not on PATH"));
        assert!(OPENCODE_DEV_INSTRUCTIONS.contains("which file_pr"));
    }

    #[tokio::test]
    async fn test_find_existing_leaf_worktree_by_slug_uses_recorded_agent_type_suffix() {
        let temp_dir = tempfile::tempdir().unwrap();
        let worktree_base = temp_dir.path().join("worktrees");
        fs::create_dir_all(worktree_base.join("resume-leaf-opencode"))
            .await
            .unwrap();

        let (identity, path) = find_existing_leaf_worktree_by_slug(&worktree_base, "resume-leaf")
            .await
            .unwrap()
            .unwrap();

        assert_eq!(identity.slug(), "resume-leaf");
        assert_eq!(identity.agent_type(), AgentType::OpenCode);
        assert_eq!(path, worktree_base.join("resume-leaf-opencode"));
    }

    #[tokio::test]
    async fn test_find_existing_leaf_worktree_by_slug_rejects_prefix_collision() {
        let temp_dir = tempfile::tempdir().unwrap();
        let worktree_base = temp_dir.path().join("worktrees");
        fs::create_dir_all(worktree_base.join("resume-leaf-extra-opencode"))
            .await
            .unwrap();

        let found = find_existing_leaf_worktree_by_slug(&worktree_base, "resume-leaf")
            .await
            .unwrap();

        assert!(found.is_none());
    }

    #[test]
    fn test_parse_git_status_paths_lists_status_entries() {
        let paths = parse_git_status_paths(
            b" M src/lib.rs\0A  docs/new.md\0R  new.rs\0old.rs\0?? scratch.txt\0",
        );
        assert_eq!(
            paths,
            vec![
                "src/lib.rs".to_string(),
                "docs/new.md".to_string(),
                "new.rs".to_string(),
                "old.rs".to_string(),
                "scratch.txt".to_string(),
            ]
        );
    }

    #[test]
    fn test_parse_git_status_paths_preserves_spaces_and_quotes() {
        let paths = parse_git_status_paths(b"?? user file.txt\0?? \"quoted file.txt\"\0");

        assert_eq!(paths, vec!["user file.txt", "\"quoted file.txt\""]);
    }

    #[test]
    fn test_tl_runtime_checkpoint_is_the_only_exempt_subtree() {
        assert!(is_tl_runtime_checkpoint(".exo/tl-loop/root/run.json"));
        assert!(is_tl_runtime_checkpoint("./.exo/tl-loop"));
        assert!(!is_tl_runtime_checkpoint(".exo/config.toml"));
        assert!(!is_tl_runtime_checkpoint("src/.exo/tl-loop/file"));
    }

    async fn run_git_test_command(worktree: &Path, args: &[&str]) {
        assert_fixture_git_root(worktree).unwrap();
        let output = Command::new("git")
            .args(args)
            .current_dir(worktree)
            .scrub_git_repository_env()
            .output()
            .await
            .unwrap_or_else(|error| panic!("failed to run git {args:?}: {error}"));
        assert!(
            output.status.success(),
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }

    async fn init_test_repo(worktree: &Path) {
        init_fixture_git_repository(worktree).unwrap();
        run_git_test_command(worktree, &["config", "user.email", "test@example.com"]).await;
        run_git_test_command(worktree, &["config", "user.name", "Test User"]).await;
    }

    #[tokio::test]
    async fn test_ensure_clean_spawn_worktree_ignores_gitignored_tracked_files() {
        let temp_dir = tempfile::tempdir().unwrap();
        let worktree = temp_dir.path();
        init_test_repo(worktree).await;

        fs::write(worktree.join(".gitignore"), "*.db\n")
            .await
            .unwrap();
        fs::create_dir_all(worktree.join(".chainlink"))
            .await
            .unwrap();
        fs::write(worktree.join(".chainlink/issues.db"), "before")
            .await
            .unwrap();
        run_git_test_command(worktree, &["add", ".gitignore"]).await;
        run_git_test_command(worktree, &["add", "-f", ".chainlink/issues.db"]).await;
        run_git_test_command(worktree, &["commit", "-q", "-m", "init"]).await;

        fs::write(worktree.join(".chainlink/issues.db"), "after")
            .await
            .unwrap();

        ensure_clean_spawn_worktree(worktree).await.unwrap();
    }

    #[tokio::test]
    async fn test_ensure_clean_spawn_worktree_blocks_nonignored_dirty_files() {
        let temp_dir = tempfile::tempdir().unwrap();
        let worktree = temp_dir.path();
        init_test_repo(worktree).await;

        fs::create_dir_all(worktree.join("src")).await.unwrap();
        fs::write(worktree.join("src/lib.rs"), "before")
            .await
            .unwrap();
        run_git_test_command(worktree, &["add", "src/lib.rs"]).await;
        run_git_test_command(worktree, &["commit", "-q", "-m", "init"]).await;

        fs::write(worktree.join("src/lib.rs"), "after")
            .await
            .unwrap();

        let message = ensure_clean_spawn_worktree(worktree)
            .await
            .unwrap_err()
            .to_string();
        assert!(message.contains("src/lib.rs"));
        assert!(message.contains("dirty TL worktree"));
    }

    #[tokio::test]
    async fn test_ensure_clean_spawn_worktree_ignores_runtime_checkpoints_only() {
        let temp_dir = tempfile::tempdir().unwrap();
        let worktree = temp_dir.path();
        init_test_repo(worktree).await;

        fs::create_dir_all(worktree.join(".exo/tl-loop/root"))
            .await
            .unwrap();
        fs::write(worktree.join(".exo/tl-loop/root/run.json"), "checkpoint")
            .await
            .unwrap();
        ensure_clean_spawn_worktree(worktree).await.unwrap();

        fs::write(
            worktree.join(".exo/config.toml"),
            "spawn_agent_type = \"codex\"\n",
        )
        .await
        .unwrap();
        let message = ensure_clean_spawn_worktree(worktree)
            .await
            .unwrap_err()
            .to_string();
        assert!(message.contains(".exo/config.toml"));
    }

    #[test]
    fn test_dirty_spawn_error_includes_count_files_and_recovery() {
        let files = vec!["src/lib.rs".to_string(), "docs/new.md".to_string()];
        let message = dirty_spawn_error(&files).to_string();
        assert!(message.contains(
            "BLOCKED: cannot spawn agent into a dirty TL worktree. 2 file(s) have uncommitted changes:"
        ));
        assert!(message.contains("  src/lib.rs"));
        assert!(message.contains("  docs/new.md"));
        assert!(message.contains("Commit the scaffold"));
        assert!(message.contains("discard_worker_output"));
        assert!(message.contains("dev-leaves fork from your branch HEAD"));
    }

    #[test]
    fn test_format_worker_age_uses_readable_units() {
        assert_eq!(format_worker_age(std::time::Duration::from_secs(42)), "42s");
        assert_eq!(format_worker_age(std::time::Duration::from_secs(125)), "2m");
        assert_eq!(
            format_worker_age(std::time::Duration::from_secs(7_400)),
            "2h"
        );
    }

    #[test]
    fn test_active_worker_error_includes_name_options_and_scope() {
        let worker = ActiveWorker {
            name: "alpha-codex".to_string(),
            age: "2m".to_string(),
        };
        let message = active_worker_error(&worker).to_string();
        assert!(message.contains("Active worker in this TL worktree: `alpha-codex`"));
        assert!(message.contains("spawned 2m ago"));
        assert!(message.contains("Wait for the active worker's handoff"));
        assert!(message.contains("Use `spawn_leaf` for parallel work"));
        assert!(message.contains("Per-worker attribution"));
    }

    #[tokio::test]
    async fn test_reviewer_hook_preflight_reports_missing_socket() {
        let dir = tempfile::tempdir().unwrap();

        let error = preflight_reviewer_hook_environment(dir.path())
            .await
            .unwrap_err();
        let message = error.to_string();

        assert!(
            message.contains("Reviewer hook preflight failed: parent server socket missing"),
            "unexpected error: {message}"
        );
        assert!(message.contains(".exo/server.sock"));
    }

    #[tokio::test]
    async fn test_write_opencode_git_stub_points_to_project_git_dir() {
        let dir = tempfile::tempdir().unwrap();
        let project_dir = dir.path().join("project");
        let agent_config_dir = dir.path().join("agent-config");
        fs::create_dir_all(&agent_config_dir).await.unwrap();

        AgentControlService::<crate::services::Services>::write_opencode_git_stub(
            &agent_config_dir,
            &project_dir,
        )
        .await
        .unwrap();

        let content = fs::read_to_string(agent_config_dir.join(".git"))
            .await
            .unwrap();
        assert_eq!(
            content,
            format!("gitdir: {}\n", project_dir.join(".git").display())
        );
    }

    #[test]
    fn test_reviewer_harness_denies_builtin_edit_tools() {
        let denied = reviewer_harness_denied_tools();

        for tool in [
            "Edit",
            "Write",
            "MultiEdit",
            "NotebookEdit",
            "spawn_leaf",
            "spawn_worker",
            "merge_pr",
            "file_pr",
        ] {
            assert!(
                denied.contains(&tool.to_string()),
                "reviewer harness permissions must deny {tool}"
            );
        }
    }

    #[test]
    fn test_coding_profiles_use_one_shot_assignment_handoff() {
        for instructions in [OPENCODE_DEV_INSTRUCTIONS, CODEX_DEV_INSTRUCTIONS] {
            assert!(instructions.contains("one assignment"));
            assert!(instructions.contains("exact invocation"));
            assert!(instructions.contains("resume_pr"));
            assert!(instructions.contains("exit"));
            assert!(!instructions.contains("Stay active"));
            assert!(!instructions.contains("Do not exit or consider yourself done"));
        }

        assert!(CODEX_REVIEWER_INSTRUCTIONS.contains("one exact PR/SHA assignment"));
        assert!(CODEX_REVIEWER_INSTRUCTIONS.contains("Exit after submitting"));
        assert!(CODEX_REVIEWER_INSTRUCTIONS.contains("fresh SHA-scoped reviewer invocation"));
        assert!(CODEX_REVIEWER_INSTRUCTIONS.contains("never wait for CI, merge-ready"));
    }

    /// Regression test for a reviewer sandbox/instructions mismatch: `codex_config.rs`
    /// hardcodes `network_access = false` for the Codex reviewer profile, so any
    /// developer instructions that tell the reviewer to hit Forgejo over the network
    /// from its own shell (curl, fj, wget) can never actually run. The verdict must
    /// route through the MCP tools instead, which execute in the unsandboxed
    /// ExoMonad host process. See docs/decisions/agent-sandbox-profiles.md.
    #[test]
    fn test_codex_reviewer_instructions_do_not_require_sandboxed_network_access() {
        let lower = CODEX_REVIEWER_INSTRUCTIONS.to_lowercase();
        // Checks for actual shell invocations, not just the word "curl"/"fj" — the
        // instructions are allowed to mention them by name when explaining why the
        // reviewer must NOT invoke them directly (network_access = false).
        assert!(
            !lower.contains("curl -") && !lower.contains("curl http"),
            "reviewer instructions must not tell the sandboxed shell to curl Forgejo directly: {CODEX_REVIEWER_INSTRUCTIONS}"
        );
        assert!(
            !lower.contains("fj pr review") && !lower.contains("fj pr view") && !lower.contains("fj pr files"),
            "reviewer instructions must not tell the sandboxed shell to run fj against Forgejo: {CODEX_REVIEWER_INSTRUCTIONS}"
        );
        assert!(
            CODEX_REVIEWER_INSTRUCTIONS.contains("approve_pr")
                && CODEX_REVIEWER_INSTRUCTIONS.contains("request_changes"),
            "reviewer instructions must submit verdicts through the approve_pr/request_changes MCP tools"
        );
    }

    #[test]
    fn test_render_reviewer_context_section_resolves_relative_paths_against_project_dir() {
        let project_dir = std::path::PathBuf::from("/tmp/exo-project");
        let ctx = vec![
            ".exo/context/reviewer-checklist.md".to_string(),
            "AGENTS.md".to_string(),
        ];
        let section = render_reviewer_context_section(&ctx, &project_dir);
        assert!(
            section.contains("/tmp/exo-project/.exo/context/reviewer-checklist.md"),
            "relative paths must be joined with project_dir; got: {section}"
        );
        assert!(
            section.contains("/tmp/exo-project/AGENTS.md"),
            "second relative path must also be resolved; got: {section}"
        );
        assert!(
            section.starts_with("\n\nRead first:\n"),
            "header line must precede the bullet list; got: {section}"
        );
    }

    #[test]
    fn test_render_reviewer_context_section_passes_absolute_paths_through() {
        let project_dir = std::path::PathBuf::from("/tmp/exo-project");
        let ctx = vec!["/etc/some/absolute.md".to_string()];
        let section = render_reviewer_context_section(&ctx, &project_dir);
        assert!(
            section.contains("/etc/some/absolute.md"),
            "absolute paths must pass through; got: {section}"
        );
        assert!(
            !section.contains("/tmp/exo-project/etc"),
            "absolute paths must NOT be joined with project_dir; got: {section}"
        );
    }

    #[test]
    fn test_render_reviewer_context_section_empty_emits_nothing() {
        let project_dir = std::path::PathBuf::from("/tmp/exo-project");
        let section = render_reviewer_context_section(&[], &project_dir);
        assert!(
            section.is_empty(),
            "empty ctx must emit no header; got: {section}"
        );
    }

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

    #[tokio::test]
    async fn dispatch_intent_is_atomic_and_precedes_spawn_resources() {
        let temp_dir = tempfile::tempdir().unwrap();
        let agent = AgentIdentity::new("leaf-a".to_string(), AgentType::Codex).internal_name();

        persist_dispatch_intent(temp_dir.path(), &agent, Some("intent-a"))
            .await
            .unwrap();

        let agent_dir = temp_dir.path().join(".exo/agents").join(agent.as_str());
        assert_eq!(
            fs::read_to_string(agent_dir.join("dispatch_intent"))
                .await
                .unwrap(),
            "intent-a"
        );
        assert!(!agent_dir.join("dispatch_intent.tmp").exists());
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
