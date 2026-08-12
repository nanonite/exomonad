use crate::app_state::AppState;
use crate::control;
use crate::control_read_model;
use exomonad::config::{Config, REVIEWER_MAX_ROUNDS_ENV};
use std::time::Duration;

use anyhow::{Context, Result};
use axum::{
    body::Bytes,
    extract::{Extension, Path, Query, State},
    http::Request,
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use exomonad_core::protocol::Runtime as HookRuntime;
use exomonad_core::services::{
    capture_memory, git, tmux_events, HasGitWorktreeService, MemoryCapture, MemoryFilter,
    MemoryKind,
};
use exomonad_core::{
    AgentName, BirthBranch, ClaudePreToolUseOutput, HookEnvelope, HookEventType, HookInput,
    InternalAfterModelOutput, InternalBeforeModelOutput, InternalStopHookOutput, PluginManager,
    Role, RuntimeBuilder, StopDecision,
};
use std::collections::HashMap;
use std::path::{Path as StdPath, PathBuf};
use std::sync::Arc;
use tower_http::cors::{Any, CorsLayer};
use tower_http::trace::TraceLayer;
use tracing::{debug, error, info, instrument, warn, Instrument};

fn parse_reviewer_max_rounds_override(value: Option<&str>) -> Result<Option<u32>> {
    let Some(value) = value.filter(|value| !value.trim().is_empty()) else {
        return Ok(None);
    };
    let rounds = value.parse::<u32>().with_context(|| {
        format!("Invalid {REVIEWER_MAX_ROUNDS_ENV} value `{value}`: expected a positive integer")
    })?;
    if rounds == 0 {
        anyhow::bail!("Invalid {REVIEWER_MAX_ROUNDS_ENV} value `0`: must be at least 1");
    }
    Ok(Some(rounds))
}

fn reviewer_max_rounds_override_from_env() -> Result<Option<u32>> {
    let value = match std::env::var(REVIEWER_MAX_ROUNDS_ENV) {
        Ok(value) => Some(value),
        Err(std::env::VarError::NotPresent) => None,
        Err(error) => anyhow::bail!("Invalid {REVIEWER_MAX_ROUNDS_ENV}: {error}"),
    };
    parse_reviewer_max_rounds_override(value.as_deref())
}

// ============================================================================
// Config Helpers
// ============================================================================

/// Convert typed `McpServerConfig` entries into pre-serialized JSON values
/// for propagation to spawned agent configs.
fn serialize_extra_mcp_servers(
    servers: &HashMap<String, exomonad::config::McpServerConfig>,
) -> HashMap<String, serde_json::Value> {
    servers
        .iter()
        .map(|(name, server)| {
            let value = match server {
                exomonad::config::McpServerConfig::Http { url, headers } => {
                    let mut e = serde_json::json!({"type": "http", "url": url});
                    if !headers.is_empty() {
                        e["headers"] = serde_json::to_value(headers).unwrap_or_default();
                    }
                    e
                }
                exomonad::config::McpServerConfig::Stdio { command, args } => {
                    serde_json::json!({"type": "stdio", "command": command, "args": args})
                }
            };
            (name.clone(), value)
        })
        .collect()
}

// ============================================================================
// REST API Types
// ============================================================================

/// Request body for POST /agents/{role}/{name}/tools/call.
#[derive(serde::Deserialize)]
pub struct ToolCallRequest {
    pub name: String,
    #[serde(default)]
    pub arguments: serde_json::Value,
}

/// Query parameters for the `/hook` endpoint.
#[derive(Debug, serde::Deserialize)]
pub struct HookQueryParams {
    pub event: HookEventType,
    pub runtime: HookRuntime,
    pub role: Option<String>,
    /// Agent identity (forwarded from caller's env).
    pub agent_id: Option<String>,
    /// TL session ID for event routing (forwarded from caller's env).
    pub session_id: Option<String>,
    /// CHAINLINK_DB value from the agent-side hook process.
    pub chainlink_db: Option<String>,
}

#[derive(Debug, Default, serde::Deserialize)]
struct ControlTransitionQuery {
    limit: Option<usize>,
}

/// Server-side hook handler state, shared across requests.
#[derive(Clone)]
pub struct HookState {
    pub plugins: Arc<tokio::sync::RwLock<HashMap<AgentName, Arc<PluginManager>>>>,
    pub registry: Arc<exomonad_core::effects::EffectRegistry>,
    pub wasm_path: PathBuf,
    pub project_dir: PathBuf,
    pub tmux_session: String,
    pub default_role: Role,
    pub event_log: Option<Arc<exomonad_core::services::EventLog>>,
    pub agent_resolver: Arc<exomonad_core::services::AgentResolver>,
    pub inbox_store: Arc<exomonad_core::services::InboxStore>,
    pub session_memory: Arc<exomonad_core::services::SessionMemoryService>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct TurnEndCounts {
    backlog_ready_count: usize,
    pending_children: usize,
    unread_inbox: usize,
}

fn parse_ready_count(output: &str) -> usize {
    output
        .lines()
        .filter(|line| line.trim_start().starts_with('#'))
        .count()
}

async fn query_ready_count(project_dir: &StdPath, chainlink_db: Option<&str>) -> usize {
    let default_db = project_dir.join(".chainlink");
    let db_path = chainlink_db.map(PathBuf::from).unwrap_or(default_db);
    let output = tokio::process::Command::new("chainlink")
        .args(["ready"])
        .current_dir(project_dir)
        .env("CHAINLINK_DB", db_path)
        .output()
        .await;

    match output {
        Ok(output) if output.status.success() => {
            parse_ready_count(&String::from_utf8_lossy(&output.stdout))
        }
        Ok(output) => {
            warn!(status = ?output.status.code(), "TurnEnd ready-count query failed; using zero");
            0
        }
        Err(error) => {
            warn!(error = %error, "TurnEnd ready-count query could not start; using zero");
            0
        }
    }
}

fn pending_child_count(
    records: &[exomonad_core::services::AgentIdentityRecord],
    agent_name: &AgentName,
    birth_branch: &BirthBranch,
) -> usize {
    records
        .iter()
        .filter(|record| record.agent_name != *agent_name && record.parent_branch == *birth_branch)
        .count()
}

fn latest_memory_was_spawned(
    memory: &exomonad_core::services::SessionMemoryService,
    agent_name: &AgentName,
) -> bool {
    memory
        .list(MemoryFilter {
            agent_id: Some(agent_name.to_string()),
            ..Default::default()
        })
        .ok()
        .and_then(|records| records.last().map(|record| record.kind))
        == Some(MemoryKind::SpawnedChild)
}

fn asked_human(input: &HookInput, output: &InternalStopHookOutput) -> bool {
    input
        .reason
        .as_deref()
        .into_iter()
        .chain(output.reason.as_deref())
        .any(|reason| {
            let reason = reason.to_ascii_lowercase();
            reason.contains("human")
                || reason.contains("clarif")
                || reason.contains("asked for")
                || reason.contains("ask the")
                || reason.contains("request")
        })
}

fn turn_end_reason(
    event_type: HookEventType,
    runtime: HookRuntime,
    input: &HookInput,
    output: &InternalStopHookOutput,
    counts: TurnEndCounts,
    spawned_child: bool,
) -> &'static str {
    if runtime == HookRuntime::Codex || event_type == HookEventType::SubagentStop {
        return "exited";
    }
    if asked_human(input, output) {
        return "asked_human";
    }
    if counts.backlog_ready_count > 0 && spawned_child {
        return "spawned_and_idle";
    }
    if counts.pending_children > 0 {
        return "waiting_on_children";
    }
    if counts.backlog_ready_count > 0 {
        return "stopped_backlog_nonempty";
    }
    "asked_human"
}

async fn collect_turn_end_counts(
    state: &HookState,
    agent_name: &AgentName,
    birth_branch: &BirthBranch,
    chainlink_db: Option<&str>,
) -> TurnEndCounts {
    let backlog_ready_count = query_ready_count(&state.project_dir, chainlink_db).await;
    let records = state.agent_resolver.all().await;
    let pending_children = pending_child_count(&records, agent_name, birth_branch);
    let unread_inbox = state
        .inbox_store
        .unread_count(agent_name.as_str())
        .unwrap_or_else(|error| {
            warn!(agent = %agent_name, error = %error, "TurnEnd inbox count failed; using zero");
            0
        });
    TurnEndCounts {
        backlog_ready_count,
        pending_children,
        unread_inbox,
    }
}

async fn emit_turn_end_memory(
    params: &HookQueryParams,
    state: &HookState,
    input: &HookInput,
    output: &InternalStopHookOutput,
    agent_name: &AgentName,
    birth_branch: &BirthBranch,
) {
    let counts = collect_turn_end_counts(
        state,
        agent_name,
        birth_branch,
        params.chainlink_db.as_deref(),
    )
    .await;
    let spawned_child = latest_memory_was_spawned(&state.session_memory, agent_name);
    let reason = turn_end_reason(
        params.event,
        params.runtime,
        input,
        output,
        counts,
        spawned_child,
    );
    let identity = state.agent_resolver.get(agent_name).await;
    let agent_type = identity
        .as_ref()
        .map(|record| record.agent_type.suffix().to_string())
        .unwrap_or_else(|| params.runtime.to_string());
    let context = exomonad_core::effects::EffectContext {
        agent_name: agent_name.clone(),
        birth_branch: birth_branch.clone(),
        working_dir: exomonad_core::services::agent_control::resolve_working_dir(
            birth_branch.as_str(),
        ),
    };
    capture_memory(
        &context,
        &state.session_memory,
        MemoryCapture {
            issue_id: None,
            kind: MemoryKind::TurnEnd,
            importance: 50,
            summary: format!("Turn ended: {reason}"),
            detail: None,
            metadata: Some(serde_json::json!({
                "reason": reason,
                "backlog_ready_count": counts.backlog_ready_count,
                "pending_children": counts.pending_children,
                "unread_inbox": counts.unread_inbox,
                "agent_type": agent_type,
                "model": identity.as_ref().and_then(|record| record.model.clone()),
                "effort": identity.as_ref().and_then(|record| record.effort.clone()),
            })),
        },
    );
}

// ============================================================================
// Per-Role WASM Resolution
// ============================================================================

pub fn resolve_wasm_path_for_role(
    wasm_dir: &StdPath,
    role: &str,
    default_name: &str,
) -> Option<PathBuf> {
    let role_specific = wasm_dir.join(format!("wasm-guest-{role}.wasm"));
    if role_specific.exists() {
        return Some(role_specific);
    }
    let default_path = wasm_dir.join(format!("wasm-guest-{default_name}.wasm"));
    if default_path.exists() {
        return Some(default_path);
    }
    None
}

// ============================================================================
// Per-Agent Plugin Cache
// ============================================================================

pub async fn get_or_create_plugin(
    plugins: &tokio::sync::RwLock<HashMap<AgentName, Arc<PluginManager>>>,
    agent_name: AgentName,
    birth_branch: BirthBranch,
    registry: &Arc<exomonad_core::effects::EffectRegistry>,
    wasm_path: &StdPath,
) -> anyhow::Result<Arc<PluginManager>> {
    // Fast path: existing plugin
    {
        let cache = plugins.read().await;
        if let Some(p) = cache.get(&agent_name) {
            return Ok(p.clone());
        }
    }

    // Slow path: create new per-agent plugin
    let working_dir =
        exomonad_core::services::agent_control::resolve_working_dir(birth_branch.as_str());
    let ctx = exomonad_core::effects::EffectContext {
        agent_name: agent_name.clone(),
        birth_branch,
        working_dir,
    };
    let p = Arc::new(
        PluginManager::from_file(wasm_path, registry.clone(), ctx)
            .await
            .with_context(|| format!("Failed to create plugin for agent {}", agent_name))?,
    );
    let mut cache = plugins.write().await;
    // Re-check after acquiring write lock to avoid TOCTOU race
    if let Some(existing) = cache.get(&agent_name) {
        return Ok(existing.clone());
    }
    cache.insert(agent_name, p.clone());
    Ok(p)
}

pub async fn resolve_plugin(
    plugins: &tokio::sync::RwLock<HashMap<AgentName, Arc<PluginManager>>>,
    registry: &Arc<exomonad_core::effects::EffectRegistry>,
    worktree_base: &StdPath,
    name: &str,
    wasm_path: &StdPath,
    agent_resolver: Option<&exomonad_core::services::AgentResolver>,
) -> anyhow::Result<Arc<PluginManager>> {
    let agent_name = AgentName::try_from_str(name).context("agent name must not be empty")?;

    // Root's birth branch is written to .exo/agents/root/.birth_branch by init.
    // Re-resolve on every request to detect branch changes between sessions.
    if name == "root" {
        let birth_branch = resolve_agent_birth_branch(worktree_base, name).await?;
        {
            let cache = plugins.read().await;
            if let Some(p) = cache.get(&agent_name) {
                if p.effect_context().birth_branch == birth_branch {
                    return Ok(p.clone());
                }
                tracing::info!(
                    old = %p.effect_context().birth_branch,
                    new = %birth_branch,
                    "Root birth branch changed, recreating plugin"
                );
            }
        }
        let working_dir =
            exomonad_core::services::agent_control::resolve_working_dir(birth_branch.as_str());
        let ctx = exomonad_core::effects::EffectContext {
            agent_name: agent_name.clone(),
            birth_branch,
            working_dir,
        };
        let p = Arc::new(
            PluginManager::from_file(wasm_path, registry.clone(), ctx)
                .await
                .with_context(|| "Failed to create plugin for root agent")?,
        );
        let mut cache = plugins.write().await;
        cache.insert(agent_name, p.clone());
        return Ok(p);
    }

    // Non-root agents: resolver does in-memory lookup first, then probes disk.
    let (birth_branch, working_dir) = if let Some(resolver) = agent_resolver {
        resolver.resolve_or_probe(worktree_base, name).await?
    } else {
        let bb = resolve_agent_birth_branch(worktree_base, name).await?;
        let wd = exomonad_core::services::agent_control::resolve_working_dir(bb.as_str());
        (bb, wd)
    };

    let ctx = exomonad_core::effects::EffectContext {
        agent_name: agent_name.clone(),
        birth_branch,
        working_dir,
    };
    let p = Arc::new(
        PluginManager::from_file(wasm_path, registry.clone(), ctx)
            .await
            .with_context(|| format!("Failed to create plugin for agent {}", agent_name))?,
    );
    let mut cache = plugins.write().await;
    if let Some(existing) = cache.get(&agent_name) {
        return Ok(existing.clone());
    }
    cache.insert(agent_name, p.clone());
    Ok(p)
}

/// Birth branch resolution for the root agent at server startup.
///
/// Only used for the root agent before the resolver is fully initialized.
/// All other agents use `AgentResolver::resolve_or_probe()`.
///
/// Resolution order:
/// 1. identity.json (canonical, written by finalize_spawn)
/// 2. Worktree git branch (subtrees have their own git branch)
/// 3. .birth_branch file (legacy, written by spawn_worker)
/// 4. Fallback to root (with warning)
async fn resolve_agent_birth_branch(
    worktree_base: &StdPath,
    agent_name: &str,
) -> anyhow::Result<BirthBranch> {
    // 1. Try identity.json (canonical source)
    if let Some(exo_dir) = worktree_base.parent() {
        let identity_path = exo_dir
            .join("agents")
            .join(agent_name)
            .join("identity.json");
        if let Ok(contents) = tokio::fs::read_to_string(&identity_path).await {
            if let Ok(record) =
                serde_json::from_str::<exomonad_core::services::AgentIdentityRecord>(&contents)
            {
                tracing::debug!(agent = %agent_name, branch = %record.birth_branch, "Resolved birth branch from identity.json");
                return Ok(record.birth_branch);
            }
        }
    }

    // 2. Try worktree (subtrees have their own git branch)
    // Strip known suffixes to get the slug for worktree dir lookup.
    let slug = agent_name
        .trim_end_matches("-claude")
        .trim_end_matches("-codex")
        .trim_end_matches("-shoal")
        .trim_end_matches("-process");
    let worktree_path = worktree_base.join(slug);
    match tokio::process::Command::new("git")
        .args(["branch", "--show-current"])
        .current_dir(&worktree_path)
        .output()
        .await
    {
        Ok(output) if output.status.success() => {
            let branch = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !branch.is_empty() {
                tracing::debug!(agent = %agent_name, branch = %branch, "Resolved agent birth branch from worktree");
                return BirthBranch::try_from_str(branch.as_str())
                    .context("resolved git branch must not be empty");
            }
        }
        _ => {}
    }

    // 3. Try .birth_branch file (legacy fallback for workers)
    if let Some(exo_dir) = worktree_base.parent() {
        let bb_file = exo_dir
            .join("agents")
            .join(agent_name)
            .join(".birth_branch");
        if let Ok(contents) = tokio::fs::read_to_string(&bb_file).await {
            let branch = contents.trim().to_string();
            tracing::debug!(agent = %agent_name, branch = %branch, "Resolved agent birth branch from .birth_branch file");
            return BirthBranch::try_from_str(branch.as_str())
                .context(".birth_branch must not be empty");
        }
    }

    // 4. Fallback to root (with warning — this is a bug if it happens for non-root agents)
    tracing::warn!(
        agent = %agent_name,
        "Failed to resolve birth branch from identity.json, worktree, or .birth_branch — falling back to root"
    );
    BirthBranch::root().context("Failed to resolve root birth branch")
}

// ============================================================================
// Agent Identity Middleware
// ============================================================================

async fn agent_identity_middleware(
    Path((role, name)): Path<(String, String)>,
    State(state): State<AppState>,
    Extension(route_auth): Extension<control::RouteAuth>,
    mut request: Request<axum::body::Body>,
    next: Next,
) -> Response {
    if !route_auth.agent_request_authorized(request.headers()) {
        return control::unauthorized_response();
    }
    let wasm_path = resolve_wasm_path_for_role(&state.wasm_dir, &role, &state.wasm_name)
        .unwrap_or_else(|| state.wasm_path.clone());

    let plugin_result = resolve_plugin(
        &state.plugins,
        &state.registry,
        &state.worktree_base,
        &name,
        &wasm_path,
        Some(&state.agent_resolver),
    )
    .await;

    let parent = plugin_result
        .as_ref()
        .ok()
        .and_then(|p| p.effect_context().birth_branch.parent())
        .map(|p| p.to_string())
        .unwrap_or_default();

    if let Ok(ref plugin) = plugin_result {
        request.extensions_mut().insert(plugin.clone());
    }

    let span = tracing::info_span!(
        "agent_request",
        agent_id = %name,
        agent.role = %role,
        agent.parent = %parent,
        swarm.run_id = %state.run_id,
    );
    next.run(request).instrument(span).await
}

// ============================================================================
// Server-side Hook Handler Helpers
// ============================================================================

pub async fn handle_hook_inner(
    params: &HookQueryParams,
    state: &HookState,
    body: &str,
) -> Result<HookEnvelope> {
    let event_type = params.event;
    let runtime = params.runtime;
    let role = params
        .role
        .as_ref()
        .map(|r| Role::from(r.as_str()))
        .unwrap_or(state.default_role.clone());

    debug!(
        runtime = ?runtime,
        payload_len = body.len(),
        "Received hook event via HTTP"
    );

    // Emit HookReceived tmux event
    if let Ok(branch) = git::get_current_branch() {
        if let Some(agent_id_str) = git::extract_agent_id(branch.as_str()) {
            match exomonad_core::ui_protocol::AgentId::try_from(agent_id_str.clone()) {
                Ok(agent_id) => {
                    let event = exomonad_core::ui_protocol::AgentEvent::HookReceived {
                        agent_id,
                        hook_type: event_type.to_string(),
                        timestamp: tmux_events::now_iso8601(),
                    };
                    if let Err(e) = tmux_events::emit_event(&state.tmux_session, &event) {
                        warn!("Failed to emit hook:received event: {}", e);
                    }
                }
                Err(e) => warn!("Invalid agent_id in branch '{}': {}", agent_id_str, e),
            }
        }
    }

    let hook_trace = std::env::var("EXOMONAD_HOOK_TRACE").is_ok();

    // Parse and inject runtime
    let mut hook_input: HookInput =
        serde_json::from_str(body).context("Failed to parse hook input")?;
    hook_input.runtime = Some(runtime);

    if hook_trace {
        info!(
            runtime = ?runtime,
            event = ?event_type,
            tool = hook_input.tool_name.as_ref().map(|t| t.as_str()).unwrap_or("-"),
            agent = params.agent_id.as_deref().unwrap_or("root"),
            "[hook] received"
        );
    }

    // Classify hook event into dispatch category. Exhaustive match ensures new
    // event types get handled explicitly rather than silently falling through.
    #[derive(Debug, Clone, Copy)]
    enum HookDispatch {
        /// Stop/lifecycle hooks: WASM returns InternalStopHookOutput (allow/block + reason)
        Stop,
        /// Tool hooks: WASM returns ClaudePreToolUseOutput (allow/deny/ask)
        ToolUse,
        /// Worker exit: WASM handles notifyParent as side effect, returns simple allow
        WorkerExit,
        /// BeforeModel/AfterModel: passed through to WASM, response serialized as-is.
        /// Runtime-specific model hooks use these; the dispatch arm is runtime-agnostic.
        BeforeModel,
        AfterModel,
    }

    let dispatch = match event_type {
        HookEventType::Stop | HookEventType::AfterAgent => HookDispatch::Stop,
        HookEventType::SubagentStop => HookDispatch::Stop,
        HookEventType::SessionEnd => HookDispatch::Stop,
        HookEventType::PreToolUse => HookDispatch::ToolUse,
        HookEventType::BeforeTool => HookDispatch::ToolUse,
        HookEventType::BeforeModel => HookDispatch::BeforeModel,
        HookEventType::AfterModel => HookDispatch::AfterModel,
        HookEventType::PostToolUse => HookDispatch::ToolUse,
        HookEventType::WorkerExit => HookDispatch::WorkerExit,
        HookEventType::SessionStart => HookDispatch::ToolUse,
        HookEventType::Notification
        | HookEventType::SubagentStart
        | HookEventType::PreCompact
        | HookEventType::PermissionRequest
        | HookEventType::UserPromptSubmit => {
            debug!(event = ?event_type, "Hook type not handled by WASM, allowing");
            let output_json = serde_json::to_string(&ClaudePreToolUseOutput::default())
                .context("Failed to serialize output")?;

            return Ok(HookEnvelope {
                stdout: output_json,
                exit_code: 0,
            });
        }
    };

    // Normalize event name for WASM dispatch
    let normalized_event_name = match event_type {
        HookEventType::Stop | HookEventType::AfterAgent => "Stop",
        HookEventType::SubagentStop => "SubagentStop",
        HookEventType::SessionEnd => "SessionEnd",
        HookEventType::PreToolUse => "PreToolUse",
        HookEventType::BeforeTool => "PreToolUse",
        HookEventType::BeforeModel => "BeforeModel",
        HookEventType::AfterModel => "AfterModel",
        HookEventType::PostToolUse => "PostToolUse",
        HookEventType::WorkerExit => "WorkerExit",
        HookEventType::SessionStart => "SessionStart",
        _ => unreachable!("passthrough events returned early above"),
    };
    hook_input.hook_event_name = normalized_event_name.to_string();

    // Create role-aware input for unified WASM
    let mut hook_input_value =
        serde_json::to_value(&hook_input).context("Failed to serialize hook input")?;

    // Resolve agent identity: try resolver first for authoritative identity,
    // fall back to query params (which may be inaccurate for hooks fired before first tool call).
    let agent_name_for_hook = AgentName::try_from_str(params.agent_id.as_deref().unwrap_or("root"))
        .context("hook agent_id must not be empty")?;
    let birth_branch_for_hook =
        if let Some(record) = state.agent_resolver.get(&agent_name_for_hook).await {
            record.birth_branch
        } else {
            BirthBranch::try_from_str(params.session_id.as_deref().unwrap_or("main"))
                .context("hook session_id must not be empty")?
        };

    // Always inject identity into WASM input (hooks need it even when env vars aren't set)
    if let serde_json::Value::Object(ref mut map) = hook_input_value {
        map.insert("role".to_string(), serde_json::json!(role));
        map.insert(
            "agent_id".to_string(),
            serde_json::json!(agent_name_for_hook.to_string()),
        );
        map.insert(
            "exomonad_session_id".to_string(),
            serde_json::json!(birth_branch_for_hook.to_string()),
        );
        if let Some(ref chainlink_db) = params.chainlink_db {
            map.insert("chainlink_db".to_string(), serde_json::json!(chainlink_db));
        }
    }

    debug!(
        agent_name = %agent_name_for_hook,
        birth_branch = %birth_branch_for_hook,
        agent_id_from_param = ?params.agent_id,
        "Hook identity context"
    );

    let plugin = get_or_create_plugin(
        &state.plugins,
        agent_name_for_hook.clone(),
        birth_branch_for_hook.clone(),
        &state.registry,
        &state.wasm_path,
    )
    .await
    .context("Failed to get plugin for hook")?;

    match dispatch {
        HookDispatch::WorkerExit => {
            // WASM handles notifyParent as a side effect. We call it and return allow.
            let _: serde_json::Value = plugin
                .call("handle_pre_tool_use", &hook_input_value)
                .await
                .context("WASM handleWorkerExit failed")?;

            Ok(HookEnvelope {
                stdout: serde_json::to_string(&ClaudePreToolUseOutput::default())?,
                exit_code: 0,
            })
        }

        HookDispatch::Stop => {
            let internal_output: InternalStopHookOutput = plugin
                .call("handle_pre_tool_use", &hook_input_value)
                .await
                .context("WASM handle_pre_tool_use (stop) failed")?;

            let output_json = internal_output.to_runtime_json(&runtime);

            let decision_str = match internal_output.decision {
                StopDecision::Allow => "allow",
                StopDecision::Block => "block",
            };

            tracing::info!(
                otel.name = "hook.stop",
                agent_id = %agent_name_for_hook.as_str(),
                event_type = ?event_type,
                decision = %decision_str,
                reason = ?internal_output.reason,
                "[event] hook.stop"
            );
            if let Some(ref log) = state.event_log {
                let _ = log.append(
                    "hook.stop",
                    agent_name_for_hook.as_str(),
                    &serde_json::json!({
                        "event_type": format!("{:?}", event_type),
                        "decision": decision_str,
                        "reason": internal_output.reason,
                    }),
                );
            }

            if matches!(
                event_type,
                HookEventType::Stop | HookEventType::AfterAgent | HookEventType::SubagentStop
            ) {
                emit_turn_end_memory(
                    params,
                    state,
                    &hook_input,
                    &internal_output,
                    &agent_name_for_hook,
                    &birth_branch_for_hook,
                )
                .await;
            }

            // Emit StopHookBlocked tmux event
            if internal_output.decision == StopDecision::Block
                && event_type == HookEventType::SubagentStop
            {
                if let Ok(branch) = git::get_current_branch() {
                    if let Some(agent_id_str) = git::extract_agent_id(branch.as_str()) {
                        let reason = internal_output
                            .reason
                            .clone()
                            .unwrap_or_else(|| "Hook blocked agent stop".to_string());
                        match exomonad_core::ui_protocol::AgentId::try_from(agent_id_str.clone()) {
                            Ok(agent_id) => {
                                let event =
                                    exomonad_core::ui_protocol::AgentEvent::StopHookBlocked {
                                        agent_id,
                                        reason,
                                        timestamp: tmux_events::now_iso8601(),
                                    };
                                if let Err(e) = tmux_events::emit_event(&state.tmux_session, &event)
                                {
                                    warn!("Failed to emit stop_hook:blocked event: {}", e);
                                }
                            }
                            Err(e) => {
                                warn!("Invalid agent_id in branch '{}': {}", agent_id_str, e)
                            }
                        }
                    }
                }
            }

            Ok(HookEnvelope {
                stdout: output_json,
                exit_code: 0,
            })
        }

        HookDispatch::BeforeModel => {
            let output: InternalBeforeModelOutput = plugin
                .call("handle_pre_tool_use", &hook_input_value)
                .await
                .context("WASM handle_pre_tool_use (BeforeModel) failed")?;

            let output_json =
                serde_json::to_string(&output).context("Failed to serialize BeforeModel output")?;

            let exit_code = if output.continue_ { 0 } else { 2 };

            Ok(HookEnvelope {
                stdout: output_json,
                exit_code,
            })
        }

        HookDispatch::AfterModel => {
            let output: InternalAfterModelOutput = plugin
                .call("handle_pre_tool_use", &hook_input_value)
                .await
                .context("WASM handle_pre_tool_use (AfterModel) failed")?;

            let output_json =
                serde_json::to_string(&output).context("Failed to serialize AfterModel output")?;

            let exit_code = if output.continue_ { 0 } else { 2 };

            Ok(HookEnvelope {
                stdout: output_json,
                exit_code,
            })
        }

        HookDispatch::ToolUse => {
            let output: ClaudePreToolUseOutput = plugin
                .call("handle_pre_tool_use", &hook_input_value)
                .await
                .context("WASM handle_pre_tool_use failed")?;

            let output_json =
                serde_json::to_string(&output).context("Failed to serialize output")?;

            let exit_code = if output.continue_ { 0 } else { 2 };

            if hook_trace {
                info!(
                    runtime = ?runtime,
                    event = ?event_type,
                    tool = hook_input.tool_name.as_ref().map(|t| t.as_str()).unwrap_or("-"),
                    agent = params.agent_id.as_deref().unwrap_or("root"),
                    exit_code,
                    response = %output_json,
                    "[hook] dispatched"
                );
            }

            Ok(HookEnvelope {
                stdout: output_json,
                exit_code,
            })
        }
    }
}

// ============================================================================
// Axum Handlers
// ============================================================================

pub async fn health(State(state): State<AppState>) -> impl IntoResponse {
    let plugins: Vec<Arc<PluginManager>> = {
        let cache = state.plugins.read().await;
        cache.values().cloned().collect()
    };

    for plugin in &plugins {
        if let Err(e) = plugin.reload_if_changed().await {
            warn!(error = %e, "WASM hot-reload check failed during health probe");
        }
    }

    let root = AgentName::try_from_str("root").expect("literal agent name is non-empty");
    let plugin = {
        let cache = state.plugins.read().await;
        cache
            .get(&root)
            .cloned()
            .or_else(|| cache.values().next().cloned())
    };

    let wasm_hash = if let Some(p) = plugin {
        p.content_hash()
    } else {
        "unknown".to_string()
    };

    Json(serde_json::json!({
        "status": "ok",
        "version": env!("CARGO_PKG_VERSION"),
        "role": state.default_role.as_str(),
        "wasm_hash": wasm_hash,
    }))
}

async fn control_root() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "status": "ok",
        "authority": "control",
        "route_group": "/control",
    }))
}

async fn control_run(
    Path(run_id): Path<String>,
    Query(query): Query<ControlTransitionQuery>,
    State(state): State<AppState>,
) -> Response {
    control_read_response(control_read_model::read_run_model(
        &state.project_dir,
        &run_id,
        query.limit,
    ))
}

async fn control_slice(
    Path((run_id, slice_id)): Path<(String, String)>,
    State(state): State<AppState>,
) -> Response {
    control_read_response(control_read_model::read_slice_model(
        &state.project_dir,
        &run_id,
        &slice_id,
    ))
}

async fn control_transitions(
    Path(run_id): Path<String>,
    Query(query): Query<ControlTransitionQuery>,
    State(state): State<AppState>,
) -> Response {
    control_read_response(control_read_model::read_transitions_model(
        &state.project_dir,
        &run_id,
        query.limit,
    ))
}

fn control_read_response(
    result: Result<serde_json::Value, control_read_model::ReadModelError>,
) -> Response {
    match result {
        Ok(value) => Json(value).into_response(),
        Err(error) => {
            let status = match &error {
                control_read_model::ReadModelError::InvalidState(message)
                    if message.contains("limit") =>
                {
                    axum::http::StatusCode::BAD_REQUEST
                }
                control_read_model::ReadModelError::InvalidIdentifier(_) => {
                    axum::http::StatusCode::BAD_REQUEST
                }
                control_read_model::ReadModelError::MissingRun => axum::http::StatusCode::NOT_FOUND,
                control_read_model::ReadModelError::InvalidState(_)
                | control_read_model::ReadModelError::Io(_) => {
                    axum::http::StatusCode::INTERNAL_SERVER_ERROR
                }
            };
            (
                status,
                Json(serde_json::json!({"error": error.to_string()})),
            )
                .into_response()
        }
    }
}

#[instrument(skip_all, fields(hook = ?params.event, hook.type = %params.event, agent_id = tracing::field::Empty, agent.parent = tracing::field::Empty))]
pub async fn handle_hook_request(
    Query(params): Query<HookQueryParams>,
    State(state): State<HookState>,
    body: String,
) -> Json<HookEnvelope> {
    if let Some(ref id) = params.agent_id {
        tracing::Span::current().record("agent_id", id.as_str());
    }
    if let Some(ref sid) = params.session_id {
        tracing::Span::current().record("agent.parent", sid.as_str());
    }
    match handle_hook_inner(&params, &state, &body).await {
        Ok(envelope) => Json(envelope),
        Err(e) => {
            warn!(error = %e, "Hook handler failed, returning allow");
            Json(HookEnvelope {
                stdout: r#"{"continue":true}"#.to_string(),
                exit_code: 0,
            })
        }
    }
}

pub async fn list_tools(
    Path((role, _name)): Path<(String, String)>,
    Extension(plugin): Extension<Arc<PluginManager>>,
) -> impl IntoResponse {
    // Hot reload WASM if changed
    let _ = plugin.reload_if_changed().await;

    match exomonad_core::mcp::tools::get_tool_definitions(&plugin, Some(&role)).await {
        Ok(tools) => Json(serde_json::json!({ "tools": tools })).into_response(),
        Err(e) => {
            tracing::error!(error = %e, "Tool discovery failed");
            (
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": e.to_string()})),
            )
                .into_response()
        }
    }
}

fn append_unread_mail_block(
    output: &mut exomonad_core::mcp::tools::MCPCallOutput,
    messages: &[exomonad_core::services::InboxMessageRecord],
) {
    if !output.success || messages.is_empty() {
        return;
    }

    let block = format_unread_mail_block(messages);
    let Some(result) = output.result.as_mut() else {
        output.result = Some(serde_json::json!({
            "content": [{"type": "text", "text": block}],
            "isError": false
        }));
        return;
    };

    if append_to_mcp_text_content(result, &block) {
        return;
    }

    let original = serde_json::to_string_pretty(result).unwrap_or_default();
    *result = serde_json::json!({
        "content": [{"type": "text", "text": format!("{original}\n\n{block}")}],
        "isError": false
    });
}

fn append_to_mcp_text_content(result: &mut serde_json::Value, block: &str) -> bool {
    let Some(content) = result
        .get_mut("content")
        .and_then(serde_json::Value::as_array_mut)
    else {
        return false;
    };
    let Some(first) = content.first_mut() else {
        return false;
    };
    let Some(text) = first
        .get("text")
        .and_then(serde_json::Value::as_str)
        .map(str::to_string)
    else {
        return false;
    };
    first["text"] = serde_json::Value::String(format!("{text}\n\n{block}"));
    true
}

fn format_unread_mail_block(messages: &[exomonad_core::services::InboxMessageRecord]) -> String {
    let lines = messages
        .iter()
        .map(|message| {
            let summary = message.summary.as_deref().unwrap_or("No summary");
            format!(
                "[from: {}] Summary: {}. Full message: {}",
                message.from_agent, summary, message.content
            )
        })
        .collect::<Vec<_>>()
        .join("\n");
    format!("<unread-mail>\n{lines}\n</unread-mail>")
}

pub async fn call_tool(
    Path((role, name)): Path<(String, String)>,
    State(state): State<AppState>,
    Extension(plugin): Extension<Arc<PluginManager>>,
    Json(body): Json<ToolCallRequest>,
) -> impl IntoResponse {
    tracing::info!(tool = %body.name, "Executing tool");
    let tool_arguments = body.arguments.clone();

    let input = exomonad_core::mcp::tools::MCPCallInput::new(
        role.clone(),
        body.name.clone(),
        body.arguments,
    );

    let start = std::time::Instant::now();
    let result: Result<exomonad_core::mcp::tools::MCPCallOutput, anyhow::Error> =
        plugin.call("handle_mcp_call", &input).await;
    let duration_ms = start.elapsed().as_millis() as u64;

    let mut output = match result {
        Ok(o) => o,
        Err(e) => {
            tracing::error!(tool = %body.name, error = %e, "WASM call failed");
            tracing::info!(
                otel.name = "tool.called",
                tool_name = %body.name,
                duration_ms = duration_ms,
                success = false,
                error = %e,
                "[event] tool.called"
            );
            if let Some(ref log) = state.event_log {
                let _ = log.append(
                    "tool.called",
                    &name,
                    &serde_json::json!({
                        "tool_name": body.name,
                        "role": role,
                        "arguments": tool_arguments.clone(),
                        "duration_ms": duration_ms,
                        "success": false,
                        "error": e.to_string(),
                    }),
                );
            }
            return Json(serde_json::json!({
                "success": false,
                "result": null,
                "error": format!("WASM call failed: {}", e)
            }))
            .into_response();
        }
    };

    tracing::info!(
        otel.name = "tool.called",
        tool_name = %body.name,
        duration_ms = duration_ms,
        success = output.success,
        error = ?output.error,
        "[event] tool.called"
    );
    if let Some(ref log) = state.event_log {
        let _ = log.append(
            "tool.called",
            &name,
            &serde_json::json!({
                "tool_name": body.name,
                "role": role,
                "arguments": tool_arguments,
                "duration_ms": duration_ms,
                "success": output.success,
                "error": output.error,
            }),
        );
    }

    if output.success {
        match state.inbox_store.peek_unnotified(&name) {
            Ok(messages) => append_unread_mail_block(&mut output, &messages),
            Err(error) => {
                tracing::warn!(agent = %name, error = %error, "Failed to peek unread inbox messages")
            }
        }
    }

    Json(serde_json::json!({
        "success": output.success,
        "result": output.result,
        "error": output.error,
    }))
    .into_response()
}

pub async fn handle_events(
    State(queue): State<Arc<exomonad_core::services::event_queue::EventQueue>>,
    body: Bytes,
) -> impl IntoResponse {
    use exomonad_proto::effects::events::NotifyEventRequest;
    use prost::Message;

    match NotifyEventRequest::decode(body) {
        Ok(req) => {
            if let Some(event) = req.event {
                queue.notify_event(&req.session_id, event).await;
                (axum::http::StatusCode::OK, "OK")
            } else {
                (axum::http::StatusCode::BAD_REQUEST, "Missing event")
            }
        }
        Err(_) => (axum::http::StatusCode::BAD_REQUEST, "Invalid protobuf"),
    }
}

pub async fn reload(State(state): State<AppState>) -> impl IntoResponse {
    let mut cache = state.plugins.write().await;
    let evicted = cache.len();
    cache.clear();
    info!(plugins_evicted = evicted, "Plugin cache cleared (reload)");
    Json(serde_json::json!({
        "status": "ok",
        "plugins_evicted": evicted,
    }))
}

pub async fn shutdown_endpoint(
    State(signal): State<Arc<tokio::sync::Notify>>,
) -> impl IntoResponse {
    info!("Shutdown requested via /shutdown endpoint");
    signal.notify_waiters();
    Json(serde_json::json!({"status": "ok"}))
}

async fn shutdown_signal_future(signal: Arc<tokio::sync::Notify>, listener_name: &'static str) {
    let ctrl_c = tokio::signal::ctrl_c();
    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => info!(listener = listener_name, "Received SIGINT, initiating graceful shutdown"),
        _ = terminate => info!(listener = listener_name, "Received SIGTERM, initiating graceful shutdown"),
        _ = signal.notified() => info!(listener = listener_name, "Received /shutdown request, initiating graceful shutdown"),
    }
}

// ============================================================================
// Serve Command Runner
// ============================================================================

#[tracing::instrument(name = "exomonad.serve", skip_all)]
pub async fn run(config: &Config) -> Result<()> {
    // Extract TRACEPARENT from env if this is a child agent (parent injected it)
    if let Ok(tp) = std::env::var("TRACEPARENT") {
        use opentelemetry::propagation::TextMapPropagator;
        let propagator = opentelemetry_sdk::propagation::TraceContextPropagator::new();
        let mut carrier = std::collections::HashMap::new();
        carrier.insert("traceparent".to_string(), tp.clone());
        let parent_cx = propagator.extract(&carrier);
        use tracing_opentelemetry::OpenTelemetrySpanExt;
        tracing::Span::current().set_parent(parent_cx);
        info!(traceparent = %tp, "Inherited parent trace context");
    }

    let project_dir = {
        let raw = if config.project_dir.is_absolute() {
            config.project_dir.clone()
        } else {
            std::env::current_dir()?.join(&config.project_dir)
        };
        raw.canonicalize().unwrap_or(raw)
    };

    // Generate or load swarm run_id (persists across server restarts, resets on init --recreate)
    let run_id_path = project_dir.join(".exo/run_id");
    let run_id: Arc<str> = match std::fs::read_to_string(&run_id_path) {
        Ok(id) if !id.trim().is_empty() => id.trim().into(),
        _ => {
            let id = uuid::Uuid::new_v4().to_string();
            let _ = std::fs::write(&run_id_path, &id);
            info!(run_id = %id, path = %run_id_path.display(), "Generated new swarm run_id");
            id.into()
        }
    };
    // Set env so logging::init() picks it up as an OTel resource attribute in child processes
    std::env::set_var("EXOMONAD_SWARM_RUN_ID", &*run_id);

    let role_name = config.role.to_string();
    let wasm_dir = config.wasm_dir.clone();
    let wasm_name = config.wasm_name.clone();

    // Resolve default WASM (for root TL role and as fallback)
    let wasm_path =
        resolve_wasm_path_for_role(&wasm_dir, &role_name, &wasm_name).ok_or_else(|| {
            anyhow::anyhow!(
                "WASM file not found in {}
Run `exomonad recompile` first to build it.",
                wasm_dir.display()
            )
        })?;

    let server_pid_path = project_dir.join(".exo/server.pid");

    // Validate prerequisites
    exomonad_core::services::validate_git().context("Failed to validate git")?;

    let _secrets = exomonad_core::services::secrets::Secrets::load();
    let executor: Arc<dyn exomonad_core::services::command::CommandExecutor> =
        Arc::new(exomonad_core::services::local::LocalExecutor::new());
    let git = Arc::new(exomonad_core::services::git::GitService::new(executor));
    let git_wt = Arc::new(
        exomonad_core::services::git_worktree::GitWorktreeService::new(project_dir.clone()),
    );
    let http_forgejo_configured = matches!(
        (
            config.forgejo_url.as_deref(),
            config.forgejo_token.as_deref()
        ),
        (Some(_), Some(_))
    );
    let fj_backend_selected = !http_forgejo_configured
        && config.forgejo_url.is_none()
        && config.forgejo_token.is_none()
        && exomonad_core::services::ForgejoClient::fj_binary_in_path();

    let forgejo_client = match (
        config.forgejo_url.as_deref(),
        config.forgejo_token.as_deref(),
    ) {
        (Some(url), Some(token)) => Some(exomonad_core::services::ForgejoClient::new(url, token)?),
        (Some(_), None) => {
            warn!("forgejo_url configured without forgejo_token; Forgejo integration disabled");
            None
        }
        (None, Some(_)) => {
            warn!("forgejo_token configured without forgejo_url; Forgejo integration disabled");
            None
        }
        (None, None) if fj_backend_selected => {
            info!("[Forgejo] Using fj CLI backend (forgejo_url/forgejo_token not configured)");
            Some(exomonad_core::services::ForgejoClient::new_fj(
                project_dir.clone(),
            ))
        }
        (None, None) => {
            warn!(
                "[Forgejo] Not configured - spawn_reviewer, watcher_pr_state, file_pr will be unavailable"
            );
            None
        }
    };
    let forgejo_reviewer_client = if fj_backend_selected {
        forgejo_client.clone()
    } else {
        match (
            config.forgejo_url.as_deref(),
            config.forgejo_reviewer_token.as_deref(),
        ) {
            (Some(url), Some(token)) => {
                Some(exomonad_core::services::ForgejoClient::new(url, token)?)
            }
            (Some(_), None) => {
                warn!(
                    "forgejo_reviewer_token not configured; reviewer MCP tools cannot submit Forgejo PR reviews"
                );
                None
            }
            (None, Some(_)) => {
                warn!(
                    "forgejo_reviewer_token configured without forgejo_url; reviewer MCP tools cannot submit Forgejo PR reviews"
                );
                None
            }
            (None, None) => None,
        }
    };

    let team_registry = Arc::new(claude_teams_bridge::TeamRegistry::new());

    // Load canonical agent identity resolver from disk
    let agent_resolver =
        Arc::new(exomonad_core::services::AgentResolver::load(project_dir.clone()).await);
    let inbox_store = Arc::new(exomonad_core::services::InboxStore::open(&project_dir)?);
    let session_memory = Arc::new(exomonad_core::services::SessionMemoryService::open(
        &project_dir,
    )?);

    // JSONL event log (parallel to OTel span events, queryable via DuckDB/kaizen)
    let event_log = match exomonad_core::services::EventLog::open(project_dir.join(".exo/logs")) {
        Ok(log) => {
            info!(path = %project_dir.join(".exo/logs").display(), "Event log opened");
            Some(Arc::new(log))
        }
        Err(e) => {
            let _ = exomonad_core::services::sink_health::record_failure(
                &project_dir,
                exomonad_core::services::sink_health::current_session_id(&project_dir).as_deref(),
                &e.to_string(),
            );
            warn!(error = %e, "Failed to open event log, JSONL logging disabled");
            None
        }
    };

    let event_queue = Arc::new(exomonad_core::services::event_queue::EventQueue::new());
    let mutex_registry = Arc::new(exomonad_core::services::MutexRegistry::new());
    mutex_registry.spawn_expiry_task();
    let supervisor_registry = Arc::new(exomonad_core::services::SupervisorRegistry::new());
    let claude_session_registry =
        Arc::new(exomonad_core::services::claude_session_registry::ClaudeSessionRegistry::new());

    // Shared CI status map retained for compatibility with legacy webhook integrations,
    // read by MergePRHandler gate 7. Keyed by (branch, SHA) so stale CI from
    // an older push cannot satisfy the reviewer-approved commit gate.
    let ci_status_map = Arc::new(tokio::sync::RwLock::new(
        exomonad_core::services::CiStatusMap::new(),
    ));
    let watcher_runtime_state = Arc::new(exomonad_core::services::WatcherRuntimeState::new());

    // Build Services once — all shared registries in one struct
    let services = Arc::new(exomonad_core::services::Services {
        project_dir: project_dir.clone(),
        github_client: None,
        forgejo_client: forgejo_client.clone(),
        forgejo_reviewer_client: forgejo_reviewer_client.clone(),
        event_log: event_log.clone(),
        team_registry: team_registry.clone(),
        supervisor_registry,
        claude_session_registry,
        agent_resolver: agent_resolver.clone(),
        inbox_store: inbox_store.clone(),
        session_memory: session_memory.clone(),
        event_queue: event_queue.clone(),
        mutex_registry,
        git_wt,
        opencode_worker_model: config.opencode.worker_model.clone(),
        ci_status_map: ci_status_map.clone(),
        watcher_runtime_state: watcher_runtime_state.clone(),
    });

    let mut agent_control =
        exomonad_core::services::agent_control::AgentControlService::new(services.clone())
            .with_wasm_name(wasm_name.clone());
    let worktree_base = config.worktree_base.clone();
    agent_control = agent_control.with_worktree_base(worktree_base.clone());
    agent_control =
        agent_control.with_birth_branch(resolve_agent_birth_branch(&worktree_base, "root").await?);
    agent_control = agent_control.with_tmux_session(config.tmux_session.clone());
    agent_control = agent_control.with_yolo(config.yolo);
    agent_control = agent_control.with_spawn_agent_type(config.spawn_agent_type);
    agent_control = agent_control.with_spawn_agent_model(config.opencode.worker_model.clone());
    agent_control =
        agent_control.with_spawn_agent_effort(Some(config.worker_effort_level.level.to_string()));
    agent_control =
        agent_control.with_reviewer_effort(Some(config.reviewer_effort_level.level.to_string()));
    agent_control = agent_control.with_reviewer_agent_type(config.reviewer.agent_type);
    agent_control = agent_control.with_reviewer_model(config.reviewer.model.clone());
    agent_control = agent_control.with_reviewer_context(config.reviewer.context.clone());
    agent_control = agent_control
        .with_extra_mcp_servers(serialize_extra_mcp_servers(&config.extra_mcp_servers));
    let mut forgejo_spawn_env = exomonad_core::services::agent_control::ForgejoSpawnEnv::new(
        config.forgejo_url.clone(),
        config.forgejo_token.clone(),
        config.forgejo_reviewer_token.clone(),
    );
    if let Ok(repo_info) = exomonad_core::services::repo::get_repo_info(&project_dir).await {
        forgejo_spawn_env =
            forgejo_spawn_env.with_repo(repo_info.owner.to_string(), repo_info.repo.to_string());
    }
    agent_control = agent_control.with_forgejo_spawn_env(forgejo_spawn_env);
    let event_session_id = uuid::Uuid::new_v4().to_string();
    let agent_control = Arc::new(agent_control);
    // Shutdown signal shared by the /shutdown endpoint and shutdown_server effect.
    let shutdown_signal = Arc::new(tokio::sync::Notify::new());

    info!(
        wasm_path = %wasm_path.display(),
        role = %role_name,
        event_session_id = %event_session_id,
        "Starting MCP server on Unix domain socket (hot reload enabled)"
    );

    // Build runtime with handler groups
    let mut builder = RuntimeBuilder::new()
        .with_wasm_path(wasm_path.clone())
        .require_namespaces(vec![
            "log".to_string(),
            "kv".to_string(),
            "fs".to_string(),
            "git".to_string(),
            "agent".to_string(),
            "events".to_string(),
            "session".to_string(),
            "coordination".to_string(),
            "tasks".to_string(),
        ]);

    builder = builder.with_handlers(exomonad_core::core_handlers(
        project_dir.clone(),
        services.clone(),
    ));
    builder = builder.with_handlers(exomonad_core::git_handlers(services.clone(), git));
    builder = builder.with_handlers(exomonad_core::orchestration_handlers(
        agent_control.clone(),
        services.clone(),
        Some(event_session_id),
        shutdown_signal.clone(),
    ));
    let plugin_build_started_at = std::time::Instant::now();
    let rt = builder.build().await.context("Failed to build runtime")?;
    info!(
        wasm_path = %wasm_path.display(),
        elapsed_ms = plugin_build_started_at.elapsed().as_millis(),
        "WASM plugins ready for root role"
    );

    // Extract the shared registry for creating per-agent plugins
    let rt_registry = rt.registry.clone();
    let root_plugin = Arc::new(rt.plugin_manager);

    // Per-agent plugin cache — each agent gets its own PluginManager with baked-in identity
    let plugins: Arc<tokio::sync::RwLock<HashMap<AgentName, Arc<PluginManager>>>> =
        Arc::new(tokio::sync::RwLock::new(HashMap::new()));

    // Pre-populate with the root agent's plugin
    plugins.write().await.insert(
        AgentName::try_from_str("root").expect("literal agent name is non-empty"),
        root_plugin.clone(),
    );
    info!(
        plugin_count = plugins.read().await.len(),
        "Plugin cache initialized; server can dispatch root-role requests after listen"
    );

    // Check for existing server BEFORE writing our own PID
    let socket_path = project_dir.join(".exo/server.sock");
    if socket_path.exists() {
        if let Ok(content) = std::fs::read_to_string(&server_pid_path) {
            if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&content) {
                if let Some(pid) = parsed.get("pid").and_then(|v| v.as_u64()) {
                    use nix::sys::signal;
                    use nix::unistd::Pid;
                    let pid_i32 = pid as i32;
                    let is_self = pid_i32 == std::process::id() as i32;
                    if !is_self && signal::kill(Pid::from_raw(pid_i32), None).is_ok() {
                        return Err(anyhow::anyhow!(
                            "Server already running (PID {}). Stop it first or use a different project directory.",
                            pid
                        ));
                    }
                }
            }
        }
        info!(path = %socket_path.display(), "Removing stale socket");
        let _ = std::fs::remove_file(&socket_path);
        let _ = std::fs::remove_file(&server_pid_path);
    }

    let session_state = exomonad_core::services::session_state::mark_server_started(
        &project_dir,
        "exomonad serve",
    )?;
    info!(
        session_id = %session_state.session_id,
        completeness_status = %session_state.completeness_status,
        "Recorded authoritative session server start"
    );

    match exomonad_core::services::delivery::rebuild_durable_inbox_caches(
        &inbox_store,
        &agent_resolver,
        &project_dir,
    )
    .await
    {
        Ok(restored) => info!(restored, "Rebuilt durable guidance transport caches"),
        Err(error) => warn!(%error, "Failed to rebuild durable guidance transport caches"),
    }

    // Write server.pid
    let pid_info = serde_json::json!({
        "pid": std::process::id(),
        "role": role_name,
    });
    if let Some(parent) = server_pid_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&server_pid_path, serde_json::to_string_pretty(&pid_info)?)?;
    info!(path = %server_pid_path.display(), "Wrote server.pid");

    let reviewer_max_rounds = reviewer_max_rounds_override_from_env()?;
    let review_policy =
        exomonad_core::services::review_policy::ReviewPolicy::load_with_reviewer_max_rounds(
            &project_dir,
            reviewer_max_rounds,
        )
        .await
        .context("Failed to load review policy")?;
    let orphan_max_leaf = review_policy.max_leaf_session_seconds;
    let orphan_max_reviewer = review_policy.max_reviewer_session_seconds;

    // Start Worktree Event Watcher (background service — replaces GitHub poller + Copilot review)
    let mut watcher = exomonad_core::services::worktree_event_watcher::WorktreeEventWatcher::new(
        services.clone(),
    )
    .with_plugins(plugins.clone())
    .with_runtime_state(watcher_runtime_state.clone())
    .with_ci_status_map(ci_status_map.clone())
    .with_policy(review_policy);
    if let Some(interval) = config.poll_interval {
        if interval == 0 {
            anyhow::bail!("Invalid configuration: `poll_interval` must be >= 1 second, got 0");
        }
        watcher = watcher.with_poll_interval(Duration::from_secs(interval));
    }
    if let Some(interval) = config.inbox_poke_interval {
        if interval == 0 {
            anyhow::bail!(
                "Invalid configuration: `inbox_poke_interval` must be >= 1 second, got 0"
            );
        }
        watcher = watcher.with_inbox_poke_interval(Duration::from_secs(interval));
    }
    tokio::spawn(async move {
        watcher.run().await;
    });

    let orphan_reconciler_interval =
        Duration::from_secs(config.orphan_reconciler_interval_secs.unwrap_or(60));
    if orphan_reconciler_interval.is_zero() {
        anyhow::bail!(
            "Invalid configuration: `orphan_reconciler_interval_secs` must be >= 1 second, got 0"
        );
    }
    let orphan_project_dir = Arc::new(project_dir.clone());
    let orphan_git_wt = services.git_worktree_service().clone();
    let orphan_tmux_session = Some(config.tmux_session.clone());
    let orphan_event_log = event_log.clone();
    tokio::spawn(async move {
        exomonad_core::services::orphan_reconciler::run_orphan_reconciler(
            orphan_project_dir,
            orphan_git_wt,
            orphan_reconciler_interval,
            orphan_max_leaf,
            orphan_max_reviewer,
            orphan_tmux_session,
            orphan_event_log,
        )
        .await;
    });

    let app_state = AppState {
        project_dir: project_dir.clone(),
        plugins: plugins.clone(),
        registry: rt_registry.clone(),
        wasm_path: wasm_path.clone(),
        wasm_dir: wasm_dir.clone(),
        wasm_name: wasm_name.clone(),
        default_role: config.role.clone(),
        worktree_base: worktree_base.clone(),
        event_log: event_log.clone(),
        run_id: run_id.clone(),
        agent_resolver: agent_resolver.clone(),
        inbox_store: inbox_store.clone(),
        session_memory,
    };

    let forgejo_ci_state = exomonad_core::services::forgejo_ci::ForgejoCiWebhookState {
        ctx: services.clone(),
        webhook_secret: config.forgejo_webhook_secret.clone(),
    };

    let hook_state = HookState {
        plugins: plugins.clone(),
        registry: rt_registry.clone(),
        wasm_path: wasm_path.clone(),
        project_dir: project_dir.clone(),
        tmux_session: config.tmux_session.clone(),
        default_role: config.role.clone(),
        event_log: event_log.clone(),
        agent_resolver: agent_resolver.clone(),
        inbox_store: inbox_store.clone(),
        session_memory: app_state.session_memory.clone(),
    };

    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    let route_auth = control::RouteAuth::from_env();
    let agent_routes = Router::new()
        .route("/{role}/{name}/tools", get(list_tools))
        .route("/{role}/{name}/tools/call", post(call_tool))
        .layer(axum::middleware::from_fn_with_state(
            app_state.clone(),
            agent_identity_middleware,
        ))
        .layer(Extension(route_auth.clone()))
        .with_state(app_state.clone());

    let control_routes = Router::new()
        .route("/", get(control_root))
        .route("/runs/{run_id}", get(control_run))
        .route("/runs/{run_id}/slices/{slice_id}", get(control_slice))
        .route("/runs/{run_id}/transitions", get(control_transitions))
        .layer(middleware::from_fn_with_state(
            route_auth.clone(),
            control::require_control,
        ))
        .with_state(app_state.clone());

    let tcp_app = Router::new()
        .route("/health", get(health))
        .route(
            "/ci",
            post(exomonad_core::services::forgejo_ci::handle::<exomonad_core::services::Services>)
                .with_state(forgejo_ci_state.clone()),
        )
        .with_state(app_state.clone())
        .layer(cors.clone())
        .layer(TraceLayer::new_for_http());

    let app = Router::new()
        .route("/health", get(health))
        .route("/hook", post(handle_hook_request).with_state(hook_state))
        .route(
            "/ci",
            post(exomonad_core::services::forgejo_ci::handle::<exomonad_core::services::Services>)
                .with_state(forgejo_ci_state),
        )
        .nest("/agents", agent_routes)
        .nest("/control", control_routes)
        .route(
            "/events",
            post(handle_events).with_state(services.event_queue.clone()),
        )
        .route("/reload", post(reload))
        .route(
            "/shutdown",
            post(shutdown_endpoint).with_state(shutdown_signal.clone()),
        )
        .with_state(app_state)
        .layer(cors)
        .layer(TraceLayer::new_for_http());

    // Bind Unix domain socket (stale socket already cleaned up above)
    // Ensure parent directory exists
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    info!(socket = %socket_path.display(), "Binding MCP Unix domain socket");
    let listener = tokio::net::UnixListener::bind(&socket_path).map_err(|e| {
        anyhow::anyhow!(
            "Failed to bind Unix socket {}: {}",
            socket_path.display(),
            e
        )
    })?;

    let tcp_addr = format!("0.0.0.0:{}", config.port);
    info!(address = %tcp_addr, "Binding public TCP webhook listener");
    let tcp_listener = tokio::net::TcpListener::bind(&tcp_addr)
        .await
        .with_context(|| format!("Failed to bind TCP listener {tcp_addr}"))?;

    // Set socket permissions to owner-only (0600)
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&socket_path, std::fs::Permissions::from_mode(0o600))?;
    }

    info!(socket = %socket_path.display(), "MCP server listening on Unix domain socket");
    info!(address = %tcp_addr, "Public TCP webhook listener ready");
    info!(
        socket = %socket_path.display(),
        tcp_address = %tcp_addr,
        plugin_count = plugins.read().await.len(),
        "Plugins ready, accepting connections"
    );

    let socket_path_for_cleanup = socket_path.clone();
    let server_pid_for_cleanup = server_pid_path.clone();

    // Run both listeners with graceful shutdown on SIGINT, SIGTERM, or /shutdown endpoint.
    let uds_shutdown_signal = shutdown_signal.clone();
    let uds_server = axum::serve(listener, app).with_graceful_shutdown(shutdown_signal_future(
        uds_shutdown_signal,
        "MCP Unix domain socket",
    ));

    let tcp_shutdown_signal = shutdown_signal.clone();
    let tcp_server = axum::serve(tcp_listener, tcp_app).with_graceful_shutdown(
        shutdown_signal_future(tcp_shutdown_signal, "public TCP webhook listener"),
    );

    if let Err(err) = tokio::try_join!(uds_server, tcp_server) {
        error!(error = %err, "MCP server exited with error");
        return Err(err.into());
    }
    info!("MCP server exited gracefully");

    // Clean up socket and pid on shutdown
    if socket_path_for_cleanup.exists() {
        let _ = std::fs::remove_file(&socket_path_for_cleanup);
        info!("Cleaned up server socket");
    }
    if server_pid_for_cleanup.exists() {
        let _ = std::fs::remove_file(&server_pid_for_cleanup);
        info!("Cleaned up server.pid");
    }

    info!("MCP server shut down");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use exomonad_core::mcp::tools::MCPCallOutput;
    use exomonad_core::services::InboxMessageRecord;

    fn message() -> InboxMessageRecord {
        InboxMessageRecord {
            id: 1,
            from_agent: "root".to_string(),
            to_agent: "worker-1".to_string(),
            content: "Please review the latest patch".to_string(),
            summary: Some("review patch".to_string()),
            created_at: 123,
            notified_at: None,
            read_at: None,
        }
    }

    #[test]
    fn ready_count_ignores_headers_and_diagnostics() {
        let output = "Ready issues (no blockers):\n  #12 high task\nwarning: ignored";
        assert_eq!(parse_ready_count(output), 1);
    }

    #[test]
    fn turn_end_reason_distinguishes_backlog_states() {
        let input: HookInput =
            serde_json::from_str(r#"{"session_id":"session","hook_event_name":"Stop"}"#).unwrap();
        let output = InternalStopHookOutput {
            decision: StopDecision::Allow,
            reason: None,
        };
        let counts = TurnEndCounts {
            backlog_ready_count: 2,
            pending_children: 0,
            unread_inbox: 0,
        };
        assert_eq!(
            turn_end_reason(
                HookEventType::Stop,
                HookRuntime::Claude,
                &input,
                &output,
                counts,
                false,
            ),
            "stopped_backlog_nonempty"
        );
        assert_eq!(
            turn_end_reason(
                HookEventType::Stop,
                HookRuntime::Claude,
                &input,
                &output,
                counts,
                true,
            ),
            "spawned_and_idle"
        );
    }

    #[test]
    fn turn_end_reason_marks_codex_and_subagents_as_exited() {
        let input: HookInput =
            serde_json::from_str(r#"{"session_id":"session","hook_event_name":"Stop"}"#).unwrap();
        let output = InternalStopHookOutput {
            decision: StopDecision::Allow,
            reason: None,
        };
        let counts = TurnEndCounts {
            backlog_ready_count: 0,
            pending_children: 1,
            unread_inbox: 0,
        };
        assert_eq!(
            turn_end_reason(
                HookEventType::Stop,
                HookRuntime::Codex,
                &input,
                &output,
                counts,
                false,
            ),
            "exited"
        );
        assert_eq!(
            turn_end_reason(
                HookEventType::SubagentStop,
                HookRuntime::Claude,
                &input,
                &output,
                counts,
                false,
            ),
            "exited"
        );
    }

    #[test]
    fn reviewer_max_rounds_override_parser_preserves_absence_and_rejects_invalid_values() {
        assert_eq!(parse_reviewer_max_rounds_override(None).unwrap(), None);
        assert_eq!(
            parse_reviewer_max_rounds_override(Some("5")).unwrap(),
            Some(5)
        );
        assert_eq!(parse_reviewer_max_rounds_override(Some(" ")).unwrap(), None);
        assert!(parse_reviewer_max_rounds_override(Some("0")).is_err());
        assert!(parse_reviewer_max_rounds_override(Some("invalid")).is_err());
    }

    #[test]
    fn piggyback_appends_unread_mail_to_existing_text_content() {
        let mut output = MCPCallOutput {
            success: true,
            result: Some(serde_json::json!({
                "content": [{"type": "text", "text": "tool result"}],
                "isError": false
            })),
            error: None,
        };

        append_unread_mail_block(&mut output, &[message()]);

        let text = output.result.unwrap()["content"][0]["text"]
            .as_str()
            .unwrap()
            .to_string();
        assert!(text.starts_with("tool result\n\n<unread-mail>"));
        assert!(text.contains("[from: root] Summary: review patch. Full message: Please review"));
    }

    #[test]
    fn piggyback_wraps_plain_success_result_as_text_content() {
        let mut output = MCPCallOutput {
            success: true,
            result: Some(serde_json::json!({"ok": true})),
            error: None,
        };

        append_unread_mail_block(&mut output, &[message()]);

        let result = output.result.unwrap();
        assert_eq!(result["isError"], serde_json::Value::Bool(false));
        let text = result["content"][0]["text"].as_str().unwrap();
        assert!(text.contains("{\n  \"ok\": true\n}"));
        assert!(text.contains("<unread-mail>"));
    }

    #[test]
    fn piggyback_skips_failed_outputs() {
        let mut output = MCPCallOutput {
            success: false,
            result: None,
            error: Some("boom".to_string()),
        };

        append_unread_mail_block(&mut output, &[message()]);

        assert!(output.result.is_none());
    }
}
